"""
Open-Meteo Forecast ingester + writer tests — P2-B Phase 1
climate_forecast layer's live-data upgrade.

Source: https://api.open-meteo.com/v1/forecast
License: CC-BY 4.0 (commercial use permitted with attribution per
https://open-meteo.com/en/license).

The ingester pulls daily temp_max + temp_min + precipitation_sum for
15 hand-curated world cities at 6h cadence (Open-Meteo's update
frequency). Data flows into the event hypertable as
event_type='climate_forecast' rows, subtype = city name.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_open_meteo_forecast_ingester.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.open_meteo_forecast import (  # noqa: E402
    OpenMeteoForecastIngester,
    CITIES,
    _severity_for_temp,
)
from ingesters.base import GlassboxEvent  # noqa: E402
from db import init_pool, close_pool, fetch, execute  # noqa: E402
from writers import write_open_meteo_forecast_events  # noqa: E402


TEST_EXTID_PREFIX = "om-forecast:test-"


# ─── Constants / identity ────────────────────────────────────────────────


def test_ingester_layer_and_source_id():
    ing = OpenMeteoForecastIngester(broadcaster=lambda *_: None)
    assert ing.layer == "climate_forecast"
    assert ing.source_id == "open_meteo_forecast"
    # 6h poll cadence — matches Open-Meteo's forecast refresh frequency
    assert ing.poll_interval_sec == 21600.0


def test_cities_list_matches_static_seed():
    """The ingester's CITIES list must contain the same 15 cities the
    static seed at data/climate_forecast.geojson surfaces — keeps the
    layer's content consistent whether the route serves the static
    seed or DB-derived data."""
    assert len(CITIES) == 15
    names = {c["name"] for c in CITIES}
    expected = {
        "New York", "London", "Tokyo", "Delhi", "Rio de Janeiro",
        "Sydney", "Moscow", "Mexico City", "Cairo", "Paris",
        "Beijing", "Chicago", "Johannesburg", "Singapore", "Buenos Aires",
    }
    assert names == expected


# ─── Severity helper ─────────────────────────────────────────────────────


def test_severity_extreme_heat():
    """Temps ≥40°C → severity 8 (heat wave alarm)."""
    assert _severity_for_temp(42.0) >= 8


def test_severity_hot():
    """Temps 30-40°C → severity 5-7 range."""
    s = _severity_for_temp(35.0)
    assert 5 <= s <= 7


def test_severity_temperate():
    """Temps 10-30°C → severity 1-3 range (ambient)."""
    assert _severity_for_temp(20.0) <= 3


def test_severity_extreme_cold():
    """Temps ≤-20°C → severity 8 (cold snap alarm)."""
    assert _severity_for_temp(-25.0) >= 8


def test_severity_none_safe():
    """None temp must not crash — defaults to ambient (0-2)."""
    assert _severity_for_temp(None) <= 2


# ─── normalize() — fed Open-Meteo's array response shape ─────────────────


def _sample_response(*cities):
    """Open-Meteo returns a flat array when multiple lat/lng are queried.
    Each element has daily.time + daily.temperature_2m_max etc."""
    return list(cities)


def _city_response(lat, lng, t_max, t_min, precip, date_iso="2026-05-27"):
    return {
        "latitude": lat,
        "longitude": lng,
        "timezone": "UTC",
        "daily_units": {
            "temperature_2m_max": "°C",
            "temperature_2m_min": "°C",
            "precipitation_sum": "mm",
        },
        "daily": {
            "time": [date_iso],
            "temperature_2m_max": [t_max],
            "temperature_2m_min": [t_min],
            "precipitation_sum": [precip],
        },
    }


def test_normalize_one_event_per_city():
    ing = OpenMeteoForecastIngester(broadcaster=lambda *_: None)
    raw = _sample_response(
        _city_response(40.7, -73.9, 22.5, 13.1, 0.0),
        _city_response(51.5, -0.1, 14.2, 8.4, 1.2),
    )
    events = ing.normalize(raw)
    assert len(events) == 2
    by_lat = {round(e.lat): e for e in events}
    assert by_lat[41].payload["temp_max_c"] == 22.5
    assert by_lat[52].payload["temp_max_c"] == 14.2


def test_normalize_event_shape():
    ing = OpenMeteoForecastIngester(broadcaster=lambda *_: None)
    raw = _sample_response(
        _city_response(40.7, -73.9, 24.0, 16.0, 2.5, date_iso="2026-05-27"),
    )
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "climate_forecast"
    assert e.kind == "climate_forecast"
    # external_id encodes city + date so the writer's UUID5 is deterministic
    assert "2026-05-27" in e.external_id
    assert e.lat == 40.7
    assert e.lng == -73.9
    assert e.payload["temp_max_c"] == 24.0
    assert e.payload["temp_min_c"] == 16.0
    assert e.payload["precipitation_mm"] == 2.5
    assert e.payload["forecast_date"] == "2026-05-27"
    assert "_attribution" in e.payload


def test_normalize_drops_entries_missing_daily():
    """Defensive: an Open-Meteo response missing the 'daily' block must
    be dropped, not crash."""
    ing = OpenMeteoForecastIngester(broadcaster=lambda *_: None)
    raw = _sample_response(
        _city_response(40.7, -73.9, 22.0, 14.0, 0.0),
        {"latitude": 51.5, "longitude": -0.1, "error": "timeout"},  # no daily
    )
    events = ing.normalize(raw)
    assert len(events) == 1


def test_normalize_severity_for_extreme_max():
    """A city with t_max ≥40°C must emit at higher severity than a
    temperate-day city. Open-Meteo response array is matched by
    request-order against CITIES, so the response lat/lng don't drive
    the assertion — the temperatures do."""
    ing = OpenMeteoForecastIngester(broadcaster=lambda *_: None)
    # Two responses → matched against CITIES[0] (New York) and
    # CITIES[1] (London) by request-order. Temp values drive severity.
    raw = _sample_response(
        _city_response(40.7, -73.9, 20.0, 12.0, 0.0),    # temperate
        _city_response(51.5, -0.1, 45.0, 35.0, 0.0),     # synthetic extreme
    )
    events = ing.normalize(raw)
    temperate = next(e for e in events if e.payload["city"] == "New York")
    extreme = next(e for e in events if e.payload["city"] == "London")
    assert temperate.severity < extreme.severity


def test_normalize_empty_array_returns_empty():
    ing = OpenMeteoForecastIngester(broadcaster=lambda *_: None)
    assert ing.normalize([]) == []


# ─── Writer (real Postgres) ──────────────────────────────────────────────


@pytest.fixture(autouse=False)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_climate(_pool):
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='climate_forecast' "
            "AND properties->>'external_id' LIKE $1",
            f"{TEST_EXTID_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _sample_event(ext_suffix: str, **overrides) -> GlassboxEvent:
    payload = {
        "city": "TestCity",
        "temp_max_c": 22.5,
        "temp_min_c": 13.1,
        "precipitation_mm": 0.0,
        "forecast_date": "2099-01-01",
        "title": "Climate forecast — TestCity (2099-01-01)",
        "_attribution": "Climate / weather forecast: Open-Meteo",
    }
    payload.update(overrides.pop("payload_overrides", {}))
    return GlassboxEvent(
        layer="climate_forecast",
        external_id=f"{TEST_EXTID_PREFIX}{ext_suffix}",
        kind="climate_forecast",
        lat=40.0,
        lng=-70.0,
        ts="2099-01-01T00:00:00+00:00",
        severity=2,
        source="Open-Meteo Forecast",
        payload=payload,
        domain="geo",
        decay_half_life_min=720,
        **overrides,
    )


async def test_writer_persists_climate_row(_clean_climate):
    ev = _sample_event("W1")
    n = await write_open_meteo_forecast_events([ev])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype, severity, title, properties "
        "FROM event WHERE properties->>'external_id' = $1",
        f"{TEST_EXTID_PREFIX}W1",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "climate_forecast"
    assert row["event_subtype"] == "TestCity"
    import json as _json
    props = _json.loads(row["properties"]) if isinstance(row["properties"], str) else row["properties"]
    assert props["temp_max_c"] == 22.5
    assert props["forecast_date"] == "2099-01-01"


async def test_writer_idempotent(_clean_climate):
    ev = _sample_event("IDM")
    assert await write_open_meteo_forecast_events([ev]) == 1
    assert await write_open_meteo_forecast_events([ev]) == 0


async def test_writer_skips_wrong_layer(_pool):
    ev = _sample_event("WRONG")
    ev.layer = "hacker_news"
    assert await write_open_meteo_forecast_events([ev]) == 0


async def test_writer_zero_events_is_noop():
    assert await write_open_meteo_forecast_events([]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
