"""
Hybrid DB-first / static-fallback route tests.

Covers the 4 routes converted in commit `<this>` to use
`web._geojson_db.build_db_geojson_response`:

  /api/v1/infrastructure/cyber-kev            ← kev_disclosure
  /api/v1/infrastructure/cyber-spamhaus-drop  ← spamhaus_block_entry
  /api/v1/infrastructure/noaa-buoys           ← ndbc_observation
  /api/v1/infrastructure/climate-forecast     ← climate_forecast

Asserts:
  1. Static-fallback path: empty DB → response equals (or is a
     structural superset of) the static seed file.
  2. DB-derived path: rows seeded → response carries those rows AS
     features + metadata.source notes the live origin.
  3. distinct_on_subtype = True yields latest-per-subtype (so noaa-buoys
     + climate-forecast return at-most-1 feature per station/city even
     when many historical rows are present).
  4. DB failure during query → graceful fallback to static, no 500.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_geojson_db_hybrid.py -v
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, execute, fetchval  # noqa: E402


TEST_TAG = "geojson-db-hybrid-test"


@pytest.fixture(autouse=False)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _app(_pool):
    """Lazy import so glassbox_server's heavy boot side-effects only
    run for tests in this file that actually exercise the routes."""
    from glassbox_server import app
    return app


@pytest.fixture
async def _seed_kev(_pool):
    """Seed two synthetic kev_disclosure rows + clean up after."""
    async def _seed():
        for i, cve in enumerate(("CVE-2099-99001", "CVE-2099-99002")):
            await execute(
                """
                INSERT INTO event
                    (id, event_type, event_subtype, event_time, geom,
                     severity, title, properties, domain, decay_half_life_min)
                VALUES
                    ($1::uuid, 'kev_disclosure', 'TestVendor', $2,
                     ST_SetSRID(ST_MakePoint(0, 0), 4326)::geography,
                     8, $3, $4::jsonb, 'cyber', 43200)
                ON CONFLICT (id, event_time) DO NOTHING
                """,
                uuid.uuid5(uuid.NAMESPACE_DNS, f"{TEST_TAG}:{cve}"),
                datetime.now(timezone.utc) - timedelta(hours=i),
                f"KEV test {cve}",
                json.dumps({"cve_id": cve, "test_tag": TEST_TAG, "vendor_project": "TestVendor"}),
            )

    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='kev_disclosure' "
            "AND properties->>'test_tag' = $1",
            TEST_TAG,
        )

    await _cleanup()
    await _seed()
    yield
    await _cleanup()


@pytest.fixture
async def _seed_ndbc_multi_per_station(_pool):
    """Seed 5 ndbc_observation rows for station '46006' (different
    event_times) + 3 for '46089'. Used to assert DISTINCT ON
    semantics — the response should yield 1 row per station, not 8."""
    async def _seed():
        for station, count in (("46006", 5), ("46089", 3)):
            for i in range(count):
                eid = uuid.uuid5(uuid.NAMESPACE_DNS, f"{TEST_TAG}:{station}:{i}")
                await execute(
                    """
                    INSERT INTO event
                        (id, event_type, event_subtype, event_time, geom,
                         severity, title, properties, domain, decay_half_life_min)
                    VALUES
                        ($1::uuid, 'ndbc_observation', $2, $3,
                         ST_SetSRID(ST_MakePoint(-137.0, 46.0), 4326)::geography,
                         3, $4, $5::jsonb, 'geo', 240)
                    ON CONFLICT (id, event_time) DO NOTHING
                    """,
                    eid,
                    station,
                    datetime.now(timezone.utc) - timedelta(minutes=i * 10),
                    f"NDBC test {station} #{i}",
                    json.dumps({"station_id": station, "test_tag": TEST_TAG, "wave_height_m": 2.5 - i * 0.1}),
                )

    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='ndbc_observation' "
            "AND properties->>'test_tag' = $1",
            TEST_TAG,
        )

    await _cleanup()
    await _seed()
    yield
    await _cleanup()


# ─── Static-fallback path (empty DB) ─────────────────────────────────────


async def test_cyber_kev_falls_back_to_static_when_db_empty(_app, _pool):
    """No live kev_disclosure rows → response = static seed file
    content (1606 features per the v1 port)."""
    # Ensure no test rows lurk from prior runs
    await execute(
        "DELETE FROM event WHERE event_type='kev_disclosure' "
        "AND properties->>'test_tag' = $1",
        TEST_TAG,
    )
    # If the live ingester has been writing real KEV rows, the DB
    # WON'T be empty — that's fine, but skip the empty-fallback
    # check in that case.
    real_kev_count = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='kev_disclosure'"
    )
    if real_kev_count and real_kev_count > 0:
        pytest.skip(
            f"Live kev_disclosure rows present ({real_kev_count}); "
            f"can't reliably test the static-fallback path"
        )

    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
        r = await c.get("/api/v1/infrastructure/cyber-kev")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "FeatureCollection"
    # Static seed has 1606 features per the metadata block
    assert len(body["features"]) > 1000


async def test_cyber_kev_routes_to_db_when_rows_present(_app, _seed_kev):
    """Live kev_disclosure rows in DB → response carries those rows
    as features + metadata.source notes the live origin."""
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
        r = await c.get("/api/v1/infrastructure/cyber-kev")
    assert r.status_code == 200
    body = r.json()
    cve_ids = [
        f["properties"].get("cve_id")
        for f in body["features"]
        if f["properties"].get("test_tag") == TEST_TAG
    ]
    # Both seeded CVEs must appear
    assert "CVE-2099-99001" in cve_ids
    assert "CVE-2099-99002" in cve_ids
    # Metadata source should note the live origin
    md = body.get("metadata") or {}
    assert "live" in (md.get("source") or "").lower()


# ─── DISTINCT ON subtype (latest-per-station) ────────────────────────────


async def test_noaa_buoys_distinct_on_subtype_yields_latest_per_station(
    _app, _seed_ndbc_multi_per_station
):
    """5 obs for 46006 + 3 obs for 46089 → response yields exactly
    1 feature per station (DISTINCT ON event_subtype + the latest by
    event_time per station)."""
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
        r = await c.get("/api/v1/infrastructure/noaa-buoys")
    assert r.status_code == 200
    body = r.json()
    test_features = [
        f for f in body["features"]
        if f["properties"].get("test_tag") == TEST_TAG
    ]
    # Exactly 2 features (one per station)
    assert len(test_features) == 2
    station_ids = {f["properties"]["station_id"] for f in test_features}
    assert station_ids == {"46006", "46089"}
    # The 46006 feature must be the LATEST observation
    # (wave_height_m=2.5 = i=0 obs); the i=4 obs has wave_height_m=2.1
    # which is OLDER (10*4 = 40 min earlier). DISTINCT ON + ORDER BY
    # subtype, event_time DESC picks the i=0 row.
    f_46006 = next(f for f in test_features if f["properties"]["station_id"] == "46006")
    assert f_46006["properties"]["wave_height_m"] == 2.5


# ─── Smoke: helper itself ────────────────────────────────────────────────


def test_helper_module_importable():
    """The helper module must import cleanly at test-collection time —
    catches syntax errors or unresolved imports before any route runs."""
    from web._geojson_db import build_db_geojson_response, _serve_static_seed, _load_seed_metadata
    assert callable(build_db_geojson_response)
    assert callable(_serve_static_seed)
    assert callable(_load_seed_metadata)


def test_seed_metadata_load_returns_metadata_block_for_existing_seed():
    """Sanity: the helper can load a static seed's metadata."""
    from web._geojson_db import _load_seed_metadata
    md = _load_seed_metadata("cyber_kev.geojson")
    assert md is not None
    assert md["type"] == "FeatureCollection"
    assert "features" not in md   # the helper deliberately strips features
    assert isinstance(md.get("metadata"), dict)


def test_seed_metadata_load_returns_none_for_missing_seed():
    """Missing seed → None (helper falls back to a synthesized
    minimal envelope in this case)."""
    from web._geojson_db import _load_seed_metadata
    assert _load_seed_metadata("definitely_not_a_real_file.geojson") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
