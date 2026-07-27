"""
NOAA SWPC ingester + writer tests.

Asserts:
  Parser
  - K-index 4 Warning  ('K04W') → kind='K', level=4, alert='W'
  - K-index 5 Alert    ('K05A') → kind='K', level=5, alert='A'
  - R-flare Alert      ('R02A') → kind='R', level=2
  - S-radiation Alert  ('S01A') → kind='S', level=1
  - Forecast 'FORECAST_FOO'    → None (we skip non-K/G/R/S categories for v1.0)
  Severity mapping
  - K=1 → 1; K=4 → 3; K=5 → 5; K=8 → 9; K=9 → 10
  - G=2 → 4; R=3 → 6; S=4 → 8
  Anchors
  - K/G alerts emit at (60, 0) — Arctic Circle
  - R alerts emit at (0, 0)    — equator
  - S alerts emit at (85, 0)   — polar cap
  Normalize
  - Live SWPC alert → GlassboxEvent w/ layer='space_weather', kind='swpc_alert'
  - external_id is unique per (product_id, issue_datetime)
  Writer
  - One alert in → one event row out, kind/severity/properties round-trip
  - Re-emit (same external_id) → idempotent (ON CONFLICT DO NOTHING)
  - Layer mismatch → skipped (don't catch DONKI events)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_noaa_swpc.py -v
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
from ingesters.noaa_swpc import (  # noqa: E402
    NoaaSwpcIngester,
    _parse_product_id, _severity_from_level, _short_headline, _ANCHORS,
)
from writers import write_space_weather_events  # noqa: E402


TEST_PREFIX = "swpc:test10"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_swpc_events():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='swpc_alert' "
            "AND properties->>'external_id' LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Parser tests ─────────────────────────────────────────────────────────


def test_parse_kindex_warning():
    p = _parse_product_id("K04W")
    assert p is not None
    assert p["kind"] == "K"
    assert p["level"] == 4
    assert p["alert"] == "W"
    assert p["alert_label"] == "warning"


def test_parse_kindex_alert():
    p = _parse_product_id("K05A")
    assert p["kind"] == "K"
    assert p["level"] == 5
    assert p["alert_label"] == "alert"


def test_parse_radio_alert():
    p = _parse_product_id("R02A")
    assert p["kind"] == "R"
    assert p["level"] == 2


def test_parse_solar_radiation():
    p = _parse_product_id("S01A")
    assert p["kind"] == "S"
    assert p["level"] == 1


def test_parse_unrecognized_returns_none():
    """Forecasts / advisories with non-standard product_ids return None
    (we skip them for v1.0)."""
    assert _parse_product_id("FORECAST_DAILY") is None
    assert _parse_product_id("WATM01") is None
    assert _parse_product_id("") is None


# ─── Severity mapping ─────────────────────────────────────────────────────


def test_severity_kindex_low_is_low():
    assert _severity_from_level("K", 1) == 1
    assert _severity_from_level("K", 3) == 1


def test_severity_kindex_4_is_3():
    assert _severity_from_level("K", 4) == 3


def test_severity_kindex_severe_capped_at_10():
    assert _severity_from_level("K", 8) == 9
    assert _severity_from_level("K", 9) == 10


def test_severity_g_storm_doubled():
    assert _severity_from_level("G", 2) == 4
    assert _severity_from_level("G", 5) == 10


def test_severity_r_flare_scaled():
    assert _severity_from_level("R", 3) == 6


# ─── Anchor coordinates ───────────────────────────────────────────────────


def test_anchor_kindex_arctic_circle():
    lat, lng = _ANCHORS["K"]
    assert lat == 60.0 and lng == 0.0


def test_anchor_radio_equator():
    lat, lng = _ANCHORS["R"]
    assert lat == 0.0 and lng == 0.0


def test_anchor_solar_radiation_polar_cap():
    lat, lng = _ANCHORS["S"]
    assert lat == 85.0


# ─── Headline parser ──────────────────────────────────────────────────────


def test_short_headline_picks_warning_line():
    msg = """Space Weather Message Code: WARK04
Serial Number: 5335
Issue Time: 2026 May 07 1637 UTC

WARNING: Geomagnetic K-index of 4 expected
Valid From: 2026 May 07 1635 UTC"""
    assert _short_headline(msg) == "WARNING: Geomagnetic K-index of 4 expected"


def test_short_headline_picks_alert_line():
    msg = """Space Weather Message Code: ALTK05
ALERT: Geomagnetic K-index of 5
NOAA Scale: G1 - Minor"""
    assert _short_headline(msg) == "ALERT: Geomagnetic K-index of 5"


def test_short_headline_empty_input():
    assert _short_headline("") == ""


# ─── Normalize ────────────────────────────────────────────────────────────


def test_normalize_emits_glassbox_event():
    ing = NoaaSwpcIngester(broadcaster=lambda *_: None)
    raw = [{
        "product_id": "K04W",
        "issue_datetime": "2026-05-07 16:37:40.453",
        "message": "WARNING: Geomagnetic K-index of 4 expected\r\nValid From: ...",
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "space_weather"
    assert e.kind == "swpc_alert"
    assert e.lat == 60.0  # K-index anchor
    assert e.severity == 3  # K=4 → severity 3
    assert e.payload["product_id"] == "K04W"
    assert e.payload["kind"] == "geomagnetic_kindex"
    assert e.payload["alert_kind"] == "warning"
    assert "K-index of 4" in e.payload["headline"]


def test_normalize_skips_unrecognized_product_id():
    ing = NoaaSwpcIngester(broadcaster=lambda *_: None)
    raw = [{
        "product_id": "FORECAST_X",
        "issue_datetime": "2026-05-07 16:37:40",
        "message": "Daily forecast bulletin",
    }]
    events = ing.normalize(raw)
    assert events == []


def test_normalize_external_id_unique_per_issuance():
    """Same product_id at two different issue_datetimes → different external_ids."""
    ing = NoaaSwpcIngester(broadcaster=lambda *_: None)
    raw = [
        {"product_id": "K05A", "issue_datetime": "2026-05-07 12:00:00",
         "message": "ALERT: K=5"},
        {"product_id": "K05A", "issue_datetime": "2026-05-07 18:00:00",
         "message": "ALERT: K=5"},
    ]
    events = ing.normalize(raw)
    assert len(events) == 2
    assert events[0].external_id != events[1].external_id


# ─── Writer ───────────────────────────────────────────────────────────────


async def test_writer_persists_event(_clean_swpc_events):
    ev = GlassboxEvent(
        layer="space_weather",
        external_id=f"{TEST_PREFIX}:K04W:2026-05-07 16:37:40",
        kind="swpc_alert",
        lat=60.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=3,
        source="NOAA SWPC",
        payload={
            "product_id": "K04W",
            "kind": "geomagnetic_kindex",
            "alert_kind": "warning",
            "level": 4,
            "headline": "WARNING: Geomagnetic K-index of 4 expected",
            "message": "...",
        },
        domain="atmospheric",
        decay_half_life_min=720,
    )
    n = await write_space_weather_events([ev])
    assert n == 1

    rows = await fetch(
        "SELECT event_type, event_subtype, severity, title, properties FROM event "
        "WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}:K04W:2026-05-07 16:37:40",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "swpc_alert"
    assert r["event_subtype"] == "geomagnetic_kindex"
    assert r["severity"] == 3
    assert "K-index of 4" in r["title"]


async def test_writer_is_idempotent(_clean_swpc_events):
    ev = GlassboxEvent(
        layer="space_weather",
        external_id=f"{TEST_PREFIX}:R02A:2026-05-07 17:00:00",
        kind="swpc_alert",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=4,
        payload={"product_id": "R02A", "kind": "radio_blackout",
                 "alert_kind": "alert", "level": 2,
                 "headline": "ALERT: R2 — minor radio blackout"},
        domain="atmospheric",
        decay_half_life_min=720,
    )
    n1 = await write_space_weather_events([ev])
    assert n1 == 1
    n2 = await write_space_weather_events([ev])
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}:R02A:2026-05-07 17:00:00",
    )
    assert total == 1


async def test_writer_skips_non_swpc_kind():
    """Defensive: don't catch DONKI events (kind != 'swpc_alert')."""
    ev = GlassboxEvent(
        layer="space_weather",
        external_id=f"{TEST_PREFIX}:donki1",
        kind="donki_flare",   # NOT swpc_alert
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=5,
    )
    n = await write_space_weather_events([ev])
    assert n == 0


async def test_writer_skips_wrong_layer():
    ev = GlassboxEvent(
        layer="planes",  # WRONG
        external_id=f"{TEST_PREFIX}:wrong",
        kind="swpc_alert",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
    )
    n = await write_space_weather_events([ev])
    assert n == 0


async def test_writer_zero_events_is_noop():
    n = await write_space_weather_events([])
    assert n == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
