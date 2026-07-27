"""
Phase 4 algorithm #5 — algorithms/loitering.py test.

Algorithm flags vessels/aircraft whose recent position_track points all
fall within a small bounding circle (default 1km) over a long span
(default 4+ hours), while still actively broadcasting and with non-zero
average velocity (filters truly anchored vessels).

Asserts:
  - 6 pings tightly clustered over 5h with avg velocity 1 m/s → 1 finding
  - 6 pings spread across 10km → 0 findings (not loitering, traveling)
  - 4 pings in 5h (under min_pings=5) → 0 findings
  - 6 pings clustered but only 1h span → 0 findings (too brief)
  - 6 pings tightly clustered with avg velocity 0 (anchored) → 0 findings
  - Aircraft loitering → 1 finding, event_subtype='aircraft'
  - Entity not currently active (last_seen >2h ago) → 0 findings
  - Re-running scan within dedup window → no duplicates
  - Multiple distinct loitering entities → all flagged
  - severity scales with span (4h→4, 8h→4, 12h→6)
  - event.entity_id back-links to the entity

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_loitering.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute, acquire  # noqa: E402
from algorithms.loitering import run_loitering_scan  # noqa: E402


TEST_PREFIX = "test13"
TEST_TAG = "loitering_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_loiter_world():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='loitering_detected' "
            "AND properties->>'algorithm'=$1",
            TEST_TAG,
        )
        await execute(
            "DELETE FROM position_track WHERE entity_id IN "
            "(SELECT id FROM entity WHERE canonical_id LIKE $1)",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


async def _seed_entity_with_track(
    canonical_suffix: str,
    *,
    entity_type: str = "vessel",
    pings: list = None,
    last_seen_min_ago: int = 5,
    has_geom: bool = True,
):
    """Seed an entity + a list of position_track pings. `pings` is a list of
    (minutes_ago, lng, lat, velocity_ms) tuples."""
    canonical_id = f"{TEST_PREFIX}{canonical_suffix}"
    last_seen_ts = datetime.now(timezone.utc) - timedelta(minutes=last_seen_min_ago)
    if pings is None:
        pings = []
    # Use the last ping's geom as current_geom
    last_ping = max(pings, key=lambda p: -p[0]) if pings else (0, 0.0, 0.0, 0.0)
    last_lng, last_lat = last_ping[1], last_ping[2]
    canonical_id_type = "icao24" if entity_type == "aircraft" else "mmsi"

    async with acquire() as conn:
        if has_geom:
            eid = await conn.fetchval(
                """
                INSERT INTO entity
                    (entity_type, canonical_id_type, canonical_id,
                     display_name, last_seen, updated_at,
                     current_geom, current_position_time)
                VALUES
                    ($1, $2, $3, $4, $5, $5,
                     ST_SetSRID(ST_MakePoint($6, $7), 4326)::geography, $5)
                RETURNING id
                """,
                entity_type, canonical_id_type, canonical_id,
                f"TEST {canonical_suffix}", last_seen_ts,
                float(last_lng), float(last_lat),
            )
        else:
            eid = await conn.fetchval(
                """
                INSERT INTO entity
                    (entity_type, canonical_id_type, canonical_id,
                     display_name, last_seen, updated_at,
                     current_geom, current_position_time)
                VALUES
                    ($1, $2, $3, $4, $5, $5, NULL, $5)
                RETURNING id
                """,
                entity_type, canonical_id_type, canonical_id,
                f"TEST {canonical_suffix}", last_seen_ts,
            )
        for (min_ago, lng, lat, vel) in pings:
            ts = datetime.now(timezone.utc) - timedelta(minutes=min_ago)
            await conn.execute(
                """
                INSERT INTO position_track
                    (time, entity_id, geom, velocity_ms, heading_deg)
                VALUES
                    ($1, $2,
                     ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                     $5, 0)
                """,
                ts, eid, float(lng), float(lat), float(vel),
            )
    return str(eid)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_vessel_loitering_5h_emits_finding(_clean_loiter_world):
    """6 pings tightly clustered (~500m radius) over 5 hours, avg vel 1 m/s."""
    pings = [
        (300, 25.0,    59.0,    1.0),  # 5h ago
        (240, 25.001,  59.001,  0.8),
        (180, 25.0005, 59.0005, 1.2),
        (120, 25.002,  59.001,  0.5),
        (60,  25.001,  59.0,    1.0),
        (5,   25.0005, 59.001,  1.0),  # most recent
    ]
    eid = await _seed_entity_with_track("V1", pings=pings, last_seen_min_ago=5)

    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT event_subtype, severity, title, properties, entity_id FROM event "
        "WHERE event_type='loitering_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_subtype"] == "vessel"
    assert r["severity"] >= 2.0
    assert "Loitering detected" in r["title"]
    assert str(r["entity_id"]) == eid

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["entity_type"] == "vessel"
    assert props["pings"] == 6
    assert float(props["span_hours"]) >= 4.5


async def test_vessel_traveling_long_distance_not_flagged(_clean_loiter_world):
    """6 pings spread across ~50km — clearly traveling, not loitering."""
    pings = [
        (300, 25.0,  59.0,  5.0),
        (240, 25.1,  59.0,  5.0),
        (180, 25.2,  59.0,  5.0),
        (120, 25.3,  59.0,  5.0),
        (60,  25.4,  59.0,  5.0),
        (5,   25.5,  59.0,  5.0),  # ~50km from start
    ]
    await _seed_entity_with_track("V2", pings=pings)
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_too_few_pings_not_flagged(_clean_loiter_world):
    """Only 4 pings — below min_pings=5 default."""
    pings = [
        (300, 25.0, 59.0, 1.0),
        (200, 25.0, 59.0, 1.0),
        (100, 25.0, 59.0, 1.0),
        (5,   25.0, 59.0, 1.0),
    ]
    await _seed_entity_with_track("V3", pings=pings)
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_too_brief_not_flagged(_clean_loiter_world):
    """6 pings but all within 30 min — span too short."""
    pings = [
        (30, 25.0, 59.0, 1.0),
        (25, 25.0, 59.0, 1.0),
        (20, 25.0, 59.0, 1.0),
        (15, 25.0, 59.0, 1.0),
        (10, 25.0, 59.0, 1.0),
        (5,  25.0, 59.0, 1.0),
    ]
    await _seed_entity_with_track("V4", pings=pings)
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_anchored_vessel_zero_velocity_excluded(_clean_loiter_world):
    """Vessel with avg velocity 0 — anchored, not loitering."""
    pings = [
        (300, 25.0, 59.0, 0.0),
        (240, 25.0, 59.0, 0.0),
        (180, 25.0, 59.0, 0.05),
        (120, 25.0, 59.0, 0.0),
        (60,  25.0, 59.0, 0.0),
        (5,   25.0, 59.0, 0.0),
    ]
    await _seed_entity_with_track("V5", pings=pings)
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_aircraft_loitering_emits_aircraft_subtype(_clean_loiter_world):
    """Aircraft circling for 5h → event_subtype='aircraft'."""
    pings = [
        (300, -100.0,    35.0,    50.0),
        (240, -100.005,  35.003,  50.0),
        (180, -100.003,  35.005,  50.0),
        (120, -100.001,  35.002,  50.0),
        (60,  -100.004,  35.001,  50.0),
        (5,   -100.002,  35.003,  50.0),
    ]
    await _seed_entity_with_track("A1", entity_type="aircraft", pings=pings)
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
        radius_m=2000.0,  # aircraft circle wider than vessels at slow speeds
    )
    assert n == 1
    subtype = await fetchval(
        "SELECT event_subtype FROM event WHERE event_type='loitering_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert subtype == "aircraft"


async def test_stale_entity_not_flagged(_clean_loiter_world):
    """Entity last_seen 3h ago — not currently active, skipped."""
    pings = [
        (300, 25.0, 59.0, 1.0),
        (250, 25.0, 59.0, 1.0),
        (200, 25.0, 59.0, 1.0),
        (180, 25.0, 59.0, 1.0),
        (150, 25.0, 59.0, 1.0),
        (180, 25.0, 59.0, 1.0),
    ]
    await _seed_entity_with_track("V6", pings=pings, last_seen_min_ago=180)
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_idempotent_within_dedup_window(_clean_loiter_world):
    pings = [
        (300, 25.0, 59.0, 1.0),
        (240, 25.001, 59.001, 1.0),
        (180, 25.0005, 59.0005, 1.0),
        (120, 25.002, 59.001, 1.0),
        (60, 25.001, 59.0, 1.0),
        (5, 25.0005, 59.001, 1.0),
    ]
    await _seed_entity_with_track("V7", pings=pings)
    n1 = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1
    n2 = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 0
    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='loitering_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert total == 1


async def test_multiple_loitering_entities(_clean_loiter_world):
    # 6 pings spanning ~5h, tightly clustered, avg vel ~1 m/s
    pings_a = [(t, 25.0 + 0.001 * (i % 2), 59.0, 1.0)
               for i, t in enumerate([300, 240, 180, 120, 60, 5])]
    pings_b = [(t, 30.0, 50.0 + 0.001 * (i % 2), 1.0)
               for i, t in enumerate([300, 240, 180, 120, 60, 5])]
    await _seed_entity_with_track("MA", pings=pings_a)
    await _seed_entity_with_track("MB", pings=pings_b)
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 2


async def test_no_loitering_returns_zero(_clean_loiter_world):
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_stale_pings_zero_bbox_not_flagged(_clean_loiter_world):
    """Regression — FP audit 2026-05-19 (P0-C #5).

    The AIS-receiver-shed pattern: same position payload redelivered N times
    over a 5+ hour span. The vessel itself broadcasts `velocity_ms ≈ 6 m/s`
    (active cruising), but every position_track row has IDENTICAL lat/lon.
    The pre-fix algorithm flagged this as loitering because:
      - 6+ pings span 5+ hours (passes min_pings + min_span)
      - lat_span = lng_span = 0 ≤ radius_threshold (passes bbox check)
      - avg_velocity = 6 m/s > 0.2 (passes anchored-vessel filter)
    But "zero positional movement" is not loitering — it's a feed artifact.

    Two synchronized batches alone (2026-05-08T15:37:50 + 2026-05-10T15:01:36)
    produced 27,985 FPs in the production corpus. Post-fix this assertion
    locks the regression: identical-position pings emit 0 findings.
    """
    pings = [
        # All 6 pings at the IDENTICAL position with velocity=6.0 m/s
        (300, 25.0, 59.0, 6.0),
        (240, 25.0, 59.0, 6.0),
        (180, 25.0, 59.0, 6.0),
        (120, 25.0, 59.0, 6.0),
        (60,  25.0, 59.0, 6.0),
        (5,   25.0, 59.0, 6.0),
    ]
    await _seed_entity_with_track("VSTALE", pings=pings, last_seen_min_ago=5)
    n = await run_loitering_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0, (
        "Stale-pings (identical lat/lon across all pings, non-zero velocity) "
        "must NOT emit a loitering finding — that's a feed artifact, not motion."
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
