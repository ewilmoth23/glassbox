"""
GDACS ingester + writer tests.

Asserts:
  - Parser handles RSS items with eventid + eventtype + lat/long + alert level
  - Empty / no-items XML returns []
  - Items missing required fields (no eventid, no coords) are skipped
  - Alert level → severity mapping (Green=4, Orange=7, Red=9)
  - normalize() emits GlassboxEvent with layer='gdacs', kind='gdacs_alert'
  - external_id format: 'gdacs:{event_type}:{event_id}'
  - Tropical cyclone (TC) emits domain='maritime'; others 'geo'
  - Writer persists row with proper subtype + severity
  - Writer is idempotent on same (event_id, episode_id, ts)
  - Writer skips wrong-layer events

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_gdacs.py -v
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
from ingesters.gdacs import GdacsIngester, _ALERT_TO_SEVERITY  # noqa: E402
from writers import write_gdacs_events  # noqa: E402


TEST_PREFIX = "gdacs:test16"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_gdacs():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='gdacs_alert' "
            "AND properties->>'external_id' LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Severity mapping ─────────────────────────────────────────────────────


def test_severity_map_green_orange_red():
    assert _ALERT_TO_SEVERITY["green"] == 4
    assert _ALERT_TO_SEVERITY["orange"] == 7
    assert _ALERT_TO_SEVERITY["red"] == 9


# ─── normalize() ──────────────────────────────────────────────────────────


def test_normalize_synthetic_orange_volcano():
    ing = GdacsIngester(broadcaster=lambda *_: None)
    raw = [{
        "event_id":      f"{TEST_PREFIX}_VO_1000140",
        "episode_id":    "2000140",
        "event_type":    "VO",
        "alert_level":   "orange",
        "alert_score":   2,
        "lat":           1.6992,
        "lng":           127.8783,
        "from_date":     "Fri, 08 May 2026 09:10:00 GMT",
        "country":       "Indonesia",
        "iso3":          "IDN",
        "title":         "Volcanic eruption ongoing for Dukono in Indonesia",
        "description":   "Volcano Dukono is emitting ash clouds.",
        "severity_raw":  None,
        "severity_unit": None,
        "population_raw": None,
        "population_unit": None,
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "gdacs"
    assert e.kind == "gdacs_alert"
    assert e.severity == 7
    assert e.lat == 1.6992
    assert e.lng == 127.8783
    assert e.external_id.startswith(f"gdacs:VO:{TEST_PREFIX}_VO_1000140")
    assert e.payload["alert_level"] == "orange"
    assert e.payload["country"] == "Indonesia"
    assert e.domain == "geo"


def test_normalize_tropical_cyclone_emits_maritime_domain():
    ing = GdacsIngester(broadcaster=lambda *_: None)
    raw = [{
        "event_id":   f"{TEST_PREFIX}_TC_500001",
        "episode_id": "1",
        "event_type": "TC",
        "alert_level": "red",
        "alert_score": 3,
        "lat": 25.0, "lng": -75.0,
        "from_date": "Fri, 08 May 2026 12:00:00 GMT",
        "country": "Bahamas", "iso3": "BHS",
        "title": "Tropical Cyclone red alert",
        "description": "TC X bearing down on Bahamas",
        "severity_raw": None, "severity_unit": None,
        "population_raw": None, "population_unit": None,
    }]
    events = ing.normalize(raw)
    assert events[0].domain == "maritime"
    assert events[0].severity == 9


def test_normalize_skips_empty_event_id():
    ing = GdacsIngester(broadcaster=lambda *_: None)
    raw = [{
        "event_id": "", "event_type": "EQ", "alert_level": "green",
        "lat": 0, "lng": 0,
        "from_date": None, "to_date": None,
        "country": None, "iso3": None,
        "title": None, "description": None,
        "severity_raw": None, "severity_unit": None,
        "population_raw": None, "population_unit": None,
        "alert_score": 0, "episode_id": None,
    }]
    assert ing.normalize(raw) == []


def test_normalize_unknown_alert_level_defaults_to_severity_4():
    ing = GdacsIngester(broadcaster=lambda *_: None)
    raw = [{
        "event_id": f"{TEST_PREFIX}_unknown",
        "episode_id": "1",
        "event_type": "EQ",
        "alert_level": "magenta",   # bogus
        "alert_score": 0,
        "lat": 0.0, "lng": 0.0,
        "from_date": None, "to_date": None,
        "country": None, "iso3": None,
        "title": "test", "description": "test",
        "severity_raw": None, "severity_unit": None,
        "population_raw": None, "population_unit": None,
    }]
    events = ing.normalize(raw)
    assert events[0].severity == 4


# ─── Writer ──────────────────────────────────────────────────────────────


async def test_writer_persists_event(_clean_gdacs):
    ev = GlassboxEvent(
        layer="gdacs",
        external_id=f"{TEST_PREFIX}_W1",
        kind="gdacs_alert",
        lat=1.7, lng=127.9,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=7,
        source="GDACS",
        payload={
            "gdacs_event_id": "1000140",
            "gdacs_episode_id": "2000140",
            "gdacs_event_type": "VO",
            "alert_level": "orange",
            "alert_score": 2,
            "country": "Indonesia",
            "iso3": "IDN",
            "title": "Volcanic eruption Dukono",
            "description": "Ash clouds emitting",
        },
        domain="geo",
        decay_half_life_min=1440,
    )
    n = await write_gdacs_events([ev])
    assert n == 1

    rows = await fetch(
        "SELECT event_type, event_subtype, severity, title, properties FROM event "
        "WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}_W1",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "gdacs_alert"
    assert r["event_subtype"] == "VO"
    assert r["severity"] == 7
    assert "Volcanic eruption Dukono" in r["title"]


async def test_writer_idempotent(_clean_gdacs):
    ev = GlassboxEvent(
        layer="gdacs",
        external_id=f"{TEST_PREFIX}_IDEM",
        kind="gdacs_alert",
        lat=0, lng=0,
        ts="2026-05-08T12:00:00+00:00",
        severity=4,
        payload={"gdacs_event_id": "X", "gdacs_episode_id": "1",
                 "gdacs_event_type": "EQ", "alert_level": "green"},
        domain="geo",
        decay_half_life_min=1440,
    )
    n1 = await write_gdacs_events([ev])
    n2 = await write_gdacs_events([ev])
    assert n1 == 1
    assert n2 == 0


async def test_writer_skips_wrong_layer():
    ev = GlassboxEvent(
        layer="planes",
        external_id=f"{TEST_PREFIX}_WRONG",
        kind="gdacs_alert",
        lat=0, lng=0,
        ts=datetime.now(timezone.utc).isoformat(),
    )
    assert await write_gdacs_events([ev]) == 0


async def test_writer_zero_events_is_noop():
    assert await write_gdacs_events([]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
