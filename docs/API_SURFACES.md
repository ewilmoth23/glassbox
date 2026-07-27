# Glassbox API surfaces — `/api/v1/*`, `/api/intel/*`, and the rest

## TL;DR

Glassbox exposes several HTTP route prefixes from the same `glassbox_server.py` daemon. The two that look most similar — `/api/v1/*` and `/api/intel/*` — serve **different content**, not aliased content as the older backlog claim implied. This doc catalogs each surface, names its purpose, and identifies overlap so future operators don't think the duplicates are accidental.

## Surfaces

| Prefix | Purpose | Stability | Defined in |
|---|---|---|---|
| **`/api/v1/*`** | Canonical public versioned API. SSE streams, viewport queries, entity profiles, signals feed (RSS/JSON/CSV), Splink aliases, health, sources, analytics ingest. | Public — versioned, contract surface. Backwards-compat through v1.x. | `21_GLASSBOX_AI/api_v1.py` (router) + some in `glassbox_server.py` |
| **`/api/intel/*`** | Glassbox-cockpit-specific reshape layer for the **legacy** `glassbox.html` dashboard's intel panels. Each handler either reshapes shared in-memory state (`_latest_sitrep`) into panel-specific JSON, hand-crafts SQL for a specific panel, or stubs a not-yet-implemented capability. | Internal — driven by the cockpit's panel needs. Nothing external depends on it. May be retired with the legacy `glassbox.html` cockpit when the new `atlas.js` cockpit fully replaces it. | `21_GLASSBOX_AI/glassbox_server.py` |
| `/api/glassbox/*` | Pre-V2 internal alerts + SSE surface (`/api/glassbox/stream`, `/api/glassbox/sitrep/latest`). Predates `/api/v1/*` and `/api/intel/*` and still serves the legacy cockpit. | Internal-legacy. | `glassbox_server.py` |
| `/api/health` | Single health endpoint for launchd + monitoring. | Stable. | `glassbox_server.py` |
| `/api/sources` | Public sources catalog (which feeds are live, licensing posture). | Stable. | `glassbox_server.py` |
| `/api/intel/query` | LLM-backed natural-language intel query (Claude primary, Ollama fallback). Lives under `/api/intel/` for cockpit affinity but is genuinely its own surface. | Internal — rate-limited per IP. | `glassbox_server.py:3129` |

## `/api/intel/*` route catalog (2026-05-20)

Ten endpoints, all `GET` except `/api/intel/query` (POST):

| Endpoint | What it actually does | Reads from |
|---|---|---|
| `/api/intel/latest` | Latest sitrep summary reshaped for glassbox.html intel panels | `_latest_sitrep` in-memory |
| `/api/intel/anomalies` | Returns recent multi-juris / shadow-fleet / dark-vessel / sanctioned-rendezvous events | `event` table (hand-crafted SQL) |
| `/api/intel/predictions` | Stub — returns empty list. Forecasting lives elsewhere in the empire. | (none) |
| `/api/intel/threat-briefing` | Text payload from the brief publisher | `_latest_sitrep` |
| `/api/intel/alerts` | Recent tier-1 alerts as one-shot snapshot for cold-load rendering (mirrors what the SSE stream pushes) | `event` table |
| `/api/intel/alerts/poll` | Polling variant of `/api/intel/alerts` — same payload | calls `intel_alerts(limit)` |
| `/api/intel/confidence` | Confidence score for the current intel cycle | `_latest_sitrep` |
| `/api/intel/accuracy` | Static placeholder — accuracy tracking not implemented | hardcoded |
| `/api/intel/type/{intel_type}` | Typed intel dispatch. Returns `{type, generated_at, items[]}` envelope; item shape varies by type but JSON-stable for panel rendering | `_latest_sitrep` |
| `/api/intel/query` | Real-time AI intel query — Claude primary, Ollama fallback, grounded in `_hot_cache` globe state | `_hot_cache` + LLM call |

## Where `/api/intel/*` and `/api/v1/*` overlap (themes, not URLs)

These are conceptual overlaps — the data shows up in both surfaces but in different shapes for different consumers:

| Concept | `/api/intel/*` (cockpit shape) | `/api/v1/*` (public/versioned shape) |
|---|---|---|
| Latest cycle / brief | `/api/intel/latest`, `/api/intel/threat-briefing` | `/api/v1/brief/latest`, `/api/v1/recent-cycle` |
| Real-time alerts | `/api/intel/alerts`, `/api/intel/alerts/poll` | `/api/v1/signals/today`, SSE stream |
| Anomaly list | `/api/intel/anomalies` | `/api/v1/signals/snapshot.csv`, `/api/v1/signals.json`, `/api/v1/signals.rss` |
| LLM intel | `/api/intel/query` | (no public equivalent — internal-only) |

If you need the **public, versioned** shape, use `/api/v1/*`. If you're editing legacy cockpit code in `21_GLASSBOX_AI/landing/glassbox.html` (the old 2 MB dashboard, distinct from the new `atlas.js`-based landing), you'll likely need `/api/intel/*` shapes.

## What's NOT here

- There is **no formal aliasing layer** — neither prefix redirects to or wraps the other. Each handler reads its own data source and shapes its own response. Removing one surface would not break the other (modulo cockpits that have been wired to specific URLs).
- There is **no public consumer** of `/api/intel/*` outside of the legacy cockpit. Safe to evolve as the cockpit evolves; safe to retire when the legacy cockpit is retired.

## Future direction

When the legacy `glassbox.html` cockpit is fully retired in favor of the `atlas.js` cockpit (see `GLASSBOX_BUILD_PLAN_2026_05_18.md`), the `/api/intel/*` surface can be retired with it — the `atlas.js` cockpit reads from `/api/v1/*` directly. No external migration window needed since nothing external depends on `/api/intel/*`.

`/api/v1/*` continues forward and is the surface to evolve for any new public-facing capability.
