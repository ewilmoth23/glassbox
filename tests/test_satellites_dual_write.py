"""
Phase 2B — satellites.py dual-write to entity (satellites) + position_track.

Asserts the contract:
  - Each unique NORAD id → exactly ONE entity row (entity_type='satellite',
    canonical_id_type='norad')
  - Each cycle adds a position_track snapshot per satellite
  - Re-observation of same NORAD updates last_seen + current_geom but
    doesn't insert a duplicate entity
  - properties.name → entity.display_name
  - group preserved in entity.properties
  - Phase 2.5 columns (current_geom, current_position_time) populated

Sentinel NORAD prefix '8888' for deterministic cleanup. Real NORAD numbers
are 5-digit (now also 6-digit in the new alpha-5 era), but we use the
canonical_id as a string so 'test09_*' style works too.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_satellites_dual_write.py -v
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
from ingesters.satellites import SatellitesIngester  # noqa: E402
from writers import write_satellite_events  # noqa: E402


TEST_PREFIX = "test09"


def _sat_record(norad: str, lat: float, lng: float, *,
                name: str = "TEST_SAT",
                group: str = "stations",
                alt_km: float = 408.0,
                vel_km_s: float = 7.66) -> dict:
    """Build a raw satellite record in the shape SatellitesIngester.normalize() consumes."""
    return {
        "norad": norad,
        "name": name,
        "group": group,
        "lat": lat,
        "lng": lng,
        "alt_km": alt_km,
        "vel_km_s": vel_km_s,
    }


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_sats():
    async def _cleanup():
        await execute(
            "DELETE FROM position_track WHERE entity_id IN ("
            "  SELECT id FROM entity WHERE canonical_id LIKE $1 AND entity_type='satellite')",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1 AND entity_type='satellite'",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_satellite_creates_entity_and_position_track(_clean_test_sats):
    ingester = SatellitesIngester()
    raw = [_sat_record(f"{TEST_PREFIX}001", 35.7, 139.8, name="ISS_TEST")]
    events = ingester.normalize(raw)
    assert len(events) == 1

    written = await write_satellite_events(events)
    assert written == 1

    rows = await fetch(
        "SELECT id, entity_type, canonical_id_type, canonical_id, display_name "
        "FROM entity WHERE canonical_id = $1 AND entity_type='satellite'",
        f"{TEST_PREFIX}001",
    )
    assert len(rows) == 1
    e = rows[0]
    assert e["entity_type"] == "satellite"
    assert e["canonical_id_type"] == "norad"
    assert e["display_name"] == "ISS_TEST"

    pt = await fetch(
        "SELECT ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "       altitude_m, velocity_ms "
        "FROM position_track WHERE entity_id = $1",
        e["id"],
    )
    assert len(pt) == 1
    p = pt[0]
    assert abs(p["lat"] - 35.7) < 1e-4
    assert abs(p["lng"] - 139.8) < 1e-4
    # 408 km → 408,000 m
    assert p["altitude_m"] == pytest.approx(408_000.0, rel=1e-3)
    # 7.66 km/s → 7660 m/s
    assert p["velocity_ms"] == pytest.approx(7660.0, rel=1e-3)


async def test_write_satellite_dedups_entity_on_second_cycle(_clean_test_sats):
    ingester = SatellitesIngester()
    raw1 = [_sat_record(f"{TEST_PREFIX}002", 0.0, 0.0)]
    raw2 = [_sat_record(f"{TEST_PREFIX}002", 0.5, 1.0)]

    await write_satellite_events(ingester.normalize(raw1))
    await write_satellite_events(ingester.normalize(raw2))

    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id = $1 AND entity_type='satellite'",
        f"{TEST_PREFIX}002",
    )
    assert entity_count == 1

    pt_count = await fetchval(
        "SELECT count(*) FROM position_track WHERE entity_id = ("
        "  SELECT id FROM entity WHERE canonical_id = $1 AND entity_type='satellite')",
        f"{TEST_PREFIX}002",
    )
    assert pt_count == 2


async def test_write_satellite_handles_multiple_distinct(_clean_test_sats):
    ingester = SatellitesIngester()
    raw = [
        _sat_record(f"{TEST_PREFIX}{i:03d}", lat=10 + i, lng=10 + i, name=f"SAT_{i}")
        for i in range(10, 16)
    ]
    events = ingester.normalize(raw)
    written = await write_satellite_events(events)
    assert written == 6

    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id LIKE $1 AND entity_type='satellite'",
        f"{TEST_PREFIX}%",
    )
    assert entity_count == 6


async def test_write_satellite_preserves_group_in_properties(_clean_test_sats):
    ingester = SatellitesIngester()
    raw = [_sat_record(f"{TEST_PREFIX}grp1", 0, 0, name="GPS_TEST", group="gps-ops")]
    events = ingester.normalize(raw)
    await write_satellite_events(events)

    row = await fetch(
        "SELECT properties FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}grp1",
    )
    assert len(row) == 1
    import json
    props = row[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props.get("group") == "gps-ops"
    assert props.get("name") == "GPS_TEST"
    assert props.get("norad") == f"{TEST_PREFIX}grp1"


async def test_write_satellite_populates_current_geom_phase25(_clean_test_sats):
    """Phase 2.5: writer must set entity.current_geom + current_position_time."""
    ingester = SatellitesIngester()
    raw = [_sat_record(f"{TEST_PREFIX}cg1", 51.5, -0.1, name="LON_SAT")]
    events = ingester.normalize(raw)
    await write_satellite_events(events)

    row = await fetch(
        "SELECT ST_Y(current_geom::geometry) AS lat, "
        "       ST_X(current_geom::geometry) AS lng, "
        "       current_position_time "
        "FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}cg1",
    )
    assert len(row) == 1
    assert abs(row[0]["lat"] - 51.5) < 1e-4
    assert abs(row[0]["lng"] - (-0.1)) < 1e-4
    assert row[0]["current_position_time"] is not None


async def test_write_satellite_zero_events_is_noop():
    n = await write_satellite_events([])
    assert n == 0


async def test_write_satellite_skips_non_satellites_layer(_clean_test_sats):
    """Defensive: passing a planes event must not corrupt the entity table."""
    from ingesters.base import GlassboxEvent
    from datetime import datetime, timezone

    bogus = [GlassboxEvent(
        layer="planes",
        external_id="ae012a",
        kind="position",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
    )]
    n = await write_satellite_events(bogus)
    assert n == 0


async def test_full_satellites_cycle_with_db_writer_hook(_clean_test_sats):
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_satellite_events(events)

    broadcast_log = []
    def noop_b(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = SatellitesIngester(broadcaster=noop_b, db_writer=capture_writer)

    fake_raw = [_sat_record(f"{TEST_PREFIX}cycle", 22.5, 114.0, name="HK_SAT")]

    async def fake_fetch():
        return fake_raw

    with patch.object(ingester, "fetch", side_effect=fake_fetch):
        broadcast_count = await ingester.cycle()

    assert broadcast_count == 1
    assert len(db_writer_calls) == 1

    entity_count = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id = $1 AND entity_type='satellite'",
        f"{TEST_PREFIX}cycle",
    )
    assert entity_count == 1

    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
