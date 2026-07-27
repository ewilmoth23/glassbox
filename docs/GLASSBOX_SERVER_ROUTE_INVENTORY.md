# `glassbox_server.py` Route Inventory

**Generated:** 2026-05-21 LATE (P3-H session)
**Source:** `21_GLASSBOX_AI/glassbox_server.py` @ commit `99e90f7`
**File size:** 3,814 lines / 170 KB
**Total decorated routes:** 81 (plus 2 lifecycle hooks: `@app.on_event("startup")` line 608, `@app.on_event("shutdown")` line 1440)

This is the pre-refactor snapshot for P3-H (god-file split). The smoke
test at `21_GLASSBOX_AI/tests/test_routes_smoke.py` introspects
`app.routes` at runtime; this doc is the human-readable map.

## Extraction strategy

Smallest concerns first. Each extraction is its own commit. The route
smoke test MUST run green after each commit; if it doesn't, revert.

| Order | Concern | Routes | Target file | Risk |
|---|---|---|---|---|
| 1 | `/network/*` | 2 | `web/routes/network.py` | Low — page handler + sibling JS, recently touched in P2-D so well-understood |
| 2 | `/signals/*` (page + feeds) | 4 | `web/routes/signals.py` | Low — page handler + RSS + JSON + embed |
| 3 | `/monitor/*` | 3 | `web/routes/monitor.py` | Low — page + JS + geojson |
| 4 | `/globe/*` | 2 | `web/routes/globe.py` | Low |
| 5 | Static assets | 14 | `web/routes/static.py` | Low — favicons, robots, sitemap, og-image |
| 6 | Landing + shell pages | 7 | `web/routes/pages.py` | Low — `/`, `/web`, `/glassbox`, `/markets`, `/pro`, `/console`, `/demo` |
| 7 | `/admin/*` + `/pricing` + `/entity/{id}` + `/status` | 4 | `web/routes/admin.py` | Low |
| 8 | Infrastructure layer endpoints | 5 | `web/routes/infrastructure.py` | Medium — `/api/v1/infrastructure/*` |
| 9 | `/api/glassbox/*` (legacy surface) | 21 | `web/routes/api_glassbox.py` | High — largest single group, watchlist + ask + state + diagnostic |
| 10 | `/api/intel/*` | 10 | `web/routes/api_intel.py` | High — alerts, predictions, query |
| 11 | `/api/briefings/*` + `/api/issues/*` | 5 | `web/routes/api_briefings.py` | Medium |
| 12 | Health + sources + markets singletons | 3 | `web/routes/api_misc.py` | Low |

After all 12 splits land, `glassbox_server.py` becomes a thin assembler
that imports routers, wires the `@app.on_event` lifecycle hooks,
configures middleware, and runs `uvicorn`.

## Full route list (by line number)

| Line | Method | Path | Group |
|---|---|---|---|
| 1389 | GET | `/api/v1/brief/latest` | api_v1 (rare; most are in `api_v1.py`) |
| 1408 | GET | `/api/v1/recent-cycle` | api_v1 |
| 1425 | GET | `/api/sources` | api_misc |
| 1478 | GET | `/favicon.svg` | static |
| 1492 | GET | `/favicon.ico` | static |
| 1501 | GET | `/api/v1/cesium-token` | api_v1 |
| 1523 | GET | `/atlas.js` | static |
| 1552 | GET | `/command.js` | static |
| 1567 | GET | `/globe` | globe |
| 1578 | GET | `/globe/globe.js` | globe |
| 1592 | GET | `/monitor` | monitor |
| 1604 | GET | `/monitor/monitor.js` | monitor |
| 1626 | GET | `/api/v1/satellites/tle` | api_v1 |
| 1688 | GET | `/track.js` | static |
| 1704 | GET | `/satellite.min.js` | static |
| 1719 | GET | `/satellites_worker.js` | static |
| 1735 | GET | `/admin/analytics` | admin |
| 1755 | GET | `/pricing` | admin |
| 1767 | GET | `/og-image.png` | static |
| 1796 | GET | `/robots.txt` | static |
| 1818 | GET | `/sitemap.xml` | static |
| 1849 | GET | `/api/v1/infrastructure/military-bases` | infrastructure |
| 1874 | GET | `/api/v1/infrastructure/nuclear` | infrastructure |
| 1900 | GET | `/api/v1/infrastructure/cables` | infrastructure |
| 1928 | GET | `/api/v1/infrastructure/trafficking` | infrastructure |
| 1957 | GET | `/api/v1/infrastructure/pipelines` | infrastructure |
| 1986 | GET | `/monitor/countries.geojson` | monitor |
| 2004 | GET | `/network` | **network (extraction #1)** |
| 2018 | GET | `/network/network.js` | **network (extraction #1)** |
| 2059 | GET | `/` | pages |
| 2112 | GET | `/web` | pages |
| 2138 | GET | `/glassbox` | pages |
| 2145 | GET | `/markets` | pages |
| 2153 | GET | `/pro` | pages |
| 2160 | GET | `/console` | pages |
| 2186 | GET | `/demo` | pages |
| 2203 | GET | `/signals/embed` | signals |
| 2234 | GET | `/signals` | signals |
| 2248 | GET | `/signals.rss` | signals |
| 2258 | GET | `/signals.json` | signals |
| 2273 | GET | `/entity/{entity_id}` | admin |
| 2295 | GET | `/status` | admin |
| 2309 | GET | `/api/health` | api_misc |
| 2326 | GET | `/health` | api_misc |
| 2331 | GET | `/api/glassbox/diagnostic` | api_glassbox |
| 2380 | GET | `/api/glassbox/layers` | api_glassbox |
| 2398 | GET | `/api/glassbox/layer/{name}` | api_glassbox |
| 2411 | GET | `/api/glassbox/entities` | api_glassbox |
| 2529 | POST | `/api/glassbox/sitrep/publish` | api_glassbox |
| 2564 | GET | `/api/glassbox/sitrep/latest` | api_glassbox |
| 2577 | GET | `/api/glassbox/state` | api_glassbox |
| 2590 | GET | `/api/intel/latest` | api_intel |
| 2604 | GET | `/api/intel/anomalies` | api_intel |
| 2649 | GET | `/api/intel/predictions` | api_intel |
| 2661 | GET | `/api/intel/threat-briefing` | api_intel |
| 2673 | GET | `/api/intel/alerts` | api_intel |
| 2731 | GET | `/api/intel/alerts/poll` | api_intel |
| 2737 | GET | `/api/intel/confidence` | api_intel |
| 2747 | GET | `/api/intel/accuracy` | api_intel |
| 2773 | POST | `/api/markets/edges/email-capture` | api_misc |
| 2802 | GET | `/api/intel/type/{intel_type}` | api_intel |
| 2890 | GET | `/api/glassbox/anomalies/latest` | api_glassbox |
| 2900 | GET | `/api/glassbox/correlations/latest` | api_glassbox |
| 2912 | POST | `/api/glassbox/watchlist` | api_glassbox |
| 2960 | GET | `/api/glassbox/watchlist` | api_glassbox |
| 2970 | GET | `/api/glassbox/watchlist/{wl_id}` | api_glassbox |
| 2993 | DELETE | `/api/glassbox/watchlist/{wl_id}` | api_glassbox |
| 3036 | POST | `/api/glassbox/ask` | api_glassbox |
| 3130 | POST | `/api/intel/query` | api_intel |
| 3238 | GET | `/api/glassbox/forecast/latest` | api_glassbox |
| 3273 | GET | `/api/glassbox/pro-status` | api_glassbox |
| 3310 | POST | `/api/glassbox/pro/activate` | api_glassbox |
| 3333 | POST | `/api/glassbox/pro/cancel` | api_glassbox |
| 3365 | POST | `/api/issues/report` | api_briefings |
| 3428 | GET | `/api/issues/open` | api_briefings |
| 3468 | GET | `/api/briefings/latest` | api_briefings |
| 3519 | GET | `/api/briefings/{slug}` | api_briefings |
| 3557 | GET | `/api/briefings/track-record/summary` | api_briefings |
| 3572 | GET | `/api/glassbox/news-manifest` | api_glassbox |
| 3687 | GET | `/api/glassbox/history/{layer}` | api_glassbox |
| 3735 | GET | `/api/glassbox/stream` | api_glassbox (SSE — special handling in smoke test) |

## Lifecycle hooks (NOT routes — extracted last)

| Line | Hook | Notes |
|---|---|---|
| 608 | `@app.on_event("startup")` | Pool init, broadcaster start, all the boot wiring |
| 1440 | `@app.on_event("shutdown")` | Pool close, graceful broadcaster stop |

These stay in `glassbox_server.py` until the final round. The startup
function is the most fragile single thing in the file — touching it
risks the daemon failing to boot. Save for last.

## What's intentionally NOT in this inventory

- Routes in `api_v1.py` (137 KB, 3,257 lines — own refactor, separate
  P3-H sub-task)
- Routes in `writers.py` (114 KB, 2,842 lines — pure writers, no HTTP
  routes; lives outside the web concern)
- MCP server routes (separate process, separate venv — see
  `21_GLASSBOX_AI/mcp_servers/README.md`)

## Verification

Route count + manifest assertion lives in
`21_GLASSBOX_AI/tests/test_routes_smoke.py`. If any route in the table
above disappears from `app.routes` without an explicit code change to
the manifest, the smoke test fails loudly.

Re-generate this table after any extraction:

```bash
grep -nE '^@app\.(get|post|put|delete|websocket)' \
  21_GLASSBOX_AI/glassbox_server.py | wc -l
# Expected to go DOWN by the size of each extracted group.
```
