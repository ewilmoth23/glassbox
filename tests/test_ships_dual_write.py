"""
Phase 2A — ships.py dual-write to entity (vessels) + position_track.

Asserts the contract:
  - Each unique MMSI → exactly ONE entity row (entity_type='vessel',
    canonical_id_type='mmsi')
  - Each cycle adds a position_track snapshot per vessel
  - Re-observation of same MMSI updates entity's last_seen, doesn't insert
    a duplicate entity
  - properties.name → entity.display_name
  - dark-vessel flag preserved in entity.properties

Hits real Postgres on the Mac Mini. Sentinel MMSI prefix 'test06' for
deterministic cleanup. (Real MMSIs are 9-digit numerics; using a prefix
that won't collide with any real MMSI keeps the test isolated.)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_ships_dual_write.py -v
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
from ingesters.ships import ShipsIngester  # noqa: E402
from writers import write_vessel_events  # noqa: E402


TEST_PREFIX = "test06"


def _ais_record(mmsi: str, lat: float, lng: float, *,
                name: str = "TEST_SHIP",
                ship_type: int = 70,
                sog: float = 12.0,
                heading: float = 90.0,
                cog: float = 88.0,
                dark: bool = False) -> dict:
    """Build a raw AIS record in the shape ShipsIngester.normalize() consumes."""
    return {
        "mmsi": mmsi,
        "lat": lat,
        "lng": lng,
        "name": name,
        "ship_type": ship_type,
        "sog": sog,
        "heading": heading,
        "cog": cog,
        "_dark": dark,
        "_source": "test_ais",
    }


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_vessels():
    async def _cleanup():
        await execute(
            "DELETE FROM position_track WHERE entity_id IN ("
            "  SELECT id FROM entity WHERE canonical_id LIKE $1 AND entity_type='vessel')",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1 AND entity_type='vessel'",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_vessel_events_creates_entity_and_position_track(_clean_test_vessels):
    ingester = ShipsIngester()
    raw = [_ais_record(f"{TEST_PREFIX}001", 60.2, 25.0, name="EVERGIVEN_TEST")]
    events = ingester.normalize(raw)
    assert len(events) == 1

    written = await write_vessel_events(events)
    assert written == 1

    rows = await fetch(
        "SELECT id, entity_type, canonical_id_type, canonical_id, display_name "
        "FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}001",
    )
    assert len(rows) == 1
    e = rows[0]
    assert e["entity_type"] == "vessel"
    assert e["canonical_id_type"] == "mmsi"
    assert e["display_name"] == "EVERGIVEN_TEST"

    pt = await fetch(
        "SELECT ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "       velocity_ms, heading_deg "
        "FROM position_track WHERE entity_id = $1",
        e["id"],
    )
    assert len(pt) == 1
    p = pt[0]
    assert abs(p["lat"] - 60.2) < 1e-4
    assert abs(p["lng"] - 25.0) < 1e-4
    # sog 12 knots * 0.514444 = ~6.17 m/s
    assert p["velocity_ms"] is not None
    assert 5.5 < p["velocity_ms"] < 7.0


async def test_write_vessel_events_dedups_entity_on_second_cycle(_clean_test_vessels):
    ingester = ShipsIngester()
    raw1 = [_ais_record(f"{TEST_PREFIX}002", 60.0, 25.0)]
    raw2 = [_ais_record(f"{TEST_PREFIX}002", 60.1, 25.05)]

    await write_vessel_events(ingester.normalize(raw1))
    await write_vessel_events(ingester.normalize(raw2))

    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id = $1 AND entity_type='vessel'",
        f"{TEST_PREFIX}002",
    )
    assert entity_count == 1

    pt_count = await fetchval(
        "SELECT count(*) FROM position_track WHERE entity_id = ("
        "  SELECT id FROM entity WHERE canonical_id = $1 AND entity_type='vessel')",
        f"{TEST_PREFIX}002",
    )
    assert pt_count == 2


async def test_write_vessel_events_handles_multiple_distinct_vessels(_clean_test_vessels):
    ingester = ShipsIngester()
    raw = [
        _ais_record(f"{TEST_PREFIX}{i:03d}", 60 + i * 0.1, 25 + i * 0.1, name=f"SHIP_{i}")
        for i in range(10, 15)
    ]
    events = ingester.normalize(raw)
    written = await write_vessel_events(events)
    assert written == 5

    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id LIKE $1 AND entity_type='vessel'",
        f"{TEST_PREFIX}%",
    )
    assert entity_count == 5


async def test_write_vessel_events_preserves_dark_flag(_clean_test_vessels):
    ingester = ShipsIngester()
    raw = [_ais_record(f"{TEST_PREFIX}dark1", 25.0, 55.0, dark=True, name="GHOST")]
    events = ingester.normalize(raw)
    await write_vessel_events(events)

    row = await fetch(
        "SELECT properties FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}dark1",
    )
    assert len(row) == 1
    import json
    props = row[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props.get("dark") is True
    assert props.get("name") == "GHOST"


async def test_write_vessel_events_zero_events_is_noop():
    n = await write_vessel_events([])
    assert n == 0


async def test_write_vessel_events_skips_non_ships_layer(_clean_test_vessels):
    """Defensive: passing an aircraft event must not corrupt the entity table."""
    from ingesters.base import GlassboxEvent
    from datetime import datetime, timezone

    bogus = [GlassboxEvent(
        layer="planes",
        external_id="ae012a",
        kind="position",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
    )]
    n = await write_vessel_events(bogus)
    assert n == 0


async def test_write_vessel_events_coerces_non_string_name(_clean_test_vessels):
    """Some AIS sources put a numeric MMSI in the `name` field when the vessel
    hasn't reported a string name. The writer must coerce to str for the TEXT
    column — bug found in Phase 2 gate run (2026-05-07)."""
    ingester = ShipsIngester()
    raw = [_ais_record(f"{TEST_PREFIX}numname", 25.0, 55.0)]
    # Override `name` to be an int (simulates some Digitraffic AIS records)
    raw[0]["name"] = 230994000
    events = ingester.normalize(raw)
    n = await write_vessel_events(events)
    assert n == 1

    row = await fetch(
        "SELECT display_name FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}numname",
    )
    assert len(row) == 1
    assert row[0]["display_name"] == "230994000"


async def test_full_ships_cycle_with_db_writer_hook(_clean_test_vessels):
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_vessel_events(events)

    broadcast_log = []
    def noop_b(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = ShipsIngester(broadcaster=noop_b, db_writer=capture_writer)

    fake_raw = [_ais_record(f"{TEST_PREFIX}cycle", 22.5, 114.0, name="HONG_KONG_VESSEL")]

    async def fake_fetch():
        return fake_raw

    with patch.object(ingester, "fetch", side_effect=fake_fetch):
        broadcast_count = await ingester.cycle()

    assert broadcast_count == 1
    assert len(db_writer_calls) == 1

    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id = $1 AND entity_type='vessel'",
        f"{TEST_PREFIX}cycle",
    )
    assert entity_count == 1

    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
