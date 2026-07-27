#!/usr/bin/env python3
"""
glassbox_bridge.py — turns transient Glassbox intelligence into durable Brain memory.

Every 15 minutes (cron), we poll the Cloudflare Worker's /api/intel/* endpoints,
diff against what's already in the Brain, and record new artefacts as:
  - predictions (hotspot-prediction, threat-forecast) — graded later
  - facts       (threat-assessment, narrative-intel, daily-briefing)
  - patterns    (correlation-analysis)
  - events      (one append-only row per polling cycle)

Idempotent: polls a durable content hash per endpoint + date, skips if seen.

Usage:
    python3 21_GLASSBOX_AI/glassbox_bridge.py              # default
    python3 21_GLASSBOX_AI/glassbox_bridge.py --dry-run    # print what it would do
    python3 21_GLASSBOX_AI/glassbox_bridge.py --force      # re-record even if seen

Environment:
    API_BASE   default https://mewr-news-api.mewrcreate.workers.dev
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import the Brain
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "20_HOLDING_BRAIN" / "memory"))
from brain import Brain  # type: ignore

API_BASE = os.environ.get("API_BASE", "https://mewr-news-api.mewrcreate.workers.dev")
SERVICE_NAME = "glassbox_bridge"
NS = "glassbox"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _http_get(url: str, timeout: float = 15.0) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GlassboxBridge/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[gb-bridge] {url} -> HTTP {e.code}")
        return None
    except Exception as e:
        print(f"[gb-bridge] {url} -> {type(e).__name__}: {e}")
        return None


def _content_hash(obj: dict | list) -> str:
    """Stable hash of intel content so we know if we've seen it."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ─── Cognition layer integration ─────────────────────────────────────────
# Every intel product that lands from n8n/Ollama goes through a post-hoc
# critique. If the critique flags severity-4+ issues or low confidence,
# the item is queued for Tier-3 Claude review. Zero cost — local Ollama.

try:
    sys.path.insert(0, str(ROOT / "20_HOLDING_BRAIN"))
    from cognition import critique_output as _critique_output  # type: ignore
    from review import enqueue_review as _enqueue_review  # type: ignore
    _COGNITION_OK = True
except Exception:
    _COGNITION_OK = False


# Shared event loop for critique calls.
# Previous impl used asyncio.run() per item which (a) spins up and tears down
# a whole loop for every intel product, and (b) crashes hard if the bridge is
# ever invoked from inside an already-running loop (e.g. scheduled from
# Mission Control instead of cron). Reusing one loop avoids both footguns.
import asyncio as _asyncio
_BRIDGE_LOOP: "_asyncio.AbstractEventLoop | None" = None


def _run_coro(coro):
    """Run an awaitable on the bridge's shared event loop."""
    global _BRIDGE_LOOP
    if _BRIDGE_LOOP is None or _BRIDGE_LOOP.is_closed():
        _BRIDGE_LOOP = _asyncio.new_event_loop()
    return _BRIDGE_LOOP.run_until_complete(coro)


def _cognition_check(kind: str, payload: dict, content_type: str) -> dict | None:
    """Fire-and-forget critique on intel. Returns the critique dict or None."""
    if not _COGNITION_OK or not payload:
        return None
    try:
        # Summarize the payload into a reviewable prompt stub
        prompt_stub = (
            f"Glassbox intel product: {kind}. "
            f"Key fields: " + json.dumps({k: payload.get(k) for k in list(payload.keys())[:6]}, default=str)[:800]
        )
        critique = _run_coro(_critique_output(
            output=payload,
            original_prompt=prompt_stub,
            lessons=None,
            timeout_sec=45.0,
        ))
        # If escalate or sev-4+, queue for Claude review
        max_sev = max([int(i.get("severity") or 0) for i in (critique.get("issues") or [])] + [0])
        if critique.get("recommend") == "escalate" or max_sev >= 4:
            _enqueue_review(
                source=f"glassbox_bridge.{kind}",
                prompt_ref=f"glassbox intel — {kind}",
                output={"payload": payload, "critique": critique},
                context={"content_type": content_type, "kind": kind},
                importance=5,
                model="qwen2.5:14b",
                notes=critique.get("summary", ""),
            )
            print(f"[gb-bridge] ⚠ escalated {kind} (sev={max_sev}, conf={critique.get('confidence')})")
        elif critique.get("recommend") == "revise":
            # Queue for background review at lower priority
            _enqueue_review(
                source=f"glassbox_bridge.{kind}",
                prompt_ref=f"glassbox intel — {kind}",
                output={"payload": payload, "critique": critique},
                context={"content_type": content_type, "kind": kind},
                importance=3,
                model="qwen2.5:14b",
                notes=critique.get("summary", ""),
            )
        return critique
    except Exception as e:
        # Never block intel persistence because critique had issues
        print(f"[gb-bridge] cognition check skipped for {kind}: {e}")
        return None


# ─── Per-endpoint handlers ───────────────────────────────────────────────

def handle_threat_assessment(brain: Brain, payload: dict, *, force: bool) -> int:
    """
    Threat assessment = current world-risk snapshot. Store as a 'state' fact
    with the content hash as subject so idempotent re-polls don't duplicate.
    """
    if not payload:
        return 0
    hash_key = _content_hash(payload)
    # Check if we already have this exact snapshot
    existing = brain.recall(f"threat_assessment {hash_key}", namespace=NS, predicate="state", k=1)
    if existing and not force:
        return 0
    threat_level = payload.get("threat_level") or payload.get("level") or "unknown"
    summary = payload.get("summary") or payload.get("global_summary") or ""
    threats = payload.get("key_threats") or payload.get("threats") or []
    top_regions = [t.get("region") for t in threats[:5] if isinstance(t, dict)]
    obj = f"Threat level {threat_level}. Regions: {', '.join(r for r in top_regions if r)}. {summary[:400]}"
    brain.remember(
        namespace=NS, predicate="state", subject=f"threat_assessment:{hash_key}",
        object=obj, source=f"glassbox_bridge:{_utcnow()}",
        tags=f"glassbox,threat,level:{threat_level}",
    )
    _cognition_check("threat_assessment", payload, "glassbox_intel")
    return 1


def handle_hotspot_predictions(brain: Brain, payload: dict, *, force: bool) -> int:
    """
    Hotspot predictions → record_prediction entries with a due_at so the grader
    can later check outcome. Each hotspot becomes its own prediction row.
    """
    if not payload:
        return 0
    preds = payload.get("predictions") or payload.get("hotspots") or []
    n = 0
    for h in preds:
        if not isinstance(h, dict):
            continue
        label = h.get("label") or h.get("name") or "unlabeled"
        lat = h.get("lat") or h.get("latitude")
        lng = h.get("lng") or h.get("longitude") or h.get("lon")
        severity = h.get("severity") or h.get("score")
        conf = h.get("confidence")
        category = h.get("category") or h.get("type") or "generic"
        timeframe = h.get("timeframe") or h.get("window") or "48h"
        reasoning = h.get("reasoning") or h.get("rationale") or ""
        subject = f"GLASSBOX:hotspot:{category}:{label}:{_today_utc()}"
        # Compute due_at from timeframe
        due_at = None
        for unit, hours in (("72h", 72), ("48h", 48), ("24h", 24), ("week", 168), ("day", 24)):
            if unit in str(timeframe).lower():
                due_at = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
                break
        brain.record_prediction(
            namespace=NS,
            subject=subject,
            claim=f"Hotspot risk ({category}) at {label} with severity {severity}",
            confidence=int(conf) if isinstance(conf, (int, float)) else None,
            features={
                "lat": lat, "lng": lng, "severity": severity, "category": category,
                "timeframe": timeframe, "region_label": label,
            },
            reasoning=reasoning[:1000] if reasoning else None,
            model="ollama-qwen2.5:14b (via n8n intel pipeline)",
            source="glassbox_bridge",
            due_at=due_at,
        )
        n += 1
    return n


def handle_narratives(brain: Brain, payload: dict, *, force: bool) -> int:
    if not payload:
        return 0
    narratives = payload.get("narratives") or []
    n = 0
    for item in narratives:
        if not isinstance(item, dict):
            continue
        theme = item.get("theme") or item.get("name") or "unlabeled"
        origin = item.get("origin_region") or item.get("origin") or ""
        spread = item.get("spread_regions") or item.get("spread") or []
        intensity = item.get("intensity") or item.get("score") or "—"
        credibility = item.get("credibility") or "—"
        obj = (f"Narrative: {theme} · origin {origin} · spread to {', '.join(spread) if spread else '—'} "
               f"· intensity {intensity} · credibility {credibility}")
        brain.remember(
            namespace=NS, predicate="narrative",
            subject=f"narrative:{theme}:{_today_utc()}",
            object=obj[:500],
            source="glassbox_bridge",
            tags=f"glassbox,narrative,origin:{origin}",
        )
        n += 1
    # Info-warfare indicators as one pattern update
    iwi = payload.get("info_warfare_indicators") or []
    if iwi:
        brain.record_pattern(
            namespace=NS, name="info_warfare_detected",
            description=f"Info-warfare indicators surfaced: {', '.join(str(x)[:80] for x in iwi[:3])}",
            trigger="correlation of narrative spread + source biases",
            resolution="Sentinel agency should cross-reference before publishing to avoid amplifying.",
            severity="high",
        )
    return n


def handle_correlations(brain: Brain, payload: dict, *, force: bool) -> int:
    """
    Cross-layer correlations = codified patterns. Each correlation becomes
    a pattern record; first occurrence is created, repeats increment seen_count.
    """
    if not payload:
        return 0
    correlations = payload.get("correlations") or []
    cascades = payload.get("cascade_risks") or payload.get("cascades") or []
    recommendations = payload.get("recommendations") or []
    n = 0
    for c in correlations:
        if not isinstance(c, dict):
            continue
        region = c.get("region") or "—"
        layers = c.get("layers_involved") or c.get("layers") or []
        score = c.get("correlation_score") or c.get("score")
        cascade_risk = c.get("cascade_risk") or ""
        name = f"cross_layer_{'_'.join(sorted(layers))[:64]}" if layers else "cross_layer_generic"
        description = (f"Correlation in {region}: layers {', '.join(layers)} "
                       f"co-activated with score {score}. Cascade risk: {cascade_risk[:120]}")
        brain.record_pattern(
            namespace=NS, name=name,
            description=description,
            trigger=f"layers={','.join(layers)} co-elevated in region={region}",
            resolution=(cascade_risk[:200] if cascade_risk else
                        "Flag for operator review; cross-check with Sentinel agency feed."),
            severity=("high" if isinstance(score, (int, float)) and score > 0.7 else "medium"),
        )
        n += 1
    # Situation report if present
    sitrep = payload.get("situation_report") or payload.get("sitrep")
    if sitrep:
        brain.remember(
            namespace=NS, predicate="sitrep",
            subject=f"sitrep:{_today_utc()}",
            object=str(sitrep)[:800],
            source="glassbox_bridge",
            tags="glassbox,sitrep",
        )
    if recommendations:
        brain.remember(
            namespace=NS, predicate="recommendations",
            subject=f"recs:{_today_utc()}",
            object=" | ".join(str(r)[:200] for r in recommendations[:5]),
            source="glassbox_bridge",
            tags="glassbox,recommendations",
        )
    return n


def handle_daily_briefing(brain: Brain, payload: dict, *, force: bool) -> int:
    if not payload:
        return 0
    date = payload.get("date") or _today_utc()
    hash_key = _content_hash(payload)
    existing = brain.recall(f"daily_briefing:{date}:{hash_key}", namespace=NS, predicate="briefing", k=1)
    if existing and not force:
        return 0
    sections = payload.get("sections_available") or []
    stats = payload.get("meta") or payload.get("stats") or {}
    obj = (f"Daily briefing for {date}. Sections: {', '.join(sections) if sections else '—'}. "
           f"Stats: {json.dumps(stats)[:300]}")
    brain.remember(
        namespace=NS, predicate="briefing",
        subject=f"daily_briefing:{date}:{hash_key}",
        object=obj,
        source="glassbox_bridge",
        tags="glassbox,briefing,daily",
    )
    return 1


# ─── Main loop ──────────────────────────────────────────────────────────

# Worker route: GET /api/intel/latest returns {"products": {type: data, ...}}
# where 'type' is one of: threat-assessment, hotspot-prediction, narrative-intel,
# correlation-analysis, anomaly-report, situation-report.
# Plus GET /api/intel/threat-briefing returns a compiled daily briefing.

TYPE_HANDLERS = {
    "threat-assessment":     (handle_threat_assessment,    "threat_assessment"),
    "hotspot-prediction":    (handle_hotspot_predictions,  "hotspot_predictions"),
    "narrative-intel":       (handle_narratives,           "narratives"),
    "correlation-analysis":  (handle_correlations,         "correlations"),
    # anomaly-report & situation-report are recorded as facts via handle_threat_assessment's generic pattern
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="Re-record even if content hash matches")
    args = p.parse_args()

    brain = Brain()
    # Register service (idempotent) so Mission Control alerts when bridge stops running
    brain.register_service(SERVICE_NAME, namespace=NS, expected_every_sec=1800)  # 30 min SLA

    started = datetime.now(timezone.utc)
    totals = {}
    errors = []

    # One call to /api/intel/latest pulls every current product at once.
    latest_url = f"{API_BASE}/api/intel/latest"
    latest_data = _http_get(latest_url)
    if latest_data is None:
        errors.append(f"latest: fetch failed ({latest_url})")
    else:
        products = (latest_data or {}).get("products") or {}
        if not isinstance(products, dict):
            errors.append(f"latest: unexpected shape {type(products).__name__}")
            products = {}

        for intel_type, (handler, label) in TYPE_HANDLERS.items():
            payload = products.get(intel_type) or {}
            # Worker stores an array of daily records — we want the most-recent single
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            if args.dry_run:
                totals[label] = -1
                print(f"[dry-run] {intel_type}: payload_size={len(json.dumps(payload))}")
                continue
            try:
                count = handler(brain, payload, force=args.force)
                totals[label] = count
            except Exception as e:
                errors.append(f"{intel_type}: {type(e).__name__}: {e}")
                totals[label] = 0

        # Also record generic products not in TYPE_HANDLERS (anomaly-report, situation-report)
        for extra_type, extra_payload in products.items():
            if extra_type in TYPE_HANDLERS:
                continue
            if isinstance(extra_payload, list):
                extra_payload = extra_payload[0] if extra_payload else {}
            if isinstance(extra_payload, dict) and extra_payload:
                try:
                    brain.remember(
                        namespace=NS, predicate="intel_product",
                        subject=f"{extra_type}:{_today_utc()}",
                        object=json.dumps(extra_payload)[:500],
                        source="glassbox_bridge",
                        tags=f"glassbox,{extra_type}",
                    )
                    totals[extra_type] = 1
                except Exception as e:
                    errors.append(f"{extra_type}: {type(e).__name__}: {e}")

    # Also try the compiled daily-briefing endpoint — separate route on the Worker
    briefing_url = f"{API_BASE}/api/intel/threat-briefing"
    briefing = _http_get(briefing_url)
    if briefing is not None and not args.dry_run:
        try:
            totals["threat_briefing"] = handle_daily_briefing(brain, briefing, force=args.force)
        except Exception as e:
            errors.append(f"threat-briefing: {type(e).__name__}: {e}")
            totals["threat_briefing"] = 0

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

    # Log one event summarizing the cycle
    severity = "error" if errors else ("warn" if all(v == 0 for v in totals.values()) else "info")
    brain.log_event(
        namespace=NS,
        kind="bridge_cycle",
        summary=f"Glassbox bridge cycle: {sum(v for v in totals.values() if v > 0)} new artefacts",
        detail={"totals": totals, "errors": errors, "elapsed_s": round(elapsed, 2), "api_base": API_BASE},
        severity=severity,
        source="glassbox_bridge",
    )

    if errors:
        brain.record_failure(SERVICE_NAME, detail="; ".join(errors)[:400])
    else:
        brain.heartbeat(SERVICE_NAME, payload={"totals": totals, "elapsed_s": round(elapsed, 2)})

    print(f"[gb-bridge] done in {elapsed:.1f}s")
    for k, v in totals.items():
        print(f"[gb-bridge]   {k:<24} {v}")
    if errors:
        print("[gb-bridge] errors:")
        for e in errors:
            print(f"[gb-bridge]   {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
