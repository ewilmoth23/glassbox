"""
NOAA NHC tropical-cyclone ingester + writer tests.

Asserts:
  Coord parser
  - "21.1N" → 21.1
  - "94.4W" → -94.4
  - "21.1"  → 21.1 (bare numeric)
  - bad input → None
  Severity scaling
  - 30 kt (TD) → 3
  - 50 kt (TS) → 5
  - 75 kt (Cat 1) → 7
  - 100 kt (Cat 2) → 8
  - 120 kt (Cat 3) → 9
  - 140 kt (Cat 4) → 10
  Normalize
  - Empty activeStorms → 0 events
  - Synthetic Cat-1 hurricane → GlassboxEvent w/ severity 7, layer='tropical_storms'
  - Tropical depression with NULL coords → skipped
  - Hurricane with classification='HU' → market_tags include 'weather:hurricane'
  Writer
  - One storm in → one event row out, name + classification + wind in properties
  - Re-run with same external_id + same ts → idempotent (ON CONFLICT DO NOTHING)
  - Re-run with same external_id + DIFFERENT ts → new row (advisory timeline)
  - Layer mismatch → skipped

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_nhc_storms.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from ingesters.nhc_storms import (  # noqa: E402
    NhcStormsIngester,
    _parse_coord, _severity_from_wind_kt,
)
from writers import write_tropical_storm_events  # noqa: E402


TEST_PREFIX = "nhc:test12_"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_storm_events():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='tropical_storm' "
            "AND properties->>'external_id' LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Coord parser tests ──────────────────────────────────────────────────


def test_parse_coord_north_positive():
    assert _parse_coord("21.1N") == 21.1


def test_parse_coord_west_negative():
    assert _parse_coord("94.4W") == -94.4


def test_parse_coord_south_negative():
    assert _parse_coord("12.5S") == -12.5


def test_parse_coord_east_positive():
    assert _parse_coord("100.0E") == 100.0


def test_parse_coord_bare_numeric():
    assert _parse_coord("21.1") == 21.1


def test_parse_coord_signed_numeric():
    assert _parse_coord("-94.4") == -94.4


def test_parse_coord_none_input():
    assert _parse_coord(None) is None


def test_parse_coord_empty():
    assert _parse_coord("") is None


def test_parse_coord_garbage():
    assert _parse_coord("not a coord") is None


# ─── Severity tests ──────────────────────────────────────────────────────


def test_severity_tropical_depression():
    assert _severity_from_wind_kt(30) == 3


def test_severity_tropical_storm():
    assert _severity_from_wind_kt(50) == 5


def test_severity_cat1_hurricane():
    assert _severity_from_wind_kt(75) == 7


def test_severity_cat2_hurricane():
    """95 kt is upper edge of Cat 2 (83-95)."""
    assert _severity_from_wind_kt(95) == 8


def test_severity_cat3_major():
    """100 kt is Cat 3 major (96-112)."""
    assert _severity_from_wind_kt(100) == 9


def test_severity_cat4_major():
    """120 kt is Cat 4 major (113-136)."""
    assert _severity_from_wind_kt(120) == 10


def test_severity_cat5_capped():
    assert _severity_from_wind_kt(180) == 10


def test_severity_null_wind_default_5():
    assert _severity_from_wind_kt(None) == 5


# ─── Normalize ──────────────────────────────────────────────────────────


def test_normalize_empty_returns_zero_events():
    ing = NhcStormsIngester(broadcaster=lambda *_: None)
    assert ing.normalize([]) == []


def test_normalize_synthetic_cat1_hurricane():
    ing = NhcStormsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": f"{TEST_PREFIX}AL01",
        "name": "Alex",
        "classification": "HU",
        "intensity": "75",
        "pressure": "990",
        "latitude": "25.0N",
        "longitude": "75.0W",
        "movementDir": "270",
        "movementSpeed": "10",
        "lastUpdate": "2026-08-15T12:00:00.000Z",
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "tropical_storms"
    assert e.kind == "tropical_storm"
    assert e.severity == 7  # 75 kt = cat 1
    assert e.lat == 25.0
    assert e.lng == -75.0
    assert e.payload["wind_kt"] == 75.0
    assert e.payload["pressure_mb"] == 990.0
    assert e.payload["classification"] == "HU"
    assert e.payload["class_label"] == "hurricane"
    assert e.payload["name"] == "Alex"
    assert "weather:hurricane" in e.market_tags


def test_normalize_tropical_storm_market_tag():
    ing = NhcStormsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": f"{TEST_PREFIX}TS",
        "name": "Bonnie",
        "classification": "TS",
        "intensity": "50",
        "latitude": "20.0N",
        "longitude": "60.0W",
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert "weather:tropical_storm" in events[0].market_tags


def test_normalize_skips_storm_with_null_coords():
    ing = NhcStormsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": "X",
        "name": "Phantom",
        "classification": "TD",
        "intensity": "30",
        # latitude/longitude missing
    }]
    assert ing.normalize(raw) == []


def test_normalize_skips_storm_with_blank_id():
    ing = NhcStormsIngester(broadcaster=lambda *_: None)
    raw = [{
        "name": "Anonymous",
        "classification": "TS",
        "latitude": "20N", "longitude": "70W",
    }]
    assert ing.normalize(raw) == []


# ─── Writer ──────────────────────────────────────────────────────────────


async def test_writer_persists_storm(_clean_storm_events):
    ev = GlassboxEvent(
        layer="tropical_storms",
        external_id=f"{TEST_PREFIX}AL01",
        kind="tropical_storm",
        lat=25.0, lng=-75.0,
        ts="2026-08-15T12:00:00.000Z",
        severity=7,
        source="NOAA NHC",
        payload={
            "storm_id": "AL012026",
            "name": "Alex",
            "classification": "HU",
            "class_label": "hurricane",
            "wind_kt": 75.0,
            "pressure_mb": 990.0,
            "movement_dir": 270.0,
            "movement_kt": 10.0,
        },
        domain="atmospheric",
        decay_half_life_min=720,
    )
    n = await write_tropical_storm_events([ev])
    assert n == 1

    rows = await fetch(
        "SELECT event_type, event_subtype, severity, title, properties FROM event "
        "WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}AL01",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "tropical_storm"
    assert r["event_subtype"] == "hurricane"
    assert r["severity"] == 7
    assert "Alex" in r["title"]


async def test_writer_idempotent_same_advisory(_clean_storm_events):
    """Re-running with same external_id + same ts → no new row."""
    ev = GlassboxEvent(
        layer="tropical_storms",
        external_id=f"{TEST_PREFIX}AL02",
        kind="tropical_storm",
        lat=20.0, lng=-60.0,
        ts="2026-08-15T12:00:00.000Z",
        severity=5,
        payload={"storm_id": "AL022026", "name": "Bonnie", "classification": "TS",
                 "class_label": "tropical_storm", "wind_kt": 50.0},
        domain="atmospheric",
        decay_half_life_min=720,
    )
    n1 = await write_tropical_storm_events([ev])
    assert n1 == 1
    n2 = await write_tropical_storm_events([ev])
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}AL02",
    )
    assert total == 1


async def test_writer_new_advisory_creates_new_row(_clean_storm_events):
    """Same external_id but DIFFERENT ts → new row (advisory timeline)."""
    base = {
        "layer": "tropical_storms",
        "external_id": f"{TEST_PREFIX}AL03",
        "kind": "tropical_storm",
        "lat": 25.0, "lng": -75.0,
        "severity": 6,
        "payload": {"storm_id": "AL032026", "name": "Cara",
                    "classification": "HU", "class_label": "hurricane",
                    "wind_kt": 70.0},
        "domain": "atmospheric",
        "decay_half_life_min": 720,
    }
    ev1 = GlassboxEvent(**base, ts="2026-08-15T12:00:00.000Z")
    ev2 = GlassboxEvent(**base, ts="2026-08-15T18:00:00.000Z")  # 6h later
    n1 = await write_tropical_storm_events([ev1])
    n2 = await write_tropical_storm_events([ev2])
    assert n1 == 1
    assert n2 == 1   # new advisory at later ts → new row

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}AL03",
    )
    assert total == 2


async def test_writer_skips_wrong_layer():
    ev = GlassboxEvent(
        layer="planes",
        external_id=f"{TEST_PREFIX}WRONG",
        kind="tropical_storm",
        lat=0, lng=0,
        ts=datetime.now(timezone.utc).isoformat(),
    )
    assert await write_tropical_storm_events([ev]) == 0


async def test_writer_zero_events_is_noop():
    assert await write_tropical_storm_events([]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
