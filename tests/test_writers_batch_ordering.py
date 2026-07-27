"""
P1-B regression suite — vessel/aircraft/satellite writers must sort their
batches by (canonical_id, ts) before the per-event loop so that any two
concurrent writers from different upstream feeds process shared
canonical_ids in the same order, eliminating the cross-writer
INSERT-ON-CONFLICT-DO-UPDATE deadlock seen on the entity table.

Root cause (from postgresql@17.log analysis, 2026-05-20):
  ERROR:  deadlock detected
  DETAIL: Process 60341 waits for ShareLock on transaction 273719;
          blocked by process 59982.
          Process 59982 waits for ShareLock on transaction 273720;
          blocked by process 60341.
  Both transactions running INSERT INTO entity ON CONFLICT DO UPDATE
  on overlapping MMSIs in opposite orders.

Pre-fix rate: 406 deadlocks across 12.7 days (~32/day, ~1.3/hour).

These tests are hermetic — they mock the asyncpg connection rather than
hitting the real DB, so they're fast (<50ms total) and don't depend on
P0-F.1's glassbox_test isolation. The contract being pinned is purely
about the order in which events flow into the per-event loop.
"""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from ingesters.base import GlassboxEvent  # noqa: E402
from writers import _sort_batch_for_upsert  # noqa: E402
import writers as writers_module  # noqa: E402

# Phase 3: writers are now per-cluster modules. To monkey-patch the
# `acquire_write` symbol that each writer body resolves, patch on the
# cluster module's own namespace (not the top-level `writers` package),
# because `from db import acquire_write` at the cluster module's top
# captures the binding at import time.
import writers.aircraft as _writers_aircraft  # noqa: E402
import writers.vessel as _writers_vessel  # noqa: E402
import writers.satellite as _writers_satellite  # noqa: E402
# Note: sanction_entities is still inline in
# writers/__init__.py at the time of this commit, so they remain
# patchable via writers_module. As each one extracts, update its
# patch site to writers.<cluster>.


# ─── Helpers ──────────────────────────────────────────────────────────────


def _mk_vessel_event(mmsi: str, *, ts_offset_s: int = 0,
                     lat: float = 0.0, lng: float = 0.0) -> GlassboxEvent:
    """Build a minimum-viable GlassboxEvent for the vessel writer.

    Keep payload tight — the writer only reads `name`, `mmsi`, and the
    flat lng/lat/velocity/heading on the event. Anything else is just
    properties-merge noise that doesn't affect the ordering contract."""
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    ts = (base + timedelta(seconds=ts_offset_s)).isoformat()
    return GlassboxEvent(
        layer="ships",
        kind="position",
        ts=ts,
        external_id=mmsi,
        lat=lat,
        lng=lng,
        severity=3,
        payload={"mmsi": mmsi, "name": f"TEST_{mmsi}"},
        velocity_ms=5.0,
        heading_deg=90.0,
    )


def _mk_aircraft_event(icao24: str, *, ts_offset_s: int = 0) -> GlassboxEvent:
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    ts = (base + timedelta(seconds=ts_offset_s)).isoformat()
    return GlassboxEvent(
        layer="planes",
        kind="position",
        ts=ts,
        external_id=icao24,
        lat=40.0,
        lng=-74.0,
        severity=2,
        payload={"callsign": f"TST{icao24[-3:]}"},
        velocity_ms=200.0,
        heading_deg=180.0,
        altitude_m=10000.0,
    )


def _mk_satellite_event(norad: str) -> GlassboxEvent:
    return GlassboxEvent(
        layer="satellites",
        kind="position",
        ts="2026-05-20T12:00:00+00:00",
        external_id=norad,
        lat=0.0,
        lng=0.0,
        severity=1,
        payload={"name": f"SAT-{norad}", "norad": norad},
        velocity_ms=7800.0,
        altitude_m=400000.0,
    )


# ─── Tests on the pure sort helper ────────────────────────────────────────


def test_sort_batch_sorts_by_canonical_id_ascending():
    """Trivial correctness — sorted by external_id alphabetically."""
    e = [_mk_vessel_event("999000003"),
         _mk_vessel_event("999000001"),
         _mk_vessel_event("999000002")]
    out = _sort_batch_for_upsert(e)
    assert [x.external_id for x in out] == ["999000001", "999000002", "999000003"]


def test_sort_batch_uses_ts_as_tiebreaker_for_same_canonical_id():
    """Multi-position-same-MMSI batches process chronologically.

    Two events with same external_id but different ts — earlier ts comes
    first. The entity row's GREATEST/CASE guards mean this is advisory,
    not strictly required, but processing in temporal order is cleaner
    and matches the on-disk timeline of position_track."""
    early = _mk_vessel_event("999000007", ts_offset_s=0)
    late = _mk_vessel_event("999000007", ts_offset_s=60)
    out = _sort_batch_for_upsert([late, early])
    # Same MMSI; the earlier ts should come first.
    assert out[0].ts == early.ts
    assert out[1].ts == late.ts


def test_sort_batch_is_stable_for_already_sorted_input():
    """Sorting an already-sorted input yields the same order."""
    e = [_mk_vessel_event("999000001"),
         _mk_vessel_event("999000002"),
         _mk_vessel_event("999000003")]
    out = _sort_batch_for_upsert(e)
    assert [x.external_id for x in out] == [x.external_id for x in e]


def test_sort_batch_handles_empty_input():
    """Edge case — an empty batch returns an empty list, no crash."""
    assert _sort_batch_for_upsert([]) == []


def test_sort_batch_with_missing_external_id_sorts_to_end():
    """An event with external_id=None sorts to the tail (sentinel U+FFFD)
    because such events are skipped inside the per-event loop anyway —
    the sort just needs to handle the None gracefully rather than crash."""
    valid = _mk_vessel_event("999000001")
    broken = GlassboxEvent(
        layer="ships",
        kind="position",
        ts="2026-05-20T12:00:00+00:00",
        external_id=None,
        lat=0.0, lng=0.0,
        severity=3,
        payload={},
    )
    out = _sort_batch_for_upsert([broken, valid])
    assert out[0] is valid       # valid IDs sort before None
    assert out[1] is broken


def test_sort_batch_does_not_mutate_input():
    """sorted() returns a new list — verify the caller's list is untouched.
    Important because writers may share batch references with other code."""
    e = [_mk_vessel_event("999000003"),
         _mk_vessel_event("999000001")]
    original_order = [x.external_id for x in e]
    _sort_batch_for_upsert(e)
    assert [x.external_id for x in e] == original_order


# ─── Tests on the writers actually invoking the sort ──────────────────────


class _MockConn:
    """Records the canonical_id of every INSERT INTO entity, then no-ops
    everything else. Returns a fresh UUID per fetchval so the writer's
    downstream INSERT INTO position_track can use it as entity_id."""

    def __init__(self):
        self.entity_inserts: List[str] = []

    async def fetchval(self, sql: str, *args, **kwargs):
        if "INSERT INTO entity" in sql:
            # arg[0] is the canonical_id (external_id) in every entity writer.
            self.entity_inserts.append(args[0])
        return uuid.uuid4()

    async def execute(self, *args, **kwargs):
        return "INSERT 0 1"

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield self
        return _tx()


def _mock_acquire_factory(conn: _MockConn):
    """Build an async-context-manager-style factory that yields the mock
    conn, mirroring db.acquire()'s shape."""
    @asynccontextmanager
    async def _acquire():
        yield conn
    return _acquire


@pytest.mark.asyncio
async def test_write_vessel_events_inserts_in_sorted_order(monkeypatch):
    """If a writer receives [C, A, B], it should INSERT INTO entity in
    order [A, B, C]. This is the contract that prevents the
    cross-writer batch-ordering deadlock from re-emerging."""
    conn = _MockConn()
    # Vessel writer extracted to writers/vessel.py in P3-H Phase 3 (#22).
    monkeypatch.setattr(_writers_vessel, "acquire_write", _mock_acquire_factory(conn))

    # Reverse-sorted input — worst case for the deadlock scenario.
    batch = [_mk_vessel_event("999000003"),
             _mk_vessel_event("999000001"),
             _mk_vessel_event("999000002")]
    n = await writers_module.write_vessel_events(batch)
    assert n == 3
    assert conn.entity_inserts == ["999000001", "999000002", "999000003"]


@pytest.mark.asyncio
async def test_write_aircraft_events_inserts_in_sorted_order(monkeypatch):
    """Same contract as vessels — aircraft writer must sort too because
    adsb_lol or other ADS-B sources could exhibit the same overlapping-batch
    behavior across concurrent ingester instances."""
    conn = _MockConn()
    # Aircraft writer extracted to writers/aircraft.py in P3-H Phase 3 (#21).
    # Patch site is the cluster module, not the top-level package.
    monkeypatch.setattr(_writers_aircraft, "acquire_write", _mock_acquire_factory(conn))

    batch = [_mk_aircraft_event("ABC003"),
             _mk_aircraft_event("ABC001"),
             _mk_aircraft_event("ABC002")]
    n = await writers_module.write_aircraft_events(batch)
    assert n == 3
    assert conn.entity_inserts == ["ABC001", "ABC002", "ABC003"]


@pytest.mark.asyncio
async def test_write_satellite_events_inserts_in_sorted_order(monkeypatch):
    """Defensive — satellite ingester runs from one source today (CelesTrak)
    so the deadlock risk is theoretical, but the sort is free and
    consistency across writers is the contract."""
    conn = _MockConn()
    # Satellite writer extracted to writers/satellite.py in P3-H Phase 3 (#23).
    monkeypatch.setattr(_writers_satellite, "acquire_write", _mock_acquire_factory(conn))

    batch = [_mk_satellite_event("25544"),  # ISS
             _mk_satellite_event("20580"),  # Hubble
             _mk_satellite_event("48274")]  # Tiangong
    n = await writers_module.write_satellite_events(batch)
    assert n == 3
    # NORADs are numeric strings; lexical sort happens to match numeric here.
    assert conn.entity_inserts == ["20580", "25544", "48274"]


@pytest.mark.asyncio
async def test_writer_skips_event_with_no_external_id_after_sort(monkeypatch):
    """An event with external_id=None must still be sorted (to end) AND
    skipped inside the loop. The writer's `if not ev.external_id: continue`
    guard handles the skip — this test pins both halves: sort tolerates
    None, loop drops it, sorted real events still land."""
    conn = _MockConn()
    monkeypatch.setattr(_writers_vessel, "acquire_write", _mock_acquire_factory(conn))

    valid_a = _mk_vessel_event("999000001")
    valid_b = _mk_vessel_event("999000002")
    broken = GlassboxEvent(
        layer="ships",
        kind="position",
        ts="2026-05-20T12:00:00+00:00",
        external_id=None,
        lat=0.0, lng=0.0,
        severity=3,
        payload={},
    )
    n = await writers_module.write_vessel_events([broken, valid_b, valid_a])
    assert n == 2  # broken was dropped
    assert conn.entity_inserts == ["999000001", "999000002"]
