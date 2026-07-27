"""
Canonical-id entity lookup endpoints:
  GET /api/v1/vessel/{mmsi}
  GET /api/v1/aircraft/{icao24}

These resolve a canonical_id (MMSI / ICAO24 hex) to the entity UUID and
dispatch to the same query_entity_detail used by /api/v1/entity/{id}, so
external links + share URLs can use stable identifiers instead of UUIDs.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_canonical_lookup.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, execute, acquire  # noqa: E402
from api_v1 import build_router  # noqa: E402


_VESSEL_MMSI = "999999801"
_AIRCRAFT_ICAO = "abcdef"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _seed_entities():
    async def _do_clean():
        await execute(
            "DELETE FROM entity WHERE canonical_id IN ($1, $2)",
            _VESSEL_MMSI, _AIRCRAFT_ICAO,
        )
    await _do_clean()
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                                display_name, properties, last_seen, updated_at,
                                current_geom, current_position_time)
            VALUES ('vessel', 'mmsi', $1, 'TEST CANONICAL VESSEL',
                    '{}'::jsonb, NOW(), NOW(),
                    ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography, NOW())
            """,
            _VESSEL_MMSI,
        )
        await conn.execute(
            """
            INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                                display_name, properties, last_seen, updated_at,
                                current_geom, current_position_time)
            VALUES ('aircraft', 'icao24', $1, 'TEST CANONICAL AIRCRAFT',
                    '{}'::jsonb, NOW(), NOW(),
                    ST_SetSRID(ST_MakePoint(2.0, 48.0), 4326)::geography, NOW())
            """,
            _AIRCRAFT_ICAO,
        )
    yield
    await _do_clean()


def _client():
    app = FastAPI()
    app.include_router(build_router())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_vessel_lookup_by_mmsi_returns_entity(_seed_entities):
    async with _client() as c:
        r = await c.get(f"/api/v1/vessel/{_VESSEL_MMSI}")
    assert r.status_code == 200
    j = r.json()
    assert j["entity"]["canonical_id"] == _VESSEL_MMSI
    assert j["entity"]["display_name"] == "TEST CANONICAL VESSEL"
    assert j["entity"]["entity_type"] == "vessel"


async def test_aircraft_lookup_by_icao24_normalizes_case(_seed_entities):
    """ICAO24 is hex; the endpoint should accept upper-case input and
    match the lower-case storage convention."""
    async with _client() as c:
        r = await c.get(f"/api/v1/aircraft/{_AIRCRAFT_ICAO.upper()}")
    assert r.status_code == 200
    j = r.json()
    assert j["entity"]["canonical_id"] == _AIRCRAFT_ICAO
    assert j["entity"]["entity_type"] == "aircraft"


async def test_vessel_unknown_mmsi_returns_404():
    async with _client() as c:
        r = await c.get("/api/v1/vessel/000000000")
    assert r.status_code == 404


async def test_aircraft_unknown_icao24_returns_404():
    async with _client() as c:
        r = await c.get("/api/v1/aircraft/zzzzzz")
    assert r.status_code == 404


async def test_vessel_lookup_passes_through_query_params(_seed_entities):
    """track_window_hours param applies to the entity-detail subquery."""
    async with _client() as c:
        r = await c.get(f"/api/v1/vessel/{_VESSEL_MMSI}?track_window_hours=12&related_events_radius_m=1000")
    assert r.status_code == 200
    meta = r.json().get("meta") or {}
    assert meta.get("track_window_hours") == 12
    assert meta.get("related_events_radius_m") == 1000


async def test_vessel_endpoint_does_not_match_aircraft_canonical(_seed_entities):
    """Defense in depth: vessel/<icao24> should return 404 since the
    lookup is scoped by entity_type=vessel."""
    async with _client() as c:
        r = await c.get(f"/api/v1/vessel/{_AIRCRAFT_ICAO}")
    assert r.status_code == 404
