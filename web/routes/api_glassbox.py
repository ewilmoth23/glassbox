"""`/api/glassbox/*` — Glassbox-branded daemon-state surface.

Extracted from `glassbox_server.py` 2026-05-22 EVE as P3-H extraction
#9 — the final route group. 21 routes, the biggest single concern:

  GET    /api/glassbox/diagnostic                — full transparency endpoint
  GET    /api/glassbox/layers                    — per-ingester summary
  GET    /api/glassbox/layer/{name}              — hot_cache window for one layer
  GET    /api/glassbox/entities                  — viewport-culled entity query
  POST   /api/glassbox/sitrep/publish            — intel-loop sitrep ingest
  GET    /api/glassbox/sitrep/latest             — latest sitrep
  GET    /api/glassbox/state                     — cache-friendly empty hydration
  GET    /api/glassbox/anomalies/latest          — sitrep anomalies slice
  GET    /api/glassbox/correlations/latest       — sitrep correlations slice
  POST   /api/glassbox/watchlist                 — Pro create watchlist
  GET    /api/glassbox/watchlist                 — list (filter by ?email=)
  GET    /api/glassbox/watchlist/{wl_id}         — single watchlist by id
  DELETE /api/glassbox/watchlist/{wl_id}         — delete
  POST   /api/glassbox/ask                       — natural-language brain query
  GET    /api/glassbox/forecast/latest           — 48h hotspot forecast
  GET    /api/glassbox/pro-status                — is-pro check
  POST   /api/glassbox/pro/activate              — Stripe-webhook → mark_pro
  POST   /api/glassbox/pro/cancel                — Stripe-webhook → cancel_pro
  GET    /api/glassbox/news-manifest             — live news pins + disk fallback
  GET    /api/glassbox/history/{layer}           — archived events from Brain
  GET    /api/glassbox/stream                    — SSE real-time push

All shared daemon state (`ingesters`, `subscribers`, `hot_cache`,
`latest_sitrep`, `entities_cache`, `layer_aliases`, `subscriber_drops`)
is accessed via `request.app.state.<name>` — populated by the additive
bridges in `glassbox_server.py` startup (commits `3231f63`, `b97bab3`,
`fec4193`). The `getattr(state, "X", <default>)` pattern keeps each
route's response 200 in test contexts that don't trigger startup.

Helpers imported from sibling modules:
  - `llm_rate_check` from web._rate_limit (gates /ask + /sitrep/publish)
  - `deliver_to_subscribers` from web._broadcast (single-message SSE
    delivery used by /sitrep/publish)

External-system imports (brain, watchlist, pro, llm_ollama) are kept
lazy per-handler as in the original — they may not be available in
fresh checkouts or CI, and the routes return 500/503 instead of
crashing the daemon import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ingesters.base import GlassboxEvent
from ingesters.citizen_adapter import CITIZEN_FEED_FILE as _CITIZEN_FEED_FILE
from web._broadcast import deliver_to_subscribers
from web._rate_limit import llm_rate_check

log = logging.getLogger("glassbox-server.api_glassbox")
router = APIRouter()

# Constants used only by the /stream handler — inlined rather than bridged
# (they were defined once in glassbox_server.py as plain module constants).
_SUBSCRIBER_QUEUE_SIZE = 1000
_SSE_PING_SEC = 20.0

# `web/routes/api_glassbox.py` → parent.parent.parent is 21_GLASSBOX_AI/.
_PKG_ROOT = Path(__file__).resolve().parent.parent.parent
_EMPIRE_ROOT = _PKG_ROOT.parent


# ─── Helpers ────────────────────────────────────────────────────────────────

def _check_activation_auth(request: Request) -> bool:
    """Gate for /api/glassbox/pro/{activate,cancel} — shared-secret
    header from the Worker Stripe webhook. Fail closed if not set."""
    expected = os.environ.get("PRO_ACTIVATE_SECRET") or ""
    if not expected:
        return False
    got = request.headers.get("X-Activation-Secret") or ""
    return got == expected


# ─── Diagnostic + layer introspection ───────────────────────────────────────

@router.get("/api/glassbox/diagnostic")
async def diagnostic(request: Request) -> JSONResponse:
    """One-stop transparency endpoint. The frontend hits this on demand (or
    every ~30s) to render per-layer health badges and answer the "why is
    this layer empty" question without hunting through logs."""
    state = request.app.state
    started_at = getattr(state, "started_at", None)
    ingesters = getattr(state, "ingesters", [])
    subscribers = getattr(state, "subscribers", [])
    hot_cache = getattr(state, "hot_cache", {})
    entities_cache = getattr(state, "entities_cache", {})
    layer_aliases = getattr(state, "layer_aliases", {})

    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        if started_at:
            started_dt = datetime.fromisoformat(started_at)
            uptime_sec = (datetime.now(timezone.utc) - started_dt).total_seconds()
        else:
            uptime_sec = 0.0
    except Exception:
        uptime_sec = 0.0

    ing_rows: List[Dict[str, Any]] = []
    for ing in ingesters:
        try:
            st = ing.status() or {}
        except Exception as e:
            st = {"health": "error", "tracked_entities": 0, "_status_error": str(e)}
        layer_name = getattr(ing, "layer", "?")
        alias = layer_aliases.get(layer_name, "")
        ing_rows.append({
            "layer": layer_name,
            "source": getattr(ing, "source", ""),
            "health": st.get("health"),
            "tracked_entities": st.get("tracked_entities"),
            "last_fetch_ts": getattr(ing, "last_fetch_ts", None),
            "last_fetch_count": getattr(ing, "last_fetch_count", None),
            "last_cycle_ms": getattr(ing, "last_cycle_ms", None),
            "last_error": getattr(ing, "last_error", None),
            "cycles_run": getattr(ing, "cycles_run", None),
            "cycles_failed": getattr(ing, "cycles_failed", None),
            "events_in_cache": len(hot_cache.get(layer_name, [])),
            "alias": alias or None,
            "alias_events": len(hot_cache.get(alias, [])) if alias else 0,
        })

    return JSONResponse({
        "ts": now_iso,
        "started_at": started_at,
        "uptime_sec": round(uptime_sec, 1),
        "subscribers": len(subscribers),
        "ingesters": ing_rows,
        "hot_cache_layers": {k: len(v) for k, v in hot_cache.items()},
        "hot_cache_total_events": sum(len(v) for v in hot_cache.values()),
        "entities_cache_size": len(entities_cache),
        "layer_aliases": dict(layer_aliases),
    })


@router.get("/api/glassbox/layers")
async def layers(request: Request) -> JSONResponse:
    state = request.app.state
    ingesters = getattr(state, "ingesters", [])
    hot_cache = getattr(state, "hot_cache", {})
    return JSONResponse({
        "count": len(ingesters),
        "layers": [
            {
                "name": ing.layer,
                "source": ing.source,
                "health": ing.status()["health"],
                "tracked": ing.status()["tracked_entities"],
                "last_update": ing.last_fetch_ts,
                "events_cached": len(hot_cache.get(ing.layer, [])),
            }
            for ing in ingesters
        ],
    })


@router.get("/api/glassbox/layer/{name}")
async def layer(name: str, request: Request, limit: int = 500) -> JSONResponse:
    state = request.app.state
    hot_cache = getattr(state, "hot_cache", {})
    cache_cap = getattr(state, "hot_cache_per_layer", 5000)
    events = list(hot_cache.get(name, []))
    limit = max(1, min(limit, cache_cap))
    window = events[-limit:]
    return JSONResponse({
        "layer": name,
        "total_cached": len(events),
        "returned": len(window),
        "events": [e.to_dict() for e in window],
    })


@router.get("/api/glassbox/entities")
async def entities_bbox(
    request: Request,
    min_lat: float = -90.0,
    max_lat: float = 90.0,
    min_lng: float = -180.0,
    max_lng: float = 180.0,
    layer_name: Optional[str] = None,
    limit: int = 2000,
) -> JSONResponse:
    """Viewport-culled entity endpoint. The browser passes its current
    bounding box and the server returns ONLY entities that fall within
    that viewport. Single biggest perf fix for Glassbox."""
    state = request.app.state
    hot_cache = getattr(state, "hot_cache", {})
    entities_cache = getattr(state, "entities_cache", {})
    q = getattr(state, "entities_bbox_quantum", 0.5)
    cache_ttl = getattr(state, "entities_cache_ttl_sec", 1.0)
    limit = max(1, min(limit, 5000))

    bbox_key = (
        round(min_lat / q) * q, round(max_lat / q) * q,
        round(min_lng / q) * q, round(max_lng / q) * q,
    )
    cache_key = (layer_name or "", bbox_key, limit)
    now_t = asyncio.get_event_loop().time()
    cached = entities_cache.get(cache_key)
    if cached is not None:
        expires_at, payload = cached
        if expires_at > now_t:
            return JSONResponse(payload)
        entities_cache.pop(cache_key, None)

    crosses_antimeridian = min_lng > max_lng
    if layer_name:
        layers_to_scan = [layer_name] if layer_name in hot_cache else []
    else:
        layers_to_scan = list(hot_cache.keys())

    matched: list = []
    total_in_cache = 0
    for lname in layers_to_scan:
        dq = hot_cache.get(lname)
        if dq is None:
            continue
        total_in_cache += len(dq)
        if crosses_antimeridian:
            for ev in dq:
                lat = ev.lat
                if lat < min_lat or lat > max_lat:
                    continue
                lng = ev.lng
                if not (lng >= min_lng or lng <= max_lng):
                    continue
                matched.append(ev)
        else:
            for ev in dq:
                lat = ev.lat
                if lat < min_lat or lat > max_lat:
                    continue
                lng = ev.lng
                if lng < min_lng or lng > max_lng:
                    continue
                matched.append(ev)

    matched.sort(key=lambda e: e.severity, reverse=True)
    window = matched[:limit]

    payload = {
        "bbox": {
            "min_lat": min_lat, "max_lat": max_lat,
            "min_lng": min_lng, "max_lng": max_lng,
        },
        "layers_queried": layers_to_scan,
        "total_in_cache": total_in_cache,
        "returned": len(window),
        "events": [e.to_dict() for e in window],
    }
    entities_cache[cache_key] = (now_t + cache_ttl, payload)
    # Bound cache size — drop oldest if it grows past a sane ceiling.
    if len(entities_cache) > 256:
        items_sorted = sorted(entities_cache.items(), key=lambda kv: kv[1][0])
        for k, _ in items_sorted[:64]:
            entities_cache.pop(k, None)
    return JSONResponse(payload)


# ─── Sitrep — intel-loop publish/read ───────────────────────────────────────

@router.post("/api/glassbox/sitrep/publish")
async def sitrep_publish(request: Request):
    """Called by intelligence_loop.py after each cycle. Stores the latest
    AI-generated SITREP + anomalies + correlations for client polling."""
    llm_rate_check(request, scope="sitrep", max_per_window=20, window_sec=300)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict) or "sitrep" not in body:
        return JSONResponse({"ok": False, "error": "missing sitrep field"}, status_code=400)
    state = request.app.state
    latest_sitrep = getattr(state, "latest_sitrep", None)
    if latest_sitrep is None:
        return JSONResponse({"ok": False, "error": "daemon state not initialized"}, status_code=503)
    latest_sitrep.clear()
    latest_sitrep.update(body)
    try:
        msg = {
            "layer": "_sitrep", "external_id": "latest", "kind": "state",
            "lat": 0.0, "lng": 0.0, "ts": body.get("generated_at", ""),
            "severity": 0,
            "payload": {
                "headline": (body.get("sitrep") or {}).get("headline"),
                "brief": (body.get("sitrep") or {}).get("brief"),
                "anomaly_count": len(body.get("anomalies") or []),
                "correlation_count": len(body.get("correlations") or []),
            },
            "source": "intelligence_loop",
        }
        deliver_to_subscribers(state, msg)
    except Exception:
        pass
    return JSONResponse({"ok": True, "received_at": datetime.now(timezone.utc).isoformat()})


@router.get("/api/glassbox/sitrep/latest")
async def sitrep_latest(request: Request) -> JSONResponse:
    return JSONResponse(getattr(request.app.state, "latest_sitrep", {}) or {})


# ─── glassbox.html intel-panel stubs ───────────────────────────────────────

@router.get("/api/glassbox/state")
async def glassbox_state() -> JSONResponse:
    """Edge-cache hydration endpoint — glassbox-web.html prefetches this
    BEFORE any other JS runs so the dashboard paints with cached events.
    For now we return an empty events array; the SSE stream populates
    real-time data within seconds. Cache-friendly response."""
    return JSONResponse({
        "ts": datetime.now(timezone.utc).isoformat(),
        "events": [],
        "cache": "miss",
    })


@router.get("/api/glassbox/anomalies/latest")
async def anomalies_latest(request: Request, limit: int = 20) -> JSONResponse:
    state = getattr(request.app.state, "latest_sitrep", {}) or {}
    anomalies = (state.get("anomalies") or [])[:max(1, min(limit, 100))]
    return JSONResponse({
        "generated_at": state.get("generated_at"),
        "count": len(anomalies),
        "anomalies": anomalies,
    })


@router.get("/api/glassbox/correlations/latest")
async def correlations_latest(request: Request, limit: int = 20) -> JSONResponse:
    state = getattr(request.app.state, "latest_sitrep", {}) or {}
    corrs = (state.get("correlations") or [])[:max(1, min(limit, 100))]
    return JSONResponse({
        "generated_at": state.get("generated_at"),
        "count": len(corrs),
        "correlations": corrs,
    })


# ─── Watchlist CRUD (Pro feature — the $29/mo unlock) ──────────────────────

@router.post("/api/glassbox/watchlist")
async def watchlist_create(request: Request):
    try:
        from watchlist import create_watchlist  # type: ignore
        from pro import is_pro  # type: ignore
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"module unavailable: {e}"}, 500)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, 400)
    required = ("email", "label", "layers", "center_lat", "center_lng", "radius_km")
    missing = [k for k in required if body.get(k) in (None, "")]
    if missing:
        return JSONResponse({"ok": False, "error": f"missing: {missing}"}, 400)

    email_norm = str(body.get("email") or "").strip().lower()
    if not is_pro(email_norm):
        try:
            from watchlist import list_watchlists  # type: ignore
            existing = [w for w in list_watchlists(email=email_norm) if w.enabled]
            if len(existing) >= 1:
                return JSONResponse({
                    "ok": False,
                    "error": "free tier allows 1 watchlist. Upgrade to Pro for unlimited.",
                    "upgrade_url": "/glassbox-pro",
                }, 402)
        except Exception:
            pass
    try:
        wl = create_watchlist(
            email=str(body["email"]),
            label=str(body["label"]),
            layers=list(body["layers"]) if isinstance(body["layers"], list) else [str(body["layers"])],
            center_lat=float(body["center_lat"]),
            center_lng=float(body["center_lng"]),
            radius_km=float(body["radius_km"]),
            min_severity=int(body.get("min_severity") or 5),
            slack_webhook=body.get("slack_webhook"),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)
    if not wl:
        return JSONResponse({"ok": False, "error": "watchlist cap reached or save failed"}, 400)
    return JSONResponse({"ok": True, "watchlist": wl.to_dict()}, 201)


@router.get("/api/glassbox/watchlist")
async def watchlist_list(email: Optional[str] = None):
    try:
        from watchlist import list_watchlists  # type: ignore
    except Exception as e:
        return JSONResponse({"error": f"watchlist module unavailable: {e}"}, 500)
    items = list_watchlists(email=email)
    return JSONResponse({"count": len(items), "watchlists": [w.to_dict() for w in items]})


@router.get("/api/glassbox/watchlist/{wl_id}")
async def watchlist_get(wl_id: str):
    """Read a single watchlist by id. Iterates and breaks via next() —
    sub-second on the live ~500-row shape; if the table grows past
    ~10k rows we'll add a proper SELECT-by-id in watchlist.py."""
    try:
        from watchlist import list_watchlists  # type: ignore
    except Exception as e:
        return JSONResponse({"error": f"watchlist module unavailable: {e}"}, 500)
    match = next(
        (w for w in list_watchlists() if str(getattr(w, "id", "")) == wl_id),
        None,
    )
    if match is None:
        return JSONResponse({"ok": False, "error": "not found", "id": wl_id}, 404)
    return JSONResponse({"ok": True, "watchlist": match.to_dict()})


@router.delete("/api/glassbox/watchlist/{wl_id}")
async def watchlist_delete(wl_id: str):
    try:
        from watchlist import delete_watchlist  # type: ignore
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"unavailable: {e}"}, 500)
    ok = delete_watchlist(wl_id)
    return JSONResponse({"ok": bool(ok), "id": wl_id})


# ─── Natural-language query — "Ask Glassbox" ───────────────────────────────

@router.post("/api/glassbox/ask")
async def ask_glassbox(request: Request):
    """Takes a natural-language question. Pulls grounding facts from the
    Brain (glassbox + holding namespaces), synthesizes an answer via
    Ollama, and returns the answer + citations."""
    llm_rate_check(request, scope="ask")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, 400)
    question = (body.get("question") or "").strip()
    if not question or len(question) < 4:
        return JSONResponse({"ok": False, "error": "question required (min 4 chars)"}, 400)
    if len(question) > 500:
        question = question[:500]

    # 1. Recall top-10 relevant facts from Brain.
    hits: List[Dict[str, Any]] = []
    brain = None
    try:
        sys.path.insert(0, str(_EMPIRE_ROOT / "20_HOLDING_BRAIN" / "memory"))
        from brain import Brain  # type: ignore
        brain = Brain()
        for ns in ("glassbox", "holding", "best_bets", "markets"):
            try:
                r = brain.recall(question, namespace=ns, k=3) or []
                for h in r:
                    hits.append({
                        "id": h.get("id"),
                        "namespace": h.get("namespace"),
                        "object": (h.get("object") or "")[:600],
                        "similarity": h.get("similarity"),
                    })
            except Exception:
                continue
        hits.sort(key=lambda x: float(x.get("similarity") or 0), reverse=True)
        hits = hits[:10]
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"brain unavailable: {e}"}, 500)

    # 2. Ollama synthesis.
    ctx = "\n".join([f"- [#{h['id']} · {h['namespace']}] {h['object']}" for h in hits]) or "(no memory hits)"
    system = (
        "You are Glassbox, a live OSINT intelligence assistant. Answer the user's question "
        "using ONLY the memory hits provided. If the hits don't answer the question, say so "
        "honestly and don't invent information. Cite hits as [#ID]. Keep the answer under "
        "180 words, direct, operational."
    )
    user = "## User question\n" + question + "\n\n## Memory hits\n" + ctx + "\n\n## Your answer (≤180 words, cite as [#ID])"
    answer = ""
    try:
        from llm_ollama import generate_text
        answer = await generate_text(
            system=system, prompt=user,
            task="ask",
            temperature=0.2, num_ctx=8192, max_tokens=500,
            timeout_total=120,
        )
    except Exception as e:
        answer = (
            f"(Ollama unavailable — raw memory only: {e})\n\n"
            + "Top hits:\n" + ctx
        )

    # 3. Log query to Brain.
    try:
        if brain is not None:
            brain.log_event(
                namespace="glassbox", kind="ask_query",
                summary=f"Ask: {question[:120]}",
                detail={"question": question, "hits": len(hits), "answer_length": len(answer)},
                severity="info", source="glassbox_server.ask",
            )
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "question": question,
        "answer": answer,
        "citations": hits,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── Forecast + Pro tier endpoints ──────────────────────────────────────────

@router.get("/api/glassbox/forecast/latest")
async def forecast_latest():
    """Return the most recent 48-hr hotspot forecast produced by the
    intel loop. Reads from Brain 'glassbox/forecast' records."""
    try:
        import sqlite3
        sys.path.insert(0, str(_EMPIRE_ROOT / "20_HOLDING_BRAIN" / "memory"))
        from brain import Brain  # type: ignore
        brain = Brain()
        con = sqlite3.connect(str(brain.db_path), timeout=10.0)
        con.row_factory = sqlite3.Row
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        rows = con.execute(
            "SELECT object, created_at FROM facts "
            "WHERE namespace='glassbox' AND predicate='forecast' "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 10",
            (since_iso,),
        ).fetchall()
        con.close()
        hotspots = []
        for r in rows:
            try: hotspots.append(json.loads(r["object"]))
            except Exception: continue
        return JSONResponse({
            "generated_at": rows[0]["created_at"] if rows else None,
            "count": len(hotspots),
            "hotspots": hotspots,
            "window_hours": 48,
        })
    except Exception as e:
        return JSONResponse({"error": f"{e}"}, 500)


@router.get("/api/glassbox/pro-status")
async def pro_status(email: Optional[str] = None):
    """Front-end checks whether an email is on the Pro plan."""
    try:
        from pro import is_pro  # type: ignore
    except Exception:
        return JSONResponse({"is_pro": False, "error": "pro module unavailable"}, 500)
    return JSONResponse({"email": (email or "").strip().lower(), "is_pro": bool(is_pro(email))})


@router.post("/api/glassbox/pro/activate")
async def pro_activate(request: Request):
    if not _check_activation_auth(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, 400)
    email = (body.get("email") or "").strip().lower()
    plan = (body.get("plan") or "pro").strip().lower()
    customer_id = body.get("stripe_customer_id")
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "valid email required"}, 400)
    if plan not in ("pro", "intel", "enterprise"):
        return JSONResponse({"ok": False, "error": "invalid plan"}, 400)
    try:
        from pro import mark_pro  # type: ignore
        ok = mark_pro(email, plan=plan, stripe_customer_id=customer_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{e}"}, 500)
    return JSONResponse({"ok": bool(ok), "email": email, "plan": plan})


@router.post("/api/glassbox/pro/cancel")
async def pro_cancel(request: Request):
    if not _check_activation_auth(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, 401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, 400)
    email = (body.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"ok": False, "error": "email required"}, 400)
    try:
        from pro import cancel_pro  # type: ignore
        ok = cancel_pro(email)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{e}"}, 500)
    return JSONResponse({"ok": bool(ok), "email": email})


# ─── News-manifest + history ────────────────────────────────────────────────

@router.get("/api/glassbox/news-manifest")
async def news_manifest(request: Request, limit: int = 100) -> JSONResponse:
    """Bootstrap news-pin manifest for the Glassbox globe.

    On page load the globe hits this endpoint to get a snapshot of
    geocoded news events it can pin immediately (before the SSE stream
    catches up). Client should switch to the live SSE stream for updates.

    Data source priority:
      1. hot_cache["news"]  — live GDELT events already ingested
      2. gdelt_sentinel_feed.json  — file GDELT ingester wrote last cycle
      3. citizen feed file — social + YouTube events with coords
      4. empty payload
    """
    limit = max(1, min(limit, 500))
    state = request.app.state
    hot_cache = getattr(state, "hot_cache", {})

    # Priority 1: live cache.
    live_events = list(hot_cache.get("news", []))
    if live_events:
        live_events.sort(key=lambda e: e.severity, reverse=True)
        pins = [
            {
                "id": ev.external_id,
                "title": ev.payload.get("title", ""),
                "url": ev.payload.get("url", ""),
                "domain": ev.payload.get("domain", ""),
                "location": ev.payload.get("location_name", ""),
                "lat": ev.lat,
                "lng": ev.lng,
                "severity": ev.severity,
                "article_count": ev.payload.get("article_count", 1),
                "source_country": ev.payload.get("source_country", ""),
                "language": ev.payload.get("language", "English"),
                "agency": ev.payload.get("agency", "sentinel"),
                "ts": ev.ts,
            }
            for ev in live_events[:limit]
        ]
        return JSONResponse({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "live_cache",
            "count": len(pins),
            "pins": pins,
        })

    # Priority 2: sentinel feed file (cold-start fallback).
    sentinel_feed_path = _PKG_ROOT / "data" / "gdelt_sentinel_feed.json"
    if sentinel_feed_path.exists():
        try:
            with open(sentinel_feed_path, "r", encoding="utf-8") as f:
                feed = json.load(f)
            pins = feed.get("events", [])[:limit]
            return JSONResponse({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "sentinel_feed_file",
                "feed_generated_at": feed.get("generated_at", "unknown"),
                "count": len(pins),
                "pins": pins,
            })
        except Exception as e:
            log.warning("news-manifest: sentinel feed read error: %s", e)

    # Priority 3: citizen OSINT feed file.
    if _CITIZEN_FEED_FILE.exists():
        try:
            with open(_CITIZEN_FEED_FILE, "r", encoding="utf-8") as f:
                citizen = json.load(f)
            pins = citizen.get("events", [])[:limit]
            if pins:
                return JSONResponse({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "source": "citizen_feed_file",
                    "feed_generated_at": citizen.get("generated_at", "unknown"),
                    "count": len(pins),
                    "pins": pins,
                })
        except Exception as e:
            log.warning("news-manifest: citizen feed read error: %s", e)

    # Priority 4: nothing yet.
    return JSONResponse({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "empty",
        "count": 0,
        "pins": [],
        "message": "Ingesters have not run yet. Pins will appear after first poll cycle.",
    })


@router.get("/api/glassbox/history/{layer}")
async def glassbox_history(layer: str, hours: int = 24, limit: int = 200):
    """Return recent events for a layer from the Brain archive
    (glassbox namespace). Full bbox + time-range support is in the
    next pass; MVP does time-ordered."""
    hours = max(1, min(720, int(hours)))
    limit = max(1, min(1000, int(limit)))
    try:
        sys.path.insert(0, str(_EMPIRE_ROOT / "20_HOLDING_BRAIN" / "memory"))
        from brain import Brain  # type: ignore
        brain = Brain()
        import sqlite3
        since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        con = sqlite3.connect(str(brain.db_path), timeout=10.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT subject, object, created_at FROM facts "
            "WHERE namespace='glassbox' AND created_at >= ? "
            "AND (subject LIKE ? OR tags LIKE ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (since_iso, f"%{layer}%", f"%{layer}%", limit),
        ).fetchall()
        con.close()
        out = []
        for r in rows:
            obj = r["object"] or ""
            try:
                parsed = json.loads(obj) if obj.startswith("{") else {"raw": obj}
            except Exception:
                parsed = {"raw": obj}
            out.append({
                "subject": r["subject"], "at": r["created_at"], "data": parsed,
            })
        return JSONResponse({
            "layer": layer,
            "hours": hours,
            "count": len(out),
            "events": out,
        })
    except Exception as e:
        return JSONResponse({"error": f"history query failed: {e}"}, 500)


# ─── Server-Sent Events: real-time push to every client ────────────────────

@router.get("/api/glassbox/stream")
async def stream(request: Request):
    state = request.app.state
    subscribers = getattr(state, "subscribers", None)
    subscriber_drops = getattr(state, "subscriber_drops", None)
    hot_cache = getattr(state, "hot_cache", {})
    ingesters = getattr(state, "ingesters", [])

    if subscribers is None or subscriber_drops is None:
        # Daemon state not initialized — degrade to a one-shot empty payload
        # instead of crashing the client. Tests that build a fresh FastAPI()
        # without running startup land here.
        async def _empty():
            yield {"event": "hello", "data": json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "layers": [], "ingesters": [], "note": "daemon state not initialized",
            })}
        return EventSourceResponse(_empty())

    q: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
    subscribers.append(q)
    log.info(f"SSE subscriber connected — total={len(subscribers)}")

    async def event_gen():
        try:
            # Hello envelope — describes server state, no events yet.
            yield {
                "event": "hello",
                "data": json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "layers": list(hot_cache.keys()),
                    "ingesters": [ing.layer for ing in ingesters],
                }),
            }

            # Initial-state snapshot — push the most recent ~50 events
            # from the hot cache so the page populates instantly. Without
            # this the client would sit at "0 EVENTS LIVE" until the
            # next ingester cycle (60+ seconds for some layers).
            try:
                pool: List[GlassboxEvent] = []
                for dq in hot_cache.values():
                    if not dq:
                        continue
                    n = len(dq)
                    take = 50 if n > 50 else n
                    for i in range(n - take, n):
                        pool.append(dq[i])
                pool.sort(key=lambda e: getattr(e, "severity", 0) or 0, reverse=True)
                snap_events = [e.to_dict() for e in pool[:50]]
                yield {
                    "event": "snapshot",
                    "data": json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "count": len(snap_events),
                        "events": snap_events,
                    }),
                }
            except Exception as _snap_err:
                log.warning("SSE snapshot generation failed: %s", _snap_err)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=_SSE_PING_SEC)
                    yield {"event": "event", "data": json.dumps(msg)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            if q in subscribers:
                subscribers.remove(q)
            subscriber_drops.pop(id(q), None)
            log.info(f"SSE subscriber disconnected — total={len(subscribers)}")

    return EventSourceResponse(event_gen())
