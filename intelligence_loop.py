#!/usr/bin/env python3
"""
intelligence_loop.py — every 5 minutes, reason on the live event stream.

Pipeline:
    1. Pull latest events from glassbox-server's hot cache via HTTP
    2. Run statistical anomaly detection per (layer, region)
    3. Find cross-layer correlations (geo + time proximity)
    4. Compose an AI SITREP via the cognition pipeline (content_type="glassbox_sitrep")
    5. Write anomalies + correlations + SITREP to the Brain
    6. POST the SITREP to glassbox-server (/api/glassbox/sitrep/publish)
       so the front-end can render it live

Why this is the unlock:
    Ingesters collect. Cognition critiques. This loop *reasons*.
    It's the difference between a dashboard that shows live data and a
    system that notices things, correlates them, and tells you what's
    changing — in natural language — every 5 minutes.

Runs as an interval service via supervisor.sh. Single-cycle default; use
--forever for a standalone loop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
MONOREPO = ROOT.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(MONOREPO / "20_HOLDING_BRAIN"))
sys.path.insert(0, str(MONOREPO / "20_HOLDING_BRAIN" / "memory"))

from anomaly import bucket_events, detect_anomalies, update_baselines  # type: ignore
from correlator import find_correlations                                 # type: ignore
from watchlist import list_watchlists, evaluate as evaluate_watchlists, dispatch_alerts  # type: ignore

from pydantic import BaseModel, Field

from llm_json import parse_with_schema


class _SitrepSchema(BaseModel):
    """Pydantic schema for the LLM SITREP JSON. Matches the four-key
    contract the prior tolerant parser implicitly required, but now
    enforced by validation rather than ad-hoc .get() calls everywhere."""
    headline: str = Field(default="", max_length=400)
    brief: str = Field(default="", max_length=4000)
    priorities: List[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

try:
    from brain import Brain  # type: ignore
    _BRAIN_OK = True
except Exception as e:
    _BRAIN_OK = False
    print(f"[intel-loop] brain unavailable: {e}")

try:
    from cognition import process_content  # type: ignore
    _COGNITION_OK = True
except Exception as e:
    _COGNITION_OK = False
    print(f"[intel-loop] cognition unavailable: {e}")


# ─── Config ────────────────────────────────────────────────────────────────

GLASSBOX_SERVER = os.environ.get("GLASSBOX_SERVER_URL", "http://127.0.0.1:8790").rstrip("/")
OLLAMA_MODEL = os.environ.get("FULCRUM_LLM_MODEL", "qwen2.5:14b")
LAYERS_TO_ANALYZE = os.environ.get(
    "INTEL_LAYERS", "planes,ships,earthquakes,satellites"
).split(",")
INTERVAL_SEC = int(os.environ.get("INTEL_INTERVAL_SEC", "300"))
MAX_EVENTS_PER_LAYER = int(os.environ.get("INTEL_MAX_EVENTS", "500"))
BASELINE_BRAIN_KEY = "intel_loop:baselines"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger("intel-loop")


# ─── Data pulls ────────────────────────────────────────────────────────────

def fetch_layer_events(layer: str, limit: int) -> List[Dict[str, Any]]:
    url = f"{GLASSBOX_SERVER}/api/glassbox/layer/{layer}?limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FulcrumIntelLoop/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("events") or []
    except Exception as e:
        log.info(f"fetch {layer} failed: {e}")
        return []


def fetch_server_health() -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"{GLASSBOX_SERVER}/api/health", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# ─── Brain-backed baseline persistence ─────────────────────────────────────

def load_baselines(brain: Any) -> Dict[str, Dict[str, float]]:
    if not brain:
        return {}
    try:
        hits = brain.recall(BASELINE_BRAIN_KEY, namespace="glassbox", k=1)
        if not hits:
            return {}
        obj = hits[0].get("object") or ""
        if not obj.startswith("{"):
            return {}
        return json.loads(obj)
    except Exception as e:
        log.debug(f"load baselines failed: {e}")
        return {}


def save_baselines(brain: Any, baselines: Dict[str, Dict[str, float]]) -> None:
    if not brain:
        return
    try:
        brain.remember(
            namespace="glassbox",
            predicate="state",
            subject="intel_loop:baselines",
            object=json.dumps(baselines, separators=(",", ":")),
            source="intelligence_loop.py",
            tags="intel-loop,baselines",
        )
    except Exception as e:
        log.debug(f"save baselines failed: {e}")


# ─── AI SITREP composition (via cognition pipeline) ────────────────────────

def _build_sitrep_prompt(
    anomalies: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    layer_counts: Dict[str, int],
    server_health: Optional[Dict[str, Any]],
) -> str:
    total_events = sum(layer_counts.values())
    return json.dumps({
        "task": (
            "You are the Glassbox world-situation analyst. Summarize the current "
            "state of the world based on THESE specific signals only. Be concise "
            "(120-200 words), high-signal, no filler, no hedging language. "
            "Lead with the single most consequential development. Cite specific "
            "regions/events. Don't invent data. If anomalies or correlations are "
            "empty, say the world appears quiet across monitored layers."
        ),
        "return_format": (
            "JSON only: {\"headline\":\"<one sentence>\",\"brief\":\"<120-200 "
            "words>\",\"priorities\":[\"<top 1-3 regions/layers to watch>\"],"
            "\"confidence\":0.0-1.0}"
        ),
        "now_utc": datetime.now(timezone.utc).isoformat(),
        "total_events_last_cycle": total_events,
        "events_per_layer": layer_counts,
        "top_anomalies": anomalies[:8],
        "top_correlations": correlations[:6],
        "ingester_health": [
            {"layer": i.get("layer"), "health": i.get("health"),
             "tracked": i.get("tracked_entities"), "last_error": i.get("last_error")}
            for i in (server_health or {}).get("ingesters", [])
        ],
    }, separators=(",", ":"))


def _parse_sitrep(raw: str) -> Dict[str, Any]:
    """Schema-bound parse via llm_json.parse_with_schema.

    On total failure (no JSON object in the LLM output) returns the same
    'SITREP parse failed' fallback the prior bracket-extraction path
    used, with the raw text truncated to 400 chars in `brief` so an
    operator looking at /api/intel/* can see what actually came back.
    """
    parsed, err = parse_with_schema(
        raw, _SitrepSchema, fallback=None,
    )
    if parsed is not None:
        return parsed.model_dump()
    log.debug("intelligence_loop: SITREP parse_with_schema -> %s", err)
    return {
        "headline":   "SITREP parse failed",
        "brief":      (raw or "").strip()[:400],
        "priorities": [],
        "confidence": 0.0,
    }


async def compose_sitrep(
    anomalies: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    layer_counts: Dict[str, int],
    server_health: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Call Ollama via the cognition pipeline to produce a natural-language SITREP."""
    prompt = _build_sitrep_prompt(anomalies, correlations, layer_counts, server_health)

    async def _ollama_generate(enhanced_prompt: str) -> str:
        # Routes through llm_ollama which picks /api/generate (default,
        # legacy) or /v1/chat/completions (set GLASSBOX_OLLAMA_USE_CHAT_API=1).
        # Note: the SITREP uses num_ctx=8192 which is honored by
        # /api/generate but NOT by Ollama's OpenAI shim. Before
        # flipping the feature flag for this call site, set
        # PARAMETER num_ctx 8192 in the qwen2.5:14b Modelfile or the
        # SITREP context will silently truncate.
        from llm_ollama import generate_json
        return await generate_json(
            model=OLLAMA_MODEL,
            task="sitrep",
            system=(
                "You are the Glassbox world-situation analyst. Produce a single "
                "concise world-state briefing from the evidence provided. Return "
                "JSON only with keys headline, brief, priorities, confidence. "
                "No prose, no code fences."
            ),
            prompt=enhanced_prompt,
            temperature=0.2,
            num_ctx=8192,
            max_tokens=700,
            timeout_total=150,
        )

    if _COGNITION_OK:
        try:
            result = await process_content(
                content_type="glassbox_intel",
                prompt=prompt,
                generator_fn=_ollama_generate,
                recall_k=4,
                recall_namespaces=["claude_meta", "glassbox", "holding"],
                recall_extra_terms="world state situation analysis anomalies",
                model=OLLAMA_MODEL,
                context={"anomalies": len(anomalies), "correlations": len(correlations)},
            )
            return {
                "sitrep": _parse_sitrep(result.get("output") or ""),
                "cognition": {
                    "action": result.get("action"),
                    "confidence": (result.get("critique") or {}).get("confidence"),
                    "lessons_used": result.get("lessons_used_count"),
                    "critique_summary": (result.get("critique") or {}).get("summary"),
                },
            }
        except Exception as e:
            log.warning(f"cognition path failed, direct: {e}")

    # Fallback: direct Ollama without cognition
    try:
        raw = await _ollama_generate(prompt)
        return {"sitrep": _parse_sitrep(raw), "cognition": {"action": "direct"}}
    except Exception as e:
        log.warning(f"direct ollama also failed: {e}")
        return {"sitrep": {"headline": "SITREP unavailable",
                           "brief": f"Ollama offline: {e}",
                           "priorities": [], "confidence": 0.0},
                "cognition": {"action": "error"}}


# ─── Server push ───────────────────────────────────────────────────────────

def publish_to_server(report: Dict[str, Any]) -> bool:
    url = f"{GLASSBOX_SERVER}/api/glassbox/sitrep/publish"
    try:
        data = json.dumps(report).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        log.info(f"server push failed (server may not have endpoint yet): {e}")
        return False


# ─── Main cycle ────────────────────────────────────────────────────────────

async def run_cycle() -> Dict[str, Any]:
    t0 = datetime.now(timezone.utc)
    log.info(f"=== intel-loop cycle start {t0.isoformat()} ===")

    brain = Brain() if _BRAIN_OK else None

    # 1. Pull events
    all_events: List[Dict[str, Any]] = []
    layer_counts: Dict[str, int] = {}
    for layer in LAYERS_TO_ANALYZE:
        events = fetch_layer_events(layer.strip(), MAX_EVENTS_PER_LAYER)
        all_events.extend(events)
        layer_counts[layer] = len(events)
    log.info(f"fetched {len(all_events)} events across {len(layer_counts)} layers: {layer_counts}")

    server_health = fetch_server_health()

    # 2. Anomalies
    baselines = load_baselines(brain)
    buckets = bucket_events(all_events)
    anomalies = detect_anomalies(buckets, baselines)
    baselines = update_baselines(baselines, buckets)
    save_baselines(brain, baselines)
    log.info(f"anomalies detected: {len(anomalies)}")

    # 3. Correlations
    correlations = find_correlations(all_events)
    log.info(f"correlation clusters: {len(correlations)}")

    # 3b. Watchlists — user-defined geofences evaluated against this cycle.
    alerts_fired = 0
    try:
        watchlists = list_watchlists()
        fired = evaluate_watchlists(watchlists, all_events, anomalies, correlations)
        alerts_fired = dispatch_alerts(fired)
        if watchlists:
            log.info(f"watchlists: {len(watchlists)} · fired {alerts_fired} alerts")
    except Exception as e:
        log.warning(f"watchlist evaluation failed: {e}")

    # 4. AI SITREP
    sitrep_bundle = await compose_sitrep(anomalies, correlations, layer_counts, server_health)
    sitrep = sitrep_bundle["sitrep"]
    log.info(f"SITREP: {sitrep.get('headline', '')[:120]}")

    # 4b. 48-hour forecast — only runs every 6th cycle (~30 min) since it
    # uses Ollama heavily. Supervisor fires the loop every 5 min; we gate
    # the forecaster on a simple time-of-day modulo check.
    forecast_report = None
    try:
        now_min = datetime.now(timezone.utc).minute
        if now_min < 5 or (now_min % 30) < 5:   # fire at :00 and :30 each hour
            from forecaster import forecast_next_48h   # type: ignore
            forecast_report = await forecast_next_48h(top_k=5, hours_back=72)
            log.info(f"forecast: {len(forecast_report.get('hotspots', []))} hotspots")
    except Exception as e:
        log.info(f"forecast skipped/failed: {e}")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cycle_started_at": t0.isoformat(),
        "cycle_duration_sec": (datetime.now(timezone.utc) - t0).total_seconds(),
        "layer_counts": layer_counts,
        "total_events": len(all_events),
        "anomalies": anomalies,
        "correlations": correlations,
        "sitrep": sitrep,
        "cognition": sitrep_bundle.get("cognition"),
        "alerts_fired": alerts_fired,
        "forecast": forecast_report,
        "server_health_snapshot": {
            "ingesters": [(i.get("layer"), i.get("health")) for i in (server_health or {}).get("ingesters", [])],
            "subscribers": (server_health or {}).get("subscribers"),
        },
    }

    # 5. Brain persistence
    if brain:
        try:
            brain.remember(
                namespace="glassbox", predicate="sitrep",
                subject=f"sitrep:{t0.strftime('%Y%m%dT%H%M')}",
                object=json.dumps({
                    "headline": sitrep.get("headline"),
                    "brief": sitrep.get("brief"),
                    "confidence": sitrep.get("confidence"),
                    "anomaly_count": len(anomalies),
                    "correlation_count": len(correlations),
                })[:3800],
                source="intelligence_loop.py",
                tags="sitrep,auto,world-state",
            )
            for a in anomalies[:10]:
                brain.remember(
                    namespace="glassbox", predicate="anomaly",
                    subject=f"anomaly:{a['layer']}:{a['region']}:{t0.strftime('%Y%m%dT%H%M')}",
                    object=json.dumps(a, default=str)[:3800],
                    source="intelligence_loop.py",
                    tags=f"anomaly,{a['layer']},{a['region']},sev:{a['anomaly_severity']}",
                )
            brain.log_event(
                namespace="glassbox", kind="intel_cycle",
                summary=f"SITREP: {sitrep.get('headline', '')[:140]}",
                detail={"layer_counts": layer_counts, "anomalies": len(anomalies),
                        "correlations": len(correlations)},
                severity="info", source="intelligence_loop.py",
            )
            brain.heartbeat("intel_loop", payload={
                "total_events": len(all_events), "anomalies": len(anomalies),
                "correlations": len(correlations),
            })
        except Exception as e:
            log.warning(f"brain write failed: {e}")

    # 6. Publish to server so front-end can render live
    publish_to_server(report)
    return report


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Glassbox real-time intelligence loop")
    p.add_argument("--forever", action="store_true",
                   help="run continuously; default is single cycle (for supervisor interval mode)")
    p.add_argument("--dry-run", action="store_true",
                   help="run but don't publish or persist")
    return p.parse_args()


async def main_loop():
    while True:
        try:
            await run_cycle()
        except Exception as e:
            log.exception(f"cycle crashed: {e}")
        log.info(f"sleeping {INTERVAL_SEC}s")
        await asyncio.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    args = _parse_args()
    if args.forever:
        asyncio.run(main_loop())
    else:
        report = asyncio.run(run_cycle())
        print(json.dumps({
            "headline": report["sitrep"].get("headline"),
            "brief": report["sitrep"].get("brief"),
            "anomalies": len(report.get("anomalies", [])),
            "correlations": len(report.get("correlations", [])),
            "total_events": report.get("total_events"),
        }, indent=2, default=str))
