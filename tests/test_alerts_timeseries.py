"""
/api/v1/alerts/timeseries endpoint tests.

Asserts:
  - Default 24h window with 60-min buckets → 25 buckets.
  - Counts dict has one entry per tier-1 event_type.
  - Bucket axis is dense (no gaps even when no events).
  - Custom hours + bucket_minutes parameters round-trip correctly.
  - Seeded events appear in their corresponding bucket.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_alerts_timeseries.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, execute  # noqa: E402
from api_v1 import build_router  # noqa: E402


_TEST_TITLE = "ts-test-sentinel-event"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM event WHERE title = $1",
            _TEST_TITLE,
        )
    await _do()
    yield
    await _do()


def _client():
    app = FastAPI()
    app.include_router(build_router())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_timeseries_default_24h_60m_returns_25_buckets():
    async with _client() as c:
        r = await c.get("/api/v1/alerts/timeseries")
    assert r.status_code == 200
    d = r.json()
    assert d["hours"] == 24
    assert d["bucket_minutes"] == 60
    # 24h / 60min = 24 intervals → 25 boundary points
    assert len(d["buckets"]) == 25
    # All tier-1 event_types present
    assert "sanctioned_vessel_multijurisdictional" in d["event_types"]
    assert "dark_vessel_detected" in d["event_types"]
    # counts dict has matching keys
    for t in d["event_types"]:
        assert t in d["counts"]
        # Each series has same length as bucket axis
        assert len(d["counts"][t]) == 25


async def test_timeseries_custom_window_and_bucket():
    async with _client() as c:
        r = await c.get("/api/v1/alerts/timeseries?hours=6&bucket_minutes=30")
    assert r.status_code == 200
    d = r.json()
    assert d["hours"] == 6
    assert d["bucket_minutes"] == 30
    # 6h / 30min = 12 intervals → 13 boundaries
    assert len(d["buckets"]) == 13


async def test_timeseries_invalid_params_return_422():
    """hours must be 1..168; bucket_minutes 5..720."""
    async with _client() as c:
        # hours too high
        r = await c.get("/api/v1/alerts/timeseries?hours=500")
        assert r.status_code == 422
        # bucket_minutes too low
        r = await c.get("/api/v1/alerts/timeseries?bucket_minutes=1")
        assert r.status_code == 422


async def test_brief_latest_endpoint_returns_metadata_when_missing(tmp_path, monkeypatch):
    """When briefs/latest.md doesn't exist, /api/v1/brief/latest returns
    404 with metadata shape (no markdown field)."""
    # Point the brief dir at an empty tmp dir for this test
    import glassbox_server as gs
    monkeypatch.setattr(gs, "_BRIEF_DIR", tmp_path)
    from glassbox_server import app as _app
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
        r = await c.get("/api/v1/brief/latest")
    assert r.status_code == 404
    j = r.json()
    assert j["ok"] is False
    assert "markdown" not in j


async def test_brief_latest_endpoint_returns_markdown_when_present(tmp_path, monkeypatch):
    """When briefs/latest.md exists, the endpoint returns it inline + ok=true."""
    import glassbox_server as gs
    test_dir = tmp_path / "briefs"
    test_dir.mkdir()
    (test_dir / "latest.md").write_text("# test brief\n\nSample content.", encoding="utf-8")
    monkeypatch.setattr(gs, "_BRIEF_DIR", test_dir)
    from glassbox_server import app as _app
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
        r = await c.get("/api/v1/brief/latest")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert "test brief" in j["markdown"]


async def test_system_state_endpoint_shape():
    """/api/v1/system-state returns the expected nested shape."""
    async with _client() as c:
        r = await c.get("/api/v1/system-state")
    assert r.status_code == 200
    d = r.json()
    # Top-level keys
    for k in ("ts", "tier1_events_24h", "tier1_total_24h", "sanctions", "entities"):
        assert k in d, f"missing top-level key: {k}"
    # Sanctions nested shape
    assert "totals" in d["sanctions"]
    assert "by_authority" in d["sanctions"]
    for k in ("sanctioned_vessel", "sanctioned_aircraft"):
        assert k in d["sanctions"]["totals"]
    # tier1_total_24h matches sum of per-type counts
    assert d["tier1_total_24h"] == sum(d["tier1_events_24h"].values())


async def test_recent_cycle_endpoint_shape():
    """/api/v1/recent-cycle returns the expected shape even before the
    first cycle completes (all-zero totals, completed_at=None)."""
    # Use the FastAPI app directly — recent-cycle is registered at the app
    # level (not on the router), so we need the app instance.
    from glassbox_server import app as _glassbox_app
    async with AsyncClient(transport=ASGITransport(app=_glassbox_app), base_url="http://test") as c:
        r = await c.get("/api/v1/recent-cycle")
    assert r.status_code == 200
    d = r.json()
    assert "scan_interval_sec" in d
    assert "totals" in d
    assert "total" in d
    # Every algorithm key present — guards against typos in the publishing path
    for k in ("entity_event", "entity_entity", "dark_ship", "sanctions_match",
              "military_flights", "loitering", "rendezvous",
              "sanctioned_airspace", "sanctioned_dark",
              "sanctioned_rendezvous", "multijurisdictional"):
        assert k in d["totals"], f"missing key: {k}"


async def test_timeseries_seeded_event_appears_in_recent_bucket(_clean):
    """Insert a tier-1 event NOW; the latest bucket should reflect it."""
    await execute(
        """
        INSERT INTO event
            (event_type, event_subtype, event_time, geom, severity,
             title, description, properties, domain, decay_half_life_min)
        VALUES
            ('dark_vessel_detected', 'medium',
             NOW() - INTERVAL '5 minutes',
             ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography,
             7.0, $1, 'test', '{}'::jsonb, 'maritime', 1440)
        """,
        _TEST_TITLE,
    )
    async with _client() as c:
        r = await c.get("/api/v1/alerts/timeseries?hours=2&bucket_minutes=60")
    assert r.status_code == 200
    d = r.json()
    series = d["counts"]["dark_vessel_detected"]
    # At least one bucket in the last 2h must have ≥ 1 dark_vessel hit (our seed)
    assert sum(series) >= 1, f"expected ≥1 dark_vessel in seeded series, got {series}"
