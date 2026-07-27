# `writers.py` Inventory (P3-H Phase 3)

**Generated:** 2026-05-27 (P3-H Phase 3 session)
**Source:** `21_GLASSBOX_AI/writers.py` @ commit `f106b05` (Phase 2 closeout)
**File size:** 2,842 lines / 121 KB
**Public writer functions:** 24

This is the pre-refactor snapshot for P3-H **Phase 3** (writers.py god-module
split). The smoke test at `21_GLASSBOX_AI/tests/test_writers_smoke.py` pins
the public surface; this doc is the human-readable map.

## Structural difference from Phase 1 + Phase 2

| Phase | Target | Shape | Smoke test mechanism |
|---|---|---|---|
| 1 | `glassbox_server.py` (81 routes) | Module-level `@app.get(...)` decorators | Introspect `app.routes` at runtime |
| 2 | `api_v1.py` (32 routes) | `build_router(prefix, tag)` factory with nested `@router.get(...)` | Mount the factory, introspect `app.routes` for both prefixes |
| **3** | **`writers.py` (24 functions)** | **Flat module of `async def write_*_events()` functions; NO classes; NO routes** | **Symbol-table introspection + signature shape + empty-list contract** |

`writers.py` has no HTTP surface to introspect. The closest analog is to
pin **(a)** the public symbol set, **(b)** the test-coupled private symbol
set, **(c)** every writer's `(events) -> int` signature contract, and
**(d)** the universal `if not events: return 0` early-return path (which
proves the function evaluates without DB activity on the trivial input).

All 24 writers share an identical signature shape — `async def write_X(events: List[GlassboxEvent]) -> int` — and every one of them starts with `if not events: return 0` (verified via grep at 24 of 24 call sites). This is what makes the smoke test possible without database fixtures: every writer must accept `[]` and return `0` synchronously after the guard, regardless of the refactor that follows.

## The writer contract (per the module docstring)

> - Input: a list of `GlassboxEvent` objects (ALREADY post-dedup, post-classify)
> - Side effect: UPSERT/INSERT into the appropriate table(s) per shape
> - Output: integer count of NEW rows persisted (re-runs of the same input return 0 if dedup catches them)
> - Errors are logged but do not raise — DB downtime must NEVER break the SSE broadcast pipeline

## Two writer shapes

Per the module docstring header, every writer falls into one of two shapes:

### ENTITY + POSITION (entity that moves over time)

UPSERT into `entity` table on canonical id (icao24, mmsi, NORAD ID, OFAC entity_id) AND INSERT into `position_track` table for the snapshot. Requires `_sort_batch_for_upsert()` to prevent cross-writer ON-CONFLICT deadlocks (P1-B, 2026-05-20). Four writers:

| Writer | Canonical id | Table(s) | Source |
|---|---|---|---|
| `write_aircraft_events` | icao24 | entity + position_track | OpenSky / ADS-B Exchange |
| `write_vessel_events` | mmsi | entity + position_track | AISStream / Digitraffic / Helcom |
| `write_satellite_events` | norad_id | entity + position_track | SGP4 over Celestrak TLE |
| `write_sanction_entities` | ofac_entity_id (UID) | entity (list-membership; NOT position-track) | OFAC SDN / EU CFSP / UK OFSI |

**Naming inconsistency:** `write_sanction_entities` lacks the `_events` suffix
that the other 23 writers carry. This is deliberate (sanctions are entities,
not events) but breaks the regex `^async def write_\w+_events` — anything
introspecting writers must name it explicitly. Future cleanup
candidate, but renaming risks breaking 4 test files; defer unless
extraction forces the issue.

### EVENT-INTO-EVENT-TABLE (point-in-time happening)

INSERT into the `event` hypertable with a deterministic UUID derived from `(event_type, external_id)` via `_EVENT_UUID_NAMESPACE`. 20 writers; no entity-table side effect.

(See helper-usage matrix below for the full list.)

## Public writer manifest (24)

Source: every `async def write_*` at module top.

```
write_aircraft_events            write_news_events            write_metar_events
write_vessel_events              write_gdelt_bulk_events      write_aqi_events
write_satellite_events           write_weather_alert_events   write_neo_events
write_seismic_events             write_space_weather_events   write_sec_filing_events
write_emsc_quake_events          write_hn_events              write_social_events
write_natural_event_events       write_gdacs_events           write_newsdata_events
write_wildfire_events            write_tropical_storm_events  write_donki_events
write_volcanic_events            write_fema_events            write_sanction_entities
```

**Caller surface:** `glassbox_server.py:58` imports 24 of 24 in a single
multi-line `from writers import (…)` block. 21 test files import individual
writers by name. **Every extraction must keep all 24 names re-exported from
`writers` (whether the module stays as a module or becomes a package).**

## Helper-usage matrix

`SORT` = `_sort_batch_for_upsert`; `CONF` = `_with_confidence`;
`EMBED` = `_maybe_embed`; `TS` = `_parse_ts`; `UUID` = `_EVENT_UUID_NAMESPACE`.

| Writer | SORT | CONF | EMBED | TS | UUID |
|---|:---:|:---:|:---:|:---:|:---:|
| `write_aircraft_events`         | ✓ |   |   | ✓ |   |
| `write_vessel_events`           | ✓ |   |   | ✓ |   |
| `write_satellite_events`        | ✓ | ✓ |   | ✓ |   |
| `write_sanction_entities`       | ✓ |   |   | ✓ |   |
| `write_seismic_events`          |   | ✓ |   | ✓ | ✓ |
| `write_wildfire_events`         |   | ✓ |   | ✓ | ✓ |
| `write_natural_event_events`    |   | ✓ |   | ✓ | ✓ |
| `write_emsc_quake_events`       |   | ✓ |   | ✓ | ✓ |
| `write_news_events`             |   | ✓ | ✓ | ✓ | ✓ |
| `write_gdelt_bulk_events`       |   | ✓ | ✓ | ✓ | ✓ |
| `write_weather_alert_events`    |   | ✓ |   | ✓ | ✓ |
| `write_space_weather_events`    |   | ✓ |   | ✓ | ✓ |
| `write_hn_events`               |   | ✓ | ✓ | ✓ | ✓ |
| `write_gdacs_events`            |   | ✓ |   | ✓ | ✓ |
| `write_tropical_storm_events`   |   | ✓ |   | ✓ | ✓ |
| `write_volcanic_events`         |   | ✓ |   | ✓ | ✓ |
| `write_fema_events`             |   |   |   | ✓ | ✓ |
| `write_social_events`           |   | ✓ |   | ✓ | ✓ |
| `write_newsdata_events`         |   | ✓ | ✓ | ✓ | ✓ |
| `write_donki_events`            |   | ✓ |   | ✓ | ✓ |
| `write_metar_events`            |   | ✓ |   | ✓ | ✓ |
| `write_aqi_events`              |   | ✓ |   | ✓ | ✓ |
| `write_neo_events`              |   | ✓ |   | ✓ | ✓ |
| `write_sec_filing_events`       |   |   | ✓ | ✓ | ✓ |

**Observations:**
- `_parse_ts` is universal (24 of 24). Strongest lift candidate.
- `_EVENT_UUID_NAMESPACE` is in 20 of 24 (every event-table writer; the 4 entity-position writers compute canonical ids without it).
- `_sort_batch_for_upsert` is exactly the 4 ENTITY+POSITION writers (matches the P1-B fix scope precisely).
- `_with_confidence` is 21 of 24. Missing from: aircraft + vessel (P3-N coverage gap; their `confidence_score` is injected at ingest time, not writer time) and fema + sec_filing (writer-time coverage gap — `fema_declarations` and `securities_filings` ARE mapped in `_LAYER_TO_PLATFORM` but the writer doesn't call `_with_confidence`; pre-existing gap, not refactor scope).
- `_maybe_embed` is exactly the 5 text-heavy writers (news, gdelt_bulk, hn, newsdata, sec_filing).

## Module-level state to migrate

| Symbol | Type | Public? | Consumers |
|---|---|:---:|---|
| `_EVENT_UUID_NAMESPACE` | UUID constant | private | 20 event-table writers |
| `_LAYER_TO_PLATFORM` | dict[str, str] | private — but **test-imported** | `_with_confidence` + `test_writers_confidence.py` |
| `_CONFIDENCE_OK` | bool flag | private | `_with_confidence` (guard) |
| `_score_event` | imported fn-or-None | private | `_with_confidence` (guarded call) |
| `_embed_text` | imported fn-or-None | private | `_maybe_embed` (guard) |
| `_to_pgvec` | imported fn-or-None | private | `_maybe_embed` (guard) |
| `_log` | logging.Logger | private | currently only `writers.aircraft`-named; other writers create their own loggers inline |
| `_OSINT_HIGH_SEVERITY` | set[str] | private (line 2172) | `_bluesky_subtype` only (social_events) |

## Test-coupling map

The smoke test must keep ALL of the following importable from `writers`
(re-export if moved during extraction):

| Test file | Symbol(s) imported from `writers` |
|---|---|
| `test_writers_confidence.py` | `_with_confidence`, `_LAYER_TO_PLATFORM` |
| `test_writers_batch_ordering.py` | `_sort_batch_for_upsert` (+ monkey-patches `acquire_write` on `writers as writers_module`) |
| `test_planes_dual_write.py` | `write_aircraft_events` |
| `test_ships_dual_write.py` | `write_vessel_events` |
| `test_satellites_dual_write.py` | `write_satellite_events` |
| `test_seismic_dual_write.py` | `write_seismic_events` |
| `test_emsc_dual_write.py` | `write_emsc_quake_events`, `write_seismic_events` |
| `test_eonet_dual_write.py` | `write_natural_event_events` |
| `test_news_dual_write.py` | `write_news_events` |
| `test_wildfires_dual_write.py` | `write_wildfire_events` |
| `test_weather_alert_dual_write.py` | `write_weather_alert_events` |
| `test_noaa_swpc.py` | `write_space_weather_events` |
| `test_hacker_news.py` | `write_hn_events` |
| `test_gdacs.py` | `write_gdacs_events` |
| `test_nhc_storms.py` | `write_tropical_storm_events` |
| `test_usgs_volcano.py` | `write_volcanic_events` |
| `test_openfema.py` | `write_fema_events` |
| `test_eu_cfsp.py`, `test_uk_ofsi.py`, `test_sanction_dual_write.py` | `write_sanction_entities` |
| `test_phase2_round2_dual_write.py` | bulk import — social, newsdata, donki, metar, aqi, neo, sec_filing |

**Special hazard — `test_writers_batch_ordering.py`:** monkey-patches
`writers_module.acquire_write` directly. If the refactor moves the
`acquire_write` import out of the top-level `writers` namespace (e.g.
inside individual cluster modules only), this test fails. The test is
already aware (existing comment block); the safety net pins this via
the import-resolution smoke. **Extractions must keep `acquire_write` as a
top-level `writers` attribute** — easiest path is `from db import
acquire_write` in `writers/__init__.py` even after the inner modules
move.

## Proposed cluster shape (STARTING FRAME, not contract)

Phases 1 + 2 began with an inventory + smoke-test commit and then
adjusted the cluster shape across extractions. Phase 3 should do the
same. The frame below is a starting hypothesis based on the helper-usage
matrix — to be refined commit by commit.

**The likely target layout:**

```
21_GLASSBOX_AI/writers/
    __init__.py             # re-exports all 24 write_* + test-coupled privates
    _shared.py              # _parse_ts, _maybe_embed, _with_confidence,
                            # _sort_batch_for_upsert, _LAYER_TO_PLATFORM,
                            # _EVENT_UUID_NAMESPACE
    positions.py            # aircraft, vessel, satellite (ENTITY+POSITION)
    sanctions.py            # sanction_entities (also ENTITY+POSITION shape
                            # but isolated by source — OFAC/UK/EU — and by
                            # the entity-not-events naming)
    seismic.py              # seismic, emsc_quake
    geo_events.py           # wildfires, natural_events (EONET), volcanic, gdacs
    weather.py              # weather_alerts, tropical_storms, fema, metar, aqi
    space.py                # space_weather (SWPC), donki, neo
    news.py                 # news, gdelt_bulk, newsdata, hn (text+embed)
    sec.py                  # sec_filing (text+embed, but distinct domain)
    social.py               # social_events (bluesky)
```

10 cluster modules + 1 shared. This is roughly:
- ~150 lines `_shared.py`
- ~510 lines positions
- ~270 lines sanctions
- ~280 lines seismic
- ~430 lines geo_events
- ~510 lines weather
- ~340 lines space
- ~570 lines news
- ~75 lines sec
- ~120 lines social

(Lines are estimates from `wc -l` regions; will firm up during extraction.)

**Alternative groupings worth considering during extraction:**

1. **Merge sec into news** — both are text+embed; sec_filing is small. Argument against: sec_filing is sole-source SEC EDGAR vs news being multi-source GDELT-derived; mixing them obscures the domain boundary.
2. **Merge social into news** — bluesky_events is text-derived. Argument against: bluesky has its own subtype-classifier (`_bluesky_subtype` + `_OSINT_HIGH_SEVERITY`) that doesn't fit news/sec patterns.
3. **Merge donki/swpc** — space_weather (SWPC) and donki (DONKI/CME) both come from NOAA SWPC. Argument for: same upstream provider; small modules; would unify.
4. **Split positions into per-domain** — `aircraft.py` / `vessel.py` / `satellite.py` separately. Argument for: each is ~150-200 lines and uses different canonical-id semantics. Argument against: they share `_sort_batch_for_upsert` and the same UPSERT-into-entity SQL shape; keeping them together preserves the pattern visibility.

Defer until first 2-3 extractions land and the pattern is clearer.

## Lift-first candidates (Option-A pattern from Phase 1/2)

The same Phase 1/2 pattern applies: lift shared infrastructure to its
own module BEFORE extracting the first cluster that consumes it. Order
of operations:

1. **First commit (this one):** inventory + smoke test. No extractions.
2. **Second commit:** lift `_shared.py` — all 6 cross-cutting symbols
   (`_parse_ts`, `_maybe_embed`, `_with_confidence`, `_sort_batch_for_upsert`,
   `_LAYER_TO_PLATFORM`, `_EVENT_UUID_NAMESPACE`). Keep top-level aliases
   in `writers.py` so the existing in-file writer bodies + the
   `test_writers_*.py` imports keep working unchanged.
3. **Extraction commits:** pull cluster modules one at a time, ordered
   smallest → largest, with the same shim-vs-no-shim decision matrix
   that Phase 2 codified (see `API_V1_ROUTE_INVENTORY.md` § Shim
   Placement Decision Matrix). Most cluster modules will use **no shim**
   because writers don't read from each other — the test-coupling layer
   is the only re-export concern.
4. **Final commit:** convert `writers.py` to `writers/__init__.py` —
   becomes a thin re-export shell exposing all 24 + the test-coupled
   private symbols.

## Shim placement decision matrix (forecast)

Unlike Phase 2 (where cluster modules had to import each other through
the `build_router` factory), writers are independent of each other.
The expected decision matrix simplifies:

| Pattern | When to use | Forecast frequency |
|---|---|---|
| **Top-of-file re-export in `writers/__init__.py`** | Test imports the writer or a test-coupled private symbol | Every extraction (re-export of `write_X_events`) |
| **No shim needed** | Symbol is module-private and only consumed within its own cluster | Most per-writer helpers (e.g. `_aircraft_entity_properties`) |
| **Bottom-of-file shim** | Test reaches in for a symbol whose source module imports from `writers` at its top | None expected (writers don't import from each other) |

The `writers/__init__.py` will lead with re-exports rather than putting
them at the bottom — this matches Python's package convention and
matches what Phase 1's `_assets.py` did. Bottom-shims are a Phase 2
artifact of cluster-to-cluster imports inside the factory.

## Test-coupled symbols to re-export from `writers/__init__.py`

To keep the 21 test files working unchanged across all extractions, the
new `writers/__init__.py` must expose:

```python
# Top-level writer functions (24)
from writers.positions import (write_aircraft_events, write_vessel_events,
                                write_satellite_events)
from writers.sanctions import write_sanction_entities
from writers.seismic import write_seismic_events, write_emsc_quake_events
# ... (the rest)

# Test-coupled private helpers (4 symbols across 2 test files)
from writers._shared import (_with_confidence, _LAYER_TO_PLATFORM,
                              _sort_batch_for_upsert)
from db import acquire_write  # for test_writers_batch_ordering monkey-patch
```

## Daemon restart consideration (deferred)

Phase 1's final extraction (#9 `/api/glassbox/*`) landed 2026-05-22 NIGHT.
Phase 2 finished 2026-05-27 LATE NIGHT. Pytest exercises the new module
structure end-to-end on every run, but the production daemon at PID
38504 is still serving Phase 1's structure pre-Phase 2 (the import
graph hasn't been live-loaded since well before Phase 2). Pytest-only
verification has held for 5 days; before Phase 3 begins making structural
assumptions, a deferred-since-Phase-2 daemon restart is worth doing —
`launchctl kickstart` or the `nohup` boot pattern per the
`[[glassbox-server-daemon-boot]]` memory, followed by `curl
http://localhost:8790/health` + `curl http://localhost:8790/api/v1/health/full`
to confirm both `api_v1` and the Phase 1 route extractions activate
cleanly.

## Open questions deferred to extraction time

1. **`write_fema_events` confidence gap.** Pre-existing — `fema_declarations` is mapped in `_LAYER_TO_PLATFORM` but the writer doesn't invoke `_with_confidence`. Pure pre-existing bug, not refactor scope, but the extraction commit for `weather.py` could fix it as a one-liner. Defer decision.
2. **`write_sec_filing_events` confidence gap.** Same as above. `securities_filings` is mapped; writer doesn't invoke. Defer decision.
3. **Inconsistent `_log` usage.** `_log = logging.getLogger("writers.aircraft")` is module-top but each writer creates its own logger inline. Cleanup candidate during the `_shared.py` lift — pick one pattern (per-cluster logger or module-top default).
4. **Optional dependencies guard at module top.** The `try: from embeddings…` and `try: from confidence_scorer…` blocks at module top will need to move to `_shared.py`. Verify both still gracefully degrade (CI doesn't load these by default).

## Progress scoreboard

| State | writers/__init__.py lines | Public writers re-exported | Pytest baseline |
|---|---|---|---|
| **Phase 3 close (24 of 24)** | 2,842 → 124 (-2,718, -95.6%) | 24 of 24 | 1179 passed / 1 skipped |
| **Post-cleanup (commit `b8dc1f9`)** | **124 → 78 (-46, total -97.3%)** | 24 of 24 | 1178 passed / 1 skipped |

The post-cleanup commit (`b8dc1f9` 2026-05-27 LATE NIGHT) dropped:
- The `from db import acquire_write` re-export (back-compat surface
  no longer needed — `test_writers_batch_ordering.py` now patches
  each cluster module directly via `import writers.<cluster> as ...`)
- 5 dead imports left over from pre-Phase-3 (`json`, `logging`, `uuid`,
  `typing.List`, `ingesters.base.GlassboxEvent` — all only used by
  writer bodies that moved to cluster modules)
- Long stretch of blank lines (37 → 2)
- `tests/test_writers_smoke.py::test_acquire_write_remains_top_level_attribute`
  (the assertion that pinned the back-compat surface during refactor;
  served its purpose, dropped post-completion)

Pytest count: 1179 → 1178 (the -1 is the intentionally-dropped
back-compat assertion). No other regressions; full suite green.

### Per-extraction commits (in order)

| # | Cluster | Module | Commit | __init__.py after |
|---|---|---|---|---|
| 1 | aqi (no-shim, simple) | `writers/aqi.py` | `70bfcce` | 2587 |
| 2 | metar | `writers/metar.py` | `978617e` | 2496 |
| 3 | neo | `writers/neo.py` | `f0ca756` | 2400 |
| 4 | donki | `writers/donki.py` | `711f33f` | 2308 |
| 5 | sec_filing (first EMBED) | `writers/sec.py` | `191c2ba` | 2221 |
| 6 | gdacs | `writers/gdacs.py` | `c581f9e` | 2131 |
| 7 | volcanic | `writers/volcanic.py` | `21f604e` | 2028 |
| 8 | fema | `writers/fema.py` | `ef9665d` | 1919 |
| 9 | wildfire | `writers/wildfire.py` | `168d303` | 1820 |
| 10 | eonet/natural_events | `writers/eonet.py` | `55b6a69` | 1720 |
| 11 | seismic | `writers/seismic.py` | `ead6753` | 1629 |
| 12 | emsc_quake (50% milestone) | `writers/emsc.py` | `2088b70` | 1524 |
| 13 | weather_alert (first WHERE NOT EXISTS dedup) | `writers/weather_alert.py` | `f749319` | 1420 |
| 14 | space_weather | `writers/space_weather.py` | `6aca71a` | 1318 |
| 15 | tropical_storm | `writers/tropical_storm.py` | `6e4f63a` | 1213 |
| 16 | hn | `writers/hn.py` | `d3fc71c` | 1121 |
| 17 | newsdata | `writers/newsdata.py` | `4625a77` | 1016 |
| 18 | social/bluesky (1-cluster privates) | `writers/social.py` | `72b48df` | 920 |
| 19 | news (GDELT topical) | `writers/news.py` | `d51dae4` | 808 |
| 20 | gdelt_bulk | `writers/gdelt_bulk.py` | `8d70048` | 681 |
| 21 | aircraft (first ENTITY+POSITION) | `writers/aircraft.py` | `4ecc2f5` | 530 |
| 22 | vessel | `writers/vessel.py` | `d6a2a19` | 380 |
| 23 | satellite | `writers/satellite.py` | `51c8879` | 238 |
| 24 | **sanctions (FINAL 🎉)** | `writers/sanctions.py` | `b0f2d9c` | **124** |

### Pattern observations from the 24 extractions

1. **No-shim pattern fits every cluster.** Unlike Phase 2 which required
   3 lift-then-extract pairs + top/bottom shim placement decisions, every
   Phase 3 cluster was self-contained — no cross-cluster imports needed.
   The single import block at the end of `writers/__init__.py` is the
   only coupling.

2. **The Option-A lift (commit `8e554a8`) was essential up-front.**
   Doing all 6 cross-cutting helpers in one prep commit before any
   extraction simplified every subsequent cluster — they all do
   `from writers._shared import _EVENT_UUID_NAMESPACE, _parse_ts,
   _with_confidence` and (when text-heavy) `_maybe_embed`. No
   per-extraction Option-A negotiations.

3. **Test patch-site coupling caught during aircraft extraction (#21).**
   `test_writers_batch_ordering.py` monkey-patches `acquire_write` on
   the writer's module — extracting the writer to a sub-module breaks
   the patch if the test keeps patching `writers.acquire_write`.
   Fixed at extraction time: each entity-position extraction (#21, #22, #23)
   adds an `import writers.<cluster> as _writers_<cluster>` and updates
   the patch site. Smoke test's `test_acquire_write_remains_top_level_attribute`
   pins symbol presence but NOT patch-propagation semantics — a useful
   distinction to know for future refactors.

4. **The 4 ENTITY+POSITION writers each landed as their own commit**
   rather than batching into one `positions.py` — keeping them separate
   was simpler than merging (each ~150 lines, each with its own canonical
   id type, each with subtly different UPSERT SQL). Splitting also
   meant the broken aircraft test could be fixed without affecting
   the not-yet-extracted vessel/satellite/sanction writers.

5. **`write_sanction_entities` naming inconsistency preserved.** It's
   the only writer without an `_events` suffix; renaming would break
   3 test files + glassbox_server.py imports. Deferred indefinitely
   (low value, high risk).

## Daemon restart consideration

Phase 3's structure is verified under pytest (1179 passed across all
24 extractions). The production daemon at PID 49684 still serves the
pre-Phase-3 module graph. Restart pattern (per the corrected
`[[glassbox-server-daemon-boot]]` memory):

```bash
cd 21_GLASSBOX_AI
kill <PID> && sleep 3 && \
  nohup ./.venv/bin/python glassbox_server.py > /tmp/glassbox-server.log 2>&1 & \
  disown
```

After restart, `curl localhost:8790/health` should report a fresh
`started_at` and ≥26/27 ingesters healthy.

## Open questions (still deferred)

1. **`write_fema_events` confidence gap.** Pre-existing — `fema_declarations`
   IS mapped in `_LAYER_TO_PLATFORM` but the writer's `_event_properties`
   call chain doesn't hit `_with_confidence`. Wait — actually fema's
   extraction (#8) preserved the original behavior; on re-review,
   the `_fema_event_properties` helper DOES call `_with_confidence(out, event.layer)`
   at the end. The inventory's pre-extraction matrix may have been
   wrong on this. Quick verification: `grep _with_confidence writers/fema.py`
   shows the call. Flag as resolved/false-alarm.
2. **`write_sec_filing_events` confidence gap.** Same — `_sec_event_properties`
   in `writers/sec.py` does call `_with_confidence`. Also a false alarm
   from the original inventory's mechanical grep (which looked at
   `_with_confidence` calls in writer bodies, not in property helpers
   called by the writer body).
3. **Drop test-only re-export shims in api_v1.py (Phase 2 cleanup).** Still
   open — the post-Phase-2 leftover `_RATE_BUCKETS` and `_CSV_COLUMNS`
   aliases in api_v1.py exist purely for legacy test imports. Could
   rewrite ~10 test files to import from the new module paths directly,
   then delete the shims. ~30 min cleanup, low priority.
