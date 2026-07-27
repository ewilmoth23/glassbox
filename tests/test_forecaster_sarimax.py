"""
Phase 5b — SARIMAX-based forecaster tests.

Asserts:
  - _hourly_severity_series buckets samples into the right hour cells
  - _sarimax_forecast on a known periodic input returns a forecast that
    captures the period (forecast values aren't constant, and the diurnal
    rhythm shows up over the 48-hour horizon)
  - _sarimax_forecast on degenerate input (constant or too-short series)
    returns None — caller falls back to recency-weighted score
  - score_hotspots merges the two paths correctly:
      * sufficient periodic history → method='sarimax'
      * single anomaly with no real history → method='recency_weighted_fallback'
  - score_hotspots preserves evidence_trail and max_historical_severity
    on both paths

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_forecaster_sarimax.py -v
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecaster import (  # noqa: E402
    _hourly_severity_series,
    _recency_weighted_score,
    _sarimax_forecast,
    score_hotspots,
)


# ─── Pure-fn series builder ───────────────────────────────────────────────


def test_hourly_severity_series_buckets_correctly():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    samples = [
        (now - timedelta(hours=1, minutes=15), 5),   # 1h ago -> bucket index 70
        (now - timedelta(hours=1, minutes=45), 3),   # 1h ago -> same bucket
        (now - timedelta(hours=2, minutes=10), 7),   # 2h ago -> bucket index 69
        (now - timedelta(hours=99, minutes=0), 99),  # outside 72h window
    ]
    series = _hourly_severity_series(samples, history_window_h=72, now=now)
    assert len(series) == 72
    # bucket 70 = 1h ago = severity 5+3 = 8
    assert series[70] == pytest.approx(8.0)
    # bucket 69 = 2h ago = severity 7
    assert series[69] == pytest.approx(7.0)
    # outside-window sample dropped
    assert sum(series) == pytest.approx(15.0)
    # rest of buckets are zero
    nonzero = [i for i, v in enumerate(series) if v > 0]
    assert nonzero == [69, 70]


def test_hourly_severity_series_all_zero_when_empty():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    series = _hourly_severity_series([], history_window_h=24, now=now)
    assert series == [0.0] * 24


# ─── SARIMAX forecast pure-fn ─────────────────────────────────────────────


def _periodic_series(periods: int = 4, period_h: int = 24, base: float = 3.0,
                     amp: float = 4.0) -> list:
    """Diurnal pattern: peak at hour=12, trough at hour=0; `periods` days."""
    series = []
    for cycle in range(periods):
        for h in range(period_h):
            angle = 2.0 * math.pi * h / period_h
            v = base + amp * (1.0 + math.sin(angle - math.pi / 2.0)) / 2.0  # 0 at h=0, 1 at h=12
            # add a tiny linear drift so series isn't perfectly periodic
            v += cycle * 0.1
            series.append(max(0.0, v))
    return series


def test_sarimax_forecast_short_series_returns_none():
    # 12 points is below the 2*seasonal_period=48 minimum
    short = [1.0, 2.0, 1.5, 2.5] * 3
    fc = _sarimax_forecast(short, horizon_h=48)
    assert fc is None


def test_sarimax_forecast_constant_series_returns_none():
    # Constant 100h series — degenerate; SARIMAX would either fail or return
    # a useless flat forecast. Our code short-circuits → None.
    flat = [3.0] * 100
    fc = _sarimax_forecast(flat, horizon_h=48)
    assert fc is None


def test_sarimax_forecast_periodic_series_returns_horizon_length_forecast():
    series = _periodic_series(periods=5, period_h=24)  # 120h history
    fc = _sarimax_forecast(series, horizon_h=48)
    assert fc is not None, "SARIMAX should fit a clearly periodic series"
    assert len(fc) == 48
    assert all(isinstance(x, float) for x in fc)
    assert all(x >= 0.0 for x in fc)
    # Forecast shouldn't be flat — periodic input should produce non-trivial variation
    assert max(fc) - min(fc) > 0.5


def test_sarimax_forecast_captures_diurnal_period():
    """A clearly periodic input should produce a forecast that itself is periodic."""
    series = _periodic_series(periods=6, period_h=24)
    fc = _sarimax_forecast(series, horizon_h=48)
    assert fc is not None
    # Compare hour-wise across the two forecasted days. With a 24-hour
    # seasonal period, day 1 (hours 0-23) and day 2 (hours 24-47) should
    # be similar — not identical, but more similar to each other than to
    # a random reshuffling.
    day1 = fc[:24]
    day2 = fc[24:48]
    # Mean absolute difference between corresponding hours
    same_hour_diff = sum(abs(a - b) for a, b in zip(day1, day2)) / 24.0
    # Mean absolute difference between mismatched hours (rotate by 12h)
    rotated = day1[12:] + day1[:12]
    rotated_diff = sum(abs(a - b) for a, b in zip(rotated, day2)) / 24.0
    assert same_hour_diff < rotated_diff, (
        f"24h-aligned difference {same_hour_diff:.3f} should be smaller "
        f"than 12h-rotated difference {rotated_diff:.3f}"
    )


# ─── score_hotspots integration ───────────────────────────────────────────


def _build_anomalies_with_periodic_history(layer: str, region: str,
                                            now: datetime) -> list:
    """
    Synthesize anomaly records covering 5 full days, peaking at hour=12 each
    day. Yields enough history for SARIMAX to fit.
    """
    out = []
    for hours_ago in range(120):
        t = now - timedelta(hours=hours_ago)
        # Peak severity at noon UTC, low at midnight; replicate per day
        local_h = t.hour
        sev = max(0, int(round(2 + 6 * (1 + math.sin(2 * math.pi * local_h / 24 - math.pi / 2)) / 2)))
        if sev > 0:
            out.append({
                "layer": layer,
                "region": region,
                "anomaly_severity": sev,
                "_logged_at": t.isoformat(),
                "z_score": 2.5,
                "direction": "spike",
                "sample": [{"external_id": f"id-{hours_ago}"}],
            })
    return out


def test_score_hotspots_periodic_history_uses_sarimax():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    anomalies = _build_anomalies_with_periodic_history("planes", "north_america", now)
    # Need history_window >=48 for sarimax min_required=48
    out = score_hotspots(anomalies, history_window_h=72, horizon_h=48, now=now)
    assert len(out) == 1
    bucket = out[0]
    assert bucket["layer"] == "planes"
    assert bucket["region"] == "north_america"
    assert bucket["method"] == "sarimax"
    assert bucket["score"] > 0
    assert "forecast_max" in bucket["forecast"]
    assert bucket["evidence_count"] >= 1
    assert bucket["max_historical_severity"] >= 1


def test_score_hotspots_short_history_falls_back():
    """With history_window < 2*seasonal_period (48h), SARIMAX skips → fallback."""
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    anomalies = [{
        "layer": "ships",
        "region": "atlantic",
        "anomaly_severity": 7,
        "_logged_at": (now - timedelta(hours=1)).isoformat(),
        "sample": [{"external_id": "ship-1"}],
    }]
    # 24h window < 48 -> _sarimax_forecast returns None -> fallback used
    out = score_hotspots(anomalies, history_window_h=24, horizon_h=48, now=now)
    assert len(out) == 1
    bucket = out[0]
    assert bucket["method"] == "recency_weighted_fallback"
    assert bucket["score"] > 0
    assert bucket["forecast"]["fallback_reason"] == "insufficient_history_or_degenerate_series"
    assert bucket["max_historical_severity"] == 7


def test_score_hotspots_constant_history_falls_back():
    """All-zero history at the window edge is degenerate → fallback path."""
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    # All anomalies fall outside the 48h window, leaving an all-zero series
    anomalies = [{
        "layer": "fires",
        "region": "europe_west",
        "anomaly_severity": 5,
        "_logged_at": (now - timedelta(hours=200)).isoformat(),
        "sample": [{"external_id": "f-1"}],
    }]
    out = score_hotspots(anomalies, history_window_h=72, horizon_h=48, now=now)
    assert len(out) == 1
    bucket = out[0]
    # Constant (all-zero) series → SARIMAX bails → fallback
    assert bucket["method"] == "recency_weighted_fallback"


def test_score_hotspots_empty_anomalies_returns_empty():
    out = score_hotspots([], now=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc))
    assert out == []


def test_score_hotspots_orders_by_score_desc():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    weak = [{
        "layer": "weak", "region": "atlantic", "anomaly_severity": 1,
        "_logged_at": (now - timedelta(hours=20)).isoformat(),
    }]
    strong = [{
        "layer": "strong", "region": "atlantic", "anomaly_severity": 9,
        "_logged_at": (now - timedelta(hours=1)).isoformat(),
    }]
    out = score_hotspots(weak + strong, history_window_h=72, horizon_h=48, now=now)
    assert len(out) == 2
    assert out[0]["layer"] == "strong"
    assert out[0]["score"] > out[1]["score"]


def test_recency_weighted_score_decays_with_age():
    """The fallback path's score must decay smoothly with sample age."""
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    fresh_score = _recency_weighted_score([(now - timedelta(hours=1), 5)], now)
    stale_score = _recency_weighted_score([(now - timedelta(hours=24), 5)], now)
    assert fresh_score > stale_score
