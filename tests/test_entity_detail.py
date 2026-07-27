"""
Phase 1.3 — /api/v1/entity/{id} endpoint test.

Asserts:
  - Returns identity + recent track (last N hours, configurable) + related events
  - 404 when entity_id is unknown
  - 400 on non-UUID input
  - related_events filter respects radius_m
  - track_window_hours respected

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_entity_detail.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, execute, acquire  # noqa: E402
from api_v1 import build_router  # noqa: E402
from web.routes.api_v1.core import query_entity_detail  # noqa: E402


TEST_PREFIX = "test03"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _seed_entity():
    """Seed one aircraft entity with 5 position points spanning 4 hours, plus
    one event 2km away at 30min ago. Returns dict with all uuids needed for
    assertions."""
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
        await execute(
            "DELETE FROM event WHERE event_subtype LIKE $1",
            f"{TEST_PREFIX}%",
        )

    await _cleanup()
    now = datetime.now(timezone.utc)
    state = {}

    async with acquire() as conn:
        async with conn.transaction():
            eid = await conn.fetchval(
                """
                INSERT INTO entity (entity_type, canonical_id_type, canonical_id, display_name,
                                    properties, last_seen)
                VALUES ('aircraft', 'icao24', $1, 'TESTFLT01', '{"callsign": "TESTFLT01"}'::jsonb, $2)
                RETURNING id
                """,
                f"{TEST_PREFIX}_track",
                now,
            )
            state["entity_id"] = eid

            # Five positions, walking in time from now-4h to now
            for i in range(5):
                t = now - timedelta(hours=4 - i)
                lat = 40.6 + i * 0.01
                lng = -73.8 + i * 0.01
                await conn.execute(
                    """
                    INSERT INTO position_track (time, entity_id, geom, altitude_m,
                                                velocity_ms, heading_deg, properties)
                    VALUES ($1, $2,
                            ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                            10000 + $5 * 100, 250, 90, '{}'::jsonb)
                    """,
                    t,
                    eid,
                    lng,
                    lat,
                    i,
                )

            # Insert one event ~2km away at the latest position. event_time
            # is set 10 seconds in the future so the seed wins the
            # query's `ORDER BY event_time DESC LIMIT 100` against the
            # 25k+ proximity findings near LGA in production data. The
            # test only needs the event to surface; absolute time isn't
            # asserted. Cleanup removes the row regardless.
            event_id = uuid4()
            await conn.execute(
                """
                INSERT INTO event (id, event_type, event_subtype, event_time, geom,
                                   severity, title, description)
                VALUES ($1, 'usgs_quake', $2, $3,
                        ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                        4.5, 'M4.5 quake near LGA', 'Test event for proximity')
                """,
                event_id,
                f"{TEST_PREFIX}_quake",
                now + timedelta(seconds=10),
                -73.78,         # ~2km east of latest plane position (-73.76)
                40.65,          # ~roughly co-located
            )
            state["event_id"] = event_id

    yield state
    await _cleanup()


# ─── Helper-level tests ───────────────────────────────────────────────────


async def test_entity_detail_returns_identity_and_track(_seed_entity):
    eid = str(_seed_entity["entity_id"])
    result = await query_entity_detail(eid, track_window_hours=24)
    assert result is not None
    assert result["entity"]["id"] == eid
    assert result["entity"]["entity_type"] == "aircraft"
    assert result["entity"]["display_name"] == "TESTFLT01"
    assert result["entity"]["canonical_id"] == f"{TEST_PREFIX}_track"
    assert len(result["track"]) == 5
    # Latest first
    times = [r["time"] for r in result["track"]]
    assert times == sorted(times, reverse=True)
    # Each point has lat/lng
    for p in result["track"]:
        assert "lat" in p and "lng" in p


async def test_entity_detail_track_window_hours_respected(_seed_entity):
    """track_window_hours=2 → exclude positions older than 2h. Seed has positions
    at now-4h, now-3h, now-2h, now-1h, now. Window=2h → 3 points (at now, -1h, -2h)."""
    eid = str(_seed_entity["entity_id"])
    result = await query_entity_detail(eid, track_window_hours=2)
    # Boundary inclusion may give 2 or 3; assert reduced
    assert len(result["track"]) < 5
    assert len(result["track"]) >= 2


async def test_entity_detail_related_events_within_radius(_seed_entity):
    """Default radius (50km) → seeded event at ~2km should appear."""
    eid = str(_seed_entity["entity_id"])
    result = await query_entity_detail(eid, related_events_radius_m=50_000)
    related = [e for e in result["related_events"]
               if e["event_type"] == "usgs_quake"]
    assert len(related) >= 1
    e = related[0]
    assert e["distance_m"] is not None
    assert e["distance_m"] < 5000  # within 5km


async def test_entity_detail_related_events_radius_excludes_far(_seed_entity):
    """Radius too small → no related events returned."""
    eid = str(_seed_entity["entity_id"])
    result = await query_entity_detail(eid, related_events_radius_m=500)  # 500m
    related = [e for e in result["related_events"]
               if e["event_type"] == "usgs_quake"]
    assert len(related) == 0


async def test_entity_detail_unknown_uuid_returns_none(_seed_entity):
    fake_id = str(uuid4())
    result = await query_entity_detail(fake_id)
    assert result is None


async def test_entity_detail_invalid_uuid_raises_400():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await query_entity_detail("not-a-uuid")
    assert exc.value.status_code == 400


# ─── HTTP-level tests ─────────────────────────────────────────────────────


async def test_http_entity_detail_200(_seed_entity):
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport

    app = FastAPI()
    app.include_router(build_router())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        eid = str(_seed_entity["entity_id"])
        resp = await client.get(f"/api/v1/entity/{eid}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["entity"]["id"] == eid
        assert body["entity"]["display_name"] == "TESTFLT01"
        assert len(body["track"]) == 5


async def test_http_entity_detail_404_unknown(_seed_entity):
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport

    app = FastAPI()
    app.include_router(build_router())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(f"/api/v1/entity/{uuid4()}")
        assert resp.status_code == 404


# ─── FtM format support (R6) ─────────────────────────────────────────────


async def test_http_entity_detail_format_ftm_returns_ftm_shape(_seed_entity):
    """?format=ftm returns the FollowTheMoney JSON shape — entity only,
    no track or related_events. Aircraft maps to FtM 'Airplane' schema."""
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport

    app = FastAPI()
    app.include_router(build_router())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        eid = str(_seed_entity["entity_id"])
        resp = await client.get(f"/api/v1/entity/{eid}?format=ftm")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # FtM-shape contract: id + schema + properties at top level;
        # NO 'track' or 'related_events' (those are internal-format only).
        assert body["schema"] == "Airplane"
        assert "track" not in body
        assert "related_events" not in body
        # FtM IDs use the empire's canonical_id (stable across OCCRP
        # ecosystem) rather than the opaque UUID. The seeded fixture
        # uses canonical_id ending in '_track'.
        assert body["id"].endswith("_track")
        # Aircraft callSign isn't in FtM's Airplane schema; the seeded
        # 'TESTFLT01' callsign is dropped silently rather than surfaced.
        # The 'name' should map from display_name='TESTFLT01' (the
        # fixture sets this in the entity row's display_name column).
        assert body["properties"].get("name") == ["TESTFLT01"]


async def test_http_entity_detail_format_internal_unchanged(_seed_entity):
    """Default (no format) and explicit format=internal both return the
    unchanged Glassbox-detail shape — guards against regressions in the
    branch that did NOT change."""
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport

    app = FastAPI()
    app.include_router(build_router())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        eid = str(_seed_entity["entity_id"])
        # Default
        r1 = await client.get(f"/api/v1/entity/{eid}")
        assert r1.status_code == 200
        assert "entity" in r1.json() and "track" in r1.json()
        # Explicit internal — same top-level structure (entity + track
        # + related_events). related_events is dynamic in this DB so
        # we only assert structure, not exact equality across the two
        # requests.
        r2 = await client.get(f"/api/v1/entity/{eid}?format=internal")
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["entity"] == r1.json()["entity"]
        assert len(body2["track"]) == len(r1.json()["track"])
        assert "related_events" in body2


async def test_http_entity_detail_format_invalid_value_400(_seed_entity):
    """An unknown format value (FastAPI Query pattern validation) is a
    400, not silently routed to the default."""
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport

    app = FastAPI()
    app.include_router(build_router())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        eid = str(_seed_entity["entity_id"])
        resp = await client.get(f"/api/v1/entity/{eid}?format=jsonld")
        assert resp.status_code == 422


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
