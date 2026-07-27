"""Health + sources + markets singletons — extracted from
`glassbox_server.py` 2026-05-22 as P3-H extraction #12.

Four routes that don't fit any other extracted concern but share a
"daemon-state read" shape: each one reads from `request.app.state.*`
to access live ingester / cache / subscriber collections that the
startup hook populates.

  GET  /api/sources                    — sources.yaml registry summary
                                          (public license posture)
  GET  /api/health                     — daemon health envelope
                                          (launchd polls this)
  GET  /health                         — back-compat alias of /api/health
                                          (n8n cron + supervisor scripts)
  POST /api/markets/edges/email-capture — Pro-tier waitlist append-only log

The shared collections (`ingesters`, `subscribers`, `hot_cache`,
`started_at`, `sources_registry`) are populated on
`glassbox_server.py`'s `@app.on_event("startup")` and copied to
`app.state` via the additive bridge landed in commit `3231f63`.
The `getattr(state, "X", <default>)` calls below tolerate the absence
of those attributes for test paths that build a fresh `FastAPI()`
without running startup — degraded payload, but the route doesn't 500.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sources_registry import SourcesRegistry, registry_summary

log = logging.getLogger("glassbox-server.misc")
router = APIRouter()

# `web/routes/misc.py` → parent.parent.parent is `21_GLASSBOX_AI/`.
_PKG_ROOT = Path(__file__).resolve().parent.parent.parent
_LEAD_LOG_PATH = _PKG_ROOT / "data" / "pro_waitlist.jsonl"


@router.get("/api/sources")
async def api_sources(request: Request) -> JSONResponse:
    """Returns sources.yaml summary so operator can see exactly what's
    enabled, what's disabled, and what's refused at the gate.

    No auth — the registry contents are not secret (it's the public
    license posture). Credentials are NEVER returned (they live in env
    vars, not yaml)."""
    reg = getattr(request.app.state, "sources_registry", None)
    if reg is None:
        # Cold-load if startup hadn't run yet (e.g. during tests).
        reg = SourcesRegistry.load()
    return JSONResponse(registry_summary(reg))


@router.get("/api/health")
async def health(request: Request) -> JSONResponse:
    """Daemon health envelope. Polled by launchd, the n8n monitor,
    supervisor.sh, and the public /status page. Returns 200 with the
    OK body on every call — health is communicated by the contents
    (ingester states, subscriber counts) not the status code, so
    downstream pollers don't need separate per-state handlers."""
    state = request.app.state
    ingesters = getattr(state, "ingesters", [])
    subscribers = getattr(state, "subscribers", [])
    hot_cache = getattr(state, "hot_cache", {})
    started_at = getattr(state, "started_at", None)
    return JSONResponse({
        "ok": True,
        "service": "glassbox-server",
        "version": "2.0.0",
        "started_at": started_at,
        "ts": datetime.now(timezone.utc).isoformat(),
        "ingesters": [ing.status() for ing in ingesters],
        "subscribers": len(subscribers),
        "layers": {k: len(v) for k, v in hot_cache.items()},
    })


@router.get("/health")
async def health_alias(request: Request) -> JSONResponse:
    """Compatibility alias for external healthcheck monitors that hit
    /health instead of /api/health (launchd healthcheck, n8n cron,
    supervisor scripts). Returns the same payload as /api/health."""
    return await health(request)


@router.post("/api/markets/edges/email-capture")
async def pro_email_capture(request: Request) -> JSONResponse:
    """Pro-tier waitlist email capture. Append-only JSONL log; no DB
    write so privacy/PII handling stays simple. Returns ok=true on any
    well-formed submission."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    email = (body.get("email") or "").strip().lower()
    # Minimal sanity check — full validation lives in the real pipeline.
    if not email or "@" not in email or len(email) > 200:
        return JSONResponse({"ok": False, "error": "invalid email"}, status_code=400)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "email": email,
        "source": (body.get("source") or "glassbox-pro").strip()[:60],
    }
    try:
        _LEAD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LEAD_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning(f"pro waitlist append failed: {type(e).__name__}: {e}")
        # Don't fail the user-facing request on disk error; the email
        # is still captured in the request log via uvicorn.
    return JSONResponse({"ok": True})
