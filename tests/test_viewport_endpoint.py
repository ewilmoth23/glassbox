"""
Phase 1.2 — /api/v1/viewport endpoint test.

Two layers:
  1. Helper-function tests — query_viewport() against real Postgres with seeded
     entities + positions. Asserts spatial filtering, time filtering, types
     filtering, and the entity+position join shape.
  2. HTTP test — FastAPI TestClient round-trip through /api/v1/viewport.

Uses sentinel canonical_ids (icao24 'test02_*') so cleanup is deterministic.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_viewport_endpoint.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, execute, acquire  # noqa: E402
from api_v1 import build_router  # noqa: E402
from web.routes.api_v1.core import query_viewport  # noqa: E402


TEST_PREFIX = "test02"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _seed_aircraft():
    """Seed three aircraft entities with one position each at known points:
        - test02_nyc  @ 40.6,-73.8   (NYC area)
        - test02_lax  @ 33.9,-118.4  (LAX area)
        - test02_lhr  @ 51.5,-0.1    (London)
    Yields a dict mapping label → entity uuid for later assertions.
    Cleanup at teardown.
    """
    async def _cleanup():
        await execute(
            "DELETE FROM position_track WHERE entity_id IN ("
            "  SELECT id FROM entity WHERE canonical_id LIKE $1)",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"{TEST_PREFIX}%",
        )

    await _cleanup()

    rows = [
        ("nyc", 40.6, -73.8, "UAL100"),
        ("lax", 33.9, -118.4, "AAL200"),
        ("lhr", 51.5, -0.1, "BAW300"),
    ]
    ids = {}
    now = datetime.now(timezone.utc)

    async with acquire() as conn:
        async with conn.transaction():
            for label, lat, lng, callsign in rows:
                # Populate the denormalized current_* columns on entity at
                # insert. The viewport query reads entity.current_geom +
                # current_position_time directly since the 2026-05-14 rewrite
                # (commit c45e16f) — joining position_track was too slow on a
                # 100M-row hypertable. Tests that pre-date the rewrite need
                # to set both rows.
                eid = await conn.fetchval(
                    """
                    INSERT INTO entity (entity_type, canonical_id_type, canonical_id, display_name,
                                        properties, last_seen, updated_at,
                                        current_geom, current_position_time,
                                        current_altitude_m, current_velocity_ms, current_heading_deg)
                    VALUES ('aircraft', 'icao24', $1, $2, '{}'::jsonb, $3, $3,
                            ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography, $3,
                            10000, 250, 90)
                    RETURNING id
                    """,
                    f"{TEST_PREFIX}_{label}",
                    callsign,
                    now,
                    lng,
                    lat,
                )
                ids[label] = eid
                await conn.execute(
                    """
                    INSERT INTO position_track (time, entity_id, geom, altitude_m,
                                                velocity_ms, heading_deg, properties)
                    VALUES ($1, $2,
                            ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                            10000, 250, 90, '{}'::jsonb)
                    """,
                    now,
                    eid,
                    lng,
                    lat,
                )
    yield ids
    await _cleanup()


# ─── Helper-level tests ───────────────────────────────────────────────────


async def test_viewport_filters_by_bbox_returns_only_inside(_seed_aircraft):
    """Bbox covering NYC only → 1 aircraft returned, the test02_nyc one."""
    now = datetime.now(timezone.utc)
    result = await query_viewport(
        bbox=(-75.0, 39.0, -72.0, 42.0),  # NYC area
        time_from=now - timedelta(hours=1),
        time_to=now + timedelta(minutes=1),
        types=["aircraft"],
        limit=100,
    )
    canonical_ids_seen = sorted(
        e["canonical_id"]
        for e in result["entities"]
        if e["canonical_id"].startswith(TEST_PREFIX)
    )
    assert canonical_ids_seen == [f"{TEST_PREFIX}_nyc"]


async def test_viewport_returns_three_when_bbox_is_global(_seed_aircraft):
    now = datetime.now(timezone.utc)
    result = await query_viewport(
        bbox=(-180.0, -90.0, 180.0, 90.0),
        time_from=now - timedelta(hours=1),
        time_to=now + timedelta(minutes=1),
        types=["aircraft"],
        limit=1000,
    )
    seen = sorted(
        e["canonical_id"]
        for e in result["entities"]
        if e["canonical_id"].startswith(TEST_PREFIX)
    )
    assert seen == [f"{TEST_PREFIX}_lax", f"{TEST_PREFIX}_lhr", f"{TEST_PREFIX}_nyc"]


async def test_viewport_time_filter_excludes_old_positions(_seed_aircraft):
    """Time window in the past (before seed) → 0 results."""
    far_past = datetime.now(timezone.utc) - timedelta(days=30)
    result = await query_viewport(
        bbox=(-180, -90, 180, 90),
        time_from=far_past - timedelta(hours=1),
        time_to=far_past,
        types=["aircraft"],
        limit=100,
    )
    seen = [e for e in result["entities"] if e["canonical_id"].startswith(TEST_PREFIX)]
    assert seen == []


async def test_viewport_types_filter_excludes_other_types(_seed_aircraft):
    """types=[vessel] → 0 aircraft results (none of our seeds are vessels)."""
    now = datetime.now(timezone.utc)
    result = await query_viewport(
        bbox=(-180, -90, 180, 90),
        time_from=now - timedelta(hours=1),
        time_to=now + timedelta(minutes=1),
        types=["vessel"],
        limit=100,
    )
    seen = [e for e in result["entities"] if e["canonical_id"].startswith(TEST_PREFIX)]
    assert seen == []


async def test_viewport_returns_position_alongside_entity(_seed_aircraft):
    """Each entity must include its latest position with lat/lng/altitude."""
    now = datetime.now(timezone.utc)
    result = await query_viewport(
        bbox=(-75.0, 39.0, -72.0, 42.0),
        time_from=now - timedelta(hours=1),
        time_to=now + timedelta(minutes=1),
        types=["aircraft"],
        limit=10,
    )
    nyc = [e for e in result["entities"] if e["canonical_id"] == f"{TEST_PREFIX}_nyc"]
    assert len(nyc) == 1
    pos = nyc[0]["position"]
    assert pos is not None
    assert abs(pos["lat"] - 40.6) < 0.01
    assert abs(pos["lng"] - (-73.8)) < 0.01
    assert pos["altitude_m"] == pytest.approx(10000.0)
    assert pos["velocity_ms"] == pytest.approx(250.0)


async def test_viewport_meta_includes_query_ms(_seed_aircraft):
    now = datetime.now(timezone.utc)
    result = await query_viewport(
        bbox=(-180, -90, 180, 90),
        time_from=now - timedelta(hours=1),
        time_to=now + timedelta(minutes=1),
        types=["aircraft"],
        limit=100,
    )
    assert "meta" in result
    assert "query_ms" in result["meta"]
    assert result["meta"]["query_ms"] >= 0
    assert result["meta"]["entity_count"] >= 3
    assert "bbox" in result["meta"]


async def test_viewport_takes_only_latest_position_per_entity(_seed_aircraft):
    """If an entity has 5 position rows, viewport returns only the latest."""
    now = datetime.now(timezone.utc)
    nyc_id = _seed_aircraft["nyc"]
    # Insert 4 older positions for nyc
    async with acquire() as conn:
        for i in range(4):
            old_time = now - timedelta(minutes=30 - i)
            await conn.execute(
                """
                INSERT INTO position_track (time, entity_id, geom)
                VALUES ($1, $2, ST_SetSRID(ST_MakePoint(-73.8, 40.6), 4326)::geography)
                """,
                old_time,
                nyc_id,
            )

    result = await query_viewport(
        bbox=(-75.0, 39.0, -72.0, 42.0),
        time_from=now - timedelta(hours=1),
        time_to=now + timedelta(minutes=1),
        types=["aircraft"],
        limit=10,
    )
    nyc = [e for e in result["entities"] if e["canonical_id"] == f"{TEST_PREFIX}_nyc"]
    assert len(nyc) == 1, "lateral join should return one row per entity, not one per position"


# ─── HTTP-level tests ─────────────────────────────────────────────────────


async def test_http_get_viewport_returns_200_and_entities(_seed_aircraft):
    """Hit the FastAPI app via httpx.AsyncClient — stays in this test's event
    loop so it can share the asyncpg pool created by the autouse fixture.
    (TestClient runs the app in a separate thread/loop, which conflicts with
    asyncpg's connection-loop binding.)"""
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport

    app = FastAPI()
    app.include_router(build_router())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        now = datetime.now(timezone.utc)
        params = {
            "bbox": "-75.0,39.0,-72.0,42.0",
            "time_from": (now - timedelta(hours=1)).isoformat(),
            "time_to": (now + timedelta(minutes=1)).isoformat(),
            "types": "aircraft",
            "limit": 10,
        }
        resp = await client.get("/api/v1/viewport", params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "entities" in body
        assert "events" in body
        assert "meta" in body
        seen = [e for e in body["entities"] if e["canonical_id"].startswith(TEST_PREFIX)]
        assert len(seen) == 1
        assert seen[0]["canonical_id"] == f"{TEST_PREFIX}_nyc"


async def test_http_get_viewport_400_on_malformed_bbox(_seed_aircraft):
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport

    app = FastAPI()
    app.include_router(build_router())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/v1/viewport", params={"bbox": "not-a-bbox"})
        assert resp.status_code in (400, 422)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
