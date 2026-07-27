"""
writers.py surface smoke test — P3-H Phase 3 safety net for the
`writers.py` god-module refactor.

`writers.py` has no HTTP routes and no classes, so the introspect-routes
mechanism that test_routes_smoke.py + test_api_v1_routes_smoke.py used
in Phase 1 + Phase 2 doesn't apply. The closest analogs:

1. `test_public_writer_manifest_complete` — the hardcoded 24-name
   manifest is fully importable as attributes of `writers`. Catches the
   "extraction silently dropped a writer" failure.

2. `test_writer_is_async_coroutine_function` (parametrized) — each
   writer is `async def`. Catches accidental sync-conversion or
   `functools.partial` wrapping.

3. `test_writer_empty_list_returns_zero` (parametrized) — universal
   contract: every writer's `if not events: return 0` early-return
   path must remain intact. Catches signature regressions AND any
   accidental top-level side-effect (e.g. the writer trying to acquire
   a DB pool before checking emptiness). Runs without a Postgres
   fixture because the empty-list path never touches `acquire_write`.

4. `test_test_coupled_private_symbol_present` (parametrized) — every
   private symbol that an existing test file reaches into for must
   remain importable from `writers`. Catches refactors that
   move symbols deeper without re-exporting.

5. `test_glassbox_server_import_block_resolves` — the 24-symbol
   multi-line `from writers import (...)` block in glassbox_server.py
   must succeed. Resolves all 24 names at production-import time.

This file is the load-bearing safety net for Phase 3 extractions. After
each extraction commit, this test MUST pass unchanged. If it doesn't,
revert.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_writers_smoke.py -v
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import writers as writers_module  # noqa: E402


# ---------------------------------------------------------------------------
# Manifest of every public writer in `writers.py`.
#
# Source of truth: docs/WRITERS_INVENTORY.md (2026-05-27).
# When a writer is intentionally added or removed, update BOTH this list
# AND the inventory doc.
# ---------------------------------------------------------------------------
EXPECTED_WRITERS = (
    # ENTITY+POSITION shape (4) — UPSERT entity + INSERT position_track,
    # require _sort_batch_for_upsert
    "write_aircraft_events",
    "write_vessel_events",
    "write_satellite_events",
    "write_sanction_entities",  # NOTE: lacks the _events suffix the other 23 carry
    # EVENT-INTO-EVENT-TABLE shape (20)
    "write_seismic_events",
    "write_emsc_quake_events",
    "write_natural_event_events",
    "write_wildfire_events",
    "write_volcanic_events",
    "write_gdacs_events",
    "write_news_events",
    "write_gdelt_bulk_events",
    "write_newsdata_events",
    "write_hn_events",
    "write_sec_filing_events",
    "write_social_events",
    "write_weather_alert_events",
    "write_tropical_storm_events",
    "write_space_weather_events",
    "write_donki_events",
    "write_metar_events",
    "write_aqi_events",
    "write_neo_events",
    "write_fema_events",
    # P2-A Phase 1 MVP (cyber-attack data layers, 2026-05-27)
    "write_cisa_kev_events",
    "write_spamhaus_drop_events",
    # P2-B Phase 1.5 (live-ingester upgrade for the climate_forecast static layer)
    "write_open_meteo_forecast_events",
    "write_noaa_ndbc_events",
)

# Private symbols reached into by existing test files. The refactor must
# keep these importable from `writers` (re-export if moved).
TEST_COUPLED_PRIVATE_SYMBOLS = (
    # test_writers_confidence.py
    "_with_confidence",
    "_LAYER_TO_PLATFORM",
    # test_writers_batch_ordering.py
    "_sort_batch_for_upsert",
)

# Additional shared helpers — not directly imported by tests today, but
# they're cross-writer and the Phase 3 plan lifts them to a shared module.
# Pinning their continued availability as top-level `writers` attributes
# guards against accidental deletion during the lift.
PRIVATE_HELPERS_TO_PRESERVE = (
    "_maybe_embed",
    "_parse_ts",
    "_EVENT_UUID_NAMESPACE",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_public_writer_manifest_complete():
    """All 24 expected writers must be importable from `writers`."""
    found = [n for n in EXPECTED_WRITERS if hasattr(writers_module, n)]
    missing = set(EXPECTED_WRITERS) - set(found)
    assert not missing, (
        f"Missing writers in `writers` module: {sorted(missing)}\n"
        f"All 24 must remain importable across Phase 3 extractions."
    )


def test_public_writer_count_floor():
    """The count of `write_*` callables in `writers` must be ≥ 24.

    A floor (not exact match) so future writer additions don't trigger
    a refactor-safety-net failure. Lowering this number means an
    extraction dropped a writer, which is a regression."""
    write_attrs = [
        n for n in dir(writers_module)
        if n.startswith("write_") and callable(getattr(writers_module, n))
    ]
    assert len(write_attrs) >= len(EXPECTED_WRITERS), (
        f"Found {len(write_attrs)} writers in `writers`, expected ≥ {len(EXPECTED_WRITERS)}. "
        f"Present: {sorted(write_attrs)}"
    )


@pytest.mark.parametrize("name", EXPECTED_WRITERS)
def test_writer_is_async_coroutine_function(name):
    """Every public writer must be an `async def` (coroutine function)."""
    fn = getattr(writers_module, name, None)
    assert fn is not None, f"writers.{name} missing"
    assert inspect.iscoroutinefunction(fn), (
        f"writers.{name} is not an async function "
        f"(found {type(fn).__name__})"
    )


@pytest.mark.parametrize("name", EXPECTED_WRITERS)
def test_writer_signature_accepts_single_list_arg(name):
    """Every writer's signature must be `(events: ...) -> ...` — exactly
    one required positional/keyword arg. The runtime type isn't pinned
    here (would require pulling all 24 List[GlassboxEvent] annotations)
    but the arity contract is."""
    fn = getattr(writers_module, name)
    sig = inspect.signature(fn)
    params = [
        p for p in sig.parameters.values()
        if p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
    ]
    required = [p for p in params if p.default is inspect.Parameter.empty]
    assert len(required) == 1, (
        f"writers.{name} has {len(required)} required positional args, "
        f"expected exactly 1 (events). Signature: {sig}"
    )


@pytest.mark.parametrize("name", EXPECTED_WRITERS)
async def test_writer_empty_list_returns_zero(name):
    """Universal contract: every writer must return 0 on `[]` input
    without DB activity. This is the load-bearing assertion of the
    smoke test — proves the writer evaluates, the signature matches,
    and no top-level side-effects break on the trivial input path.

    All 24 writers have an `if not events: return 0` guard at the top
    of their body (verified via grep at 24 of 24 sites in writers.py
    @ commit f106b05). The contract is pre-existing and load-bearing
    for the SSE-broadcast-first architecture."""
    fn = getattr(writers_module, name)
    result = await fn([])
    assert result == 0, (
        f"writers.{name}([]) returned {result!r}, expected 0. "
        f"The `if not events: return 0` guard must remain at the top "
        f"of every writer."
    )


@pytest.mark.parametrize("name", TEST_COUPLED_PRIVATE_SYMBOLS)
def test_test_coupled_private_symbol_present(name):
    """test_writers_confidence.py and test_writers_batch_ordering.py
    reach into `writers` for these private symbols. Every Phase 3
    extraction MUST keep them importable from `writers` (re-export
    them if they move to a sub-module)."""
    assert hasattr(writers_module, name), (
        f"writers.{name} missing — would break existing test imports. "
        f"If this symbol moved to a sub-module during refactor, add "
        f"`from writers.<sub> import {name}` to writers/__init__.py."
    )


@pytest.mark.parametrize("name", PRIVATE_HELPERS_TO_PRESERVE)
def test_shared_helper_remains_importable(name):
    """Helpers shared across multiple writer clusters must remain
    importable from top-level `writers` so the `_shared.py` lift
    doesn't accidentally remove the back-compat surface."""
    assert hasattr(writers_module, name), (
        f"writers.{name} missing. The `_shared.py` lift planned for "
        f"the first Phase 3 commit must keep top-level `writers` aliases."
    )


def test_glassbox_server_import_block_resolves():
    """The 24-symbol `from writers import (...)` block at
    `glassbox_server.py:58` must succeed. This is the production
    import path; if it breaks, the daemon won't start."""
    # Import as a module-level side-effect — simulates the production
    # import path. The `noqa: F401` markers are intentional: we want
    # the names bound so we can compare identity below.
    from writers import (  # noqa: F401
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
        write_social_events,
        write_newsdata_events,
        write_donki_events,
        write_metar_events,
        write_aqi_events,
        write_neo_events,
        write_sec_filing_events,
        # P2-A Phase 1 MVP (cyber-attack data layers, 2026-05-27)
        write_cisa_kev_events,
        write_spamhaus_drop_events,
        # P2-B Phase 1.5 (live-ingester upgrade)
        write_open_meteo_forecast_events,
        write_noaa_ndbc_events,
    )

    # Each name imported from the package must be the same object as
    # the corresponding attribute on the module — catches "shim
    # accidentally created a wrapper that diverges from the real fn".
    local = locals()
    for name in EXPECTED_WRITERS:
        from_module = getattr(writers_module, name)
        from_import = local[name]
        assert from_module is from_import, (
            f"`from writers import {name}` returned a different object "
            f"than `writers.{name}`. Likely the package __init__ wrapped "
            f"the function instead of re-exporting it."
        )


# NOTE: the prior `test_acquire_write_remains_top_level_attribute` was
# dropped 2026-05-27 after Phase 3 close-out. test_writers_batch_ordering.py
# was migrated to patch each cluster module's own namespace directly
# (e.g., `import writers.aircraft as _writers_aircraft; monkeypatch.setattr(
# _writers_aircraft, "acquire_write", ...)`) so the top-level shim
# `from db import acquire_write` in writers/__init__.py is no longer
# needed. Removing the shim makes writers/__init__.py purely a re-export
# shell for the 24 public writers + the 6 _shared helpers.
