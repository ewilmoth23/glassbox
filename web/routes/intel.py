"""`/api/intel/*` — Glassbox intel surface (the Glassbox-branded
counterpart to `/api/v1/*`, per API_SURFACES.md).

Extracted from `glassbox_server.py` 2026-05-22 as P3-H extraction #10.
Ten routes — nine read handlers that surface `latest_sitrep` data the
brief publisher refreshes in the background, plus the one POST handler
that runs a live LLM query grounded against the hot_cache.

  GET  /api/intel/latest          — top-line sitrep summary
  GET  /api/intel/anomalies       — recent tier-1 anomalies (DB read)
  GET  /api/intel/predictions     — placeholder (no backend forecast)
  GET  /api/intel/threat-briefing — text body from brief publisher
  GET  /api/intel/alerts          — tier-1 alerts in the last 6 hours
  GET  /api/intel/alerts/poll     — alias of /alerts
  GET  /api/intel/confidence      — current cycle confidence score
  GET  /api/intel/accuracy        — placeholder for historical accuracy
  GET  /api/intel/type/{type}     — typed intel dispatch (threat-assessment,
                                    narrative-intel, hotspot-prediction,
                                    correlation-analysis)
  POST /api/intel/query           — live LLM query grounded against hot_cache

All shared daemon state (`latest_sitrep`, `hot_cache`) is accessed via
`request.app.state.<name>` — populated by the additive bridge in
`glassbox_server.py`'s startup hook (commit `3231f63`). The
`getattr(state, "X", <default>)` pattern keeps responses 200 even in
test contexts that build a fresh `FastAPI()` without running startup.

The /api/intel/query handler imports `llm_rate_check` from
`web._rate_limit` (lifted in commit `1039777`) — same helper used by
the /api/glassbox/{ask,sitrep/publish} handlers still in
glassbox_server.py until extraction #9 lands.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web._rate_limit import llm_rate_check

log = logging.getLogger("glassbox-server.intel")
router = APIRouter()


def _sitrep(request: Request) -> Dict[str, Any]:
    """Read the latest_sitrep dict from app.state, returning an empty
    dict if startup hasn't run or the brief publisher hasn't produced
    one yet. Centralized so the 8 read handlers below don't each have
    to repeat the getattr dance."""
    return getattr(request.app.state, "latest_sitrep", {}) or {}


@router.get("/api/intel/latest")
async def intel_latest(request: Request) -> JSONResponse:
    """Latest intel summary — same data as /api/glassbox/sitrep/latest
    but in the shape glassbox.html's intel panels expect."""
    state = _sitrep(request)
    s = state.get("sitrep") or {}
    return JSONResponse({
        "generated_at": state.get("generated_at"),
        "headline": s.get("headline"),
        "brief": s.get("brief"),
        "confidence": s.get("confidence", 0.0),
        "total_events": state.get("total_events", 0),
    })


@router.get("/api/intel/anomalies")
async def intel_anomalies(request: Request, limit: int = 20) -> JSONResponse:
    """Anomaly list — pulled from the multi-juris + shadow-fleet + dark-
    vessel + sanctioned-rendezvous events that have lit up recently."""
    sql = """
        SELECT id, event_type, event_subtype, event_time, severity,
               title, description, properties,
               ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng
        FROM event
        WHERE event_type IN ('sanctioned_vessel_multijurisdictional',
                             'shadow_fleet_cluster',
                             'sanctioned_vessel_went_dark',
                             'sanctioned_vessel_rendezvous',
                             'dark_vessel_detected')
          AND event_time >= NOW() - INTERVAL '24 hours'
        ORDER BY severity DESC NULLS LAST, event_time DESC
        LIMIT $1
    """
    try:
        from db import fetch as _db_fetch
        rows = await _db_fetch(sql, max(1, min(limit, 100)))
        anomalies = [
            {
                "id": str(r["id"]),
                "event_type": r["event_type"],
                "event_subtype": r["event_subtype"],
                "event_time": r["event_time"].isoformat() if r["event_time"] else None,
                "severity": float(r["severity"]) if r["severity"] is not None else None,
                "title": r["title"],
                "description": r["description"],
                "lat": float(r["lat"]) if r["lat"] is not None else None,
                "lng": float(r["lng"]) if r["lng"] is not None else None,
            }
            for r in rows
        ]
    except Exception as e:
        log.warning(f"/api/intel/anomalies: {type(e).__name__}: {e}")
        anomalies = []
    return JSONResponse({
        "generated_at": _sitrep(request).get("generated_at"),
        "count": len(anomalies),
        "anomalies": anomalies,
    })


@router.get("/api/intel/predictions")
async def intel_predictions(request: Request) -> JSONResponse:
    """Predictions panel — we don't run a forecasting model in the
    backend, so return an empty list. (forecasting lives elsewhere
    in the empire.)"""
    return JSONResponse({
        "generated_at": _sitrep(request).get("generated_at"),
        "count": 0,
        "predictions": [],
    })


@router.get("/api/intel/threat-briefing")
async def intel_threat_briefing(request: Request) -> JSONResponse:
    """Threat-briefing — text payload from the brief publisher."""
    state = _sitrep(request)
    s = state.get("sitrep") or {}
    return JSONResponse({
        "generated_at": state.get("generated_at"),
        "title": s.get("headline") or "Glassbox threat briefing",
        "body": s.get("brief") or "",
        "confidence": s.get("confidence", 0.0),
    })


@router.get("/api/intel/alerts")
async def intel_alerts(request: Request, limit: int = 20) -> JSONResponse:
    """Recent tier-1 alerts (any type). Wraps the same data the SSE
    stream pushes, but as a one-shot snapshot for cold-load rendering."""
    tier1_types = (
        "shadow_fleet_cluster",
        "sanctioned_vessel_multijurisdictional",
        "sanctioned_vessel_went_dark",
        "sanctioned_vessel_rendezvous",
        "sanctioned_vessel_underway",
        "aircraft_in_sanctioned_airspace",
        "dark_vessel_detected",
        "military_aircraft_underway",
        "rendezvous_detected",
        "loitering_detected",
        "volcanic_alert",
        "swpc_alert",
        "gdacs_alert",
    )
    try:
        from db import fetch as _db_fetch
        rows = await _db_fetch(
            """
            SELECT id, event_type, event_subtype, event_time, severity,
                   title, description,
                   ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng
            FROM event
            WHERE event_type = ANY($1::text[])
              AND event_time >= NOW() - INTERVAL '6 hours'
            ORDER BY event_time DESC
            LIMIT $2
            """,
            list(tier1_types), max(1, min(limit, 200)),
        )
        alerts = [
            {
                "id": str(r["id"]),
                "event_type": r["event_type"],
                "event_subtype": r["event_subtype"],
                "event_time": r["event_time"].isoformat() if r["event_time"] else None,
                "severity": float(r["severity"]) if r["severity"] is not None else None,
                "title": r["title"],
                "description": r["description"],
                "lat": float(r["lat"]) if r["lat"] is not None else None,
                "lng": float(r["lng"]) if r["lng"] is not None else None,
            }
            for r in rows
        ]
    except Exception as e:
        log.warning(f"/api/intel/alerts: {type(e).__name__}: {e}")
        alerts = []
    return JSONResponse({
        "generated_at": _sitrep(request).get("generated_at"),
        "count": len(alerts),
        "alerts": alerts,
    })


@router.get("/api/intel/alerts/poll")
async def intel_alerts_poll(request: Request, limit: int = 20) -> JSONResponse:
    """Polling variant of /alerts — same payload."""
    return await intel_alerts(request, limit=limit)


@router.get("/api/intel/confidence")
async def intel_confidence(request: Request) -> JSONResponse:
    """Confidence score for the current intel cycle."""
    state = _sitrep(request)
    s = state.get("sitrep") or {}
    return JSONResponse({
        "generated_at": state.get("generated_at"),
        "confidence": s.get("confidence", 0.0),
    })


@router.get("/api/intel/accuracy")
async def intel_accuracy(request: Request) -> JSONResponse:
    """Historical accuracy score — we don't track accuracy yet, so
    return a static placeholder. Pro tier could plug in real metrics."""
    return JSONResponse({
        "generated_at": _sitrep(request).get("generated_at"),
        "accuracy_30d": None,
        "accuracy_7d": None,
        "samples": 0,
    })


@router.get("/api/intel/type/{intel_type}")
async def intel_type_dispatch(intel_type: str, request: Request) -> JSONResponse:
    """Typed intel fetch. Returns a {type, generated_at, items[]} envelope.
    Item shape varies by type but stays JSON-stable so the panels can
    render uniformly."""
    state = _sitrep(request)
    s = state.get("sitrep") or {}
    base = {
        "type": intel_type,
        "generated_at": state.get("generated_at"),
        "confidence": s.get("confidence", 0.0),
    }
    t = intel_type.lower()

    if t in ("threat-assessment", "narrative-intel"):
        # Both reuse the brief publisher's text output. Threat-assessment
        # gets the headline first; narrative-intel returns the full body.
        return JSONResponse({
            **base,
            "title": s.get("headline") or "Glassbox intel",
            "body": s.get("brief") or "",
        })

    if t == "hotspot-prediction":
        # We don't run forecasts in the backend; surface recent multi-juris
        # + shadow-fleet clusters as "current hotspots" instead. Same data
        # the dashboard uses for the CRITICAL banner.
        try:
            from db import fetch as _db_fetch
            rows = await _db_fetch(
                """
                SELECT id, event_type, event_subtype, event_time, severity,
                       title, properties,
                       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng
                FROM event
                WHERE event_type IN ('shadow_fleet_cluster',
                                     'sanctioned_vessel_multijurisdictional',
                                     'sanctioned_vessel_rendezvous',
                                     'sanctioned_vessel_went_dark')
                  AND event_time >= NOW() - INTERVAL '24 hours'
                ORDER BY severity DESC NULLS LAST, event_time DESC
                LIMIT 20
                """
            )
            items = [
                {"id": str(r["id"]), "event_type": r["event_type"],
                 "title": r["title"], "severity": float(r["severity"]) if r["severity"] is not None else None,
                 "lat": float(r["lat"]) if r["lat"] is not None else None,
                 "lng": float(r["lng"]) if r["lng"] is not None else None}
                for r in rows
            ]
        except Exception as e:
            log.warning(f"/api/intel/type/{t}: {type(e).__name__}: {e}")
            items = []
        return JSONResponse({**base, "items": items})

    if t == "correlation-analysis":
        # Cross-domain proximity findings — entity-event + entity-entity
        # pairs that the proximity scan emits.
        try:
            from db import fetch as _db_fetch
            rows = await _db_fetch(
                """
                SELECT id, event_subtype, event_time, severity, title,
                       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng
                FROM event
                WHERE event_type = 'detected_proximity'
                  AND event_time >= NOW() - INTERVAL '1 hour'
                ORDER BY event_time DESC
                LIMIT 20
                """
            )
            items = [
                {"id": str(r["id"]), "event_subtype": r["event_subtype"],
                 "title": r["title"], "severity": float(r["severity"]) if r["severity"] is not None else None,
                 "lat": float(r["lat"]) if r["lat"] is not None else None,
                 "lng": float(r["lng"]) if r["lng"] is not None else None}
                for r in rows
            ]
        except Exception as e:
            log.warning(f"/api/intel/type/{t}: {type(e).__name__}: {e}")
            items = []
        return JSONResponse({**base, "items": items})

    # Unknown type: return empty envelope rather than 404 so the panel
    # renders "—" cleanly.
    return JSONResponse({**base, "items": [], "warning": f"unknown intel type: {intel_type}"})


@router.post("/api/intel/query")
async def intel_query(request: Request) -> JSONResponse:
    """Real-time AI intel query powered by Ollama. Gathers live globe
    state from `request.app.state.hot_cache`, passes it as grounded
    context, returns an intelligence brief."""
    llm_rate_check(request, scope="intel_query")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, 400)

    question = (body.get("query") or body.get("question") or "").strip()
    if not question or len(question) < 3:
        return JSONResponse({"ok": False, "error": "query required (min 3 chars)"}, 400)
    if len(question) > 1000:
        question = question[:1000]

    # ── Build live context snapshot from hot cache ────────────────────────────
    hot_cache = getattr(request.app.state, "hot_cache", {}) or {}
    layer_summary: List[str] = []
    top_severity: List[Dict[str, Any]] = []
    total_events = 0

    for layer_name, evs in hot_cache.items():
        ev_list = list(evs)
        if not ev_list:
            continue
        layer_summary.append(f"{layer_name}:{len(ev_list)}")
        total_events += len(ev_list)
        for ev in ev_list[-20:]:
            sev = getattr(ev, "severity", 0) or 0
            if sev >= 3:
                top_severity.append({
                    "layer": layer_name,
                    "title": (getattr(ev, "title", "") or "")[:80],
                    "severity": sev,
                    "lat": round(getattr(ev, "lat", 0) or 0, 2),
                    "lng": round(getattr(ev, "lng", 0) or 0, 2),
                })

    top_severity.sort(key=lambda x: x["severity"], reverse=True)
    top_severity = top_severity[:15]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    context_lines = [
        f"LIVE GLASSBOX GLOBE — {ts}",
        f"Total cached events: {total_events}",
        f"Active layers: {', '.join(layer_summary[:12])}",
        "",
        "TOP SEVERITY EVENTS:",
    ]
    for ev in top_severity:
        context_lines.append(
            f"  [{ev['layer'].upper()}] {ev['title']} "
            f"(severity:{ev['severity']}/5, lat:{ev['lat']} lng:{ev['lng']})"
        )
    context = "\n".join(context_lines)

    # ── Ollama primary (local, no API cost, no external context contamination) ─
    try:
        # Routes through llm_ollama.generate_text (legacy default;
        # GLASSBOX_OLLAMA_USE_CHAT_API=1 flips to /v1/chat/completions).
        from llm_ollama import generate_text, _model_name
        # P1-C (2026-05-21): explicit FULCRUM_LLM_MODEL still wins as a global
        # override, but if unset we route by task (intel_query → llama3.1
        # per the 2026-05-21 benchmark — 2× faster than qwen2.5:14b at
        # equal quality on this prose-tactical-brief shape).
        model = os.environ.get("FULCRUM_LLM_MODEL")  # may be None
        prompt = f"GLOBE CONTEXT:\n{context}\n\nQUERY: {question}\n\nINTEL BRIEF:"
        answer = await generate_text(
            model=model,
            task="intel_query",
            system=(
                "You are Glassbox, a live OSINT intelligence analyst. "
                "Answer directly and tactically in under 300 words."
            ),
            prompt=prompt,
            temperature=0.1, num_ctx=4096, max_tokens=500,
            timeout_total=90,
        )
        resolved_model = model or _model_name("intel_query")
        return JSONResponse({
            "ok": True,
            "answer": answer,
            "model": f"ollama:{resolved_model}",
            "context_events": total_events,
            "context_layers": len(layer_summary),
            "top_events": top_severity[:5],
        })
    except Exception as oe:
        return JSONResponse({"ok": False, "error": f"AI unavailable: {oe}"}, 503)
