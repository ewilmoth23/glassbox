# `api_v1.py` Route Inventory (P3-H Phase 2)

**Generated:** 2026-05-27 (P3-H Phase 2 session)
**Source:** `21_GLASSBOX_AI/api_v1.py` @ commit `ecd91af` (Phase 1 closeout)
**File size:** 3,257 lines / 137 KB
**Total decorated routes:** 32 (all inside the `build_router(prefix, tag)` factory at line 771)

This is the pre-refactor snapshot for P3-H **Phase 2** (api_v1 god-module
split). The smoke test at `21_GLASSBOX_AI/tests/test_api_v1_routes_smoke.py`
introspects `app.routes` at runtime; this doc is the human-readable map.

## Structural difference from Phase 1

Phase 1 split `glassbox_server.py`, which used module-level `@app.get(...)`
decorators. Phase 2 splits `api_v1.py`, which uses a `build_router(prefix,
tag)` **factory** with nested `@router.get(...)` handlers.

The factory contract is **load-bearing**:

```python
# glassbox_server.py:359-364
app.include_router(build_v1_router())                                    # /api/v1/*
app.include_router(build_v1_router(prefix="/api/intel", tag="intel"))    # /api/intel/*
```

Both mounts must keep producing the same 32 routes. **Caller signature
must NOT change.** Internally, `build_router()` will compose sub-routers
from `web/routes/api_v1/*.py`:

```python
def build_router(prefix: str = "/api/v1", tag: str = "v1") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag])
    from web.routes.api_v1 import lookups, analytics, dashboard, sanctions, \
                                  health_metrics, alerts, core_entities, signals
    for sub in (lookups, analytics, dashboard, sanctions,
                health_metrics, alerts, core_entities, signals):
        router.include_router(sub.router)
    return router
```

Each `web/routes/api_v1/<name>.py` exposes `router = APIRouter()` (no
prefix; the parent supplies it). Routes register on it with
`@router.get("/lookup/asn")` etc.

## Progress scoreboard

| Extractions done | Lines removed from api_v1.py | inline @router.* count | Pytest baseline |
|---|---|---|---|
| **8 of 8 — PHASE 2 COMPLETE 🎉** | **3257 → 197 (-3060, -94.0%)** | **32 → 0** | 1097 passed / 1 skipped |

### Shared modules created via Option-A lifts

| Helper | Lifted from api_v1 to | Commit | Consumers |
|---|---|---|---|
| `llm_rate_check` | `web/_rate_limit.py` (Phase 1) | `1039777` | /api/intel/query, /api/glassbox/{ask,sitrep} |
| `request_rate_limit` (was `_rate_limit`) | `web/_rate_limit.py` | `fcc4d10` | /analytics/*, /signals/subscribe (inline) |
| `_REQUEST_BUCKETS` (was `_RATE_BUCKETS`) | `web/_rate_limit.py` | `fcc4d10` | request_rate_limit decorator state |
| `SIGNALS_CATEGORY_ORDER` | `web/_signals_categories.py` | `30cb1c2` | /dashboard/summary, /signals/* (inline) |
| `SIGNALS_CATEGORIES_BY_TYPE` | `web/_signals_categories.py` | `30cb1c2` | /dashboard/summary, /signals/* (inline) |
| `coerce_jsonb` (was `_coerce_jsonb`) | `web/_jsonb.py` | `fa65217` | core.* handlers, sanctions, alerts, /signals/* (inline) |

### Shim placement decision matrix (codified after extractions 3 + 4)

| Pattern | When to use | Example |
|---|---|---|
| **Top-of-file shim** (`from web.routes.api_v1.X import Y` near api_v1's imports) | Cluster module does NOT import api_v1 at its own module top (only deferred imports inside handler bodies) | health_metrics.py (#3): only `/system-state` defers `from api_v1 import _TIER1_EVENT_TYPES_FOR_POLL` |
| **Bottom-of-file shim** (re-export block at end of api_v1.py) | Cluster module DOES import api_v1 at its own module top | alerts.py (#4): `from api_v1 import _parse_bbox, _coerce_jsonb` at module top |
| **No shim needed** | No test imports the symbol, no other cluster needs it | lookups.py (#1), sanctions.py (#2) |

## Test-coupling caveat

Several tests reach into `api_v1`'s private symbols:

| Test file | Symbol imported from api_v1 |
|---|---|
| `test_viewport_endpoint.py` | `query_viewport`, `build_router` |
| `test_entity_detail.py` | `query_entity_detail`, `build_router` |
| `test_health_full.py` | `build_health_snapshot` |
| `test_metrics_endpoint.py` | `build_health_snapshot`, `_render_prometheus`, `_esc_label` |
| `test_alerts_stream.py` | `_poll_new_tier1_events` |
| `test_signals_subscribe_endpoint.py` | `build_router`, `_RATE_BUCKETS` |
| `test_signals_csv_endpoint.py` | `build_router`, `_CSV_COLUMNS` |
| `test_signals_page_live_wiring.py` | `_SIGNALS_CATEGORY_ORDER` |
| `test_cross_domain_endpoint.py` | `build_router`, plus regex-scans api_v1.py source for `entity_cross_domain` handler |

**Rule:** every extraction must keep these symbols importable from
`api_v1` (re-export them via `from web.routes.api_v1.X import Y`).
`test_cross_domain_endpoint.py:299` does a regex scan on the api_v1.py
**source text** for the `entity_cross_domain` handler — when that handler
moves, the regex must be updated, NOT broken.

## Extraction strategy

Smallest concerns first. Each extraction is its own commit. The
api_v1-route smoke test MUST run green after each commit; if it doesn't,
revert.

| Order | Concern | Routes | Target file | Risk | Status |
|---|---|---|---|---|---|
| 1 | External lookups (`/lookup/*`) | 3 | `web/routes/api_v1/lookups.py` | Low — pure delegation to `lookups.py` module; no shared state | ✅ Done 2026-05-27 (commit `060476f`) |
| 2 | Sanctions (`/sanctions/*`) | 3 | `web/routes/api_v1/sanctions.py` | Low — DB SELECTs, well-tested | ✅ Done 2026-05-27 (commit `a2bcebb`) |
| 3 | Health + metrics | 5 | `web/routes/api_v1/health_metrics.py` | Medium — `build_health_snapshot`, `_render_prometheus`, `_esc_label` re-exported at api_v1 module TOP for 2 test files | ✅ Done 2026-05-27 (commit `55e6833`) |
| 4 | Alerts (`/alerts/*`) | 2 | `web/routes/api_v1/alerts.py` | Medium — `/alerts/stream` is SSE; `_poll_new_tier1_events` + `_TIER1_EVENT_TYPES_FOR_POLL` re-exported at api_v1 module BOTTOM (alerts.py needs `_parse_bbox`/`_coerce_jsonb` at its own module top, so a top-of-file shim would force a load-order ImportError) | ✅ Done 2026-05-27 (commit `5e2d4dc`) |
| 5 | Analytics (`/analytics/*`) | 2 | `web/routes/api_v1/analytics.py` | Low — DB writes via `execute_write`. Prerequisite: `_rate_limit` + `_RATE_BUCKETS` lifted to `web/_rate_limit.py` (commit `fcc4d10`). | ✅ Done 2026-05-27 (commit `f324e00`) |
| 6 | Dashboard (`/dashboard/summary`) | 1 | `web/routes/api_v1/dashboard.py` | Low — single route. Prerequisite: `SIGNALS_CATEGORY_ORDER` + `SIGNALS_CATEGORIES_BY_TYPE` lifted to `web/_signals_categories.py` (commit `30cb1c2`). | ✅ Done 2026-05-27 (commit `8010375`) |
| 7 | Core entity lookups (`/viewport`, `/entity/{id}`, `/vessel/{mmsi}`, `/aircraft/{icao24}`, `/event/{id}`, `/events/similar`, `/entities/{id}/*`) | 8 | `web/routes/api_v1/core.py` | High — biggest functional surface. `query_viewport`, `query_entity_detail` re-exported via bottom-shim. `test_cross_domain_endpoint.py` regex path updated to scan `core.py`. Prerequisite `_coerce_jsonb` lifted to `web/_jsonb.py` (commit `fa65217`). | ✅ Done 2026-05-27 (commit `e4b63c8`) |
| 8 | Signals (`/signals/*` + `/signals.json` + `/signals.rss`) | 8 | `web/routes/api_v1/signals.py` | High — largest helper graph (`_CSV_COLUMNS`, `_signals_facts_for`, `_signals_authority_for`, `_signals_csv_row`, `_signals_to_csv`, `_signals_rss_item`, `_looks_like_email`, `_rfc822`, `_clip`, `_SEVERITY_RANK`, `_SIGNALS_TODAY_CACHE` — bottom ~420 lines move with this group). Only cluster that doesn't import api_v1 at module top → top-shim re-export for `_CSV_COLUMNS`. | ✅ Done 2026-05-27 (commit `16db1c9`) |

After all 8 extractions land, `api_v1.py` becomes a thin shim:

```python
# api_v1.py (post-Phase-2 target shape, ~50 lines)
from fastapi import APIRouter

# Re-exports for backward-compat with tests that still import these names from api_v1.
from web.routes.api_v1.core import query_viewport, query_entity_detail
from web.routes.api_v1.health_metrics import build_health_snapshot, _render_prometheus, _esc_label
from web.routes.api_v1.alerts import _poll_new_tier1_events
from web.routes.api_v1.signals import _RATE_BUCKETS, _CSV_COLUMNS, _SIGNALS_CATEGORY_ORDER


def build_router(prefix: str = "/api/v1", tag: str = "v1") -> APIRouter:
    """Mount the eight cluster routers under one prefix. See API_V1_ROUTE_INVENTORY.md."""
    from web.routes.api_v1 import (lookups, analytics, dashboard, sanctions,
                                    health_metrics, alerts, core, signals)
    router = APIRouter(prefix=prefix, tags=[tag])
    for sub in (lookups, analytics, dashboard, sanctions,
                health_metrics, alerts, core, signals):
        router.include_router(sub.router)
    return router
```

## Full route inventory (by line number, with cluster mapping)

| Line | Method | Path | Cluster | Handler name | Helpers used |
|---|---|---|---|---|---|
| 778 | GET | `/viewport` | core | `viewport` | `_parse_bbox`, `_parse_iso`, `_parse_types`, `query_viewport`, `generate_brief_cached`, `generate_brief_llm_cached` |
| 811 | GET | `/entity/{entity_id}` | core | `entity_detail` | `query_entity_detail`, `_coerce_jsonb` |
| 915 | GET | `/vessel/{mmsi}` | core | (vessel canonical-lookup) | `acquire_read` |
| 930 | GET | `/aircraft/{icao24}` | core | (aircraft canonical-lookup) | `acquire_read` |
| 944 | GET | `/health/db` | health_metrics | (db ping) | `fetchval_read` |
| 953 | GET | `/metrics` | health_metrics | (prometheus expose) | `build_health_snapshot`, `_render_prometheus`, `_esc_label` |
| 997 | GET | `/metrics/prefilter` | health_metrics | (R1 prefilter metrics) | (in-process counters) |
| 1041 | GET | `/health/full` | health_metrics | (rich health) | `build_health_snapshot` |
| 1075 | GET | `/system-state` | health_metrics | (system-level rollup) | `fetch_read`, `_pool_stats` |
| 1138 | GET | `/sanctions/breakdown` | sanctions | | `fetch_read` |
| 1196 | GET | `/alerts/timeseries` | alerts | | `fetch_read`, `_parse_iso` |
| 1262 | GET | `/sanctions/search` | sanctions | | `fetch_read`, `embeddings.embed_text` (lazy) |
| 1349 | GET | `/sanctions/by-regime` | sanctions | | `fetch_read` |
| 1418 | GET | `/alerts/stream` | alerts | (SSE) | `_poll_new_tier1_events`, `EventSourceResponse` |
| 1488 | GET | `/lookup/subdomains` | lookups | | `lookup_subdomains` (from `lookups.py`) |
| 1496 | GET | `/lookup/wayback` | lookups | | `lookup_wayback` |
| 1504 | GET | `/entities/{entity_id}/aliases` | core | | `acquire_read`, `infra.er.splink_pipeline.fetch_aliases_for_vessel` (lazy import) |
| 1540 | GET | `/events/similar` | core | | `acquire_read`, `embeddings.embed_text` + `to_pgvector_literal` (lazy import) |
| 1627 | GET | `/lookup/asn` | lookups | | `lookup_asn` |
| 1642 | GET | `/entities/{entity_id}/cross_domain` | core | `entity_cross_domain` | `fetch_write` (heavy CTE; routed to write_pool per P1-A); regex-scanned by `test_cross_domain_endpoint.py:299` |
| 1799 | GET | `/event/{event_id}` | core | | `acquire_read` |
| 1872 | GET | `/signals/today` | signals | | `acquire_read`, `_signals_facts_for`, `_signals_authority_for`, `_SIGNALS_CATEGORY_ORDER` |
| 2039 | POST | `/analytics/event` | analytics | | `execute_write`, `_looks_like_email`, `_rate_limit` |
| 2084 | GET | `/analytics/summary` | analytics | | `fetch_read` |
| 2152 | POST | `/signals/subscribe` | signals | | `execute_write`, `_rate_limit`, `_looks_like_email`, `_RATE_BUCKETS`, `secrets.token_urlsafe` |
| 2226 | GET | `/signals/verify` | signals | | `execute_write` |
| 2255 | GET | `/signals/unsubscribe` | signals | | `execute_write` |
| 2275 | GET | `/dashboard/summary` | dashboard | | `fetch_read` |
| 2340 | GET | `/signals/timeline` | signals | | `acquire_read`, `_signals_facts_for` |
| 2417 | GET | `/signals/snapshot.csv` | signals | | `_CSV_COLUMNS`, `_signals_csv_row`, `_signals_to_csv` |
| 2491 | GET | `/signals.json` | signals | | `_signals_facts_for`, `_signals_authority_for` |
| 2626 | GET | `/signals.rss` | signals | | `_signals_rss_item`, `_rfc822`, `_rfc822_now`, `xml_escape` |

## Shared helpers at module top (stay in `api_v1.py` until the very end, or lift on demand)

| Symbol | Line | Used by | Notes |
|---|---|---|---|
| `_esc_label` | 261 | health_metrics + `_render_prometheus` | Used by metrics route + test_metrics_endpoint.py |
| `_render_prometheus` | 268 | health_metrics | Used by metrics route + test_metrics_endpoint.py |
| `_parse_bbox` | 391 | core | Used by /viewport |
| `_parse_types` | 409 | core | Used by /viewport |
| `_parse_iso` | 420 | core, alerts | Used by /viewport, /alerts/timeseries |
| `_coerce_jsonb` | 434 | core | Used by /entity/{id}, /event/{id}, query_viewport |
| `query_viewport` | 447 | core | Standalone pure-async; also imported by test_viewport_endpoint.py + glassbox_server.py:1331 |
| `query_entity_detail` | 646 | core | Standalone pure-async; also imported by test_entity_detail.py |
| `build_health_snapshot` | 54 | health_metrics | Imported by test_health_full.py + test_metrics_endpoint.py |

## Bottom-of-file helpers (move with `signals` cluster, group 8)

These are all used **only** by signals routes:

| Symbol | Line | Notes |
|---|---|---|
| `_looks_like_email` | 2835 | Also used by /analytics/event — TODO check during extraction; may need to lift |
| `_rate_limit` | 2846 | Decorator factory; used by signals routes + /analytics/event |
| `_RATE_BUCKETS` | (declared inside `_rate_limit`?) | Imported by test_signals_subscribe_endpoint.py |
| `_signals_facts_for` | 2920 | Used by /signals/today, /signals/timeline, /signals.json |
| `_signals_authority_for` | 3003 | Used by /signals/today, /signals.json |
| `_csv_escape` | 3037 | Used by `_signals_csv_row` |
| `_signals_csv_row` | 3052 | Used by /signals/snapshot.csv |
| `_signals_to_csv` | 3097 | Used by /signals/snapshot.csv |
| `_rfc822` / `_rfc822_now` | 3131 / 3142 | Used by /signals.rss |
| `_clip` | 3146 | Used by /signals.rss |
| `_signals_rss_item` | 3150 | Used by /signals.rss |
| `_CSV_COLUMNS` | TBD | Imported by test_signals_csv_endpoint.py |
| `_SIGNALS_CATEGORY_ORDER` | TBD | Imported by test_signals_page_live_wiring.py |

If `_looks_like_email` or `_rate_limit` is used by `/analytics/event` (POST,
group 2), it must be lifted to `web/_rate_limit.py` (already exists — Phase
1 lifted it for /api/intel + /api/glassbox use) or to a new
`web/_validation.py` for `_looks_like_email`. Lift before extracting group 2.

## How to validate after each extraction

1. `cd 21_GLASSBOX_AI && .venv/bin/python -m pytest tests/ -q --tb=line`
   → must report **1065 passed** (or +N if new tests landed).
2. `cd 21_GLASSBOX_AI && .venv/bin/python -m pytest tests/test_api_v1_routes_smoke.py -v`
   → 32-route manifest check must pass.
3. If daemon is running: `curl http://127.0.0.1:8790/openapi.json | jq '.paths | keys[]' | grep -E '/api/v1/(lookup|analytics|signals)'`
   → counts unchanged.

## How this doc gets updated

Update the cluster-mapping table + the "Done" column in the extraction
strategy table after each commit. Don't let the doc drift; the next
session reads it.
