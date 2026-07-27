"""
Integration test for Phase 1.1 — planes.py dual-write to Postgres.

Asserts the contract:
  - Each unique aircraft observed becomes exactly ONE row in `entity`.
  - Each cycle produces a position_track row per aircraft (snapshots over time).
  - Re-observing the same aircraft on a second cycle UPDATES the entity row
    (last_seen advances) but does NOT create a duplicate.
  - position_track.geom is correct WGS84 GEOGRAPHY round-trippable to (lat, lng).

Hits the real Postgres on the Mac Mini. Uses a unique sentinel ICAO24 prefix
so the tests can be isolated from real ingester runs and cleaned up at teardown.

Run from MEWR root:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_planes_dual_write.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute  # noqa: E402
from ingesters.planes import PlanesIngester  # noqa: E402
from writers import write_aircraft_events  # noqa: E402


# Sentinel prefix for test ICAO24 hex codes — kept under the test_ namespace
# in canonical_id so cleanup is unambiguous. Real ICAO24s are 6-char hex.
TEST_ICAO_PREFIX = "test01"


def _adsblol_ac_record(hex_id: str, lat: float, lng: float, *,
                       callsign: str = "TEST123",
                       alt_baro: float = 30000.0,
                       gs: float = 450.0,
                       track: float = 90.0,
                       squawk: str = "1200",
                       db_flags: int = 0) -> dict:
    """Build a single adsb.lol-shaped aircraft record for tests."""
    return {
        "hex": hex_id,
        "flight": callsign,
        "lat": lat,
        "lon": lng,
        "alt_baro": alt_baro,
        "gs": gs,
        "track": track,
        "squawk": squawk,
        "dbFlags": db_flags,
        "seen": 0.0,
    }


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_aircraft():
    """Remove any rows the test_ICAO_PREFIX may have left from prior runs.
    The CASCADE on entity_attribute means deleting entity rows is sufficient
    to clean attributes; position_track has no FK so it must be deleted by
    entity_id reference."""
    await execute(
        "DELETE FROM position_track WHERE entity_id IN ("
        "  SELECT id FROM entity WHERE canonical_id LIKE $1)",
        f"{TEST_ICAO_PREFIX}%",
    )
    await execute(
        "DELETE FROM entity WHERE canonical_id LIKE $1",
        f"{TEST_ICAO_PREFIX}%",
    )
    yield
    # Clean again on teardown
    await execute(
        "DELETE FROM position_track WHERE entity_id IN ("
        "  SELECT id FROM entity WHERE canonical_id LIKE $1)",
        f"{TEST_ICAO_PREFIX}%",
    )
    await execute(
        "DELETE FROM entity WHERE canonical_id LIKE $1",
        f"{TEST_ICAO_PREFIX}%",
    )


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_aircraft_events_creates_entity_and_position_track(_clean_test_aircraft):
    """The simplest path — one event in, one entity row + one position_track row."""
    ingester = PlanesIngester()
    raw = [
        _adsblol_ac_record(
            hex_id=f"{TEST_ICAO_PREFIX}1", lat=40.6, lng=-73.8,
            callsign="UAL123", alt_baro=35000.0, gs=480.0,
        ),
    ]
    parsed = ingester._parse_adsblol_response({"ac": raw})
    events = ingester.normalize(parsed)
    assert len(events) == 1, "PlanesIngester.normalize should produce exactly one event"

    written = await write_aircraft_events(events)
    assert written == 1

    # Entity row exists, populated correctly
    entity_row = await fetch(
        "SELECT id, entity_type, canonical_id, canonical_id_type, display_name, "
        "properties, last_seen FROM entity WHERE canonical_id = $1",
        f"{TEST_ICAO_PREFIX}1",
    )
    assert len(entity_row) == 1
    e = entity_row[0]
    assert e["entity_type"] == "aircraft"
    assert e["canonical_id_type"] == "icao24"
    assert e["display_name"] == "UAL123"

    # position_track row exists with correct geom
    pt = await fetch(
        "SELECT entity_id, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "altitude_m, velocity_ms, heading_deg "
        "FROM position_track WHERE entity_id = $1",
        e["id"],
    )
    assert len(pt) == 1
    p = pt[0]
    assert abs(p["lat"] - 40.6) < 1e-4
    assert abs(p["lng"] - (-73.8)) < 1e-4
    assert p["altitude_m"] is not None
    # alt_baro 35000 ft → ~10668 m
    assert 10500 < p["altitude_m"] < 10800
    assert p["velocity_ms"] is not None
    assert p["heading_deg"] == pytest.approx(90.0)


async def test_dual_write_dedups_entity_on_second_cycle(_clean_test_aircraft):
    """Re-observing same aircraft → SAME entity row (UPSERT), NEW position_track row."""
    ingester = PlanesIngester()

    # First cycle
    raw1 = [_adsblol_ac_record(hex_id=f"{TEST_ICAO_PREFIX}2", lat=40.0, lng=-74.0)]
    events1 = ingester.normalize(ingester._parse_adsblol_response({"ac": raw1}))
    await write_aircraft_events(events1)

    # Second cycle, same plane, slightly moved
    raw2 = [_adsblol_ac_record(hex_id=f"{TEST_ICAO_PREFIX}2", lat=40.1, lng=-74.05)]
    events2 = ingester.normalize(ingester._parse_adsblol_response({"ac": raw2}))
    await write_aircraft_events(events2)

    # Exactly one entity row
    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id = $1",
        f"{TEST_ICAO_PREFIX}2",
    )
    assert entity_count == 1, "second cycle should UPDATE not INSERT a duplicate entity"

    # Two position_track rows
    pt_count = await fetchval(
        "SELECT count(*) FROM position_track WHERE entity_id = ("
        "  SELECT id FROM entity WHERE canonical_id = $1)",
        f"{TEST_ICAO_PREFIX}2",
    )
    assert pt_count == 2, "each cycle should add a position_track snapshot"


async def test_dual_write_handles_multiple_distinct_aircraft(_clean_test_aircraft):
    """Five distinct aircraft → five entity rows + five position_track rows in one batch."""
    ingester = PlanesIngester()
    raw = [
        _adsblol_ac_record(hex_id=f"{TEST_ICAO_PREFIX}{i}", lat=40 + i * 0.1, lng=-74 + i * 0.1)
        for i in range(3, 8)
    ]
    events = ingester.normalize(ingester._parse_adsblol_response({"ac": raw}))
    assert len(events) == 5

    written = await write_aircraft_events(events)
    assert written == 5

    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id LIKE $1",
        f"{TEST_ICAO_PREFIX}%",
    )
    assert entity_count == 5

    pt_count = await fetchval(
        "SELECT count(*) FROM position_track WHERE entity_id IN ("
        "  SELECT id FROM entity WHERE canonical_id LIKE $1)",
        f"{TEST_ICAO_PREFIX}%",
    )
    assert pt_count == 5


async def test_dual_write_preserves_military_and_emergency_flags_in_properties(
    _clean_test_aircraft,
):
    """Military aircraft + emergency squawk should round-trip through entity.properties."""
    ingester = PlanesIngester()
    raw = [
        # Emergency squawk 7700 + military hex prefix AE0
        _adsblol_ac_record(
            hex_id="ae012a", lat=38.9, lng=-77.0, callsign="REACH01",
            squawk="7700", db_flags=0x01,
        ),
    ]
    events = ingester.normalize(ingester._parse_adsblol_response({"ac": raw}))
    assert len(events) == 1
    assert events[0].payload["military"] is True
    assert events[0].payload["emergency"] is True

    try:
        await write_aircraft_events(events)
        row = await fetch(
            "SELECT properties FROM entity WHERE canonical_id = 'ae012a'",
        )
        assert len(row) == 1
        props = row[0]["properties"]
        # asyncpg returns jsonb as a Python dict (or str — depends on version)
        if isinstance(props, str):
            import json
            props = json.loads(props)
        assert props.get("military") is True
        assert props.get("emergency") is True
        assert props.get("callsign") == "REACH01"
    finally:
        # Inline cleanup — fixture only handles the test_ prefix
        await execute(
            "DELETE FROM position_track WHERE entity_id IN ("
            "  SELECT id FROM entity WHERE canonical_id = 'ae012a')",
        )
        await execute("DELETE FROM entity WHERE canonical_id = 'ae012a'")


async def test_dual_write_zero_events_is_noop(_clean_test_aircraft):
    """Empty list of events → no rows written, returns 0."""
    written = await write_aircraft_events([])
    assert written == 0


async def test_full_planes_cycle_with_db_writer_hook(_clean_test_aircraft):
    """End-to-end: PlanesIngester with db_writer hook runs one cycle.
    Mocks fetch() to avoid hitting adsb.lol. Verifies dual-write triggered."""
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_aircraft_events(events)

    # Supply a no-op broadcaster so cycle() returns its broadcast count > 0;
    # the dual-write hook fires regardless of broadcaster presence.
    broadcast_log: list = []

    def noop_broadcaster(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = PlanesIngester(broadcaster=noop_broadcaster, db_writer=capture_writer)

    fake_raw = [
        _adsblol_ac_record(hex_id=f"{TEST_ICAO_PREFIX}9", lat=51.5, lng=-0.1, callsign="BAW283"),
    ]

    async def fake_fetch():
        return ingester._parse_adsblol_response({"ac": fake_raw})

    with patch.object(ingester, "fetch", side_effect=fake_fetch):
        broadcast_count = await ingester.cycle()

    assert broadcast_count == 1, "live broadcast should have fired for the new aircraft"
    assert len(broadcast_log) == 1, "no-op broadcaster captured the event"
    assert len(db_writer_calls) == 1, "db_writer hook fired exactly once for the cycle"
    assert len(db_writer_calls[0]) == 1, "db_writer received the single event"

    # Confirm it landed in the database
    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id = $1",
        f"{TEST_ICAO_PREFIX}9",
    )
    assert entity_count == 1

    # Diagnostics surfaced via status()
    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1
    assert status["db_write_failures"] == 0


# ─── Phase 2.5: denormalized current_geom + current_position_time ────────


async def test_dual_write_populates_current_position_columns(_clean_test_aircraft):
    """Phase 2.5: writer must set entity.current_geom + current_position_time
    on insert. The cross-entity proximity scan depends on these."""
    ingester = PlanesIngester()
    raw = [_adsblol_ac_record(hex_id=f"{TEST_ICAO_PREFIX}cur", lat=42.0, lng=-71.0)]
    events = ingester.normalize(ingester._parse_adsblol_response({"ac": raw}))
    await write_aircraft_events(events)

    row = await fetch(
        "SELECT ST_Y(current_geom::geometry) AS lat, "
        "       ST_X(current_geom::geometry) AS lng, "
        "       current_position_time "
        "FROM entity WHERE canonical_id = $1",
        f"{TEST_ICAO_PREFIX}cur",
    )
    assert len(row) == 1
    assert abs(row[0]["lat"] - 42.0) < 1e-4
    assert abs(row[0]["lng"] - (-71.0)) < 1e-4
    assert row[0]["current_position_time"] is not None


async def test_dual_write_advances_current_position_on_newer_observation(_clean_test_aircraft):
    """Two cycles, second is newer → entity.current_geom and current_position_time
    advance to the newer position."""
    ingester = PlanesIngester()
    icao = f"{TEST_ICAO_PREFIX}adv"

    # First cycle at 40.0/-74.0
    raw1 = [_adsblol_ac_record(hex_id=icao, lat=40.0, lng=-74.0)]
    events1 = ingester.normalize(ingester._parse_adsblol_response({"ac": raw1}))
    await write_aircraft_events(events1)

    # Force a small wait so the second event has a strictly later ts (the
    # ingester writes ts = now() per cycle, so two back-to-back cycles in the
    # same microsecond would tie). For the test we manipulate the event ts.
    from datetime import datetime, timezone, timedelta
    later = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    raw2 = [_adsblol_ac_record(hex_id=icao, lat=40.5, lng=-73.5)]
    events2 = ingester.normalize(ingester._parse_adsblol_response({"ac": raw2}))
    events2[0].ts = later
    await write_aircraft_events(events2)

    row = await fetch(
        "SELECT ST_Y(current_geom::geometry) AS lat, "
        "       ST_X(current_geom::geometry) AS lng "
        "FROM entity WHERE canonical_id = $1",
        icao,
    )
    assert abs(row[0]["lat"] - 40.5) < 1e-4
    assert abs(row[0]["lng"] - (-73.5)) < 1e-4


async def test_dual_write_does_not_regress_on_older_observation(_clean_test_aircraft):
    """Out-of-order arrival defense: if the second event has an EARLIER ts,
    current_geom must NOT be overwritten with the older position."""
    ingester = PlanesIngester()
    icao = f"{TEST_ICAO_PREFIX}reg"

    # First (newer) at 40.5/-73.5
    from datetime import datetime, timezone, timedelta
    newer_ts = datetime.now(timezone.utc).isoformat()
    raw1 = [_adsblol_ac_record(hex_id=icao, lat=40.5, lng=-73.5)]
    events1 = ingester.normalize(ingester._parse_adsblol_response({"ac": raw1}))
    events1[0].ts = newer_ts
    await write_aircraft_events(events1)

    # Then a stale (older) record at 40.0/-74.0 — simulates a delayed retry
    older_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    raw2 = [_adsblol_ac_record(hex_id=icao, lat=40.0, lng=-74.0)]
    events2 = ingester.normalize(ingester._parse_adsblol_response({"ac": raw2}))
    events2[0].ts = older_ts
    await write_aircraft_events(events2)

    # current_geom should still reflect the NEWER observation
    row = await fetch(
        "SELECT ST_Y(current_geom::geometry) AS lat, "
        "       ST_X(current_geom::geometry) AS lng "
        "FROM entity WHERE canonical_id = $1",
        icao,
    )
    assert abs(row[0]["lat"] - 40.5) < 1e-4, "older position erroneously overwrote newer"
    assert abs(row[0]["lng"] - (-73.5)) < 1e-4


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
