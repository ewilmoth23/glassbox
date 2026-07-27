"""
Phase 4 — algorithms/dark_ship.py test.

Algorithm finds vessels in entity table whose `current_position_time` is
between dark_threshold_hours and lookback_hours ago, AND were moving
(velocity_ms > 0.5) at last position. Writes one event of type
'dark_vessel_detected' per dark vessel, deduped within 24h.

Asserts:
  - Vessel dark for 12h → 1 finding inserted (subtype='short')
  - Vessel dark for 3 days → 1 finding inserted (subtype='medium')
  - Vessel dark for 10 days → 1 finding inserted (subtype='long')
  - Vessel dark < threshold (3h) → 0 findings
  - Vessel dark > lookback (20 days) → 0 findings (too stale)
  - Vessel anchored at last position (velocity=0.1) → 0 findings (not moving)
  - Vessel with NULL current_geom → 0 findings
  - Vessel with no position_track row → 0 findings (LEFT JOIN LATERAL drops NULL velocity)
  - Re-running scan → no duplicate findings (idempotent via NOT EXISTS)
  - Severity scales linearly: 6h→0.5, 24h→2, 14d→cap at 10
  - Properties carry mmsi, last_seen_ais, hours_dark, last_velocity_ms
  - event.entity_id back-links to the dark vessel

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_dark_ship.py -v
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
from algorithms.dark_ship import run_dark_ship_scan  # noqa: E402


TEST_PREFIX = "test08"
TEST_TAG = "dark_ship_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_dark_world():
    async def _cleanup():
        # Drop any test findings + test vessel positions + test vessels
        await execute(
            "DELETE FROM event WHERE event_type='dark_vessel_detected' "
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


async def _seed_vessel(
    mmsi_suffix: str,
    *,
    last_seen_hours_ago: float,
    velocity_ms: float = 4.0,
    has_position_track: bool = True,
    has_geom: bool = True,
    display_name: str | None = None,
) -> str:
    """Insert one synthetic vessel + a position_track row dated
    last_seen_hours_ago hours in the past.

    Returns the entity id (uuid)."""
    canonical_id = f"{TEST_PREFIX}{mmsi_suffix}"
    last_seen_ts = datetime.now(timezone.utc) - timedelta(hours=last_seen_hours_ago)

    async with acquire() as conn:
        if has_geom:
            entity_id = await conn.fetchval(
                """
                INSERT INTO entity
                    (entity_type, canonical_id_type, canonical_id,
                     display_name, last_seen, updated_at,
                     current_geom, current_position_time)
                VALUES
                    ('vessel', 'mmsi', $1, $2, $3, $3,
                     ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography, $3)
                RETURNING id
                """,
                canonical_id, display_name, last_seen_ts,
                25.0, 59.0,  # Baltic-ish coords (lng, lat)
            )
        else:
            entity_id = await conn.fetchval(
                """
                INSERT INTO entity
                    (entity_type, canonical_id_type, canonical_id,
                     display_name, last_seen, updated_at,
                     current_geom, current_position_time)
                VALUES
                    ('vessel', 'mmsi', $1, $2, $3, $3, NULL, $3)
                RETURNING id
                """,
                canonical_id, display_name, last_seen_ts,
            )

        if has_position_track:
            await conn.execute(
                """
                INSERT INTO position_track
                    (time, entity_id, geom, velocity_ms, heading_deg)
                VALUES
                    ($1, $2, ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography,
                     $3, $4)
                """,
                last_seen_ts, entity_id, velocity_ms, 90.0,
            )

    return str(entity_id)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_vessel_dark_12h_emits_short_finding(_clean_dark_world):
    eid = await _seed_vessel("V1", last_seen_hours_ago=12, velocity_ms=4.0,
                              display_name="MV TEST DARK")
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT event_subtype, severity, title, properties, entity_id "
        "FROM event WHERE event_type='dark_vessel_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_subtype"] == "short"
    assert 0.5 <= r["severity"] <= 2.0  # 12h / 12 = 1.0
    assert "MV TEST DARK" in r["title"]
    assert str(r["entity_id"]) == eid

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["mmsi"] == f"{TEST_PREFIX}V1"
    assert 11.5 <= float(props["hours_dark"]) <= 12.5
    assert float(props["last_velocity_ms"]) == 4.0


async def test_vessel_dark_3d_emits_medium_finding(_clean_dark_world):
    await _seed_vessel("V2", last_seen_hours_ago=72, velocity_ms=6.0)
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1
    subtype = await fetchval(
        "SELECT event_subtype FROM event WHERE event_type='dark_vessel_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert subtype == "medium"


async def test_vessel_dark_10d_emits_long_finding(_clean_dark_world):
    await _seed_vessel("V3", last_seen_hours_ago=240, velocity_ms=5.0)
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1
    row = await fetch(
        "SELECT event_subtype, severity FROM event "
        "WHERE event_type='dark_vessel_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert row[0]["event_subtype"] == "long"
    # 240h / 12 = 20 → capped at 10
    assert row[0]["severity"] == 10.0


async def test_vessel_silent_under_threshold_is_not_dark(_clean_dark_world):
    """3h silence < default 6h threshold → no finding."""
    await _seed_vessel("V4", last_seen_hours_ago=3, velocity_ms=4.0)
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_vessel_silent_too_long_is_excluded(_clean_dark_world):
    """20 days > default 14d lookback → not interesting (presumed gone for good)."""
    await _seed_vessel("V5", last_seen_hours_ago=480, velocity_ms=4.0)
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_anchored_vessel_at_last_position_not_dark(_clean_dark_world):
    """Velocity <= 0.5 at last position → vessel was anchored, legitimate AIS quiet."""
    await _seed_vessel("V6", last_seen_hours_ago=12, velocity_ms=0.1)
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_vessel_with_null_current_geom_excluded(_clean_dark_world):
    await _seed_vessel("V7", last_seen_hours_ago=12, velocity_ms=4.0, has_geom=False)
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_vessel_with_no_position_track_excluded(_clean_dark_world):
    """No position_track row → LATERAL returns NULL velocity → filtered out."""
    await _seed_vessel("V8", last_seen_hours_ago=12, velocity_ms=4.0,
                        has_position_track=False)
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_dark_ship_scan_is_idempotent(_clean_dark_world):
    """Re-running scan within dedup window → no duplicates."""
    await _seed_vessel("V9", last_seen_hours_ago=12, velocity_ms=4.0)

    n1 = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1

    n2 = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='dark_vessel_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert total == 1


async def test_same_dark_period_does_not_re_fire_after_24h(_clean_dark_world):
    """REGRESSION: a vessel that's been dark for 5 days should produce
    exactly ONE finding, not five (one per 24h cycle after dedup expired).
    Audited 2026-05-13 NIGHT: 107k findings against 34k vessels in 7 days
    because the old 24h dedup window let the same dark period re-fire
    every day. Fix: dedup on (entity_id, last_seen_ais).
    """
    # Seed a vessel dark for 5 days
    await _seed_vessel("R1", last_seen_hours_ago=120, velocity_ms=4.0)

    # First scan: should detect it
    n1 = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1

    # Now simulate the previous finding being from 48 hours ago by
    # rewriting its event_time. Under the OLD logic, the next scan
    # would not see it within the 24h dedup window and would re-fire.
    # Under the new logic, dedup is by (entity_id, last_seen_ais)
    # within 30 days — same dark period stays deduplicated.
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
            WHERE event_type='dark_vessel_detected'
              AND properties->>'algorithm'=$1
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
        TEST_TAG,
    )

    n2 = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 0, (
        f"Expected 0 re-emissions for the same dark period after 48h, "
        f"got {n2}. The (entity_id, last_seen_ais) dedup isn't working."
    )

    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='dark_vessel_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert total == 1


async def test_re_emerged_then_dark_again_emits_new_finding(_clean_dark_world):
    """A vessel that came back online and went dark AGAIN (new
    last_seen_ais) IS a new dark period and should emit a new finding."""
    await _seed_vessel("R2", last_seen_hours_ago=24, velocity_ms=4.0)
    n1 = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1

    # Simulate vessel coming back online (newer current_position_time)
    # and going dark again — last_seen_ais updates.
    await execute(
        """
        UPDATE entity
        SET current_position_time = NOW() - INTERVAL '8 hours'
        WHERE canonical_id = $1
        """,
        f"{TEST_PREFIX}R2",
    )

    n2 = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 1, (
        f"A vessel with a new last_seen_ais represents a new dark period "
        f"and should emit. Got {n2} findings."
    )


async def test_multiple_dark_vessels_emit_distinct_findings(_clean_dark_world):
    await _seed_vessel("MA", last_seen_hours_ago=10, velocity_ms=3.0)
    await _seed_vessel("MB", last_seen_hours_ago=50, velocity_ms=4.0)
    await _seed_vessel("MC", last_seen_hours_ago=200, velocity_ms=2.0)

    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 3

    rows = await fetch(
        "SELECT event_subtype, properties->>'mmsi' AS mmsi "
        "FROM event WHERE event_type='dark_vessel_detected' "
        "AND properties->>'algorithm'=$1 ORDER BY mmsi",
        TEST_TAG,
    )
    subtypes_by_mmsi = {r["mmsi"]: r["event_subtype"] for r in rows}
    assert subtypes_by_mmsi[f"{TEST_PREFIX}MA"] == "short"   # 10h
    assert subtypes_by_mmsi[f"{TEST_PREFIX}MB"] == "medium"  # 50h
    assert subtypes_by_mmsi[f"{TEST_PREFIX}MC"] == "long"    # 200h


async def test_severity_scales_with_hours_dark(_clean_dark_world):
    await _seed_vessel("S1", last_seen_hours_ago=6, velocity_ms=3.0)
    await _seed_vessel("S2", last_seen_hours_ago=120, velocity_ms=3.0)
    await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )

    rows = await fetch(
        "SELECT properties->>'mmsi' AS mmsi, severity "
        "FROM event WHERE event_type='dark_vessel_detected' "
        "AND properties->>'algorithm'=$1 ORDER BY mmsi",
        TEST_TAG,
    )
    sev_by_mmsi = {r["mmsi"]: r["severity"] for r in rows}
    # 6h / 12 = 0.5, but lower-bound clamp is 1.0
    assert sev_by_mmsi[f"{TEST_PREFIX}S1"] == 1.0
    # 120h / 12 = 10.0
    assert sev_by_mmsi[f"{TEST_PREFIX}S2"] == 10.0


async def test_dark_ship_scan_with_zero_dark_vessels(_clean_dark_world):
    """No dark vessels in DB → returns 0, no errors."""
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


# ─── Cohort-size suppression (2026-05-19 P0-C audit) ─────────────────────


async def test_isolated_dark_vessel_still_emits(_clean_dark_world):
    """REGRESSION: the cohort-size suppression must NOT suppress isolated
    legitimate darkness. A single vessel going dark → cohort_size=1 → still
    emits. This is the canonical real-signal case.

    Audit: ALGORITHM_FP_AUDIT_dark_ship_2026_05_19.md
    """
    await _seed_vessel("ISOL1", last_seen_hours_ago=12, velocity_ms=4.0,
                        display_name="ISOLATED")
    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1, f"isolated dark vessel must still emit, got n={n}"

    import json
    rows = await fetch(
        "SELECT properties FROM event WHERE event_type='dark_vessel_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["cohort_size"] == 1, (
        f"isolated-vessel cohort should be 1, got {props.get('cohort_size')}"
    )


async def test_receiver_shed_cohort_of_6_suppressed(_clean_dark_world):
    """REGRESSION (2026-05-19 P0-C audit): an AIS ingester dropping its
    websocket causes every vessel it was tracking to share the same
    `last_seen_ais` to the microsecond. Pre-fix: dark_ship emitted one
    finding per vessel — production corpus held ~209k rows, 99.6% of
    which were this single failure mode. Post-fix: cohort-size
    suppression rejects any candidate whose last_seen_ais second-bucket
    has ≥6 peers (the binomial p-value for 6 independent vessels going
    dark in the same one-second window with ~18k tracked vessels is
    ~1e-8 — reliably an infrastructure artifact, not signal).
    """
    # All 6 vessels share THE SAME current_position_time to the microsecond.
    # _seed_vessel uses datetime.now() per call which would give slightly
    # different stamps; here we set them all to one fixed timestamp inline.
    shared_ts = datetime.now(timezone.utc) - timedelta(hours=12)
    async with acquire() as conn:
        for i in range(6):
            cid = f"{TEST_PREFIX}SHED{i}"
            eid = await conn.fetchval(
                """
                INSERT INTO entity
                    (entity_type, canonical_id_type, canonical_id,
                     display_name, last_seen, updated_at,
                     current_geom, current_position_time)
                VALUES
                    ('vessel', 'mmsi', $1, $2, $3, $3,
                     ST_SetSRID(ST_MakePoint($4, 59.0), 4326)::geography, $3)
                RETURNING id
                """,
                cid, f"SHED{i}", shared_ts, 25.0 + i * 0.01,
            )
            await conn.execute(
                """
                INSERT INTO position_track (time, entity_id, geom, velocity_ms, heading_deg)
                VALUES ($1, $2, ST_SetSRID(ST_MakePoint($3, 59.0), 4326)::geography, 4.0, 90.0)
                """,
                shared_ts, eid, 25.0 + i * 0.01,
            )

    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0, (
        f"6 vessels sharing the same last_seen_ais second (receiver-shed "
        f"signature) must all be suppressed by the cohort_size>=6 filter. "
        f"got n={n}"
    )


async def test_receiver_shed_cohort_of_5_just_under_threshold(_clean_dark_world):
    """Boundary check: 5 vessels sharing the same last_seen_ais second is
    JUST under the cohort_size>=6 threshold → all 5 emit. The threshold is
    intentionally inclusive at 6 to keep small smuggling-fleet coordination
    in scope while filtering receiver-shed artifacts (which empirically
    start at hundreds to thousands of vessels per second).
    """
    shared_ts = datetime.now(timezone.utc) - timedelta(hours=12)
    async with acquire() as conn:
        for i in range(5):
            cid = f"{TEST_PREFIX}FIVE{i}"
            eid = await conn.fetchval(
                """
                INSERT INTO entity
                    (entity_type, canonical_id_type, canonical_id,
                     display_name, last_seen, updated_at,
                     current_geom, current_position_time)
                VALUES
                    ('vessel', 'mmsi', $1, $2, $3, $3,
                     ST_SetSRID(ST_MakePoint($4, 59.0), 4326)::geography, $3)
                RETURNING id
                """,
                cid, f"FIVE{i}", shared_ts, 25.0 + i * 0.01,
            )
            await conn.execute(
                """
                INSERT INTO position_track (time, entity_id, geom, velocity_ms, heading_deg)
                VALUES ($1, $2, ST_SetSRID(ST_MakePoint($3, 59.0), 4326)::geography, 4.0, 90.0)
                """,
                shared_ts, eid, 25.0 + i * 0.01,
            )

    n = await run_dark_ship_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 5, f"cohort=5 (just below threshold) should still emit, got n={n}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
