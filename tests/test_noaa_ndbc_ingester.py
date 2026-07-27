"""
NOAA NDBC ingester + writer tests — P2-B Phase 1.5 noaa_buoys
layer's live-data upgrade.

Source: https://www.ndbc.noaa.gov/data/realtime2/<station>.txt
License: US gov public domain (NOAA NDBC) — Title 17 USC § 105.

The ingester pulls per-station realtime observations (wave height,
wind, sea-surface temperature, atmospheric pressure) for the 14
hand-curated noaa_buoys stations at 30-min cadence. Data flows into
the event hypertable as event_type='ndbc_observation' rows with
subtype = station id.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_noaa_ndbc_ingester.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.noaa_ndbc import (  # noqa: E402
    NoaaNdbcIngester,
    STATIONS,
    _parse_realtime2_line,
    _severity_for_wave_height,
    _build_event_id,
)
from ingesters.base import GlassboxEvent  # noqa: E402
from db import init_pool, close_pool, fetch, execute  # noqa: E402
from writers import write_noaa_ndbc_events  # noqa: E402


TEST_EXTID_PREFIX = "ndbc:test-"


# ─── Identity ────────────────────────────────────────────────────────────


def test_ingester_layer_and_source_id():
    ing = NoaaNdbcIngester(broadcaster=lambda *_: None)
    assert ing.layer == "noaa_buoys"
    assert ing.source_id == "noaa_ndbc"
    # 30min poll cadence — NDBC observations refresh every 10-30 min
    assert ing.poll_interval_sec == 1800.0


def test_stations_match_static_seed():
    """Ingester's STATIONS list must contain the same 14 stations
    the static seed at data/noaa_buoys.geojson surfaces."""
    assert len(STATIONS) == 14
    ids = {s["station_id"] for s in STATIONS}
    expected = {
        "46006", "46059", "46086",     # Pacific NW + Pacific
        "42001", "42040",               # Gulf
        "41001", "41010", "44025",     # Atlantic
        "46035", "46073",               # Alaska
        "51001", "51002", "51101",     # Hawaii
        "46089",                        # Pacific NW
    }
    assert ids == expected


# ─── Parser ──────────────────────────────────────────────────────────────


def test_parse_realtime2_line_basic():
    """Canonical line: whitespace-separated YY MM DD hh mm + 14 fields."""
    line = "2026 05 28 00 10 310  5.0  7.0   2.5    8    6  150 1027.5  14.9  12.3   8.0   MM   -0.9   MM"
    parsed = _parse_realtime2_line(line)
    assert parsed is not None
    assert parsed["year"] == 2026
    assert parsed["month"] == 5
    assert parsed["day"] == 28
    assert parsed["hour"] == 0
    assert parsed["minute"] == 10
    assert parsed["wind_dir"] == 310
    assert parsed["wind_speed_ms"] == 5.0
    assert parsed["wave_height_m"] == 2.5
    assert parsed["dom_period_sec"] == 8
    assert parsed["pressure_hpa"] == 1027.5
    assert parsed["air_temp_c"] == 14.9
    assert parsed["sea_temp_c"] == 12.3


def test_parse_realtime2_line_missing_fields():
    """NDBC marks missing fields as 'MM'. Parser must return None for
    those slots without crashing."""
    line = "2026 05 28 00 10 310  5.0  7.0    MM    MM    MM  MM 1027.5  14.9    MM    MM   MM    MM   MM"
    parsed = _parse_realtime2_line(line)
    assert parsed is not None
    assert parsed["wave_height_m"] is None
    assert parsed["sea_temp_c"] is None
    assert parsed["pressure_hpa"] == 1027.5
    assert parsed["air_temp_c"] == 14.9


def test_parse_realtime2_line_header_returns_none():
    """Comment lines (leading `#`) must be filtered out."""
    assert _parse_realtime2_line("#YY  MM DD hh mm WDIR WSPD ...") is None
    assert _parse_realtime2_line("#yr  mo dy hr mn degT m/s  ...") is None


def test_parse_realtime2_line_too_few_fields_returns_none():
    """Lines with fewer than 5 timestamp fields can't be parsed."""
    assert _parse_realtime2_line("2026") is None
    assert _parse_realtime2_line("") is None
    assert _parse_realtime2_line("just garbage") is None


# ─── Severity ────────────────────────────────────────────────────────────


def test_severity_calm_seas():
    """Wave height < 1m → severity 1 (calm)."""
    assert _severity_for_wave_height(0.5) <= 2


def test_severity_choppy():
    """Wave height 1-3m → severity 2-4 (choppy)."""
    s = _severity_for_wave_height(2.0)
    assert 2 <= s <= 4


def test_severity_rough_seas():
    """Wave height 3-6m → severity 5-7 (rough)."""
    s = _severity_for_wave_height(4.5)
    assert 5 <= s <= 7


def test_severity_storm_seas():
    """Wave height ≥6m → severity 8+ (storm)."""
    assert _severity_for_wave_height(8.0) >= 8


def test_severity_none_safe():
    """None wave height → low ambient severity."""
    assert _severity_for_wave_height(None) <= 2


# ─── Event ID derivation ─────────────────────────────────────────────────


def test_event_id_unique_per_station_per_timestamp():
    """Two distinct (station, ts) combinations yield distinct
    external_ids — keeps the writer's UUID5 dedup at the observation
    granularity."""
    id_a = _build_event_id("46006", "2026-05-28T00:10:00+00:00")
    id_b = _build_event_id("46006", "2026-05-28T00:20:00+00:00")
    id_c = _build_event_id("46089", "2026-05-28T00:10:00+00:00")
    assert id_a != id_b
    assert id_a != id_c
    # Same input → same output (deterministic)
    assert _build_event_id("46006", "2026-05-28T00:10:00+00:00") == id_a


# ─── normalize() ─────────────────────────────────────────────────────────


def _make_raw(station_id: str, lines):
    """Mock fetch() output — a per-station dict with the raw text."""
    return {
        "station_id": station_id,
        "lines": list(lines),
    }


def test_normalize_emits_one_event_per_line():
    ing = NoaaNdbcIngester(broadcaster=lambda *_: None)
    raw = [_make_raw("46006", [
        "2026 05 28 00 10 310  5.0  7.0   2.5    8    6  150 1027.5  14.9  12.3   8.0   MM   -0.9   MM",
        "2026 05 28 00 00 310  5.0  7.0   2.4    8    6  150 1027.6  14.8  12.3   8.0   MM   -0.9   MM",
    ])]
    events = ing.normalize(raw)
    assert len(events) == 2


def test_normalize_event_shape():
    ing = NoaaNdbcIngester(broadcaster=lambda *_: None)
    raw = [_make_raw("46006", [
        "2026 05 28 00 10 310  5.0  7.0   2.5    8    6  150 1027.5  14.9  12.3   8.0   MM   -0.9   MM",
    ])]
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "noaa_buoys"
    assert e.kind == "ndbc_observation"
    assert "46006" in e.external_id
    # Match against STATIONS for 46006 (Cape Beale CA, -137.382, 46.844)
    assert abs(e.lat - 46.844) < 0.01
    assert abs(e.lng - (-137.382)) < 0.01
    assert e.payload["station_id"] == "46006"
    assert e.payload["wave_height_m"] == 2.5
    assert e.payload["sea_temp_c"] == 12.3
    assert e.payload["wind_speed_ms"] == 5.0
    assert e.payload["pressure_hpa"] == 1027.5


def test_normalize_skips_unknown_station():
    """A station_id not in STATIONS is silently skipped — keeps the
    layer's data scope aligned with the static seed."""
    ing = NoaaNdbcIngester(broadcaster=lambda *_: None)
    raw = [_make_raw("99999", [   # not in STATIONS
        "2026 05 28 00 10 310  5.0  7.0   2.5    8    6  150 1027.5  14.9  12.3   8.0   MM   -0.9   MM",
    ])]
    assert ing.normalize(raw) == []


def test_normalize_caps_per_station_observations():
    """Defensive: per station, emit at most N most-recent observations
    so a 14-station × 1000-line backfill doesn't dump 14K events into
    a single cycle."""
    lines = []
    for i in range(50):
        lines.append(f"2026 05 28 00 {i:02d} 310  5.0  7.0   2.5    8    6  150 1027.5  14.9  12.3   8.0   MM   -0.9   MM")
    raw = [_make_raw("46006", lines)]
    ing = NoaaNdbcIngester(broadcaster=lambda *_: None)
    events = ing.normalize(raw)
    # Cap: at most 10 observations per station per cycle (most-recent first)
    assert len(events) <= 10


def test_normalize_empty_returns_empty():
    ing = NoaaNdbcIngester(broadcaster=lambda *_: None)
    assert ing.normalize([]) == []
    assert ing.normalize([_make_raw("46006", [])]) == []


# ─── Writer (real Postgres) ──────────────────────────────────────────────


@pytest.fixture(autouse=False)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_ndbc(_pool):
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='ndbc_observation' "
            "AND properties->>'external_id' LIKE $1",
            f"{TEST_EXTID_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _sample_event(ext_suffix: str, **overrides) -> GlassboxEvent:
    payload = {
        "station_id": "TESTSTN",
        "wave_height_m": 2.5,
        "sea_temp_c": 12.3,
        "wind_speed_ms": 5.0,
        "wind_dir_deg": 310,
        "pressure_hpa": 1027.5,
        "air_temp_c": 14.9,
        "observed_at": "2099-01-01T00:10:00+00:00",
        "title": "NDBC TESTSTN observation (2099-01-01T00:10:00+00:00)",
        "_attribution": "NDBC observation: NOAA National Data Buoy Center",
    }
    payload.update(overrides.pop("payload_overrides", {}))
    return GlassboxEvent(
        layer="noaa_buoys",
        external_id=f"{TEST_EXTID_PREFIX}{ext_suffix}",
        kind="ndbc_observation",
        lat=46.844,
        lng=-137.382,
        ts="2099-01-01T00:10:00+00:00",
        severity=2,
        source="NOAA NDBC realtime2",
        payload=payload,
        domain="geo",
        decay_half_life_min=240,
        **overrides,
    )


async def test_writer_persists_observation(_clean_ndbc):
    ev = _sample_event("W1")
    n = await write_noaa_ndbc_events([ev])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype, severity, title, properties "
        "FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_EXTID_PREFIX}W1",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "ndbc_observation"
    assert row["event_subtype"] == "TESTSTN"
    import json as _json
    props = _json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
    assert props["wave_height_m"] == 2.5
    assert props["sea_temp_c"] == 12.3


async def test_writer_idempotent(_clean_ndbc):
    ev = _sample_event("IDM")
    assert await write_noaa_ndbc_events([ev]) == 1
    assert await write_noaa_ndbc_events([ev]) == 0


async def test_writer_skips_wrong_layer(_pool):
    ev = _sample_event("WRONG")
    ev.layer = "hacker_news"
    assert await write_noaa_ndbc_events([ev]) == 0


async def test_writer_zero_events_is_noop():
    assert await write_noaa_ndbc_events([]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
