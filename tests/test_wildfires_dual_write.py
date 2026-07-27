"""
Phase 2P — nasa_firms.py dual-write to the `event` hypertable.

Asserts:
  - GlassboxEvent (layer='wildfires') in → event row with event_type='nasa_firms'
  - event_subtype = dataset (e.g., 'VIIRS_SNPP_NRT', 'MODIS_NRT')
  - title and description populated; brightness_k + frp_mw + confidence in properties
  - Idempotent re-runs (FIRMS external_ids and acq timestamps are stable per detection)
  - Multiple distinct fires → all persist
  - End-to-end via NasaFirmsIngester with db_writer hook (mocked HTTP)

FIRMS uses ON CONFLICT (id, event_time) DO NOTHING since both id (deterministic uuid5
from stable external_id) and event_time (parsed from acq_date+acq_time) are stable
per fire-pixel observation.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_wildfires_dual_write.py -v
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
from ingesters.nasa_firms import NasaFirmsIngester  # noqa: E402
from writers import write_wildfire_events  # noqa: E402


TEST_PREFIX = "test10"


def _fire_event(label: str, *, lat: float = 40.0, lng: float = -120.0,
                dataset: str = "VIIRS_SNPP_NRT",
                brightness_k: float = 320.5,
                confidence: str = "n",
                frp_mw: float = 12.4,
                satellite: str = "N",
                instrument: str = "VIIRS",
                daynight: str = "D",
                acq_iso: str = "2026-05-08T01:30:00Z") -> GlassboxEvent:
    """Build a GlassboxEvent in the shape NasaFirmsIngester.normalize() emits."""
    payload = {
        "dataset": dataset,
        "brightness_k": brightness_k,
        "confidence": confidence,
        "frp_mw": frp_mw,
        "satellite": satellite,
        "instrument": instrument,
        "daynight": daynight,
        "_attribution": "Wildfires: NASA FIRMS (MODIS/VIIRS)",
    }
    ext_id = f"{dataset}:{lat:.4f}:{lng:.4f}:{TEST_PREFIX}_{label}"
    return GlassboxEvent(
        layer="wildfires",
        external_id=ext_id,
        kind="event",
        lat=lat,
        lng=lng,
        ts=acq_iso,
        severity=6,
        source="NASA FIRMS (MODIS + VIIRS active fire detections)",
        payload=payload,
        domain="geo",
        geocode_quality="exact",
        decay_half_life_min=120,
    )


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_fires():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type = 'nasa_firms' "
            "AND properties->>'external_id' LIKE $1",
            f"%{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_wildfire_creates_event_row(_clean_test_fires):
    ev = _fire_event("fire1", lat=39.5, lng=-120.7,
                     brightness_k=345.2, confidence="h", frp_mw=22.7)
    n = await write_wildfire_events([ev])
    assert n == 1

    rows = await fetch(
        "SELECT event_type, event_subtype, severity, "
        "       title, description, "
        "       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "       properties, domain, decay_half_life_min "
        "FROM event WHERE properties->>'external_id' LIKE $1",
        f"%{TEST_PREFIX}_fire1",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "nasa_firms"
    assert r["event_subtype"] == "VIIRS_SNPP_NRT"
    assert abs(r["lat"] - 39.5) < 1e-4
    assert abs(r["lng"] - (-120.7)) < 1e-4
    assert r["severity"] == pytest.approx(6.0)
    assert r["domain"] == "geo"
    assert r["decay_half_life_min"] == 120

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["dataset"] == "VIIRS_SNPP_NRT"
    assert props["brightness_k"] == 345.2
    assert props["confidence"] == "h"
    assert props["frp_mw"] == 22.7


async def test_write_wildfire_is_idempotent(_clean_test_fires):
    ev = _fire_event("dedup", lat=10.0, lng=10.0)
    n1 = await write_wildfire_events([ev])
    assert n1 == 1
    n2 = await write_wildfire_events([ev])
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' LIKE $1",
        f"%{TEST_PREFIX}_dedup",
    )
    assert total == 1


async def test_write_wildfire_multiple_distinct(_clean_test_fires):
    evs = [
        _fire_event(f"multi{i}", lat=40 + i * 0.01, lng=-120 - i * 0.01)
        for i in range(5)
    ]
    n = await write_wildfire_events(evs)
    assert n == 5

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' LIKE $1",
        f"%{TEST_PREFIX}_multi%",
    )
    assert total == 5


async def test_write_wildfire_distinguishes_modis_and_viirs(_clean_test_fires):
    evs = [
        _fire_event("viirs", dataset="VIIRS_SNPP_NRT"),
        _fire_event("modis", dataset="MODIS_NRT", lat=41.0, lng=-119.0),
    ]
    await write_wildfire_events(evs)
    rows = await fetch(
        "SELECT event_subtype FROM event WHERE properties->>'external_id' LIKE $1 "
        "ORDER BY event_subtype",
        f"%{TEST_PREFIX}_%",
    )
    seen = sorted(r["event_subtype"] for r in rows)
    assert seen == ["MODIS_NRT", "VIIRS_SNPP_NRT"]


async def test_write_wildfire_zero_events_is_noop():
    n = await write_wildfire_events([])
    assert n == 0


async def test_write_wildfire_skips_non_wildfires_layer(_clean_test_fires):
    bogus = [GlassboxEvent(
        layer="planes",
        external_id=f"{TEST_PREFIX}_wrong",
        kind="position",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
    )]
    n = await write_wildfire_events(bogus)
    assert n == 0


async def test_full_nasa_firms_cycle_with_db_writer_hook(_clean_test_fires):
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_wildfire_events(events)

    broadcast_log = []
    def noop_b(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = NasaFirmsIngester(broadcaster=noop_b, db_writer=capture_writer)

    # Mock fetch() — supplies one VIIRS-shaped fire detection in CSV-dict form
    fake_raw = [{
        "_dataset": "VIIRS_SNPP_NRT",
        "latitude": "40.5",
        "longitude": "-120.5",
        "bright_ti4": "330.7",
        "confidence": "h",
        "frp": "15.2",
        "satellite": "N",
        "instrument": "VIIRS",
        "daynight": "D",
        "acq_date": "2026-05-08",
        "acq_time": f"0130_{TEST_PREFIX}_cycle",
    }]

    async def fake_fetch():
        return fake_raw

    with patch.object(ingester, "fetch", side_effect=fake_fetch):
        broadcast_count = await ingester.cycle()

    # Cycle yields 1 fire event after normalize
    assert broadcast_count == 1
    assert len(db_writer_calls) == 1

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' LIKE $1",
        f"%{TEST_PREFIX}_cycle",
    )
    assert total == 1

    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
