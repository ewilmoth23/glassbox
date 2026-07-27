# P3-H Phase 4 — Lifecycle + Broadcaster Extraction — Scoping

- **Date:** 2026-05-27
- **Status:** **SCOPED ONLY — not executed.** Per CLAUDE.md, Phase 4 is the riskiest
  extraction (startup ordering matters) and should be executed in a **fresh,
  low-context session** from the companion plan `PHASE4_LIFECYCLE_PLAN.md`.
- **Target:** `21_GLASSBOX_AI/glassbox_server.py` (1,650 lines) → extract the lifecycle
  hooks, `_broadcast()`, and the 5 background loops into a new `runtime/` package,
  leaving `glassbox_server.py` as a thin app-assembly + router-mount shell.
- **Predecessor:** the P3-H trilogy (Phase 1 routes, Phase 2 api_v1, Phase 3 writers)
  is done. This is the final god-module surface. Inventory docs for the trilogy:
  `GLASSBOX_SERVER_ROUTE_INVENTORY.md`, `API_V1_ROUTE_INVENTORY.md`, `WRITERS_INVENTORY.md`.

---

## 1. Why this is the hard one

The route/writer extractions moved **stateless** handlers. Phase 4 moves the
daemon's **stateful core**:

- `_broadcast()` is the callback that **all ~33 ingesters hold** (passed as
  `broadcaster=_broadcast` at construction in the startup hook). It mutates a web
  of module-level mutable state on every ingester cycle.
- The `@app.on_event("startup")` hook has **strict ordering**: DB pools must come
  up before ingesters (they dual-write), the license gate must run before
  activation, the `app.state` bridge must be populated after `_ingesters` fills,
  and the background loops are scheduled last.
- Five background `asyncio` loops + a few stray routes read/write the same module
  state, so moving any one piece in isolation risks cross-module module-state
  imports — the exact anti-pattern the trilogy's `app.state` bridge was built to
  avoid.

**This is structural, not behavioral.** Goal: same runtime behavior, same startup
ordering, same env-var flags — just relocated into focused modules. **Explicitly
out of scope:** migrating `@app.on_event` → the newer `lifespan` context manager
(it changes ordering semantics; tempting but a separate, riskier change).

---

## 2. Current-state inventory (what lives in glassbox_server.py today)

| Region | Lines | What it is |
|---|---|---|
| Loop module-init block | 183-318 | Sets `_LOOP_CLASSIFIER/_BRIDGE/_PUBLISHER/_REGISTRY/_SUBSCRIBER` at **import time** (try/except). Opt-in via `GLASSBOX_LOOP_ENABLED`. |
| `app = FastAPI(...)` + CORS | 341-432 | App construction + middleware. |
| Router mounts | 373-404 | 2× `build_v1_router` + 12 extracted routers (trilogy Phase 1/2). **Stays.** |
| Broadcaster state | 434-511 | `_hot_cache`, `_LAYER_ALIASES`, `_entities_cache` (+TTL/quantum consts), `_subscribers`, `_subscriber_drops` (+drop consts), `_ingesters`, `_started_at`, `_RECENT_SCAN_CYCLE`, `_latest_sitrep`. |
| `_broadcast()` | 514-604 | The ingester callback. Mutates hot_cache/aliases/entities_cache/subscribers/subscriber_drops; tees to Loop bridge+publisher. |
| `@app.on_event("startup")` | 614-923 | Pools → construct ~33 ingesters → sources-registry gate → activate (`create_task(ing.run_forever())`) → optional registry refresh → `app.state` bridge (855-879) → schedule 5 loops (each behind a `GLASSBOX_*_DISABLED` flag). |
| `_proximity_scan_loop()` | 925-1158 | Writes `_RECENT_SCAN_CYCLE`. |
| `_BRIEF_RECENT` / `_LAST_SPLINK_RUN` state | 1160-1175 | Loop status dicts. |
| `_warm_embeddings()` | 1177-1209 | Off-loop ST model warm. |
| `_embed_backfill_loop()` | 1211-1251 | NULL-embedding drain. |
| `_splink_er_loop()` | 1253-1352 | Writes `_LAST_SPLINK_RUN`. |
| `_brief_publisher_loop()` | 1354-1442 | Writes `_BRIEF_RECENT` + `_latest_sitrep`. |
| Stray `@app.get` routes | 1444-1476, 1508-1631 | `/api/v1/brief/latest` (reads `_BRIEF_RECENT`), `/api/v1/recent-cycle` (reads `_RECENT_SCAN_CYCLE`), `/api/v1/cesium-token`, `/api/v1/satellites/tle`. |
| `@app.on_event("shutdown")` | 1483-1491 | `ing.stop()` for each `_ingesters` + `close_pools()`. |

## 3. Shared-state coupling matrix (the crux)

| State | Owner / writer | Other readers |
|---|---|---|
| `_hot_cache`, `_LAYER_ALIASES`, `_entities_cache`, `_subscribers`, `_subscriber_drops` (+consts) | `_broadcast()` | bridged to `app.state.*`; consumed by `/api/glassbox/*` + `/api/v1/*` handlers (already via `app.state`) |
| `_LOOP_BRIDGE`, `_LOOP_PUBLISHER`, `_LOOP_CLASSIFIER`, `_LOOP_REGISTRY` | import-time block (183-318) | `_broadcast()` (bridge/publisher tee), startup (classifier→ingesters, registry→refresh loop) |
| `_ingesters` | startup (`.append`) | shutdown (`.stop()`), `app.state.ingesters` |
| `_RECENT_SCAN_CYCLE` | `_proximity_scan_loop` | `/api/v1/recent-cycle` route |
| `_BRIEF_RECENT` | `_brief_publisher_loop` | `/api/v1/brief/latest` route |
| `_latest_sitrep` | `_brief_publisher_loop` (+ intel loop) | `app.state.latest_sitrep` |
| `_LAST_SPLINK_RUN` | `_splink_er_loop` | `/api/v1/health/full` (via app.state, future) |

**Key insight:** the `app.state` additive bridge (startup lines 855-879) already
exists for the SSE/cache state — the trilogy built it. Phase 4 *flips ownership*:
that state moves into a `Broadcaster` object (and a small runtime-state holder),
the broadcaster + loops read from there, and the module-level names get deleted
**last** — the same bridge-then-delete discipline the trilogy used.

---

## 4. Proposed module boundaries — `runtime/` package

```
21_GLASSBOX_AI/runtime/
  __init__.py
  broadcaster.py   # Broadcaster class: owns hot_cache, aliases, entities_cache,
                   #   subscribers, subscriber_drops + drop consts, loop bridge/
                   #   publisher refs. Exposes .broadcast(events). Replaces the
                   #   module-level _broadcast + its 6 state globals.
  loops.py         # the 5 background loops (proximity / brief-publisher / splink-er /
                   #   embed-backfill / warm-embeddings) + their status dicts
                   #   (_RECENT_SCAN_CYCLE, _BRIEF_RECENT, _LAST_SPLINK_RUN). Each
                   #   loop reads shared state via the args it's handed (or app.state).
  lifecycle.py     # register_lifecycle(app): wires @app.on_event("startup") +
                   #   ("shutdown"); constructs Broadcaster + ingesters; runs the
                   #   license gate; schedules loops; populates app.state. Called once
                   #   from glassbox_server.py after the routers are mounted.
  loop_init.py     # (optional) the import-time Loop block (183-318) → a
                   #   build_loop_components() returning a small dataclass; keeps the
                   #   side-effectful import block out of the top of glassbox_server.py
```

**Stray routes** (`/brief/latest`, `/recent-cycle`, `/cesium-token`,
`/satellites/tle`): fold into a small `web/routes/runtime_status.py` router (reading
`app.state`) so glassbox_server.py keeps **zero** inline `@app.*`. This finishes the
"thin shell" goal the trilogy started (Phase 1 left these 4 behind).

**End-state `glassbox_server.py`:** imports, `app = FastAPI(...)` + CORS, router
mounts, `build_loop_components()` call, `register_lifecycle(app)` call. ~150-200
lines. No inline routes, no `_broadcast`, no loops, no lifecycle bodies.

### Design forks (resolve at execution time, all low-stakes)

1. **Broadcaster ownership of Loop refs.** The bridge/publisher are set at import
   time but consumed by `_broadcast`. Cleanest: `build_loop_components()` returns
   them; `register_lifecycle` injects them into the `Broadcaster` constructor.
   (Alternative: broadcaster reads them off `app.state` — more indirection.)
   **Recommendation:** constructor injection.
2. **Loop status dicts location.** `_RECENT_SCAN_CYCLE` etc. can live as
   module-level dicts in `loops.py` (read by the relocated routes via `app.state`
   pointers set in `register_lifecycle`) — mirrors today's pattern exactly.
   **Recommendation:** keep as `loops.py` module dicts + `app.state` pointers.
3. **One `lifecycle.py` vs split startup/shutdown files.** Shutdown is 8 lines;
   not worth its own file. **Recommendation:** single `lifecycle.py`.

---

## 5. Extraction order (safest first — bridge-then-delete)

0. **Safety net.** Add `tests/test_lifecycle_smoke.py`: boots the app under the test
   harness (TestClient triggers startup/shutdown), asserts `app.state.ingesters` is
   populated, `app.state.broadcaster` exists, the 4 stray routes respond, and a
   synthetic `broadcast([event])` lands in a subscribed queue + hot_cache. Keep the
   existing `test_routes_smoke.py` green throughout.
1. **Broadcaster class** → `runtime/broadcaster.py`. Construct in startup; pass
   `broadcaster.broadcast` as the ingester callback; set `app.state.broadcaster`.
   Keep a module-level `_broadcast = app.state.broadcaster.broadcast`-style alias if
   anything still imports it (grep first — the trilogy already moved
   `_deliver_to_subscribers` out, so likely nothing does).
2. **Background loops** → `runtime/loops.py`. Move the 5 loop functions + status
   dicts. `register_lifecycle` imports + schedules them with the same env-var flags.
3. **Stray routes** → `web/routes/runtime_status.py`, mounted with the others.
4. **Lifecycle hooks** → `runtime/lifecycle.py::register_lifecycle(app)`. Move the
   startup + shutdown bodies verbatim; glassbox_server.py calls it once.
5. **Delete module-level state names** once all readers use `app.state` / injected
   refs. Final grep to confirm no stragglers.

---

## 6. Risks + verification

**Risks:**
- **Startup ordering.** pools → ingesters → gate → `app.state` bridge → loops MUST
  be preserved exactly. The smoke test (Step 0) + a live daemon boot catch breakage.
- **Import-time Loop block side effects.** Moving 183-318 changes *when* it runs;
  keep it import-time-equivalent (call `build_loop_components()` at module load or
  inside `register_lifecycle` before ingester construction — the latter is cleaner
  and still pre-ingester).
- **The ingester callback identity.** All ingesters capture whatever object is
  passed at construction; ensure the Broadcaster exists before the
  `candidate_ingesters = [...]` list is built.

**Verification (pytest alone is necessary but NOT sufficient here):**
- `node`-style import proof: pytest boots the app in a fresh interpreter every run →
  strong import-time + wiring proof. Run the full suite (1290 + 82 baseline).
- **Live daemon restart REQUIRED** (operator-side, per
  `[[reference-glassbox-server-daemon-boot]]`): after restart, confirm
  `/api/v1/health/full` shows ingesters active, SSE `/stream` delivers events,
  `briefs/latest.md` republishes, `/api/v1/recent-cycle` populates after ~5 min.
  Startup-ordering bugs only surface on a real boot, not under TestClient.

---

## 7. Effort + sequencing

~5-8h across the 5 steps + safety net. Best done **first thing** in a fresh
session (low context, full attention on ordering). Each step is one commit; pytest
green between each; the live daemon restart is the final gate before marking done.
