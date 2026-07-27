"""
Glassbox Server — the always-on intelligence backbone.

Runs on the Mac Mini. Every Glassbox client (web, desktop, mobile, enterprise
API) connects here instead of hitting third-party APIs directly. This solves
all of V1's pain at once:

  - CORS: gone. Server-side fetching has no browser restrictions.
  - Rate limits: the Mac Mini is the sole caller, not every open tab.
  - Persistence: every event lands in the hot cache + (future) Holding Brain.
  - Real-time push: clients subscribe via SSE, get events as they happen.
  - Historical record: the archive becomes a moat (see V2 architecture doc).

Endpoints:
  GET  /api/health                   — service + per-ingester status
  GET  /api/glassbox/layers          — list all active layers
  GET  /api/glassbox/layer/<name>    — last N events in a layer (default 500)
  GET  /api/glassbox/stream          — Server-Sent Events push feed

Run directly:
    python3 21_GLASSBOX_AI/glassbox_server.py

Or via supervisor (preferred on the Mac Mini):
    supervisor.sh starts a keepalive entry pointing here.

Dependencies (install once):
    pip3 install --break-system-packages fastapi uvicorn sse-starlette aiohttp
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict, deque
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from sse_starlette.sse import EventSourceResponse
import uvicorn

# Make the local ingesters package importable when run as a script
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from ingesters.base import GlassboxEvent            # noqa: E402
from ingesters.planes import PlanesIngester          # noqa: E402

# Phase 1 (2026-05-07): Postgres durable archive + /api/v1 endpoints
from db import init_pools, close_pools                 # noqa: E402
from api_v1 import build_router as build_v1_router   # noqa: E402
from writers import (                                  # noqa: E402
    write_aircraft_events,
    write_vessel_events,
    write_satellite_events,
    write_seismic_events,
    write_emsc_quake_events,
    write_natural_event_events,
    write_news_events,
    write_gdelt_bulk_events,
    write_weather_alert_events,
    write_wildfire_events,
    write_sanction_entities,
    write_space_weather_events,
    write_tropical_storm_events,
    write_gdacs_events,
    write_hn_events,
    write_volcanic_events,
    write_fema_events,
    # Phase 2 round-2 (2026-05-09): cover the remaining 7 ingesters that were
    # broadcasting but not dual-writing.
    write_social_events,
    write_newsdata_events,
    write_donki_events,
    write_metar_events,
    write_aqi_events,
    write_neo_events,
    write_sec_filing_events,
    # P2-A Phase 1 MVP (2026-05-27): cyber-attack data layers.
    write_cisa_kev_events,
    write_spamhaus_drop_events,
    # P2-B Phase 1.5 (live-ingester upgrade for the climate_forecast layer)
    write_open_meteo_forecast_events,
    # P2-B Phase 1.5 (live-ingester upgrade for the noaa_buoys layer)
    write_noaa_ndbc_events,
)
from algorithms.proximity import (                    # noqa: E402
    run_proximity_scan,
    run_cross_entity_proximity_scan,
)
from algorithms.dark_ship import run_dark_ship_scan   # noqa: E402
from algorithms.sanctions_match import run_sanctions_match_scan  # noqa: E402
from algorithms.military_flights import run_military_flights_scan  # noqa: E402
from algorithms.loitering import run_loitering_scan  # noqa: E402
from algorithms.rendezvous import run_rendezvous_scan  # noqa: E402
from algorithms.sanctioned_airspace import run_sanctioned_airspace_scan  # noqa: E402
from algorithms.sanctioned_dark_vessel import run_sanctioned_dark_scan  # noqa: E402
from algorithms.sanctioned_rendezvous import run_sanctioned_rendezvous_scan  # noqa: E402
from algorithms.sanctions_multijurisdictional import run_sanctions_multijurisdictional_scan  # noqa: E402
from algorithms.shadow_fleet_cluster import run_shadow_fleet_cluster_scan  # noqa: E402
from algorithms.port_call import (  # noqa: E402
    run_port_call_scan,
    run_port_arrival_scan,
    run_port_departure_scan,
)
from algorithms.sanctioned_port_arrival import run_sanctioned_port_arrival_scan  # noqa: E402

from brief import generate_brief_cached                # noqa: E402
from ingesters.ships import ShipsIngester            # noqa: E402
from ingesters.earthquakes import EarthquakesIngester  # noqa: E402
from ingesters.satellites import SatellitesIngester  # noqa: E402
from ingesters.gdelt import GDELTIngester            # noqa: E402
from ingesters.gdelt_topical import GDELTTopicalIngester  # noqa: E402
from ingesters.gdelt_bulk.ingester import GdeltBulkIngester  # noqa: E402  Phase 4.A — bulk-CSV path with HANDOFF_02 CAMEO + HANDOFF_03 prefilter
from ingesters.citizen_adapter import (             # noqa: E402
    CitizenOSINTAdapter, TrafficCamsAdapter,
    CITIZEN_FEED_FILE as _CITIZEN_FEED_FILE,
)
from ingesters.police_incidents import PoliceIncidentsIngester  # noqa: E402

# Phase 2 ingesters (shipped 2026-05-04 evening) — all gate-verified
from ingesters.noaa_nws import NoaaNwsIngester        # noqa: E402  US weather alerts (replaces Open-Meteo)
from ingesters.nasa_eonet import NasaEonetIngester    # noqa: E402  natural events (wildfires, storms, volcanoes)
from ingesters.emsc_fdsn import EmscFdsnIngester      # noqa: E402  European seismic catalog (CC BY 4.0)
from ingesters.ofac_sdn import OfacSdnIngester        # noqa: E402  US Treasury sanctions index
from ingesters.uk_ofsi import UkOfsiIngester          # noqa: E402  UK OFSI consolidated list (Crown Copyright, OGL v3.0)
from ingesters.eu_cfsp import EuCfspIngester          # noqa: E402  EU CFSP consolidated list (EU Open Data, free w/ attribution)
from ingesters.usgs_volcano import UsgsVolcanoIngester # noqa: E402  USGS VHP volcanic alerts (US gov public domain)
from ingesters.openfema import OpenFemaIngester        # noqa: E402  FEMA disaster declarations (US gov public domain)
from ingesters.nasa_firms import NasaFirmsIngester    # noqa: E402  wildfire detections (uses NASA_FIRMS_MAP_KEY)
from ingesters.waqi_aqi import WaqiAqiIngester        # noqa: E402  global air quality (uses WAQI_API_TOKEN)
from ingesters.nasa_neo import NasaNeoIngester        # noqa: E402  near-earth asteroids (uses NASA_API_KEY)
from ingesters.nasa_donki import NasaDonkiIngester    # noqa: E402  space weather (uses NASA_API_KEY)
from ingesters.noaa_swpc import NoaaSwpcIngester      # noqa: E402  real-time space-weather alerts (US gov PD)
from ingesters.nhc_storms import NhcStormsIngester    # noqa: E402  Atlantic + East Pacific tropical cyclones (US gov PD)
from ingesters.gdacs import GdacsIngester            # noqa: E402  global disaster alerts (CC BY 4.0)
from ingesters.hacker_news import HackerNewsIngester  # noqa: E402  HN top stories (CC0)
from ingesters.ourairports import OurAirportsIngester    # noqa: E402  global airports (CC0)
from ingesters.noaa_aviation_weather import NoaaAviationWeatherIngester  # noqa: E402  METAR (US public domain)
from ingesters.sec_edgar import SecEdgarIngester      # noqa: E402  SEC filings (US public domain)
from ingesters.bluesky_jetstream import BlueskyJetstreamIngester  # noqa: E402  ATProto social firehose
from ingesters.aisstream import AISStreamIngester  # noqa: E402  global vessel firehose (commercial-OK with key)
from ingesters.newsdata_io import NewsDataIoIngester  # noqa: E402  geocoded global news (GDELT replacement for v1.0)
# P2-A Phase 1 MVP (2026-05-27): cyber-attack data layers.
from ingesters.cisa_kev import CisaKevIngester        # noqa: E402  CISA Known Exploited Vulnerabilities (CC0)
from ingesters.spamhaus_drop import SpamhausDropIngester  # noqa: E402  Spamhaus DROP/EDROP (free + redistributable)
# P2-B Phase 1.5 (live-ingester upgrade for the climate_forecast static layer)
from ingesters.open_meteo_forecast import OpenMeteoForecastIngester  # noqa: E402  Open-Meteo (CC-BY 4.0, commercial OK)
# P2-B Phase 1.5 (live-ingester upgrade for the noaa_buoys static layer)
from ingesters.noaa_ndbc import NoaaNdbcIngester  # noqa: E402  NOAA NDBC (US gov public domain)

# Sources Registry — the structural license gate (Operating Rule 13).
# Refuses to start any ingester whose source_id is missing from
# infra/sources.yaml, disabled there, or non-commercial in v1.0.
from sources_registry import (  # noqa: E402
    SourcesRegistry, gate_ingester, registry_summary,
)

# Initialize the module-level logger BEFORE the optional-import blocks below
# (Loop init, self-heal, etc.) so any of them can call log.warning on import
# failures. Previously `log` was defined ~120 lines later, causing a
# NameError when an optional import failed -- which hid the real error.
# (2026-04-27 fix.)
log = logging.getLogger("glassbox-server")

# Self-heal feedback loop import moved to web/routes/briefings.py
# (P3-H extraction #11 — sole consumer was the /api/issues/* routes).

# ─── The Loop integration (Step 6) — optional, gated by env var ────────────
# When GLASSBOX_LOOP_ENABLED=1, the server:
#   - classifies each event via core.event_classifier (Ollama-backed)
#   - routes classified events through core.the_loop.LoopBridge → EdgeAlerts
#   - publishes aggregated state to the Worker /api/glassbox/state via
#     glassbox_publisher.GlassboxPublisher
# Default: OFF. Operator flips it on once the Mac Mini env vars are set.
_LOOP_ENABLED = os.environ.get("GLASSBOX_LOOP_ENABLED", "0") == "1"
_LOOP_CLASSIFIER = None
_LOOP_BRIDGE = None
_LOOP_PUBLISHER = None
_LOOP_REGISTRY = None  # 2026-04-27: was undefined when Loop init failed mid-way,
                       # crashing _startup with NameError. Default to None at
                       # module load so the hasattr check in _startup is safe.
_LOOP_IMPORT_ERROR: Optional[str] = None
if _LOOP_ENABLED:
    try:
        _MEWR_OS_PATH = ROOT.parent / "29_MEWR_OS"
        if str(_MEWR_OS_PATH) not in sys.path:
            sys.path.insert(0, str(_MEWR_OS_PATH))
        from core.event_classifier import EventClassifier  # noqa: E402
        from core.the_loop import LoopBridge, InMemoryRegistry  # noqa: E402
        from core.cowork_twin import OllamaClient  # noqa: E402
        from glassbox_publisher import GlassboxPublisher  # noqa: E402

        # Classifier: heuristic + Ollama for gdelt/news. Reuses the same
        # Ollama client that Cowork Twin uses (qwen2.5:14b).
        _LOOP_CLASSIFIER = EventClassifier(ollama=OllamaClient())

        # Market registry. Two modes:
        #   GLASSBOX_REGISTRY=live  → LiveMarketRegistry (Kalshi + Polymarket adapters,
        #                              60s refresh, in-memory tag-matching index)
        #   else / live unavailable → InMemoryRegistry (operator-seeded JSON)
        _registry_mode = os.environ.get("GLASSBOX_REGISTRY", "live").lower()
        _LOOP_REGISTRY = None
        if _registry_mode == "live":
            try:
                _MARKETS_PATH = ROOT.parent / "23_FULCRUM_MARKETS"
                if str(_MARKETS_PATH) not in sys.path:
                    sys.path.insert(0, str(_MARKETS_PATH))
                from registry import LiveMarketRegistry  # noqa: E402
                from sources.kalshi import KalshiSource  # noqa: E402
                from sources.polymarket import PolymarketSource  # noqa: E402
                _LOOP_REGISTRY = LiveMarketRegistry(
                    sources=[KalshiSource(), PolymarketSource()],
                )
                log.info("LiveMarketRegistry initialized (Kalshi + Polymarket); first refresh on background task")
            except Exception as e:
                log.warning(f"LiveMarketRegistry unavailable ({e}) — falling back to InMemoryRegistry")
                _LOOP_REGISTRY = None

        if _LOOP_REGISTRY is None:
            _LOOP_REGISTRY = InMemoryRegistry()
            # Optional: load persisted registry if the file exists
            _registry_path = ROOT / "loop_market_registry.json"
            if _registry_path.exists():
                try:
                    _registry_data = json.loads(_registry_path.read_text())
                    for tag, markets in (_registry_data.get("by_tag") or {}).items():
                        for m in markets:
                            _LOOP_REGISTRY.add_market(tag, m)
                    log.info(f"Loaded {_LOOP_REGISTRY.size()} markets from {_registry_path.name}")
                except Exception as e:
                    log.warning(f"Failed to load market registry: {e}")

        # Publisher: pushes aggregated state to Worker every 30s. Required
        # env vars; if missing, skip publisher but keep classifier+bridge.
        _publisher_url = os.environ.get("GLASSBOX_PUBLISHER_URL")
        _publisher_token = os.environ.get("NEWS_API_TOKEN")
        if _publisher_url and _publisher_token:
            _LOOP_PUBLISHER = GlassboxPublisher(
                worker_url=_publisher_url,
                api_token=_publisher_token,
            )
            _LOOP_PUBLISHER.start_flusher_thread()

        # Bridge: routes alerts to publisher (and any other subscribers)
        _alert_subs = []
        if _LOOP_PUBLISHER is not None:
            _alert_subs.append(_LOOP_PUBLISHER.on_alert)

        # Loop subscriber → PaperBroker (Item #100, 2026-04-26).
        # Closes intelligence-to-position end-to-end. Opt-in via env var
        # PREDIQT_AUTO_SIZE_ON_LOOP=1 because (a) it has heavier deps
        # (Prediqt + 23_FULCRUM_MARKETS imports) and (b) operator may want
        # alerts to flow only through publisher/Slack initially before
        # turning on auto-paper-trading.
        if os.environ.get("PREDIQT_AUTO_SIZE_ON_LOOP", "0") == "1":
            try:
                _PREDIQT_PATH = ROOT.parent / "30_PREDIQT"
                if str(_PREDIQT_PATH) not in sys.path:
                    sys.path.insert(0, str(_PREDIQT_PATH))
                _MARKETS_PATH2 = ROOT.parent / "23_FULCRUM_MARKETS"
                if str(_MARKETS_PATH2) not in sys.path:
                    sys.path.insert(0, str(_MARKETS_PATH2))
                from agents.loop_subscriber import LoopAlertSubscriber  # noqa: E402
                from paper_broker import PaperBroker  # noqa: E402
                _LOOP_SUBSCRIBER = LoopAlertSubscriber(
                    broker=PaperBroker(),
                    min_severity=int(os.environ.get("PREDIQT_LOOP_MIN_SEV", "7")),
                    min_liquidity_usd=float(os.environ.get("PREDIQT_LOOP_MIN_LIQ", "500")),
                )
                _alert_subs.append(_LOOP_SUBSCRIBER.handle)
                # Expose on app.state so /api/loop/health can fetch its stats
                app.state._loop_subscriber = _LOOP_SUBSCRIBER
                log.info(f"LoopAlertSubscriber wired to PaperBroker — "
                         f"min_severity={_LOOP_SUBSCRIBER.min_severity}, "
                         f"min_liquidity_usd={_LOOP_SUBSCRIBER.min_liquidity_usd}")
            except Exception as _se:
                log.warning(f"LoopAlertSubscriber failed to wire: {_se} — "
                            f"alerts still flow to publisher, just won't auto-size positions")

        if not _alert_subs:
            _alert_subs.append(lambda a: log.info(f"EdgeAlert (no subscribers): {a.matched_tag} → {a.market.get('id')}"))
        # 2026-04-27 task #174: wire contract sentiment cache into alert
        # enrichment. When the orchestrator has cached social signal for the
        # market a fired alert points at, the alert payload gets a compact
        # sentiment dict in `context['sentiment']` — which automatically
        # propagates to Slack/archive/Glassbox Predictions/Prediqt sizer.
        # Defensive: cache miss / file missing never blocks dispatch.
        # ROOT = 21_GLASSBOX_AI/ → ROOT.parent = empire root.
        _EMPIRE_ROOT = ROOT.parent
        _SENTIMENT_STATE_PATH = os.environ.get(
            "LOOP_SENTIMENT_STATE_PATH",
            str(_EMPIRE_ROOT / "29_MEWR_OS" / "data" / "contract_sentiment_state.json"),
        )
        _LOOP_BRIDGE = LoopBridge(
            registry=_LOOP_REGISTRY,
            on_alert=_alert_subs,
            min_severity=int(os.environ.get("GLASSBOX_LOOP_MIN_SEVERITY", "5")),
            dedup_ttl_sec=300,
            sentiment_state_path=_SENTIMENT_STATE_PATH,
        )

        log.info(f"The Loop ENABLED — classifier={_LOOP_CLASSIFIER is not None}, "
                 f"bridge={_LOOP_BRIDGE is not None}, "
                 f"publisher={_LOOP_PUBLISHER is not None}")
    except Exception as _loop_exc:
        _LOOP_IMPORT_ERROR = f"{type(_loop_exc).__name__}: {_loop_exc}"
        log.warning(f"The Loop FAILED to initialize ({_LOOP_IMPORT_ERROR}) — server will run without Loop")
        _LOOP_CLASSIFIER = None
        _LOOP_BRIDGE = None
        _LOOP_PUBLISHER = None
        _LOOP_REGISTRY = None   # 2026-04-27: keep parity with module-level defaults


# ─── Config ────────────────────────────────────────────────────────────────

APP_HOST = os.environ.get("GLASSBOX_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("GLASSBOX_PORT", "8790"))
HOT_CACHE_PER_LAYER = int(os.environ.get("GLASSBOX_CACHE_SIZE", "5000"))
SUBSCRIBER_QUEUE_SIZE = 1000
SSE_PING_SEC = 20.0

# Phase 6 (2026-05-09): GLASSBOX_LOG_FORMAT=json switches the root logger
# to a JSON formatter so log aggregators can index fields rather than parse
# free text. Default stays 'text' so the existing tail/grep workflow keeps
# working. See log_format.py.
from log_format import configure_logging  # noqa: E402
configure_logging(level=logging.INFO)
# Note: `log` was already initialized above (before the optional-import blocks)
# so configure_logging() above re-routes the existing logger's output.


# ─── App + shared state ────────────────────────────────────────────────────

app = FastAPI(
    title="Glassbox API",
    version="2.0.0",
    description=(
        "**Algorithm-derived OSINT intelligence** — sanctioned vessels going "
        "dark, restricted-airspace incursions, shadow-fleet clusters, "
        "rendezvous events. Updated continuously from 26+ public sources "
        "(AIS, ADS-B, OFAC SDN + EU CFSP + UK OFSI, NASA FIRMS, USGS, GDELT). "
        "\n\n"
        "**Public consumer surfaces:** [/](/) (landing), "
        "[/signals](/signals) (live dashboard), "
        "[/signals.rss](/signals.rss) (RSS), "
        "[/signals.json](/signals.json) (JSON Feed v1.1), "
        "[/api/v1/signals/snapshot.csv](/api/v1/signals/snapshot.csv) "
        "(spreadsheet export), [/signals/embed](/signals/embed) (iframe widget), "
        "[/status](/status) (operational health), "
        "/entity/{uuid} (per-entity profile)."
        "\n\n"
        "**Subscribe:** POST /api/v1/signals/subscribe with `{email, "
        "severity_floor, category_ids}` to receive the daily digest."
        "\n\n"
        "All endpoints are public + read-only in v1.0. Sanctions matches "
        "are automated (not legal allegations); see /api/sources for the "
        "full source registry."
    ),
    contact={
        "name": "MEWR Creative Enterprises",
        "url": "https://mewrcreate.com",
    },
)

# Phase 1: mount the /api/v1/* router (viewport, entity detail, db health)
app.include_router(build_v1_router())
# Glassbox-branded alias for the same routes — `/api/intel/*` is the
# distinct Glassbox name; `/api/v1/*` is the industry-standard REST
# convention every major API uses (Stripe, GitHub, AWS, Twilio, ...).
# Both prefixes resolve to identical handlers so consumers can pick.
app.include_router(build_v1_router(prefix="/api/intel", tag="intel"))

# P3-H extractions — per-concern routers split out of this file.
from web.routes.network import router as network_router  # noqa: E402
from web.routes.signals import router as signals_router  # noqa: E402
from web.routes.monitor import router as monitor_router  # noqa: E402
from web.routes.globe import router as globe_router  # noqa: E402
from web.routes.static import router as static_router  # noqa: E402
from web.routes.pages import router as pages_router  # noqa: E402
from web.routes.admin import router as admin_router  # noqa: E402
from web.routes.infrastructure import router as infrastructure_router  # noqa: E402
from web.routes.briefings import router as briefings_router  # noqa: E402
from web.routes.misc import router as misc_router  # noqa: E402
from web.routes.intel import router as intel_router  # noqa: E402
from web.routes.api_glassbox import router as api_glassbox_router  # noqa: E402
app.include_router(network_router)
app.include_router(signals_router)
app.include_router(monitor_router)
app.include_router(globe_router)
app.include_router(static_router)
app.include_router(pages_router)
app.include_router(admin_router)
app.include_router(infrastructure_router)
app.include_router(briefings_router)
app.include_router(misc_router)
app.include_router(intel_router)
app.include_router(api_glassbox_router)

app.add_middleware(
    CORSMiddleware,
    # Locked down for public launch. Wildcard reverted to a known list:
    #  - production hosts: mewrcreate.com + www variant
    #    (NOTE: `glassbox.fulcrumtechnologies.io` was mentioned in an old
    #    comment here but is NOT in this list — Fulcrum + Glassbox live
    #    on mewrcreate.com only as of 2026-05-27; add via
    #    GLASSBOX_CORS_EXTRA_ORIGINS if a fulcrum subdomain ever needs CORS)
    #  - localhost variants for local dev
    #  - regex catches preview deploys at *.mewrcreate.pages.dev
    # GLASSBOX_CORS_EXTRA_ORIGINS env var lets ops add more without redeploy.
    allow_origins=[
        "https://mewrcreate.com",
        "https://www.mewrcreate.com",
        "http://127.0.0.1:8790",
        "http://localhost:8790",
        *[o.strip() for o in os.environ.get("GLASSBOX_CORS_EXTRA_ORIGINS", "").split(",") if o.strip()],
    ],
    allow_origin_regex=r"https://[a-z0-9-]+\.mewrcreate\.pages\.dev",
    allow_credentials=False,
    # POST needed for /ask, /sitrep/publish, /watchlist/create, /pro/activate, etc.
    # OPTIONS needed for CORS preflight on any non-simple request (Content-Type: application/json).
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    # Cache preflight for 10 min so every Ask doesn't pay a round-trip.
    max_age=600,
)

# Hot in-memory cache: layer name → rolling deque of recent events
_hot_cache: Dict[str, Deque[GlassboxEvent]] = defaultdict(
    lambda: deque(maxlen=HOT_CACHE_PER_LAYER)
)

# ─── Layer-name aliasing ────────────────────────────────────────────────────
# Frontend (Implementer A) expects either spelling — we store events under
# both keys so /api/glassbox/entities + /api/glassbox/layer/{name} resolve no
# matter which name the client uses. Bidirectional map: every alias entry has
# an inverse so a single membership check covers both directions.
_LAYER_ALIASES: Dict[str, str] = {
    "news": "gdelt",
    "gdelt": "news",
    "policeIncidents": "police_incidents",
    "police_incidents": "policeIncidents",
    "citizenOsint": "citizen_osint",
    "citizen_osint": "citizenOsint",
}

# Per-(layer, bbox-quantized) viewport response cache. Invalidated when
# _broadcast() touches a layer. 1s TTL bounds staleness even if invalidation
# is missed for some reason.
_ENTITIES_CACHE_TTL_SEC = 1.0
_ENTITIES_BBOX_QUANTUM = 0.5  # degrees — adjacent map moves snap to same key
_entities_cache: Dict[tuple, tuple] = {}  # key -> (expires_at, payload_dict)

# Active SSE subscriber queues
_subscribers: List[asyncio.Queue] = []

# Per-subscriber consecutive-drop counter. Keyed by id(queue). A slow consumer
# that fails to keep up for too many messages in a row gets evicted so the
# ingester doesn't silently lose data forever (old behavior: pass on QueueFull).
_subscriber_drops: Dict[int, int] = {}
_BROADCAST_DROP_LIMIT = 50        # consecutive drops before eviction
_BROADCAST_DROP_LOG_EVERY = 10    # log every Nth drop per subscriber

# Registered ingesters (populated at startup)
_ingesters: List[Any] = []

_started_at = datetime.now(timezone.utc).isoformat()

# Last completed scan-cycle stats — published per cycle for operator
# visibility via /api/v1/recent-cycle. Empty until the first cycle
# completes; see _proximity_scan_loop for the population path.
_RECENT_SCAN_CYCLE: Dict[str, Any] = {
    "completed_at": None,
    "duration_ms": None,
    "totals": {
        "entity_event": 0,
        "entity_entity": 0,
        "dark_ship": 0,
        "sanctions_match": 0,
        "military_flights": 0,
        "loitering": 0,
        "rendezvous": 0,
        "sanctioned_airspace": 0,
        "sanctioned_dark": 0,
        "sanctioned_rendezvous": 0,
        "multijurisdictional": 0,
        "shadow_fleet_cluster": 0,
        "port_call": 0,
        "port_arrival": 0,
        "port_departure": 0,
        "sanctioned_port_arrival": 0,
    },
    "total": 0,
    "errors": [],   # list of {algorithm, error_type, message}
}

# Latest AI intelligence report (published by intelligence_loop.py)
_latest_sitrep: Dict[str, Any] = {
    "generated_at": None,
    "sitrep": {"headline": "Waiting for first intelligence cycle...",
               "brief": "The intel loop runs every 5 minutes. First SITREP lands shortly after server start.",
               "priorities": [], "confidence": 0.0},
    "anomalies": [], "correlations": [],
    "layer_counts": {}, "total_events": 0,
}


def _broadcast(event_or_events) -> None:
    """Called by each ingester when it has new/changed events.

    Polymorphic: accepts a single GlassboxEvent OR a list. Batch path iterates
    _subscribers ONCE per ingester cycle instead of N times — 50-100× throughput
    on broadcast when an ingester reports many changes at once (review finding).

    Previously: silently swallowed QueueFull, which meant a wedged browser tab
    could cause permanent data loss. Now: track consecutive drops per subscriber,
    log periodically, evict at _BROADCAST_DROP_LIMIT.
    """
    if isinstance(event_or_events, (list, tuple)):
        events = event_or_events
    else:
        events = [event_or_events]
    if not events:
        return

    # ─── The Loop tee (Step 6) ────────────────────────────────────────
    # Events are already classified by Ingester.cycle() at this point
    # (classifier ran between dedup and broadcast). Now route them through
    # the bridge (→ EdgeAlerts) and publisher (→ Worker KV mirror).
    # Both wrapped in defensive try/except so a bug in the Loop NEVER
    # breaks the SSE pipeline. The Loop is opt-in extra value.
    if _LOOP_BRIDGE is not None:
        try:
            _LOOP_BRIDGE.process_batch(events)
        except Exception as _e:
            log.warning(f"loop bridge failed (continuing broadcast): {_e}")
    if _LOOP_PUBLISHER is not None:
        try:
            _LOOP_PUBLISHER.on_event_batch(events)
        except Exception as _e:
            log.warning(f"loop publisher failed (continuing broadcast): {_e}")

    # 1. Update hot cache + materialize messages once
    #    Also mirror under any registered alias so /entities + /layer/{name}
    #    resolve regardless of which spelling the client uses (news/gdelt,
    #    policeIncidents/police_incidents, citizenOsint/citizen_osint).
    msgs: List[Dict[str, Any]] = []
    touched_layers: set = set()
    for ev in events:
        _hot_cache[ev.layer].append(ev)
        touched_layers.add(ev.layer)
        alias = _LAYER_ALIASES.get(ev.layer)
        if alias:
            _hot_cache[alias].append(ev)
            touched_layers.add(alias)
        msgs.append(ev.to_dict())

    # Invalidate any cached viewport responses for layers we just touched.
    # Entries with no layer filter (key[0] == "") are also invalidated since
    # they aggregate all layers.
    if _entities_cache:
        stale_keys = [
            k for k in _entities_cache
            if k[0] == "" or k[0] in touched_layers
        ]
        for k in stale_keys:
            _entities_cache.pop(k, None)

    # 2. Iterate subscribers ONCE; push all msgs to each
    evicted: List[asyncio.Queue] = []
    for q in list(_subscribers):
        qid = id(q)
        for msg in msgs:
            try:
                q.put_nowait(msg)
                if qid in _subscriber_drops:
                    _subscriber_drops.pop(qid, None)
            except asyncio.QueueFull:
                drops = _subscriber_drops.get(qid, 0) + 1
                _subscriber_drops[qid] = drops
                if drops == 1 or drops % _BROADCAST_DROP_LOG_EVERY == 0:
                    log.warning(
                        "SSE subscriber slow: %d consecutive drops (qsize=%d, limit=%d)",
                        drops, q.qsize(), _BROADCAST_DROP_LIMIT,
                    )
                if drops >= _BROADCAST_DROP_LIMIT:
                    evicted.append(q)
                    break  # don't keep hammering an evicted queue
    for q in evicted:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass
        _subscriber_drops.pop(id(q), None)
        log.warning(
            "SSE subscriber evicted after %d consecutive drops",
            _BROADCAST_DROP_LIMIT,
        )


# _deliver_to_subscribers fully migrated to web/_broadcast.py — its sole
# in-file caller (/sitrep/publish) moved out in P3-H extraction #9.
# Helper is still importable from web._broadcast for any future caller.


# ─── Startup: spin up every ingester ───────────────────────────────────────

@app.on_event("startup")
async def _startup() -> None:
    # Phase 1 (2026-05-07): bring up the Postgres pool BEFORE constructing
    # ingesters that may dual-write. If the DB is down at startup we log a
    # warning and continue — the durable archive is best-effort, the live
    # broadcast pipeline must still work.
    try:
        await init_pools()
        log.info("[db] Postgres pool initialized — durable archive enabled")
        # First-party analytics — auto-create on first boot.
        from db import acquire as _acquire
        async with _acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS analytics_event ("
                "  id BIGSERIAL PRIMARY KEY,"
                "  ts TIMESTAMPTZ NOT NULL DEFAULT now(),"
                "  event_type TEXT NOT NULL,"
                "  path TEXT NOT NULL,"
                "  source TEXT NOT NULL DEFAULT '',"
                "  referrer TEXT NOT NULL DEFAULT '',"
                "  meta JSONB NOT NULL DEFAULT '{}'::jsonb,"
                "  ip_hash TEXT NOT NULL,"
                "  user_agent TEXT NOT NULL DEFAULT '',"
                "  country TEXT NOT NULL DEFAULT ''"
                ");"
                "CREATE INDEX IF NOT EXISTS analytics_event_ts_idx ON analytics_event (ts DESC);"
                "CREATE INDEX IF NOT EXISTS analytics_event_path_ts_idx ON analytics_event (path, ts DESC);"
                "CREATE INDEX IF NOT EXISTS analytics_event_type_ts_idx ON analytics_event (event_type, ts DESC);"
            )
        log.info("[analytics] analytics_event table ready")

        # event.created_at lacks a btree index — the hypertable partitions
        # by event_time, not created_at, so any WHERE created_at >= NOW()-X
        # filter forced a parallel seq scan across every chunk (16GB+ on
        # the largest). BRIN is ideal here: created_at is monotonic per
        # chunk, so each range-summary holds a tight min/max and the
        # planner can skip whole chunks. 24KB total at 13M rows;
        # pages_per_range=32 gives a good time-range pruning granularity.
        async with _acquire() as conn:
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS event_created_at_brin_idx "
                "ON event USING BRIN (created_at) "
                "WITH (pages_per_range = 32);"
            )
        log.info("[db] event_created_at_brin_idx ready")
    except Exception as e:
        log.warning(f"[db] init_pool failed at startup ({type(e).__name__}: {e}); "
                    f"continuing without durable archive")

    # Instantiate each ingester. Adding a new layer = one line here.
    # The Loop integration: pass `classifier=_LOOP_CLASSIFIER` so each
    # ingester runs the 5-dim classification after dedup. None = no-op.
    # Phase 1: planes.py opts into dual-write via the db_writer hook.
    _clf = _LOOP_CLASSIFIER  # local alias for readability
    candidate_ingesters = [
        PlanesIngester(broadcaster=_broadcast, classifier=_clf,
                       db_writer=write_aircraft_events,
                       logger=logging.getLogger("ingester.planes")),
        ShipsIngester(broadcaster=_broadcast, classifier=_clf,
                      db_writer=write_vessel_events,
                      logger=logging.getLogger("ingester.ships")),
        # 2026-05-09: AISStream global firehose. Same write path as
        # ships.py (write_vessel_events keyed by MMSI) so the two
        # cleanly merge into one vessel index. Refused at gate when
        # AISSTREAM_API_KEY is unset (the ingester body skips its own
        # cycles too — defense in depth).
        AISStreamIngester(broadcaster=_broadcast, classifier=_clf,
                          db_writer=write_vessel_events,
                          logger=logging.getLogger("ingester.aisstream")),
        EarthquakesIngester(broadcaster=_broadcast, classifier=_clf,
                            db_writer=write_seismic_events,
                            logger=logging.getLogger("ingester.earthquakes")),
        SatellitesIngester(broadcaster=_broadcast, classifier=_clf,
                           db_writer=write_satellite_events,
                           logger=logging.getLogger("ingester.satellites")),
        GDELTIngester(broadcaster=_broadcast, classifier=_clf,
                      logger=logging.getLogger("ingester.gdelt")),
        # 2026-05-03: Replaces ~12 direct-browser-side GDELT topical fetches
        # (terrorism, oil_spills, drugs, mining, deforestation, border_crisis,
        # famine, etc.). One server-side ingester running on a 15-min cadence
        # vs. 12 fetches per page load per visitor. See GLASSBOX_V2_MIGRATION_PLAN.md.
        GDELTTopicalIngester(broadcaster=_broadcast, classifier=_clf,
                             db_writer=write_news_events,
                             logger=logging.getLogger("ingester.gdelt_topical")),
        # 2026-05-10 (Phase 4.A): GDELT bulk-CSV path. Replaces the disabled
        # gdelt + gdelt_topical /api/v2/doc/doc rate-limit-banned ingesters
        # via the data.gdeltproject.org/gdeltv2/lastupdate.txt endpoint.
        # Polled events route through HANDOFF_02 CAMEO lookup +
        # HANDOFF_03 prefilter chain before broadcast + dual-write.
        GdeltBulkIngester(broadcaster=_broadcast, classifier=_clf,
                          db_writer=write_gdelt_bulk_events,
                          logger=logging.getLogger("ingester.gdelt_bulk")),
        CitizenOSINTAdapter(broadcaster=_broadcast, classifier=_clf,
                            logger=logging.getLogger("ingester.citizen_osint")),
        TrafficCamsAdapter(broadcaster=_broadcast, classifier=_clf,
                           logger=logging.getLogger("ingester.traffic_cams")),
        PoliceIncidentsIngester(broadcaster=_broadcast, classifier=_clf,
                                logger=logging.getLogger("ingester.police_incidents")),
        # ─── Phase 2 ingesters (2026-05-04) ──────────────────────────
        NoaaNwsIngester(broadcaster=_broadcast, classifier=_clf,
                        db_writer=write_weather_alert_events,
                        logger=logging.getLogger("ingester.noaa_nws")),
        NasaEonetIngester(broadcaster=_broadcast, classifier=_clf,
                          db_writer=write_natural_event_events,
                          logger=logging.getLogger("ingester.nasa_eonet")),
        EmscFdsnIngester(broadcaster=_broadcast, classifier=_clf,
                         db_writer=write_emsc_quake_events,
                         logger=logging.getLogger("ingester.emsc_fdsn")),
        OfacSdnIngester(broadcaster=_broadcast, classifier=_clf,
                        db_writer=write_sanction_entities,
                        logger=logging.getLogger("ingester.ofac_sdn")),
        UkOfsiIngester(broadcaster=_broadcast, classifier=_clf,
                       db_writer=write_sanction_entities,
                       logger=logging.getLogger("ingester.uk_ofsi")),
        EuCfspIngester(broadcaster=_broadcast, classifier=_clf,
                       db_writer=write_sanction_entities,
                       logger=logging.getLogger("ingester.eu_cfsp")),
        # ─── Backend versions of frontend keyed APIs ─────────────────
        # (these replace direct browser calls in glassbox.html — Mac Mini becomes
        # the sole caller, keys live in env vars not embedded in JS shipped to clients)
        NasaFirmsIngester(broadcaster=_broadcast, classifier=_clf,
                          db_writer=write_wildfire_events,
                          logger=logging.getLogger("ingester.nasa_firms")),
        WaqiAqiIngester(broadcaster=_broadcast, classifier=_clf,
                        db_writer=write_aqi_events,
                        logger=logging.getLogger("ingester.waqi_aqi")),
        NasaNeoIngester(broadcaster=_broadcast, classifier=_clf,
                        db_writer=write_neo_events,
                        logger=logging.getLogger("ingester.nasa_neo")),
        NasaDonkiIngester(broadcaster=_broadcast, classifier=_clf,
                         db_writer=write_donki_events,
                         logger=logging.getLogger("ingester.nasa_donki")),
        NoaaSwpcIngester(broadcaster=_broadcast, classifier=_clf,
                        db_writer=write_space_weather_events,
                        logger=logging.getLogger("ingester.noaa_swpc")),
        NhcStormsIngester(broadcaster=_broadcast, classifier=_clf,
                         db_writer=write_tropical_storm_events,
                         logger=logging.getLogger("ingester.nhc_storms")),
        UsgsVolcanoIngester(broadcaster=_broadcast, classifier=_clf,
                           db_writer=write_volcanic_events,
                           logger=logging.getLogger("ingester.usgs_volcano")),
        OpenFemaIngester(broadcaster=_broadcast, classifier=_clf,
                        db_writer=write_fema_events,
                        logger=logging.getLogger("ingester.openfema")),
        GdacsIngester(broadcaster=_broadcast, classifier=_clf,
                     db_writer=write_gdacs_events,
                     logger=logging.getLogger("ingester.gdacs")),
        HackerNewsIngester(broadcaster=_broadcast, classifier=_clf,
                          db_writer=write_hn_events,
                          logger=logging.getLogger("ingester.hacker_news")),
        OurAirportsIngester(broadcaster=_broadcast, classifier=_clf,
                            logger=logging.getLogger("ingester.ourairports")),
        NoaaAviationWeatherIngester(broadcaster=_broadcast, classifier=_clf,
                                    db_writer=write_metar_events,
                                    logger=logging.getLogger("ingester.noaa_aviation_weather")),
        SecEdgarIngester(broadcaster=_broadcast, classifier=_clf,
                         db_writer=write_sec_filing_events,
                         logger=logging.getLogger("ingester.sec_edgar")),
        BlueskyJetstreamIngester(broadcaster=_broadcast, classifier=_clf,
                                 db_writer=write_social_events,
                                 logger=logging.getLogger("ingester.bluesky_jetstream")),
        # 2026-05-05: NewsData.io replaces GDELT for v1.0 news layer
        NewsDataIoIngester(broadcaster=_broadcast, classifier=_clf,
                           db_writer=write_newsdata_events,
                           logger=logging.getLogger("ingester.newsdata_io")),
        # P2-A Phase 1 MVP (2026-05-27): cyber-attack data layers.
        CisaKevIngester(broadcaster=_broadcast, classifier=_clf,
                        db_writer=write_cisa_kev_events,
                        logger=logging.getLogger("ingester.cisa_kev")),
        SpamhausDropIngester(broadcaster=_broadcast, classifier=_clf,
                             db_writer=write_spamhaus_drop_events,
                             logger=logging.getLogger("ingester.spamhaus_drop")),
        # P2-B Phase 1.5 (live-ingester upgrade for the climate_forecast layer)
        OpenMeteoForecastIngester(broadcaster=_broadcast, classifier=_clf,
                                  db_writer=write_open_meteo_forecast_events,
                                  logger=logging.getLogger("ingester.open_meteo_forecast")),
        # P2-B Phase 1.5 (live-ingester upgrade for the noaa_buoys layer)
        NoaaNdbcIngester(broadcaster=_broadcast, classifier=_clf,
                         db_writer=write_noaa_ndbc_events,
                         logger=logging.getLogger("ingester.noaa_ndbc")),
        # PowerIngester(...),
    ]

    # ─── Sources Registry gate (Operating Rule 13) ─────────────────────
    # Load infra/sources.yaml ONCE here, then refuse any ingester whose
    # source_id is missing/disabled/non-commercial. Refused ingesters are
    # dropped — never silently activated. See sources_registry.py.
    registry = SourcesRegistry.load()
    # Stash on the app so /api/sources can render it for Mission Control.
    app.state.sources_registry = registry
    if not registry.loaded_ok:
        log.error(f"[gate] sources.yaml DID NOT LOAD: {registry.load_error}")
        log.error("[gate] All ingesters refused as a fail-safe. Fix sources.yaml and restart.")

    activated = 0
    refused = 0
    for ing in candidate_ingesters:
        ok, reason = gate_ingester(ing, registry)
        if not ok:
            refused += 1
            log.warning(
                f"[gate] REFUSED {ing.__class__.__name__} "
                f"(source_id={getattr(ing, 'source_id', '')!r}): {reason}"
            )
            continue
        _ingesters.append(ing)
        asyncio.create_task(ing.run_forever())
        activated += 1
    log.info(
        f"[gate] sources.yaml loaded ok={registry.loaded_ok} "
        f"enabled_in_yaml={registry.enabled_count() if registry.loaded_ok else 0} "
        f"ingesters_activated={activated} ingesters_refused={refused}"
    )

    # If The Loop's registry is Live (Kalshi/Polymarket), spin up its refresh loop.
    # InMemoryRegistry doesn't need this — it's static.
    if _LOOP_REGISTRY is not None and hasattr(_LOOP_REGISTRY, "run_forever"):
        try:
            _refresh_interval = float(os.environ.get("GLASSBOX_REGISTRY_REFRESH_SEC", "60"))
            asyncio.create_task(_LOOP_REGISTRY.run_forever(refresh_interval_sec=_refresh_interval))
            log.info(f"LiveMarketRegistry refresh loop started — interval={_refresh_interval}s")
        except Exception as e:
            log.warning(f"failed to start registry refresh loop: {e}")

    log.info(f"Glassbox Server started — {activated} ingester(s) active "
             f"(of {len(candidate_ingesters)} candidates; {refused} refused by license gate)")

    # Phase 6 (2026-05-09): expose the live ingester list on app.state so
    # /api/v1/health/full can read it without an import-of-self dance. The
    # earlier fallback `from glassbox_server import _ingesters` returned
    # the binding at api_v1's import time (empty), missing the list that
    # startup populated. app.state is the FastAPI-blessed sharing path.
    #
    # P3-H app.state migration (2026-05-22): extending the same pattern
    # to the rest of the daemon-internal shared state, so the remaining
    # P3-H extractions (#9 /api/glassbox/*, #10 /api/intel/*, #12 health
    # + misc) can read shared state via `request.app.state.<name>` without
    # cross-module module-state imports. ADDITIVE BRIDGE — module-level
    # names below still exist and still point at the same objects; this
    # makes both readers work during the per-reader migration. The final
    # migration commit will delete the module-level names.
    app.state.ingesters = _ingesters
    app.state.subscribers = _subscribers
    app.state.hot_cache = _hot_cache
    app.state.started_at = _started_at
    app.state.latest_sitrep = _latest_sitrep
    # Extended bridge for P3-H extraction #9 (/api/glassbox/* routes):
    # the viewport-query cache + the layer-alias map. Both are mutated by
    # `_broadcast()` (which stays in this module because every ingester
    # calls it as a callback), so the dual-binding lets the broadcaster
    # keep using the module-level names while extracted handlers read
    # via `request.app.state.<name>` — both point at the same objects.
    app.state.entities_cache = _entities_cache
    app.state.layer_aliases = _LAYER_ALIASES
    app.state.entities_bbox_quantum = _ENTITIES_BBOX_QUANTUM
    app.state.entities_cache_ttl_sec = _ENTITIES_CACHE_TTL_SEC
    app.state.hot_cache_per_layer = HOT_CACHE_PER_LAYER
    # Second mini-bridge addition for /api/glassbox/sitrep/publish (P3-H #9)
    # — the SSE subscriber-drop bookkeeping must be the same object on both
    # sides of the bridge, since deliver_to_subscribers (called by both the
    # in-file broadcaster AND the extracted sitrep/publish handler) mutates
    # it. Constants exposed alongside so the lifted helper doesn't have to
    # import them separately.
    app.state.subscriber_drops = _subscriber_drops
    app.state.broadcast_drop_limit = _BROADCAST_DROP_LIMIT
    app.state.broadcast_drop_log_every = _BROADCAST_DROP_LOG_EVERY

    # Phase 1.4 (2026-05-07): proximity scan background task. Runs every
    # GLASSBOX_PROXIMITY_INTERVAL_SEC (default 300 = 5 min) per V2 plan.
    # Skipped when GLASSBOX_PROXIMITY_DISABLED=1.
    if os.environ.get("GLASSBOX_PROXIMITY_DISABLED") != "1":
        asyncio.create_task(_proximity_scan_loop())
        log.info("[proximity] scan loop scheduled every 5 minutes")

    # 2026-05-08: hourly brief auto-publish loop. Generates the brief from
    # a global-window viewport call and writes it to disk so operators can
    # read offline / via an editor / via cron. Skipped when
    # GLASSBOX_BRIEF_PUBLISHER_DISABLED=1.
    if os.environ.get("GLASSBOX_BRIEF_PUBLISHER_DISABLED") != "1":
        asyncio.create_task(_brief_publisher_loop())
        log.info("[brief-publisher] hourly publish loop scheduled")

    # Phase 4d-1 (2026-05-09): Splink ER pipeline as a periodic background
    # task. Runs every GLASSBOX_SPLINK_INTERVAL_SEC (default 3600 = 1 hr).
    # Refreshes vessel↔sanctioned alias edges so newly-added OFAC entries
    # and live AIS broadcasts get linked without an operator running the
    # one-shot. Skipped when GLASSBOX_SPLINK_DISABLED=1.
    if os.environ.get("GLASSBOX_SPLINK_DISABLED") != "1":
        asyncio.create_task(_splink_er_loop())
        log.info("[splink-er] hourly pipeline scheduled")

    # Phase 4a follow-up (2026-05-09): embedding backfill as a slow
    # background drain. Runs every GLASSBOX_EMBED_BACKFILL_INTERVAL_SEC
    # (default 900 = 15 min) and processes up to 256 NULL-embedding rows
    # per cycle. Without this loop the only way to populate embeddings on
    # historical rows was an operator running the one-shot script.
    # Skipped when GLASSBOX_EMBED_BACKFILL_DISABLED=1.
    if os.environ.get("GLASSBOX_EMBED_BACKFILL_DISABLED") != "1":
        asyncio.create_task(_embed_backfill_loop())
        log.info("[embed-backfill] drain loop scheduled (15 min cadence)")

    # 2026-05-10: eagerly warm sentence-transformers off-loop so the first
    # `events.search?q=` (or any other ad-hoc query embed) doesn't pay a
    # 5–15s cold-load that used to time out the events-MCP search tool at
    # 30s. asyncio.to_thread keeps the event loop unblocked while the
    # 80MB MiniLM model + tokenizer materialize. Skipped when
    # GLASSBOX_EMBED_WARMUP_DISABLED=1 (e.g. for unit-test boot times).
    if os.environ.get("GLASSBOX_EMBED_WARMUP_DISABLED") != "1":
        asyncio.create_task(_warm_embeddings())


async def _proximity_scan_loop() -> None:
    """Background task — call run_proximity_scan() on a fixed interval. Failures
    are logged and swallowed; the loop keeps running."""
    interval = float(os.environ.get("GLASSBOX_PROXIMITY_INTERVAL_SEC", "300"))
    proximity_log = logging.getLogger("algorithms.proximity")
    # Wait one interval before the first scan so the entity table has data.
    await asyncio.sleep(interval)
    # Phase 2.5 (2026-05-07): cross-entity scan re-enabled unconditionally
    # after the entity.current_geom denormalization made it ~10s/run at
    # v1.0 scale (was >120s and timing out). Set
    # GLASSBOX_PROXIMITY_CROSS_ENTITY_DISABLED=1 to skip if needed.
    cross_disabled = os.environ.get("GLASSBOX_PROXIMITY_CROSS_ENTITY_DISABLED") == "1"
    # Phase 4 algorithm #2 (2026-05-08): dark-ship detection.
    # Phase 4 algorithm #3 (2026-05-08): live sanctions-match.
    # Phase 4 algorithm #4 (2026-05-08): military aircraft tracking.
    # All run in the same 5-min loop. Disable each via env var.
    dark_ship_disabled = os.environ.get("GLASSBOX_DARK_SHIP_DISABLED") == "1"
    sanctions_match_disabled = os.environ.get("GLASSBOX_SANCTIONS_MATCH_DISABLED") == "1"
    mil_flights_disabled = os.environ.get("GLASSBOX_MIL_FLIGHTS_DISABLED") == "1"
    loitering_disabled = os.environ.get("GLASSBOX_LOITERING_DISABLED") == "1"
    rendezvous_disabled = os.environ.get("GLASSBOX_RENDEZVOUS_DISABLED") == "1"
    sanctioned_airspace_disabled = os.environ.get("GLASSBOX_SANCTIONED_AIRSPACE_DISABLED") == "1"
    sanctioned_dark_disabled = os.environ.get("GLASSBOX_SANCTIONED_DARK_DISABLED") == "1"
    sanctioned_rendezvous_disabled = os.environ.get("GLASSBOX_SANCTIONED_RDV_DISABLED") == "1"
    sanctions_multi_disabled = os.environ.get("GLASSBOX_SANCTIONS_MULTI_DISABLED") == "1"
    shadow_fleet_disabled = os.environ.get("GLASSBOX_SHADOW_FLEET_DISABLED") == "1"
    port_call_disabled = os.environ.get("GLASSBOX_PORT_CALL_DISABLED") == "1"
    dark_ship_log = logging.getLogger("algorithms.dark_ship")
    sanctions_match_log = logging.getLogger("algorithms.sanctions_match")
    mil_flights_log = logging.getLogger("algorithms.military_flights")
    loitering_log = logging.getLogger("algorithms.loitering")
    rendezvous_log = logging.getLogger("algorithms.rendezvous")
    sanctioned_airspace_log = logging.getLogger("algorithms.sanctioned_airspace")
    sanctioned_dark_log = logging.getLogger("algorithms.sanctioned_dark_vessel")
    sanctioned_rdv_log = logging.getLogger("algorithms.sanctioned_rendezvous")
    sanctions_multi_log = logging.getLogger("algorithms.sanctions_multijurisdictional")
    shadow_fleet_log = logging.getLogger("algorithms.shadow_fleet_cluster")
    port_call_log = logging.getLogger("algorithms.port_call")
    while True:
        try:
            n_e = 0
            # Isolate the master proximity scan from downstream algorithms.
            # At v1.0 scale (47k aircraft × 100k events) the entity↔event
            # query can time out; without this guard a single timeout would
            # skip all 9 downstream detection scans (sanctions_match,
            # multijurisdictional, dark_ship, etc.) for that 5-min cycle.
            try:
                n_e = await run_proximity_scan(radius_m=50_000, window_min=60)
            except Exception as e:
                proximity_log.warning(
                    f"proximity scan failed (skipping): {type(e).__name__}: {e}"
                )
            n_x = 0
            if not cross_disabled:
                try:
                    n_x = await run_cross_entity_proximity_scan(
                        radius_m=50_000, window_min=60,
                    )
                except Exception as e:
                    proximity_log.warning(
                        f"cross-entity scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_d = 0
            if not dark_ship_disabled:
                try:
                    n_d = await run_dark_ship_scan()
                except Exception as e:
                    dark_ship_log.warning(
                        f"dark-ship scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_s = 0
            if not sanctions_match_disabled:
                try:
                    n_s = await run_sanctions_match_scan()
                except Exception as e:
                    sanctions_match_log.warning(
                        f"sanctions-match scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_m = 0
            if not mil_flights_disabled:
                try:
                    n_m = await run_military_flights_scan()
                except Exception as e:
                    mil_flights_log.warning(
                        f"mil-flights scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_l = 0
            if not loitering_disabled:
                try:
                    n_l = await run_loitering_scan()
                except Exception as e:
                    loitering_log.warning(
                        f"loitering scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_r = 0
            if not rendezvous_disabled:
                try:
                    n_r = await run_rendezvous_scan()
                except Exception as e:
                    rendezvous_log.warning(
                        f"rendezvous scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_a = 0
            if not sanctioned_airspace_disabled:
                try:
                    n_a = await run_sanctioned_airspace_scan()
                except Exception as e:
                    sanctioned_airspace_log.warning(
                        f"sanctioned-airspace scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_sd = 0
            if not sanctioned_dark_disabled:
                try:
                    n_sd = await run_sanctioned_dark_scan()
                except Exception as e:
                    sanctioned_dark_log.warning(
                        f"sanctioned-dark scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_sr = 0
            if not sanctioned_rendezvous_disabled:
                try:
                    n_sr = await run_sanctioned_rendezvous_scan()
                except Exception as e:
                    sanctioned_rdv_log.warning(
                        f"sanctioned-rendezvous scan failed (skipping): {type(e).__name__}: {e}"
                    )
            n_smj = 0
            # Multi-jurisdictional scan runs AFTER sanctions_match so it groups
            # the freshly-emitted single-authority events. Same scan loop is
            # fine — sanctions_match completes synchronously above.
            if not sanctions_multi_disabled:
                try:
                    n_smj = await run_sanctions_multijurisdictional_scan()
                except Exception as e:
                    sanctions_multi_log.warning(
                        f"sanctions-multijurisdictional scan failed (skipping): "
                        f"{type(e).__name__}: {e}"
                    )
            n_sfc = 0
            # Shadow-fleet cluster scan runs AFTER sanctions_match too — it
            # consumes the same sanctioned_vessel_underway events to detect
            # ≥3-vessel clusters within 10 km.
            if not shadow_fleet_disabled:
                try:
                    n_sfc = await run_shadow_fleet_cluster_scan()
                except Exception as e:
                    shadow_fleet_log.warning(
                        f"shadow-fleet cluster scan failed (skipping): "
                        f"{type(e).__name__}: {e}"
                    )
            n_pc = 0
            n_pa = 0
            n_pd = 0
            n_spa = 0
            # Port-call + transitions (Phase 4 algorithm, 2026-05-09):
            # vessels at the ~103 reference ports + arrival/departure
            # transition events derived from the port_call timeline.
            # Independent of other algorithms so failure doesn't cascade.
            if not port_call_disabled:
                try:
                    n_pc = await run_port_call_scan()
                except Exception as e:
                    port_call_log.warning(
                        f"port-call scan failed (skipping): "
                        f"{type(e).__name__}: {e}"
                    )
                try:
                    n_pa = await run_port_arrival_scan()
                except Exception as e:
                    port_call_log.warning(
                        f"port-arrival scan failed (skipping): "
                        f"{type(e).__name__}: {e}"
                    )
                try:
                    n_pd = await run_port_departure_scan()
                except Exception as e:
                    port_call_log.warning(
                        f"port-departure scan failed (skipping): "
                        f"{type(e).__name__}: {e}"
                    )
                # Compound tier-1 alert: sanctioned vessel arrived at
                # a reference port. Joins port_arrival/port_call with
                # recent sanctioned_vessel_underway events for the same
                # entity. Phase 4d-4 (2026-05-09).
                try:
                    n_spa = await run_sanctioned_port_arrival_scan()
                except Exception as e:
                    port_call_log.warning(
                        f"sanctioned-port-arrival scan failed (skipping): "
                        f"{type(e).__name__}: {e}"
                    )
            total = (n_e + n_x + n_d + n_s + n_m + n_l + n_r + n_a +
                     n_sd + n_sr + n_smj + n_sfc + n_pc + n_pa + n_pd + n_spa)
            if total > 0:
                proximity_log.info(
                    f"scan cycle: {n_e} entity↔event + {n_x} entity↔entity "
                    f"+ {n_d} dark + {n_s} sanc-underway + {n_m} mil-acft "
                    f"+ {n_l} loiter + {n_r} rdv + {n_a} sanc-airspace "
                    f"+ {n_sd} SANC-DARK + {n_sr} SANC-RDV "
                    f"+ {n_smj} MULTI-JURIS "
                    f"+ {n_sfc} SHADOW-FLEET "
                    f"+ {n_pc} PORT-CALL "
                    f"+ {n_pa} ARRIVE + {n_pd} DEPART "
                    f"+ {n_spa} SANC-PORT-ARRIVAL "
                    f"= {total} new findings"
                )
            # Publish per-cycle stats for operator visibility (read by
            # /api/v1/recent-cycle). Done unconditionally so even a
            # zero-finding cycle records "we ran at T+0".
            _RECENT_SCAN_CYCLE["completed_at"] = datetime.now(timezone.utc).isoformat()
            _RECENT_SCAN_CYCLE["totals"] = {
                "entity_event": n_e, "entity_entity": n_x, "dark_ship": n_d,
                "sanctions_match": n_s, "military_flights": n_m,
                "loitering": n_l, "rendezvous": n_r,
                "sanctioned_airspace": n_a,
                "sanctioned_dark": n_sd,
                "sanctioned_rendezvous": n_sr,
                "multijurisdictional": n_smj,
                "shadow_fleet_cluster": n_sfc,
                "port_call": n_pc,
                "port_arrival": n_pa,
                "port_departure": n_pd,
                "sanctioned_port_arrival": n_spa,
            }
            _RECENT_SCAN_CYCLE["total"] = total
        except Exception as e:
            proximity_log.warning(f"proximity scan failed: {type(e).__name__}: {e}")
        await asyncio.sleep(interval)


# 2026-05-08: brief auto-publish — write the global-window brief to disk
# every hour so operators can read it via cron / editor / `tail -f` without
# needing the dashboard up. Two outputs:
#   briefs/latest.md            — overwritten each cycle
#   briefs/2026-05-08T20.md     — hourly archive (UTC), retained
_BRIEF_DIR = Path(__file__).resolve().parent / "briefs"
_BRIEF_RECENT = {"published_at": None, "path": None, "bytes": 0}


# Phase 4d-1 (2026-05-09): track the most recent Splink ER pipeline result
# so /api/v1/health/full can surface it as part of the production-monitoring
# snapshot. None until the first cycle completes.
_LAST_SPLINK_RUN = {
    "started_at":      None,
    "completed_at":    None,
    "duration_ms":     None,
    "predicted":       None,
    "persisted":       None,
    "error":           None,
}


async def _warm_embeddings() -> None:
    """One-shot: load the sentence-transformers model off-loop at startup.

    Without this, the first `embed_text(q)` from an HTTP handler pays a
    5–15s cold-load — long enough to time out the events-MCP search tool
    (httpx default 30s, with the rest of the request adding overhead).
    Loading happens in a thread so the event loop stays responsive while
    the 80MB MiniLM model + tokenizer materialize. Failure is logged but
    not fatal — embedding-bearing endpoints fail-soft to 503.
    """
    embed_log = logging.getLogger("embeddings.warmup")
    try:
        from embeddings import warm_up  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        embed_log.warning(
            f"embeddings module unavailable; skipping warmup: "
            f"{type(e).__name__}: {e}"
        )
        return
    started = time.monotonic()
    try:
        loaded = await asyncio.to_thread(warm_up)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if loaded:
            embed_log.info(f"sentence-transformers warmed in {elapsed_ms}ms")
        else:
            embed_log.warning(
                f"sentence-transformers warm-up returned False after "
                f"{elapsed_ms}ms — see embeddings.status() for the load error"
            )
    except Exception as e:  # noqa: BLE001
        embed_log.warning(f"warm-up failed: {type(e).__name__}: {e}")


async def _embed_backfill_loop() -> None:
    """Background drain — populate event.embedding for NULL rows in
    TEXT_EVENT_TYPES. Bounded per-cycle so a large backlog doesn't
    monopolize the model thread for one ~5-min stretch.

    Failures are logged + swallowed; the loop keeps running.
    """
    interval = float(os.environ.get("GLASSBOX_EMBED_BACKFILL_INTERVAL_SEC", "900"))
    batch_size = int(os.environ.get("GLASSBOX_EMBED_BACKFILL_BATCH", "64"))
    per_cycle = int(os.environ.get("GLASSBOX_EMBED_BACKFILL_PER_CYCLE", "256"))
    embed_log = logging.getLogger("embeddings.backfill")

    try:
        from embeddings import backfill_event_embeddings  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        embed_log.warning(
            f"embeddings module unavailable; backfill loop exiting: "
            f"{type(e).__name__}: {e}"
        )
        return

    # Wait one interval before the first run so the OFAC/news ingesters
    # have had a chance to populate fresh rows.
    await asyncio.sleep(interval)
    while True:
        try:
            res = await backfill_event_embeddings(
                batch_size=batch_size, max_events=per_cycle
            )
            if res.get("embedded"):
                embed_log.info(
                    f"backfill cycle: scanned={res['scanned']} "
                    f"embedded={res['embedded']} "
                    f"skipped_no_text={res['skipped_no_text']}"
                )
        except Exception as e:  # noqa: BLE001
            embed_log.warning(
                f"backfill cycle failed (skipping): {type(e).__name__}: {e}"
            )
        await asyncio.sleep(interval)


async def _splink_er_loop() -> None:
    """Background task — run the Splink vessel-ER pipeline every
    GLASSBOX_SPLINK_INTERVAL_SEC (default 3600 = 1 hour). Refreshes the
    `splink_alias` entity_relation index so newly-added OFAC entries and
    live AIS broadcasts get linked without an operator action.

    Phase 4d-3 (2026-05-09): on first run after server start (or every
    GLASSBOX_SPLINK_RETRAIN_EVERY cycles, default 24 = once per day at
    hourly cadence), retrains the Splink model via EM on the live corpus
    and saves to disk. Subsequent predict cycles load the saved model
    so name-only NULL-IMO matches surface above the actionable threshold
    (the default-untrained prior dominates and crushes them at ~0.09).

    Failures logged + swallowed so the loop keeps running. Result count
    surfaced via `_LAST_SPLINK_RUN` for /api/v1/health/full.

    The first run waits one full interval so Postgres has time to settle
    after server start; large EM training routines on a cold DB pool can
    otherwise spike latency for the first algorithm scan.
    """
    interval = float(os.environ.get("GLASSBOX_SPLINK_INTERVAL_SEC", "3600"))
    threshold = float(os.environ.get("GLASSBOX_SPLINK_THRESHOLD", "0.95"))
    retrain_every = int(os.environ.get("GLASSBOX_SPLINK_RETRAIN_EVERY", "24"))
    splink_log = logging.getLogger("er.splink.loop")

    # Local import keeps Splink (and its DuckDB / pandas deps) lazy-loaded.
    # If the package isn't available the loop logs once and exits cleanly
    # so the rest of the server keeps running.
    try:
        from infra.er.splink_pipeline import (  # noqa: WPS433
            predict_with_default_settings,
            persist_matches,
            train_and_save_model,
            model_path,
        )
    except Exception as e:  # noqa: BLE001
        splink_log.warning(
            f"splink import failed; ER loop will not run: {type(e).__name__}: {e}"
        )
        _LAST_SPLINK_RUN["error"] = f"import_failed: {type(e).__name__}: {e}"
        return

    await asyncio.sleep(interval)

    cycle_n = 0
    mp = model_path()
    while True:
        cycle_n += 1
        t0 = time.time()
        _LAST_SPLINK_RUN["started_at"] = datetime.now(timezone.utc).isoformat()
        _LAST_SPLINK_RUN["error"] = None
        # Retrain when the model file is missing OR every retrain_every cycles.
        # First cycle (cycle_n=1) trains if model is missing; bootstrap path.
        should_train = (
            (not os.path.exists(mp))
            or (retrain_every > 0 and cycle_n % retrain_every == 0)
        )
        if should_train:
            try:
                splink_log.info(
                    f"splink: training EM model (cycle={cycle_n}, path={mp})"
                )
                train_res = await train_and_save_model(save_path=mp)
                if train_res.get("error"):
                    splink_log.warning(
                        f"splink training had issue: {train_res['error']} "
                        f"(stages={train_res['stages_completed']}/3)"
                    )
                else:
                    splink_log.info(
                        f"splink training complete: stages="
                        f"{train_res['stages_completed']}/3"
                    )
            except Exception as e:  # noqa: BLE001
                splink_log.warning(
                    f"splink training failed (continuing with old model): "
                    f"{type(e).__name__}: {e}"
                )
        try:
            matches = await predict_with_default_settings(
                threshold_match_probability=threshold,
                trained_model_path=mp,
            )
            persisted = await persist_matches(matches)
            _LAST_SPLINK_RUN["predicted"] = len(matches)
            _LAST_SPLINK_RUN["persisted"] = persisted
            splink_log.info(
                f"splink ER cycle {cycle_n}: predicted={len(matches)} "
                f"persisted={persisted} threshold={threshold}"
            )
        except Exception as e:  # noqa: BLE001
            _LAST_SPLINK_RUN["error"] = f"{type(e).__name__}: {e}"
            splink_log.warning(
                f"splink ER cycle failed (skipping): {type(e).__name__}: {e}"
            )
        finally:
            _LAST_SPLINK_RUN["completed_at"] = datetime.now(timezone.utc).isoformat()
            _LAST_SPLINK_RUN["duration_ms"] = int((time.time() - t0) * 1000)
        await asyncio.sleep(interval)


async def _brief_publisher_loop() -> None:
    """Hourly: pull viewport with global bbox + 1h window, generate brief,
    write to disk. Failures logged + swallowed; loop continues."""
    interval = float(os.environ.get("GLASSBOX_BRIEF_PUBLISHER_INTERVAL_SEC", "3600"))
    brief_log = logging.getLogger("brief.publisher")
    _BRIEF_DIR.mkdir(parents=True, exist_ok=True)

    # Local import — query_viewport pulls the same data the HTTP endpoint
    # serves. Lives in web/routes/api_v1/core.py since P3-H Phase 2
    # (commit e4b63c8, 2026-05-27) — moved out of api_v1.py.
    from web.routes.api_v1.core import query_viewport

    while True:
        try:
            now = datetime.now(timezone.utc)
            time_from = now - timedelta(hours=1)
            result = await query_viewport(
                bbox=(-180.0, -85.0, 180.0, 85.0),
                time_from=time_from,
                time_to=now,
                types=["aircraft", "vessel", "satellite"],
                limit=2000,
            )
            text = generate_brief_cached(result)
            header = (
                f"# Glassbox brief — {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"_Auto-published every {int(interval/60)} min. Window: "
                f"{time_from.strftime('%H:%M')}–{now.strftime('%H:%M')} UTC._\n\n"
                f"---\n\n"
            )
            footer = (
                f"\n\n---\n"
                f"_meta: bbox=global, "
                f"entity_count={result.get('meta', {}).get('entity_count', 0)}, "
                f"event_count={result.get('meta', {}).get('event_count', 0)}_\n"
            )
            md = header + text + footer

            # Write latest.md (overwrite), then a timestamped archive.
            latest_path = _BRIEF_DIR / "latest.md"
            archive_path = _BRIEF_DIR / f"{now.strftime('%Y-%m-%dT%H')}.md"
            latest_path.write_text(md, encoding="utf-8")
            archive_path.write_text(md, encoding="utf-8")

            _BRIEF_RECENT["published_at"] = now.isoformat()
            _BRIEF_RECENT["path"] = str(latest_path)
            _BRIEF_RECENT["bytes"] = len(md)

            # Populate the SITREP panel that the dashboard reads via
            # /api/glassbox/sitrep/latest. Without this, the dashboard
            # showed "WAITING FOR FIRST INTELLIGENCE CYCLE" indefinitely
            # because no external publisher was running.
            #
            # Headline: first 'sentence' of the brief (the *** CRITICAL ***
            # line if present, otherwise the bbox-window summary).
            # Brief body: full deterministic brief text.
            first_sentence_end = text.find(". ")
            if first_sentence_end > 0:
                headline = text[: first_sentence_end + 1].strip()
            else:
                headline = "Glassbox SITREP"
            # Strip the bbox-prefix when the headline starts with it (the
            # operator already knows the bbox is global; the alert payload
            # is what they want to see first).
            if headline.startswith("In bbox"):
                colon_idx = headline.find(":")
                if colon_idx > 0 and colon_idx < len(headline) - 2:
                    headline = headline[colon_idx + 1:].strip()
            confidence = 1.0 if "*** CRITICAL ***" in text else 0.85
            _latest_sitrep["generated_at"] = now.isoformat()
            _latest_sitrep["sitrep"] = {
                "headline": headline[:200],
                "brief": text,
                "priorities": [],
                "confidence": confidence,
            }
            _latest_sitrep["total_events"] = result.get("meta", {}).get("event_count", 0)

            brief_log.info(
                f"published brief: {len(md):,} bytes → "
                f"briefs/latest.md + briefs/{archive_path.name} (+ SITREP)"
            )
        except Exception as e:
            brief_log.warning(
                f"brief publish failed (will retry next interval): "
                f"{type(e).__name__}: {e}"
            )
        await asyncio.sleep(interval)


@app.get("/api/v1/brief/latest")
async def latest_brief():
    """Read briefs/latest.md from disk and return its contents.
    Convenient for ops dashboards or curl. Falls through to JSON
    metadata when the file doesn't exist yet."""
    latest_path = _BRIEF_DIR / "latest.md"
    if not latest_path.exists():
        return JSONResponse({
            "ok": False,
            "reason": "brief publisher hasn't completed first cycle yet",
            **_BRIEF_RECENT,
        }, status_code=404)
    return JSONResponse({
        "ok": True,
        **_BRIEF_RECENT,
        "markdown": latest_path.read_text(encoding="utf-8"),
    })


@app.get("/api/v1/recent-cycle")
async def recent_scan_cycle() -> JSONResponse:
    """Last completed proximity-scan-loop cycle: per-algorithm finding
    counts, total, completion time. Empty {totals: 0, completed_at: None}
    until the first cycle finishes (~5 min after server start).

    Used by the dashboard to render a "last cycle: 5,234 new findings @
    19:42 UTC" status line so operators can spot a stalled loop without
    tailing the log file.
    """
    return JSONResponse({
        "scan_interval_sec": float(os.environ.get("GLASSBOX_PROXIMITY_INTERVAL_SEC") or 300),
        **_RECENT_SCAN_CYCLE,
    })


# Mission Control + operator visibility into the gate.
# /api/sources moved to web/routes/misc.py (P3-H extraction #12).


@app.on_event("shutdown")
async def _shutdown() -> None:
    for ing in _ingesters:
        ing.stop()
    # Phase 1: drain Postgres pool cleanly on shutdown
    try:
        await close_pools()
    except Exception as e:
        log.warning(f"[db] close_pool error on shutdown: {type(e).__name__}: {e}")


# ─── Health + layer endpoints ──────────────────────────────────────────────

# ─── Same-origin UI routes (fixes SSE/fetch from file:// origin issue) ─────
# When opened via file://, the browser uses an "opaque" origin that blocks
# EventSource and many fetch calls. Serving the dashboard FROM this server
# means same-origin (http://localhost:8790) → SSE works, no CORS dance.

# Web-asset path constants + helpers all moved to web/_assets.py and
# web/routes/{static,pages,admin}.py during the P3-H refactor.


# /favicon.svg + /favicon.ico moved to web/routes/static.py (P3-H extraction #5).


@app.get("/api/v1/cesium-token", include_in_schema=True)
async def cesium_token():
    """Returns the Cesium Ion access token for client-side init.

    Cesium Ion tokens are URL-restricted client tokens (semantically
    like a Google Maps API key, NOT a server secret) — but we still
    serve them via this endpoint instead of hardcoding in JS so:
      1. The token can be rotated without redeploying static files
      2. The pre-commit secret scanner doesn't fire on JWT shapes in
         committed source
      3. Future per-domain or per-user token issuance is a one-line
         change here

    Reads from CESIUM_ION_TOKEN_MEWR (or CESIUM_ION_TOKEN as fallback)
    env var. Returns empty string if unset (Cesium will fall back to
    its built-in default tile set, which is rate-limited but
    functional)."""
    tok = (os.environ.get("CESIUM_ION_TOKEN_MEWR")
           or os.environ.get("CESIUM_ION_TOKEN") or "")
    return {"token": tok}


# /atlas.js + /command.js moved to web/routes/static.py (P3-H extraction #5).


# /globe + /globe/globe.js moved to web/routes/globe.py (P3-H extraction #4).


# /monitor + /monitor/monitor.js moved to web/routes/monitor.py (P3-H extraction #3).


_SAT_TLE_CACHE: dict[str, Any] = {"text": None, "fetched_at": 0.0}
_SAT_TLE_TTL_SEC = 6 * 3600   # refresh every 6 hours


@app.get("/api/v1/satellites/tle", include_in_schema=True,
         tags=["satellites"])
async def serve_satellites_tle() -> Response:
    """Server-cached proxy for satellite TLE data.

    Sources tried in order, fallback to next on failure:
      1. AMSAT — https://www.amsat.org/tle/current/nasabare.txt
         (~100 amateur satellites including ISS, NOAA weather sats,
         AO/CAS/UO series; reliable, no auth, MIT-style data)
      2. CelesTrak active — https://celestrak.org/NORAD/elements/...
         (~5000 active satellites, blocked from some networks)

    Cached server-side for 6 hours so we don't hammer AMSAT and so the
    cockpit gets sub-100ms response on cache hits. TLE is a public
    domain dataset; redistributing is fine. Returns text/plain.
    """
    import time
    import httpx
    now = time.time()
    if _SAT_TLE_CACHE["text"] and (now - _SAT_TLE_CACHE["fetched_at"]) < _SAT_TLE_TTL_SEC:
        return Response(
            content=_SAT_TLE_CACHE["text"],
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": f"public, max-age={int(_SAT_TLE_TTL_SEC // 2)}"},
        )
    sources = [
        "https://www.amsat.org/tle/current/nasabare.txt",
        "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle",
    ]
    async with httpx.AsyncClient(timeout=15.0,
                                 follow_redirects=True) as client:
        for url in sources:
            try:
                r = await client.get(url, headers={"User-Agent": "Glassbox/1.0"})
                if r.status_code == 200 and len(r.text) > 200:
                    _SAT_TLE_CACHE["text"] = r.text
                    _SAT_TLE_CACHE["fetched_at"] = now
                    return Response(
                        content=r.text,
                        media_type="text/plain; charset=utf-8",
                        headers={
                            "Cache-Control": f"public, max-age={int(_SAT_TLE_TTL_SEC // 2)}",
                            "X-Glassbox-TLE-Source": url.split("/")[2],
                        },
                    )
            except Exception:
                continue
    # All sources failed
    if _SAT_TLE_CACHE["text"]:
        # Serve stale rather than 503
        return Response(
            content=_SAT_TLE_CACHE["text"],
            media_type="text/plain; charset=utf-8",
            headers={"X-Glassbox-TLE-Source": "stale"},
        )
    return Response(
        content="# All TLE sources unreachable\n",
        media_type="text/plain",
        status_code=503,
    )


# /track.js + /satellite.min.js + /satellites_worker.js moved to web/routes/static.py (P3-H extraction #5).


# /admin/analytics + /pricing moved to web/routes/admin.py (P3-H extraction #7).


# /og-image.png + /robots.txt + /sitemap.xml moved to web/routes/static.py (P3-H extraction #5).


# /api/v1/infrastructure/{military-bases,nuclear,cables,trafficking,pipelines}
# moved to web/routes/infrastructure.py (P3-H extraction #8).


# /monitor/countries.geojson moved to web/routes/monitor.py (P3-H extraction #3).
# /network + /network/network.js moved to web/routes/network.py (P3-H extraction).


# /, /web, /glassbox, /markets, /pro, /console, /demo moved to web/routes/pages.py (P3-H extraction #6).


# /signals + /signals/embed + /signals.rss + /signals.json moved to web/routes/signals.py (P3-H extraction #2).


# /entity/{entity_id} + /status moved to web/routes/admin.py (P3-H extraction #7).


# /api/health + /health moved to web/routes/misc.py (P3-H extraction #12).
# They now read shared state via request.app.state.<name>, populated by
# the additive bridge in the startup hook (commit 3231f63).


# ─── Entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Pass `app` directly instead of the import-string "glassbox_server:app".
    # The string form forces uvicorn to re-import the module by name, which
    # can resolve to a stale __pycache__/.pyc with old routes — which is
    # exactly why GET / was returning 404 after we added the dashboard route.
    # `app` is already registered in this __main__ namespace; use it.
    uvicorn.run(
        app,
        host=APP_HOST,
        port=APP_PORT,
        log_level="info",
        reload=False,
    )
