"""
Phase 2 (NASA EONET) — nasa_eonet.py dual-write to the `event` hypertable.

Asserts:
  - GlassboxEvent (layer='natural_events') in → event row with event_type='nasa_eonet'
  - event_subtype = first category code (wildfires, volcanoes, severeStorms, etc.)
  - properties.categories preserves the FULL list (events can belong to multiple)
  - title + description preserved
  - Idempotent re-runs (ON CONFLICT on stable id+event_time)
  - End-to-end via NasaEonetIngester with db_writer hook (mocked HTTP)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_eonet_dual_write.py -v
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
from ingesters.nasa_eonet import NasaEonetIngester  # noqa: E402
from writers import write_natural_event_events  # noqa: E402


TEST_PREFIX = "test12"


def _eonet_event(label: str, *, lat: float = 19.4, lng: float = -155.3,
                 categories: list = None,
                 title: str = "Kīlauea volcanic activity",
                 description: str = "Ongoing eruption with active lava flows",
                 severity: int = 7) -> GlassboxEvent:
    """Build a GlassboxEvent in the shape NasaEonetIngester.normalize() emits."""
    if categories is None:
        categories = ["volcanoes"]
    payload = {
        "title": title,
        "description": description,
        "categories": categories,
        "sources": ["SI_VOLCANO"],
        "link": f"https://eonet.gsfc.nasa.gov/api/v3/events/{label}",
        "_attribution": "NASA EONET",
    }
    return GlassboxEvent(
        layer="natural_events",
        external_id=f"EONET_{TEST_PREFIX}_{label}",
        kind="event",
        lat=lat,
        lng=lng,
        ts="2026-05-08T01:30:00+00:00",
        severity=severity,
        source="NASA EONET (Earth Observatory Natural Event Tracker)",
        payload=payload,
        domain="geo",
        geocode_quality="exact",
        decay_half_life_min=720,
    )


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_eonet():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type = 'nasa_eonet' "
            "AND properties->>'external_id' LIKE $1",
            f"%{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_eonet_creates_event_row(_clean_test_eonet):
    ev = _eonet_event("volc1", lat=19.4, lng=-155.3,
                      categories=["volcanoes"],
                      title="Kīlauea Activity",
                      description="Active eruption phase")
    n = await write_natural_event_events([ev])
    assert n == 1

    rows = await fetch(
        "SELECT event_type, event_subtype, severity, "
        "       title, description, "
        "       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "       properties, domain, decay_half_life_min "
        "FROM event WHERE properties->>'external_id' = $1",
        f"EONET_{TEST_PREFIX}_volc1",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "nasa_eonet"
    assert r["event_subtype"] == "volcanoes"
    assert r["title"] == "Kīlauea Activity"
    assert r["description"] == "Active eruption phase"
    assert abs(r["lat"] - 19.4) < 1e-4
    assert abs(r["lng"] - (-155.3)) < 1e-4
    assert r["domain"] == "geo"
    assert r["decay_half_life_min"] == 720

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["categories"] == ["volcanoes"]


async def test_write_eonet_preserves_multi_category(_clean_test_eonet):
    """EONET events can belong to multiple categories. event_subtype takes
    the first; properties.categories preserves the full list."""
    ev = _eonet_event("multi", categories=["wildfires", "drought"])
    await write_natural_event_events([ev])

    row = await fetch(
        "SELECT event_subtype, properties FROM event "
        "WHERE properties->>'external_id' = $1",
        f"EONET_{TEST_PREFIX}_multi",
    )
    assert len(row) == 1
    assert row[0]["event_subtype"] == "wildfires"
    import json
    props = row[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert sorted(props["categories"]) == ["drought", "wildfires"]


async def test_write_eonet_is_idempotent(_clean_test_eonet):
    ev = _eonet_event("dedup")
    n1 = await write_natural_event_events([ev])
    assert n1 == 1
    n2 = await write_natural_event_events([ev])
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"EONET_{TEST_PREFIX}_dedup",
    )
    assert total == 1


async def test_write_eonet_multiple_distinct(_clean_test_eonet):
    evs = [
        _eonet_event(f"multi{i}", lat=20 + i * 0.1, lng=-100 + i * 0.1)
        for i in range(5)
    ]
    n = await write_natural_event_events(evs)
    assert n == 5

    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='nasa_eonet' "
        "AND properties->>'external_id' LIKE $1",
        f"EONET_{TEST_PREFIX}_multi%",
    )
    assert total == 5


async def test_write_eonet_zero_events_is_noop():
    n = await write_natural_event_events([])
    assert n == 0


async def test_write_eonet_skips_non_natural_events_layer(_clean_test_eonet):
    bogus = [GlassboxEvent(
        layer="planes",
        external_id=f"EONET_{TEST_PREFIX}_wrong",
        kind="position",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
    )]
    n = await write_natural_event_events(bogus)
    assert n == 0


async def test_full_eonet_cycle_with_db_writer_hook(_clean_test_eonet):
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_natural_event_events(events)

    broadcast_log = []
    def noop_b(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = NasaEonetIngester(broadcaster=noop_b, db_writer=capture_writer)

    # Mock fetch() — supplies one EONET-shaped event
    fake_raw = [{
        "id": f"EONET_{TEST_PREFIX}_cycle",
        "title": "Test Volcano Activity",
        "description": "Mocked test event",
        "link": "https://eonet.gsfc.nasa.gov/test",
        "categories": [{"id": "volcanoes", "title": "Volcanoes"}],
        "sources": [{"id": "TEST_SOURCE", "url": "https://example.com"}],
        "geometry": [{
            "magnitudeValue": None,
            "magnitudeUnit": None,
            "date": "2026-05-08T01:00:00Z",
            "type": "Point",
            "coordinates": [-155.3, 19.4],
        }],
    }]

    async def fake_fetch():
        return fake_raw

    with patch.object(ingester, "fetch", side_effect=fake_fetch):
        broadcast_count = await ingester.cycle()

    assert broadcast_count == 1
    assert len(db_writer_calls) == 1

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"EONET_{TEST_PREFIX}_cycle",
    )
    assert total == 1

    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
