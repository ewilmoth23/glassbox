# P3-H Phase 4 — Lifecycle + Broadcaster Extraction — Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans (or subagent-driven-development) to implement task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extract `_broadcast()`, the startup/shutdown lifecycle hooks, and the 5 background loops out of `glassbox_server.py` into a `runtime/` package, leaving the file a thin app-assembly shell — same runtime behavior, same startup ordering.

**Architecture:** A `Broadcaster` class owns the SSE/cache state and replaces the module-level `_broadcast` + its globals. `register_lifecycle(app)` wires the startup/shutdown hooks, constructs the broadcaster + ingesters, runs the license gate, and schedules the loops. Background loops + their status dicts move to `runtime/loops.py`; the 4 stray routes move to a router. Module-level state names are deleted last (bridge-then-delete).

**Tech Stack:** FastAPI (`@app.on_event` retained — NOT migrated to lifespan), asyncio, asyncpg. No new deps.

**Spec:** `21_GLASSBOX_AI/docs/PHASE4_LIFECYCLE_SCOPING.md` — read it first (coupling matrix + design forks).

---

## Pre-flight (read before starting)

- **Run in a FRESH, low-context session.** Startup ordering is fragile; this needs full attention.
- **Branch:** `glassbox-perf` (local commits only, never push — per project memory).
- **Read these first:** the scoping doc (§3 coupling matrix, §4 boundaries, §5 order), then re-read `glassbox_server.py` lines 514-604 (`_broadcast`), 614-923 (startup), 1483-1491 (shutdown), and skim the 5 loops (925-1442).
- **Verification has two gates:** (1) pytest green (1290 + 82 baseline) after every step — a strong import-time/wiring proof; (2) a **live daemon restart** at the end — startup-ordering bugs only surface on a real boot, NOT under TestClient. Per `[[reference-glassbox-server-daemon-boot]]`, the daemon restart is operator-side.
- **Relocation note:** loops, startup/shutdown bodies, and stray-route bodies move **verbatim** — this plan gives exact source line ranges + destination + the wiring changes rather than re-pasting hundreds of unchanged lines. Only genuinely-new code (the Broadcaster class, `register_lifecycle` signature, the safety-net test) is written out in full.

---

## Task 0: Safety-net smoke test

**Files:** Create `21_GLASSBOX_AI/tests/test_lifecycle_smoke.py`

- [ ] **Step 1: Write the smoke test (it passes against the CURRENT code — pins behavior before refactor)**

```python
"""Phase-4 safety net: pins lifecycle + broadcaster behavior so the
runtime/ extraction can be verified structurally. Passes against the
pre-refactor glassbox_server.py and must stay green through every step."""
import asyncio
import pytest
from fastapi.testclient import TestClient


def test_startup_populates_app_state(monkeypatch):
    # Disable the heavy/optional background work for a fast boot.
    for flag in ("GLASSBOX_PROXIMITY_DISABLED", "GLASSBOX_BRIEF_PUBLISHER_DISABLED",
                 "GLASSBOX_SPLINK_DISABLED", "GLASSBOX_EMBED_BACKFILL_DISABLED",
                 "GLASSBOX_EMBED_WARMUP_DISABLED"):
        monkeypatch.setenv(flag, "1")
    import glassbox_server as gs
    with TestClient(gs.app) as client:           # triggers startup + shutdown
        # ingesters were constructed + activated (or refused) and bridged
        assert hasattr(gs.app.state, "ingesters")
        assert isinstance(gs.app.state.ingesters, list)
        # the SSE/cache bridge is present
        for name in ("subscribers", "hot_cache", "entities_cache",
                     "layer_aliases", "subscriber_drops"):
            assert hasattr(gs.app.state, name), name
        # the 4 stray routes still answer
        assert client.get("/api/v1/recent-cycle").status_code == 200
        assert client.get("/api/v1/brief/latest").status_code in (200, 404)
        assert client.get("/api/v1/cesium-token").status_code in (200, 500)


def test_broadcast_delivers_to_subscriber_and_hot_cache():
    """A synthetic event pushed through the broadcast path lands in a
    subscribed queue AND the hot cache. Post-refactor this exercises
    Broadcaster.broadcast; pre-refactor it exercises _broadcast."""
    import glassbox_server as gs
    # Resolve the broadcast callable for either era.
    broadcast = getattr(gs, "_broadcast", None)
    if broadcast is None:
        broadcast = gs.app.state.broadcaster.broadcast  # post-refactor
    q = asyncio.Queue(maxsize=10)
    gs._subscribers_for_test_or_state().append(q) if hasattr(gs, "_subscribers_for_test_or_state") else None
    # Simplest cross-era hook: use the module/state subscriber list directly.
    subs = getattr(gs, "_subscribers", None) or gs.app.state.subscribers
    subs.append(q)
    try:
        from models import GlassboxEvent  # adjust import to the real event class
        ev = GlassboxEvent(layer="test_layer", **_minimal_event_kwargs())
        broadcast([ev])
        assert not q.empty()
        hot = getattr(gs, "_hot_cache", None) or gs.app.state.hot_cache
        assert len(hot["test_layer"]) >= 1
    finally:
        subs.remove(q)
```

> **Executor note:** `_minimal_event_kwargs()` + the `GlassboxEvent` import must be
> filled from the real model — grep `class GlassboxEvent` and copy the minimal
> required fields from an existing writer test (e.g. `tests/test_writers_*`). The
> dual-era `getattr` shims let the SAME test pass before and after the extraction.

- [ ] **Step 2: Run it against current code**

Run: `cd 21_GLASSBOX_AI && .venv/bin/python -m pytest tests/test_lifecycle_smoke.py -v`
Expected: PASS (pins current behavior).

- [ ] **Step 3: Commit**

```bash
git add 21_GLASSBOX_AI/tests/test_lifecycle_smoke.py
git commit -m "test(glassbox): Phase 4 safety net — lifecycle + broadcast smoke"
```

---

## Task 1: Broadcaster class → runtime/broadcaster.py

**Files:** Create `21_GLASSBOX_AI/runtime/__init__.py` (empty), `21_GLASSBOX_AI/runtime/broadcaster.py`; Modify `glassbox_server.py`

- [ ] **Step 1: Write the Broadcaster class**

Translate the body of `_broadcast` (glassbox_server.py:514-604) into a method, with
the 6 state globals becoming instance attributes:

```python
"""Broadcaster: owns the SSE fan-out + hot-cache + viewport-cache invalidation
state that every ingester's broadcast callback touches. Replaces the module-level
_broadcast() + its 6 globals in glassbox_server.py (P3-H Phase 4)."""
import asyncio
import logging
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List

log = logging.getLogger("glassbox-server")


class Broadcaster:
    def __init__(self, *, hot_cache_per_layer: int, layer_aliases: Dict[str, str],
                 entities_cache_ttl_sec: float, entities_bbox_quantum: float,
                 drop_limit: int = 50, drop_log_every: int = 10,
                 loop_bridge=None, loop_publisher=None):
        self.hot_cache: Dict[str, Deque] = defaultdict(lambda: deque(maxlen=hot_cache_per_layer))
        self.layer_aliases = layer_aliases
        self.entities_cache: Dict[tuple, tuple] = {}
        self.entities_cache_ttl_sec = entities_cache_ttl_sec
        self.entities_bbox_quantum = entities_bbox_quantum
        self.subscribers: List[asyncio.Queue] = []
        self.subscriber_drops: Dict[int, int] = {}
        self.drop_limit = drop_limit
        self.drop_log_every = drop_log_every
        self.loop_bridge = loop_bridge
        self.loop_publisher = loop_publisher

    def broadcast(self, event_or_events) -> None:
        # ── verbatim port of glassbox_server.py:525-604, with:
        #    _LOOP_BRIDGE      -> self.loop_bridge
        #    _LOOP_PUBLISHER   -> self.loop_publisher
        #    _hot_cache        -> self.hot_cache
        #    _LAYER_ALIASES    -> self.layer_aliases
        #    _entities_cache   -> self.entities_cache
        #    _subscribers      -> self.subscribers
        #    _subscriber_drops -> self.subscriber_drops
        #    _BROADCAST_DROP_LIMIT     -> self.drop_limit
        #    _BROADCAST_DROP_LOG_EVERY -> self.drop_log_every
        ...
```

> **Executor:** copy lines 525-604 verbatim into `broadcast()` and apply the
> name-mapping table above. No logic changes.

- [ ] **Step 2: Construct it in startup + pass the bound method as the ingester callback**

In `glassbox_server.py` startup hook, BEFORE `candidate_ingesters = [...]` (line 668),
construct the broadcaster and alias the callback:

```python
    from runtime.broadcaster import Broadcaster
    broadcaster = Broadcaster(
        hot_cache_per_layer=HOT_CACHE_PER_LAYER,
        layer_aliases=_LAYER_ALIASES,
        entities_cache_ttl_sec=_ENTITIES_CACHE_TTL_SEC,
        entities_bbox_quantum=_ENTITIES_BBOX_QUANTUM,
        loop_bridge=_LOOP_BRIDGE, loop_publisher=_LOOP_PUBLISHER,
    )
    app.state.broadcaster = broadcaster
    _broadcast = broadcaster.broadcast   # every ingester below captures this
```

Replace the `app.state` bridge block (855-879) to point at the broadcaster's
attributes (`app.state.subscribers = broadcaster.subscribers`, etc.) so existing
`app.state`-reading handlers keep working. Delete the old `_broadcast` function
(514-604) and the 6 now-owned module globals (`_hot_cache`, `_entities_cache`,
`_subscribers`, `_subscriber_drops`, drop consts) **only after** grep confirms no
other importer (the trilogy already moved `_deliver_to_subscribers` out).

- [ ] **Step 3: Verify**

Run: `cd 21_GLASSBOX_AI && .venv/bin/python -m pytest tests/test_lifecycle_smoke.py tests/test_routes_smoke.py -v` → PASS.
Then full suite: `/glassbox-test` → 1290 + 82 baseline.

- [ ] **Step 4: Commit**

```bash
git add 21_GLASSBOX_AI/runtime/ 21_GLASSBOX_AI/glassbox_server.py
git commit -m "refactor(glassbox): extract Broadcaster class to runtime/broadcaster.py"
```

---

## Task 2: Background loops → runtime/loops.py

**Files:** Create `21_GLASSBOX_AI/runtime/loops.py`; Modify `glassbox_server.py`

- [ ] **Step 1: Move the 5 loop functions + their status dicts**

Move **verbatim** to `runtime/loops.py`:
- `_RECENT_SCAN_CYCLE` dict (478-501), `_BRIEF_RECENT` (1161), `_LAST_SPLINK_RUN` (1167-1175), `_BRIEF_DIR` (1160)
- `_proximity_scan_loop` (925-1158), `_warm_embeddings` (1177-1209), `_embed_backfill_loop` (1211-1251), `_splink_er_loop` (1253-1352), `_brief_publisher_loop` (1354-1442)

Each loop currently reads other module globals (e.g. `_ingesters`, `_latest_sitrep`,
`log`, `init_pools`/`acquire`, writer fns). Pass what they need as function args from
`register_lifecycle` (Task 4), or have them read `app` (passed in) → `app.state`.
Keep the env-var disable flags identical. Expose the status dicts so the relocated
routes (Task 3) can read them via `app.state` pointers set in `register_lifecycle`.

> **Executor:** resolve each loop's free variables by grepping its body for module
> globals; the scoping-doc coupling matrix (§3) lists the writes. `_latest_sitrep`
> and `_RECENT_SCAN_CYCLE`/`_BRIEF_RECENT`/`_LAST_SPLINK_RUN` become `loops.py`
> module dicts; set `app.state.recent_scan_cycle = loops._RECENT_SCAN_CYCLE` etc. in
> `register_lifecycle`.

- [ ] **Step 2: Verify** — `/glassbox-test` → baseline green; smoke test green.
- [ ] **Step 3: Commit** — `refactor(glassbox): move 5 background loops to runtime/loops.py`

---

## Task 3: Stray routes → web/routes/runtime_status.py

**Files:** Create `21_GLASSBOX_AI/web/routes/runtime_status.py`; Modify `glassbox_server.py`

- [ ] **Step 1: Move the 4 stray routes to an APIRouter**

Move `/api/v1/brief/latest` (1444-1460), `/api/v1/recent-cycle` (1463-1476),
`/api/v1/cesium-token` (1508-1538), `/api/v1/satellites/tle` (1543-1631) into a
`router = APIRouter()` in `runtime_status.py`. The first two read `_BRIEF_RECENT` /
`_RECENT_SCAN_CYCLE` → change to `request.app.state.brief_recent` /
`request.app.state.recent_scan_cycle` (pointers set in Task 4). Mount with the other
routers near glassbox_server.py:404: `app.include_router(runtime_status_router)`.

- [ ] **Step 2: Verify** — smoke test (the 4 routes still answer) + `test_routes_smoke.py` (count floor may need +0; these routes were already in the manifest) + full suite.
- [ ] **Step 3: Commit** — `refactor(glassbox): move 4 stray runtime routes to web/routes/runtime_status.py`

---

## Task 4: Lifecycle hooks → runtime/lifecycle.py

**Files:** Create `21_GLASSBOX_AI/runtime/lifecycle.py`; Modify `glassbox_server.py`

- [ ] **Step 1: Move startup + shutdown into register_lifecycle(app)**

```python
def register_lifecycle(app, *, loop_components, ingester_factories) -> None:
    """Wire @app.on_event startup/shutdown. Constructs the Broadcaster, builds +
    gates + activates ingesters, populates app.state, schedules loops. Preserves
    the exact ordering: pools -> ingesters -> gate -> app.state -> loops."""
    @app.on_event("startup")
    async def _startup():
        ...   # verbatim from glassbox_server.py:615-923, using loop_components +
              # the Broadcaster from Task 1, scheduling loops imported from runtime.loops
    @app.on_event("shutdown")
    async def _shutdown():
        for ing in app.state.ingesters:
            ing.stop()
        try:
            await close_pools()
        except Exception as e:
            log.warning(f"[db] close_pool error on shutdown: {type(e).__name__}: {e}")
```

In `glassbox_server.py`, after the router mounts, replace the inline hooks with:
```python
from runtime.lifecycle import register_lifecycle
register_lifecycle(app, loop_components=..., ingester_factories=...)
```

> **Executor:** the ingester `candidate_ingesters` list (668-795) moves into
> `register_lifecycle` (it references the broadcaster). Keep the import-time Loop
> block (183-318) OR move it to `runtime/loop_init.build_loop_components()` and call
> it inside `register_lifecycle` before ingester construction (still pre-ingester →
> ordering preserved). The license gate (`SourcesRegistry.load`, `gate_ingester`)
> moves with it.

- [ ] **Step 2: Verify** — smoke test + full suite green. **Confirm startup ordering** by reading the moved hook: pools → ingesters → gate → app.state bridge → loops, unchanged.
- [ ] **Step 3: Commit** — `refactor(glassbox): move lifecycle hooks to runtime/lifecycle.py`

---

## Task 5: Delete module-level state + final cleanup

**Files:** Modify `glassbox_server.py`

- [ ] **Step 1: Remove now-dead module globals + the import-time Loop block (if moved)**

Grep-confirm no remaining readers, then delete: any leftover `_broadcast` alias, the
6 broadcaster globals (if not already removed in Task 1), the loop status dicts (now
in loops.py), `_ingesters` (now `app.state.ingesters`). Run `node`-equivalent: a
fresh pytest boot must still pass.

- [ ] **Step 2: Verify final shape** — `wc -l glassbox_server.py` (~150-200 lines), `grep -c "@app\." glassbox_server.py` → 0 inline routes/hooks (only `app.include_router` + `register_lifecycle`). Full suite green.
- [ ] **Step 3: Commit** — `refactor(glassbox): drop dead module state; glassbox_server.py is a thin shell`

---

## Task 6: Live verification + docs

- [ ] **Step 1: Operator daemon restart** (per `[[reference-glassbox-server-daemon-boot]]`). After restart confirm: `/api/v1/health/full` ingesters active; SSE `/stream` delivers; `briefs/latest.md` republishes within the hour; `/api/v1/recent-cycle` populates after ~5 min; no ImportError/ordering errors in `/tmp/glassbox-server.log`.
- [ ] **Step 2: Update** `PHASE4_LIFECYCLE_SCOPING.md` status → Done with commit SHAs; add a CHANGELOG entry; update CLAUDE.md (P3-H Phase 4 closed → the whole god-module quartet is factored).

---

## Self-review (completed at write time)

**Spec coverage:** scoping §4 boundaries → Tasks 1-4 (broadcaster/loops/routes/lifecycle); §5 order (bridge-then-delete) → Task ordering + Task 5; §6 risks (ordering, import-time Loop block, callback identity) → Task 0 smoke + Task 4 note + Task 1 Step 2; §6 verification (pytest + live restart) → every task + Task 6. No gaps.

**Placeholder scan:** the two "fill from real model" notes (smoke-test `GlassboxEvent` kwargs; loop free-var resolution) are flagged executor-resolution points with the exact grep to run, not lazy placeholders — they depend on code the executor will have open. Verbatim-move steps cite exact source line ranges rather than re-pasting unchanged code (stated in pre-flight).

**Name consistency:** `Broadcaster`, `broadcaster.broadcast`, `register_lifecycle`, `runtime/{broadcaster,loops,lifecycle,loop_init}.py`, `web/routes/runtime_status.py`, `app.state.{broadcaster,ingesters,recent_scan_cycle,brief_recent}` — consistent across scoping doc + all tasks.
