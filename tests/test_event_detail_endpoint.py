"""
/api/v1/event/{event_id} endpoint tests.

Asserts:
  - Valid UUID with a matching row → full event detail dict.
  - Non-UUID id → 400 with "must be a UUID" detail.
  - UUID with no matching row → 404.
  - Properties field is normalized to a dict (not asyncpg's text-of-JSONB).
  - geom column is decomposed into lat + lng floats.
  - The embedding column is excluded from the response.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_event_detail_endpoint.py -v
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, execute  # noqa: E402
from api_v1 import build_router  # noqa: E402


_TEST_TAG = "event_detail_endpoint_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM event WHERE properties->>'_test_tag' = $1",
            _TEST_TAG,
        )
    await _do()
    yield
    await _do()


def _client():
    app = FastAPI()
    app.include_router(build_router())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_event(*, lat: float = 47.6062, lng: float = -122.3321,
                       severity: float = 3.0,
                       title: str = "endpoint-test event",
                       description: str = "test description body",
                       extra_props: dict = None) -> uuid.UUID:
    eid = uuid.uuid4()
    ts = datetime.now(timezone.utc)
    props = {"_test_tag": _TEST_TAG, "external_id": f"detail_test:{eid}"}
    if extra_props:
        props.update(extra_props)
    await execute(
        """
        INSERT INTO event
            (id, event_type, event_subtype, event_time,
             geom, severity, title, description, properties,
             domain, decay_half_life_min)
        VALUES
            ($1::uuid, 'newsdata', 'breaking', $2,
             ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
             $5, $6, $7, $8::jsonb,
             'geo', 720)
        """,
        eid, ts, lng, lat, severity, title, description,
        json.dumps(props),
    )
    return eid


# ─── Happy path ──────────────────────────────────────────────────────────


async def test_get_event_detail_returns_full_row(_clean):
    eid = await _seed_event(
        lat=37.7749, lng=-122.4194, severity=4.0,
        title="Magnitude 6.2 quake near SF",
        description="Aftershock sequence still active.",
        extra_props={"magnitude": 6.2, "depth_km": 12.4},
    )
    async with _client() as c:
        r = await c.get(f"/api/v1/event/{eid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(eid)
    assert body["event_type"] == "newsdata"
    assert body["event_subtype"] == "breaking"
    assert body["severity"] == 4.0
    assert body["title"] == "Magnitude 6.2 quake near SF"
    assert body["description"] == "Aftershock sequence still active."
    # geom decomposed correctly
    assert abs(body["lat"] - 37.7749) < 1e-4
    assert abs(body["lng"] + 122.4194) < 1e-4
    # properties is a real dict, not the text-of-JSONB asyncpg
    # sometimes hands back
    assert isinstance(body["properties"], dict)
    assert body["properties"]["magnitude"] == 6.2
    assert body["properties"]["_test_tag"] == _TEST_TAG
    assert body["domain"] == "geo"
    assert body["decay_half_life_min"] == 720
    # Embedding column is NOT surfaced (binary blob; agents use
    # /events/similar instead).
    assert "embedding" not in body


async def test_get_event_detail_with_minimal_event(_clean):
    """Description nullable; properties may be empty dict."""
    eid = await _seed_event(description="")
    async with _client() as c:
        r = await c.get(f"/api/v1/event/{eid}")
    assert r.status_code == 200
    body = r.json()
    assert body["description"] == ""
    # Even with no extra props, the seed always sets _test_tag, so
    # the dict is non-empty. Confirm the test_tag round-trips.
    assert body["properties"]["_test_tag"] == _TEST_TAG


# ─── Error paths ─────────────────────────────────────────────────────────


async def test_get_event_detail_404_when_uuid_not_found():
    """Well-formed UUID but no matching row → 404."""
    fake_uuid = uuid.uuid4()
    async with _client() as c:
        r = await c.get(f"/api/v1/event/{fake_uuid}")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


async def test_get_event_detail_400_when_not_uuid():
    """Non-UUID path component → 400 with explicit detail."""
    async with _client() as c:
        r = await c.get("/api/v1/event/not-a-uuid")
    assert r.status_code == 400
    assert "must be a uuid" in r.json()["detail"].lower()
