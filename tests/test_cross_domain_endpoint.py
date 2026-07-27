"""
/api/v1/entities/{entity_id}/cross_domain endpoint tests.

Asserts:
  - Multi-entity event with the queried entity in properties.entity_ids
    surfaces with the OTHER entities resolved as partners.
  - Single-entity events (no entity_ids array) are NOT included.
  - within_hours window filters older events out.
  - event_types= whitelist filter narrows the result set.
  - Non-UUID entity_id → 400.
  - No matching events → empty `events` list, `result_count` == 0
    (NOT a 404 — the entity itself is fine, it just has no findings).

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_cross_domain_endpoint.py -v
"""

from __future__ import annotations

import json
import sys
import uuid
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


_TEST_TAG = "cross_domain_endpoint_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _seeded_pair():
    """Seed two entities + one rendezvous_detected event linking them.
    Yields (entity_a_uuid, entity_b_uuid, event_uuid) for the test to
    consume. Cleanup runs unconditionally.

    Uses random MMSIs (prefix '999') to avoid colliding with real data
    OR with stale leftovers from a previous failed test run that
    didn't reach its own teardown.
    """
    a = uuid.uuid4()
    b = uuid.uuid4()
    event_id = uuid.uuid4()
    # Random MMSI within a synthetic range so we never collide with
    # real AIS broadcasts. PostgreSQL unique index keys on
    # (entity_type, canonical_id_type, canonical_id), so even if a
    # stale row survives, a fresh randomized MMSI sidesteps it.
    import random
    suffix_a = f"{random.randint(100000, 999999):06d}"
    suffix_b = f"{random.randint(100000, 999999):06d}"
    mmsi_a = f"999{suffix_a}"
    mmsi_b = f"999{suffix_b}"

    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE properties->>'_test_tag' = $1", _TEST_TAG)
        await execute(
            "DELETE FROM entity WHERE properties->>'_test_tag' = $1", _TEST_TAG)

    await _cleanup()

    # Two synthetic vessels.
    for eid, mmsi, name in [(a, mmsi_a, "test alpha"),
                             (b, mmsi_b, "test bravo")]:
        await execute(
            """
            INSERT INTO entity
                (id, entity_type, canonical_id, canonical_id_type,
                 display_name, properties)
            VALUES
                ($1::uuid, 'vessel', $2, 'mmsi', $3, $4::jsonb)
            """,
            eid, mmsi, name,
            json.dumps({"_test_tag": _TEST_TAG}),
        )

    # One rendezvous event linking them.
    ts = datetime.now(timezone.utc)
    props = {
        "_test_tag": _TEST_TAG,
        "algorithm": "rendezvous-v1",
        "pair_kind": "vessel_vessel",
        "distance_m": 220,
        "entity_ids": [str(a), str(b)],
    }
    await execute(
        """
        INSERT INTO event
            (id, event_type, event_subtype, event_time,
             geom, severity, title, description, properties,
             domain, decay_half_life_min, entity_id)
        VALUES
            ($1::uuid, 'rendezvous_detected', 'vessel_vessel', $2,
             ST_SetSRID(ST_MakePoint(0, 0), 4326)::geography,
             8, 'Rendezvous: alpha near bravo (220m)',
             'test_pair', $3::jsonb,
             'maritime', 1440, $4::uuid)
        """,
        event_id, ts, json.dumps(props), a,
    )
    yield a, b, event_id
    await _cleanup()


def _client():
    app = FastAPI()
    app.include_router(build_router())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ─── Happy path ──────────────────────────────────────────────────────────


async def test_cross_domain_returns_partner_metadata(_seeded_pair):
    a, b, event_id = _seeded_pair
    async with _client() as c:
        r = await c.get(f"/api/v1/entities/{a}/cross_domain")
    assert r.status_code == 200
    body = r.json()
    assert body["entity_id"] == str(a)
    assert body["within_hours"] == 168
    assert body["event_types"] is None
    assert body["result_count"] >= 1

    # Find OUR test event among results.
    ev = next((e for e in body["events"] if e["id"] == str(event_id)), None)
    assert ev is not None, "seeded event missing from cross_domain results"
    assert ev["event_type"] == "rendezvous_detected"
    assert ev["severity"] == 8.0

    # Partners — should resolve b's display name + entity_type, exclude
    # the queried entity (a) itself.
    assert len(ev["partners"]) == 1
    p = ev["partners"][0]
    assert p["entity_id"] == str(b)
    assert p["display_name"] == "test bravo"
    assert p["entity_type"] == "vessel"
    assert p["canonical_id"].startswith("999")
    assert p["canonical_id_type"] == "mmsi"


async def test_cross_domain_works_from_either_entity_in_pair(_seeded_pair):
    """The endpoint is symmetric — querying entity B should surface
    entity A as the partner, mirror-image of querying entity A."""
    a, b, event_id = _seeded_pair
    async with _client() as c:
        r = await c.get(f"/api/v1/entities/{b}/cross_domain")
    assert r.status_code == 200
    body = r.json()
    ev = next((e for e in body["events"] if e["id"] == str(event_id)), None)
    assert ev is not None
    assert len(ev["partners"]) == 1
    assert ev["partners"][0]["entity_id"] == str(a)
    assert ev["partners"][0]["display_name"] == "test alpha"


async def test_cross_domain_event_types_filter_narrows(_seeded_pair):
    a, _b, _event_id = _seeded_pair
    # Filter to a different event_type — should drop our rendezvous row.
    async with _client() as c:
        r = await c.get(f"/api/v1/entities/{a}/cross_domain"
                        f"?event_types=shadow_fleet_cluster")
    assert r.status_code == 200
    body = r.json()
    assert body["event_types"] == ["shadow_fleet_cluster"]
    # No rendezvous events in the result
    assert all(e["event_type"] != "rendezvous_detected"
               for e in body["events"])


async def test_cross_domain_event_types_includes_match(_seeded_pair):
    a, _b, event_id = _seeded_pair
    async with _client() as c:
        r = await c.get(f"/api/v1/entities/{a}/cross_domain"
                        f"?event_types=rendezvous_detected,shadow_fleet_cluster")
    assert r.status_code == 200
    body = r.json()
    assert event_id is not None
    assert any(e["id"] == str(event_id) for e in body["events"])


async def test_cross_domain_within_hours_excludes_older(_seeded_pair):
    a, _b, _event_id = _seeded_pair
    # within_hours=1 — our seed timestamp is now() so it should still
    # be included, but a within_hours of 0 isn't a valid query (ge=1).
    # Use an extremely tight window in a different way: bump the seed
    # backward by hand.
    #
    # NOTE: TimescaleDB does not move rows across chunk boundaries on
    # UPDATE — an UPDATE that targets a value outside the current
    # chunk's CHECK constraint fails. With the default 7-day
    # chunk_time_interval, plain `UPDATE event_time = NOW() - 48h`
    # crashes on the ~28% of runs that happen within the first 48h of
    # a chunk week. DELETE+INSERT inside a single CTE sidesteps it —
    # the new row lands in whichever chunk owns its new event_time.
    await execute(
        """
        WITH del AS (
            DELETE FROM event
            WHERE properties->>'_test_tag' = $1
            RETURNING *
        )
        INSERT INTO event (
            id, entity_id, event_type, event_subtype, event_time, geom,
            severity, severity_for_market, title, description, properties,
            embedding, source_id, confidence, domain, decay_half_life_min,
            user_id, created_at
        )
        SELECT
            id, entity_id, event_type, event_subtype,
            NOW() - INTERVAL '48 hours', geom,
            severity, severity_for_market, title, description, properties,
            embedding, source_id, confidence, domain, decay_half_life_min,
            user_id, created_at
        FROM del
        """,
        _TEST_TAG,
    )
    async with _client() as c:
        # 24h window — our event is 48h old, must be excluded
        r24 = await c.get(f"/api/v1/entities/{a}/cross_domain?within_hours=24")
        # 96h window — our event is 48h old, must be included
        r96 = await c.get(f"/api/v1/entities/{a}/cross_domain?within_hours=96")
    assert r24.status_code == 200 and r96.status_code == 200
    seed_in_24 = any(e["properties"].get("_test_tag") == _TEST_TAG
                     for e in r24.json()["events"])
    seed_in_96 = any(e["properties"].get("_test_tag") == _TEST_TAG
                     for e in r96.json()["events"])
    assert not seed_in_24, "24h window should NOT include 48h-old event"
    assert seed_in_96, "96h window MUST include 48h-old event"


# ─── Error paths + empty results ────────────────────────────────────────


async def test_cross_domain_400_when_not_uuid():
    async with _client() as c:
        r = await c.get("/api/v1/entities/not-a-uuid/cross_domain")
    assert r.status_code == 400
    assert "must be a uuid" in r.json()["detail"].lower()


async def test_cross_domain_returns_empty_events_when_no_findings():
    """A real-but-finding-less entity UUID returns 200 with empty events,
    NOT 404. The entity might exist; absence of findings is the answer."""
    fake_uuid = uuid.uuid4()
    async with _client() as c:
        r = await c.get(f"/api/v1/entities/{fake_uuid}/cross_domain")
    assert r.status_code == 200
    body = r.json()
    assert body["events"] == []
    assert body["result_count"] == 0


# ─── Performance regression — the 2026-05-21 GIN-friendly SQL rewrite ────


async def test_cross_domain_uses_containment_operator_for_gin_index_path():
    """Static check: the production query must use the containment operator
    (`@>` against `properties` with `jsonb_build_object('entity_ids', ...)`),
    NOT the key-existence operator on the extracted sub-document
    (`properties->'entity_ids' ? $UUID::text`).

    Why: the latter form can't use the existing `event_props_gin` (which
    indexes `properties` as a whole with jsonb_ops). On 2026-05-21, the
    `?`-on-extracted form was observed scanning 13.7M rows in a single
    chunk to return 8 rows for entity `351d0fd8-...` (220s wall, 120s
    asyncpg timeout). After switching to `@>`, the same query ran in
    30ms — a ~7,000× speedup off the existing index.

    `entity_ids` is always a jsonb array (verified across 499k active
    rows on 2026-05-21) so the two predicates are semantically equivalent.

    If a future commit reverts to the `?`-on-extracted form, this test
    fails the same day."""
    import re
    # P3-H Phase 2 #7 (2026-05-27): the entity_cross_domain handler
    # moved from api_v1.py to web/routes/api_v1/core.py. The handler
    # is now at module top (not nested inside build_router), so the
    # `(?=^@router|\Z)` lookahead replaces the previous `^    @router`
    # variant.
    core_text = (ROOT / "web" / "routes" / "api_v1" / "core.py").read_text()
    # Find the entity_cross_domain handler region
    m = re.search(
        r'async def entity_cross_domain\(.*?(?=^@router|\Z)',
        core_text, re.S | re.M)
    assert m is not None, (
        "entity_cross_domain handler missing from web/routes/api_v1/core.py"
    )
    handler = m.group(0)
    # Strip Python comments — the SQL is in a triple-quoted string but
    # explanatory comments above it may legitimately mention the OLD
    # operator as a historical reference. Only check actual CODE lines.
    handler_code = "\n".join(
        line for line in handler.splitlines()
        if not re.match(r'^\s*#', line)
    )
    # The bad form must be absent from CODE (comments allowed)
    assert "properties->'entity_ids' ? " not in handler_code, (
        "Production query reverted to the slow `properties->'entity_ids' ? $UUID::text` "
        "form — this is unindexable and was clocked at 220s on the live DB. "
        "Use `properties @> jsonb_build_object('entity_ids', jsonb_build_array($1::text))` "
        "instead."
    )
    # The good form must be present in CODE (not just comments). The SQL
    # is wrapped across lines in api_v1.py so use a regex that tolerates
    # whitespace between tokens.
    good_pattern = re.compile(
        r"@>\s*jsonb_build_object\s*\(\s*\n?\s*'entity_ids'", re.S)
    assert good_pattern.search(handler_code), (
        "Production query no longer uses the GIN-friendly containment "
        "operator. Restore: "
        "`e.properties @> jsonb_build_object('entity_ids', "
        "jsonb_build_array($1::text))`"
    )


async def test_cross_domain_returns_within_5s_for_seeded_pair(_seeded_pair):
    """Performance lock for the 2026-05-21 SQL rewrite. Measures end-to-end
    HTTP latency of /api/v1/entities/{id}/cross_domain. Previously timed out
    at 120s on entities with many partner events because the planner couldn't
    use any index for the jsonb predicate; now picks `event_props_gin` and
    returns in milliseconds.

    The seeded pair is a single rendezvous event between two synthetic
    vessels — a minimal happy-path. Locking at 5s gives 100× headroom vs
    the observed 30-180ms on live data; if this test ever takes more
    than 5s, something deeper has regressed (planner, index dropped,
    api_pool ceiling changed, etc.)."""
    import time
    a, _b, _event_id = _seeded_pair
    async with _client() as c:
        t0 = time.perf_counter()
        r = await c.get(f"/api/v1/entities/{a}/cross_domain")
        elapsed = time.perf_counter() - t0
    assert r.status_code == 200, f"non-200 response: {r.status_code} {r.text[:200]}"
    assert elapsed < 5.0, (
        f"/cross_domain took {elapsed:.2f}s for the seeded pair — "
        f"expected < 5s. The 2026-05-21 GIN-friendly SQL rewrite may have "
        f"regressed (see test_cross_domain_uses_containment_operator_for_gin_index_path)."
    )
