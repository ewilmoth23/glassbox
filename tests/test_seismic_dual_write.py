"""
Phase 2C — earthquakes.py dual-write to the `event` hypertable.

Asserts:
  - One USGS event in → one event row out, with geom + severity + properties
  - Re-running with same id is idempotent (UUID derived from event_type + external_id)
  - Multiple distinct quakes → all persist
  - Empty input → no-op, returns 0
  - Tsunami flag preserved in properties
  - Magnitude / depth / place preserved in properties
  - End-to-end: EarthquakesIngester.cycle() with db_writer hook persists rows

Hits the real Postgres on the Mac Mini. Uses sentinel external_id prefix
('test05_*') for deterministic cleanup.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_seismic_dual_write.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from ingesters.earthquakes import EarthquakesIngester  # noqa: E402
from writers import write_seismic_events  # noqa: E402


TEST_PREFIX = "test05"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_quakes():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE properties->>'external_id' LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _quake_event(external_id: str, lat: float, lng: float, *,
                 mag: float = 5.0, place: str = "Test region",
                 depth_km: float = 10.0, tsunami: bool = False,
                 alert: str = None) -> GlassboxEvent:
    """Build a GlassboxEvent in the shape EarthquakesIngester.normalize() emits."""
    payload = {
        "mag": mag,
        "place": place,
        "title": f"M {mag:.1f} - {place}",
        "depth_km": depth_km,
        "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{external_id}",
        "tsunami": tsunami,
        "alert": alert,
    }
    return GlassboxEvent(
        layer="earthquakes",
        external_id=external_id,
        kind="alert",
        lat=lat,
        lng=lng,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=int(min(10, max(0, mag * 1.5))),
        altitude_m=-1000.0 * depth_km,
        source="USGS Earthquake Hazards Program",
        payload=payload,
        domain="geo",
        geocode_quality="city",
        decay_half_life_min=60,
    )


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_seismic_events_creates_event_row(_clean_test_quakes):
    """One USGS event → one row in event table, geom + properties round-trip."""
    ev = _quake_event(f"{TEST_PREFIX}us6000abcd", 35.7, -117.6, mag=5.4,
                      place="Searles Valley, CA", depth_km=8.0)
    written = await write_seismic_events([ev])
    assert written == 1

    rows = await fetch(
        "SELECT event_type, event_subtype, severity, "
        "       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "       properties, event_time, domain, decay_half_life_min "
        "FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}us6000abcd",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "usgs_quake"
    assert abs(r["lat"] - 35.7) < 1e-4
    assert abs(r["lng"] - (-117.6)) < 1e-4
    assert r["severity"] is not None and r["severity"] > 0
    assert r["domain"] == "geo"

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["mag"] == 5.4
    assert props["depth_km"] == 8.0
    assert props["place"] == "Searles Valley, CA"
    assert props["external_id"] == f"{TEST_PREFIX}us6000abcd"


async def test_write_seismic_events_is_idempotent(_clean_test_quakes):
    """Re-running with same external_id → same row, no duplicate."""
    ev = _quake_event(f"{TEST_PREFIX}us6000efgh", 40.0, -120.0, mag=4.2)

    n1 = await write_seismic_events([ev])
    assert n1 == 1

    n2 = await write_seismic_events([ev])
    # ON CONFLICT DO NOTHING returns 0 rows affected
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}us6000efgh",
    )
    assert total == 1


async def test_write_seismic_events_multiple_distinct(_clean_test_quakes):
    """Five distinct USGS events → five event rows."""
    quakes = [
        _quake_event(f"{TEST_PREFIX}q{i}", 30 + i * 0.5, -120 + i * 0.5, mag=4.0 + i * 0.2)
        for i in range(5)
    ]
    n = await write_seismic_events(quakes)
    assert n == 5

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' LIKE $1",
        f"{TEST_PREFIX}q%",
    )
    assert total == 5


async def test_write_seismic_events_zero_events_is_noop():
    n = await write_seismic_events([])
    assert n == 0


async def test_write_seismic_events_preserves_tsunami_alert_flags(_clean_test_quakes):
    ev = _quake_event(f"{TEST_PREFIX}tsu1", -8.5, 117.0, mag=7.5,
                      place="Indonesia", depth_km=20.0, tsunami=True, alert="orange")
    await write_seismic_events([ev])

    row = await fetch(
        "SELECT properties FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}tsu1",
    )
    assert len(row) == 1
    import json
    props = row[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["tsunami"] is True
    assert props["alert"] == "orange"


async def test_full_earthquakes_cycle_with_db_writer_hook(_clean_test_quakes):
    """End-to-end: EarthquakesIngester with db_writer hook runs one cycle.
    Mocks fetch() so we don't hit USGS in the test."""
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_seismic_events(events)

    broadcast_log = []
    def noop_broadcaster(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = EarthquakesIngester(broadcaster=noop_broadcaster, db_writer=capture_writer)

    # Mock fetch() — supplies raw USGS-shaped records that normalize() will consume
    fake_raw = [
        {
            "id": f"{TEST_PREFIX}cyc1",
            "lat": 36.0, "lng": -120.0,
            "mag": 4.5,
            "place": "California Test Zone",
            "depth_km": 12.0,
            "title": "M 4.5 - California Test Zone",
            "time_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
            "tsunami": False,
            "alert": None,
            "url": "https://earthquake.usgs.gov/earthquakes/eventpage/test",
        },
    ]

    async def fake_fetch():
        return fake_raw

    with patch.object(ingester, "fetch", side_effect=fake_fetch):
        broadcast_count = await ingester.cycle()

    assert broadcast_count == 1
    assert len(db_writer_calls) == 1
    assert len(db_writer_calls[0]) == 1

    # Confirm row landed
    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}cyc1",
    )
    assert total == 1

    # Diagnostics surface
    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1


async def test_write_seismic_events_filters_non_earthquakes_layer(_clean_test_quakes):
    """Defensive: events with layer != 'earthquakes' should be skipped (caller bug)."""
    ev = GlassboxEvent(
        layer="planes",  # WRONG — not an earthquake event
        external_id=f"{TEST_PREFIX}wrongtype",
        kind="alert",
        lat=0.0, lng=0.0, ts=datetime.now(timezone.utc).isoformat(),
    )
    n = await write_seismic_events([ev])
    assert n == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
