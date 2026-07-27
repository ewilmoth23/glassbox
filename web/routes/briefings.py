"""`/api/briefings/*` + `/api/issues/*` — public accountability layer
plus the self-heal feedback endpoint.

Extracted from `glassbox_server.py` 2026-05-22 as P3-H extraction #11.
Five routes:

  POST /api/issues/report                — public write endpoint for
                                            client-side error reports
                                            (frontend, ingesters,
                                            grader, etc.). Rate-limited
                                            per-signature so a log
                                            storm can't DoS the Brain.
  GET  /api/issues/open                  — public read of open issues
                                            (for Mission Control + the
                                            status pages)
  GET  /api/briefings/latest             — most-recent N briefings;
                                            ?status=live|graded|all
  GET  /api/briefings/{slug}             — single-briefing permalink
  GET  /api/briefings/track-record/summary — public accuracy ledger
                                              (the moat — verifiable
                                              claim-to-fact record)

Selfheal helpers live in `20_HOLDING_BRAIN/bin/selfheal.py` (a sibling
empire folder, NOT a sibling Python package). Briefing helpers live in
`bin/briefing_engine.py` inside this directory. Both imports are
optional — the routes return 503 if the helper module isn't on the
import path, rather than crashing the whole daemon at startup.

Per-signature rate-limit state for /api/issues/report lives at module
scope here; the dict is process-local and reset on restart (acceptable
because the selfheal daemon dedupes by signature on its end too).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("glassbox-server.briefings")
router = APIRouter()


# ─── Selfheal optional import ────────────────────────────────────────
# Same pattern as the cognition optional import elsewhere — wire up the
# Holding Brain path so we can import `bin.selfheal`, then try; if the
# Holding Brain isn't deployed (CI, fresh checkout, etc.) the routes
# return 503 instead of the daemon crashing at boot.
#
# Path walk from this file:
#   web/routes/briefings.py
#   → .parent       = web/routes/
#   → .parent       = web/
#   → .parent       = 21_GLASSBOX_AI/
#   → .parent       = <empire root>
_EMPIRE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SELFHEAL_PATH = _EMPIRE_ROOT / "20_HOLDING_BRAIN"
if str(_SELFHEAL_PATH) not in sys.path:
    sys.path.insert(0, str(_SELFHEAL_PATH))

try:
    from bin.selfheal import report_issue as _sh_report, list_issues as _sh_list  # noqa: E402
    _SELFHEAL_ACTIVE = True
except Exception as _sh_exc:
    _SELFHEAL_ACTIVE = False
    _SH_IMPORT_ERROR = str(_sh_exc)  # noqa: F841 — kept for future debug

# ─── Self-heal feedback — public write endpoint ──────────────────────
# Any client (browser, ingester, workflow, grader) can report an issue
# here. The selfheal daemon on the Mac Mini picks it up on the next tick,
# runs playbooks, and escalates to CLAUDE_INBOX/ if it can't remediate.

_ISSUE_REPORT_LAST: Dict[str, float] = {}
_ISSUE_REPORT_MIN_INTERVAL = 2.0   # sec — simple per-signature rate limit
_ISSUE_REPORT_TTL_SEC = 600.0      # 10 min — any rate-limit entry older than this
                                   # gets pruned so the dict can't grow unbounded
_ISSUE_REPORT_PRUNE_EVERY = 100    # prune once every N reports (amortized O(1))
_ISSUE_REPORT_COUNTER = {"n": 0}   # dict to allow mutation inside endpoint


@router.post("/api/issues/report")
async def issues_report(request: Request):
    """Accept an issue from any caller. Body schema:

        {
          "category": "frontend_runtime_error" | "ingester_stuck" | ... ,
          "source":   "glassbox-ui" | "glassbox-server" | "fulcrum-markets" | ...,
          "description": "human-readable sentence",
          "severity": 0.0-1.0,     # optional, default 0.5
          "signature": "stable-id", # optional — computed if absent
          "context": { ... }        # optional JSON blob
        }

    Rate-limited per-signature to prevent log storms from DoSing the Brain.
    Returns { ok, signature } — callers don't need to poll; they just fire."""
    if not _SELFHEAL_ACTIVE:
        return JSONResponse({"ok": False, "error": "selfheal not available"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    category = str(body.get("category") or "").strip() or "unknown"
    source = str(body.get("source") or "").strip() or "unknown"
    description = str(body.get("description") or "").strip()
    if not description:
        return JSONResponse({"ok": False, "error": "description required"}, status_code=400)
    severity = float(body.get("severity") or 0.5)
    severity = max(0.0, min(1.0, severity))
    context = body.get("context") if isinstance(body.get("context"), dict) else {}
    signature = body.get("signature")

    # Rate limit — same signature can't be reported more than once every 2s.
    key = signature or f"{category}::{source}::{description[:80]}"
    now = asyncio.get_event_loop().time()
    last = _ISSUE_REPORT_LAST.get(key, 0.0)
    if now - last < _ISSUE_REPORT_MIN_INTERVAL:
        return JSONResponse({"ok": True, "throttled": True, "signature": None})
    _ISSUE_REPORT_LAST[key] = now

    # Amortized TTL prune — stops the dict from growing unbounded when many
    # unique signatures flow through (e.g. after an upstream schema change).
    _ISSUE_REPORT_COUNTER["n"] += 1
    if _ISSUE_REPORT_COUNTER["n"] >= _ISSUE_REPORT_PRUNE_EVERY:
        _ISSUE_REPORT_COUNTER["n"] = 0
        cutoff = now - _ISSUE_REPORT_TTL_SEC
        stale = [k for k, ts in _ISSUE_REPORT_LAST.items() if ts < cutoff]
        for k in stale:
            _ISSUE_REPORT_LAST.pop(k, None)
        if stale:
            log.info("Pruned %d stale issue-report keys (now %d live)",
                     len(stale), len(_ISSUE_REPORT_LAST))

    try:
        sig = _sh_report(
            category=category, source=source, description=description,
            severity=severity, signature=signature, context=context,
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "signature": sig})


@router.get("/api/issues/open")
async def issues_open(limit: int = 50):
    """Public read of open issues (for Mission Control + status pages).
    Summarized — no internal SQL state leaks."""
    if not _SELFHEAL_ACTIVE:
        return JSONResponse({"ok": False, "error": "selfheal not available"}, status_code=503)
    issues = _sh_list(state="open")
    # Strip any heavy context fields so we don't bloat public responses.
    slim = []
    for i in issues[: max(1, min(limit, 200))]:
        slim.append({
            "signature": i.get("signature"),
            "category": i.get("category"),
            "source": i.get("source"),
            "severity": i.get("severity"),
            "attempts": i.get("attempts"),
            "first_seen": i.get("first_seen"),
            "last_seen": i.get("last_seen"),
            "state": i.get("state"),
            "description": (i.get("description") or "")[:240],
        })
    return JSONResponse({"ok": True, "count": len(slim), "issues": slim})


# ─── Briefings — public accountability layer ──────────────────────
# The Brain composes briefings when thresholds are met. These endpoints
# serve them to mewrcreate.com/briefings and the track-record ledger.

_BRIEFING_HELPERS_AVAILABLE = False
try:
    # These are the cheap read helpers — the compose + grade passes run
    # in cron, not in the HTTP request path.
    from bin.briefing_engine import _brain as _brf_brain  # type: ignore
    from bin.briefing_engine import track_record as _brf_track_record  # type: ignore
    _BRIEFING_HELPERS_AVAILABLE = True
except Exception:
    pass


@router.get("/api/briefings/latest")
async def briefings_latest(limit: int = 20, status: Optional[str] = None):
    """Most recent briefings. ?status=live | graded | all (default all).
    Public — this is what the /briefings hub consumes."""
    if not _BRIEFING_HELPERS_AVAILABLE:
        return JSONResponse(
            {"ok": False, "error": "briefing engine not available"},
            status_code=503,
        )
    brain = _brf_brain()
    conn = brain._connect()
    try:
        rows = conn.execute(
            "SELECT f.subject, f.object, f.created_at FROM facts f "
            "JOIN ("
            "  SELECT subject, MAX(created_at) mx FROM facts "
            "  WHERE namespace='briefings' AND predicate='briefing' GROUP BY subject"
            ") latest ON f.subject=latest.subject AND f.created_at=latest.mx "
            "WHERE f.namespace='briefings' AND f.predicate='briefing' "
            "ORDER BY f.created_at DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for subj, raw, ts in rows:
        try:
            b = json.loads(raw)
        except Exception:
            continue
        if status and status != "all" and b.get("status") != status:
            continue
        # Attach grade if one exists.
        conn2 = brain._connect()
        try:
            grow = conn2.execute(
                "SELECT object FROM facts WHERE namespace='briefing_grades' "
                "AND subject=? AND predicate='grade' ORDER BY created_at DESC LIMIT 1",
                (subj,),
            ).fetchone()
        finally:
            conn2.close()
        if grow:
            try:
                b["grade"] = json.loads(grow[0])
            except Exception:
                pass
        out.append(b)
    return JSONResponse({"ok": True, "count": len(out), "briefings": out})


@router.get("/api/briefings/{slug}")
async def briefings_one(slug: str):
    """Single-briefing detail view. Used by /briefings/<slug> permalink."""
    if not _BRIEFING_HELPERS_AVAILABLE:
        return JSONResponse(
            {"ok": False, "error": "briefing engine not available"},
            status_code=503,
        )
    brain = _brf_brain()
    conn = brain._connect()
    try:
        row = conn.execute(
            "SELECT object, created_at FROM facts "
            "WHERE namespace='briefings' AND subject=? AND predicate='briefing' "
            "ORDER BY created_at DESC LIMIT 1",
            (slug,),
        ).fetchone()
        grow = conn.execute(
            "SELECT object FROM facts WHERE namespace='briefing_grades' "
            "AND subject=? AND predicate='grade' ORDER BY created_at DESC LIMIT 1",
            (slug,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    try:
        b = json.loads(row[0])
    except Exception:
        return JSONResponse({"ok": False, "error": "bad payload"}, status_code=500)
    if grow:
        try:
            b["grade"] = json.loads(grow[0])
        except Exception:
            pass
    return JSONResponse({"ok": True, "briefing": b})


@router.get("/api/briefings/track-record/summary")
async def briefings_track_record_summary(days: int = 90):
    """Public accuracy ledger. THIS is the moat. Hit rate, per-month breakdown,
    counts. Every visitor can verify our claim-to-fact record."""
    if not _BRIEFING_HELPERS_AVAILABLE:
        return JSONResponse(
            {"ok": False, "error": "briefing engine not available"},
            status_code=503,
        )
    try:
        return JSONResponse({"ok": True, **_brf_track_record(days=max(7, min(days, 365)))})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
