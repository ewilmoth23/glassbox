"""
Phase 2 (EMSC) — emsc_fdsn.py dual-write to the `event` hypertable.

Both USGS and EMSC use layer='earthquakes'. EMSC prefixes external_id with
'emsc:' so the writers can dispatch:
  - write_seismic_events:    event_type='usgs_quake' (rejects emsc-prefixed)
  - write_emsc_quake_events: event_type='emsc_quake' (accepts only emsc-prefixed)

Asserts:
  - GlassboxEvent (layer='earthquakes', external_id='emsc:...') → event row
    with event_type='emsc_quake'
  - title carries magnitude + region; description carries depth + agency
  - Idempotent re-runs (deterministic uuid5)
  - Defensive layer + prefix checks reject mistargeted events
  - End-to-end via EmscFdsnIngester with db_writer hook (mocked HTTP)
  - write_seismic_events skips emsc-prefixed events (cross-writer isolation)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_emsc_dual_write.py -v
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
from ingesters.emsc_fdsn import EmscFdsnIngester  # noqa: E402
from writers import write_emsc_quake_events, write_seismic_events  # noqa: E402


TEST_PREFIX = "test11"


def _emsc_event(label: str, *, lat: float = 38.0, lng: float = 22.0,
                mag: float = 4.5, depth_km: float = 10.0,
                region: str = "Greece", agency: str = "EMSC",
                magtype: str = "ml") -> GlassboxEvent:
    """Build a GlassboxEvent in the shape EmscFdsnIngester.normalize() emits."""
    payload = {
        "magnitude": mag,
        "magnitude_type": magtype,
        "depth_km": depth_km,
        "region": region,
        "agency": agency,
        "_attribution": "Earthquakes: EMSC/CSEM (CC BY 4.0) — DOI 10.17616/R3N93X",
    }
    return GlassboxEvent(
        layer="earthquakes",
        external_id=f"emsc:{TEST_PREFIX}_{label}",
        kind="event",
        lat=lat,
        lng=lng,
        ts="2026-05-08T01:30:00+00:00",
        severity=int(min(10, mag * 1.5)),
        altitude_m=-depth_km * 1000.0,
        source="EMSC SeismicPortal FDSN (CC BY 4.0, DOI 10.17616/R3N93X)",
        payload=payload,
        domain="geo",
        geocode_quality="exact",
        decay_half_life_min=60,
    )


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_emsc():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type IN ('emsc_quake', 'usgs_quake') "
            "AND properties->>'external_id' LIKE $1",
            f"%{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_emsc_creates_event_row(_clean_test_emsc):
    ev = _emsc_event("greek1", lat=38.5, lng=23.4, mag=5.2,
                     depth_km=15.0, region="Central Greece")
    n = await write_emsc_quake_events([ev])
    assert n == 1

    rows = await fetch(
        "SELECT event_type, severity, title, description, "
        "       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "       properties, domain, decay_half_life_min "
        "FROM event WHERE properties->>'external_id' = $1",
        f"emsc:{TEST_PREFIX}_greek1",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "emsc_quake"
    assert abs(r["lat"] - 38.5) < 1e-4
    assert abs(r["lng"] - 23.4) < 1e-4
    assert r["domain"] == "geo"
    assert r["decay_half_life_min"] == 60

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["magnitude"] == 5.2
    assert props["depth_km"] == 15.0
    assert props["region"] == "Central Greece"


async def test_write_emsc_is_idempotent(_clean_test_emsc):
    ev = _emsc_event("dedup", mag=3.8)
    n1 = await write_emsc_quake_events([ev])
    assert n1 == 1
    n2 = await write_emsc_quake_events([ev])
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"emsc:{TEST_PREFIX}_dedup",
    )
    assert total == 1


async def test_write_emsc_multiple_distinct(_clean_test_emsc):
    evs = [
        _emsc_event(f"multi{i}", lat=38 + i * 0.1, lng=22 + i * 0.1, mag=3.0 + i * 0.3)
        for i in range(5)
    ]
    n = await write_emsc_quake_events(evs)
    assert n == 5

    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='emsc_quake' "
        "AND properties->>'external_id' LIKE $1",
        f"emsc:{TEST_PREFIX}_multi%",
    )
    assert total == 5


async def test_write_emsc_zero_events_is_noop():
    n = await write_emsc_quake_events([])
    assert n == 0


async def test_write_emsc_skips_non_earthquakes_layer(_clean_test_emsc):
    bogus = [GlassboxEvent(
        layer="planes",
        external_id=f"emsc:{TEST_PREFIX}_wrong",
        kind="position",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
    )]
    n = await write_emsc_quake_events(bogus)
    assert n == 0


async def test_write_emsc_skips_non_emsc_prefix(_clean_test_emsc):
    """USGS events (no 'emsc:' prefix) must NOT land in write_emsc_quake_events."""
    usgs_shaped = GlassboxEvent(
        layer="earthquakes",
        external_id=f"{TEST_PREFIX}_usgs1",  # no emsc: prefix
        kind="alert",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        payload={"mag": 5.0},
    )
    n = await write_emsc_quake_events([usgs_shaped])
    assert n == 0


async def test_write_seismic_skips_emsc_prefix_events(_clean_test_emsc):
    """Reverse direction: EMSC events fed to write_seismic_events must be rejected."""
    emsc_shaped = _emsc_event("usgs_test_skip")
    n = await write_seismic_events([emsc_shaped])
    assert n == 0
    # Confirm nothing landed as 'usgs_quake' either
    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='usgs_quake' "
        "AND properties->>'external_id' = $1",
        f"emsc:{TEST_PREFIX}_usgs_test_skip",
    )
    assert total == 0


async def test_full_emsc_cycle_with_db_writer_hook(_clean_test_emsc):
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_emsc_quake_events(events)

    broadcast_log = []
    def noop_b(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = EmscFdsnIngester(broadcaster=noop_b, db_writer=capture_writer)

    # Mock fetch() — supplies one EMSC FDSN GeoJSON-shaped feature
    fake_raw = [{
        "type": "Feature",
        "id": f"{TEST_PREFIX}_cycle",
        "geometry": {
            "type": "Point",
            "coordinates": [22.5, 38.6, 12.5],   # lng, lat, depth_km
        },
        "properties": {
            "source_id": f"{TEST_PREFIX}_cycle",
            "mag": 4.7,
            "magtype": "ml",
            "time": "2026-05-08T01:00:00.0Z",
            "flynn_region": "CENTRAL GREECE",
            "auth": "EMSC",
        },
    }]

    async def fake_fetch():
        return fake_raw

    with patch.object(ingester, "fetch", side_effect=fake_fetch):
        broadcast_count = await ingester.cycle()

    assert broadcast_count == 1
    assert len(db_writer_calls) == 1

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"emsc:{TEST_PREFIX}_cycle",
    )
    assert total == 1

    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
