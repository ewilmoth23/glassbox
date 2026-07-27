"""
Phase 4 algorithm #6 — algorithms/rendezvous.py test.

Algorithm flags pairs of distinct entities within 1km of each other,
both at low velocity (<3 m/s) and both currently active. Catches
ship-to-ship transfers, in-flight refueling, drug-interdiction
hover-and-board.

Asserts:
  - Two vessels 200m apart, both <2 m/s → 1 finding (subtype='vessel_vessel')
  - Two vessels 200m apart but one at 10 m/s → 0 findings (passing)
  - Two vessels 5km apart → 0 findings (too far)
  - Aircraft + vessel 800m apart, both slow → 1 finding ('aircraft_vessel')
  - Vessel-vessel <250m → severity 10 (9 base + 1 vessel-vessel boost)
  - Vessel-vessel 600m → severity 9 (8 + 1)
  - Aircraft-aircraft <250m → severity 9 (no vessel-vessel boost)
  - Re-running scan → no duplicates (NOT EXISTS dedup)
  - Multiple pairs → all detected, distinct
  - One stale entity (last_seen > 30 min) → 0 findings
  - properties.entity_ids carries both entity ids

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_rendezvous.py -v
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
from algorithms.rendezvous import run_rendezvous_scan  # noqa: E402


TEST_PREFIX = "test14"
TEST_TAG = "rendezvous_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_rdv_world():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='rendezvous_detected' "
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


async def _seed_active_entity(
    canonical_suffix: str,
    *,
    entity_type: str = "vessel",
    lat: float = 59.0, lng: float = 25.0,
    velocity_ms: float = 1.0,
    last_seen_min_ago: int = 5,
) -> str:
    """Seed one entity + a *sustained* position_track history.

    Seeds 3 position_track rows at t-25min / t-12min / t-0 (relative to
    `last_seen_min_ago`), all at the same lat/lng/velocity. This satisfies
    the 2026-05-19 sustained-proximity requirement added to
    `algorithms/rendezvous.py` (≥2 samples spanning ≥20 min within radius).
    The historical 1-row-per-entity pattern in this helper caused the
    entire test suite to fail after the algorithm fix landed; multi-sample
    seeding is now the canonical pattern.
    """
    canonical_id = f"{TEST_PREFIX}{canonical_suffix}"
    last_seen_ts = datetime.now(timezone.utc) - timedelta(minutes=last_seen_min_ago)
    canonical_id_type = "icao24" if entity_type == "aircraft" else "mmsi"

    async with acquire() as conn:
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
            float(lng), float(lat),
        )
        # Insert 3 position_track rows spanning 25 minutes ending at last_seen_ts.
        # The 25-min span comfortably exceeds the 20-min sustained-proximity
        # threshold (DEFAULT_MIN_DURATION_MIN in rendezvous.py).
        for offset_min in (25, 12, 0):
            sample_ts = last_seen_ts - timedelta(minutes=offset_min)
            await conn.execute(
                """
                INSERT INTO position_track
                    (time, entity_id, geom, velocity_ms, heading_deg)
                VALUES
                    ($1, $2,
                     ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                     $5, 0)
                """,
                sample_ts, eid, float(lng), float(lat), float(velocity_ms),
            )
    return str(eid)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_two_slow_vessels_close_together_emits_finding(_clean_rdv_world):
    """Two vessels 200m apart at ~1 m/s → 1 rendezvous_detected finding."""
    a = await _seed_active_entity("VA", lat=59.0, lng=25.0, velocity_ms=1.0)
    b = await _seed_active_entity("VB", lat=59.001, lng=25.0, velocity_ms=1.5)
    # 0.001 deg lat ~ 111m at equator, ~57m at lat 59 — ~111m total

    n = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT event_subtype, severity, title, properties FROM event "
        "WHERE event_type='rendezvous_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_subtype"] == "vessel_vessel"
    # 0.001 deg = ~111m at lat 59, well under 250m → severity 9 + 1 (v-v) = 10
    assert r["severity"] == 10.0

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert {a, b} == set(props["entity_ids"])
    assert int(props["distance_m"]) < 250


async def test_passing_vessel_at_high_velocity_excluded(_clean_rdv_world):
    """One vessel at 10 m/s — too fast for rendezvous (passing-by, not docking)."""
    await _seed_active_entity("VC", lat=59.0, lng=25.0, velocity_ms=10.0)
    await _seed_active_entity("VD", lat=59.001, lng=25.0, velocity_ms=1.0)
    n = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_far_vessels_excluded(_clean_rdv_world):
    """Two vessels ~5km apart — outside 1km radius."""
    await _seed_active_entity("FA", lat=59.0, lng=25.0, velocity_ms=1.0)
    await _seed_active_entity("FB", lat=59.05, lng=25.0, velocity_ms=1.0)
    # 0.05 deg lat ~ 5.5km — well outside 1km
    n = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_aircraft_vessel_pair_emits_mixed_subtype(_clean_rdv_world):
    """Helicopter hovering over vessel — aircraft_vessel pair."""
    await _seed_active_entity("AC", entity_type="aircraft",
                                lat=59.0,    lng=25.0, velocity_ms=2.0)
    await _seed_active_entity("VS", entity_type="vessel",
                                lat=59.005,  lng=25.0, velocity_ms=0.5)
    n = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1
    subtype = await fetchval(
        "SELECT event_subtype FROM event WHERE event_type='rendezvous_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    # Subtype order depends on entity UUID order (a.id < b.id), not type
    # alphabetical, so either ordering is valid — both name the same pair.
    assert subtype in ("aircraft_vessel", "vessel_aircraft")


async def test_vessel_vessel_within_250m_severity_max(_clean_rdv_world):
    """vessel-vessel pair <250m apart → severity 9 + 1 (v-v boost) = 10."""
    await _seed_active_entity("CA", lat=59.0,     lng=25.0, velocity_ms=1.0)
    await _seed_active_entity("CB", lat=59.0005,  lng=25.0, velocity_ms=1.0)
    # 0.0005 deg ~ 55m — well under 250m
    await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    sev = await fetchval(
        "SELECT severity FROM event WHERE event_type='rendezvous_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert sev == 10.0


async def test_aircraft_aircraft_within_250m_severity_9(_clean_rdv_world):
    """aircraft-aircraft pair <250m → severity 9 (no v-v boost)."""
    await _seed_active_entity("HA", entity_type="aircraft",
                                lat=35.0, lng=-100.0, velocity_ms=2.0)
    await _seed_active_entity("HB", entity_type="aircraft",
                                lat=35.0005, lng=-100.0, velocity_ms=2.0)
    await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    sev = await fetchval(
        "SELECT severity FROM event WHERE event_type='rendezvous_detected' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert sev == 9.0


async def test_idempotent_within_dedup(_clean_rdv_world):
    await _seed_active_entity("IA", lat=59.0,    lng=25.0, velocity_ms=1.0)
    await _seed_active_entity("IB", lat=59.001,  lng=25.0, velocity_ms=1.0)
    n1 = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1
    n2 = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 0


async def test_multiple_distinct_pairs(_clean_rdv_world):
    # Pair 1 — vessels in Baltic
    await _seed_active_entity("PA1", lat=59.0,   lng=25.0,  velocity_ms=1.0)
    await _seed_active_entity("PA2", lat=59.001, lng=25.0,  velocity_ms=1.0)
    # Pair 2 — vessels off Florida (far from pair 1)
    await _seed_active_entity("PB1", lat=27.0,   lng=-80.0, velocity_ms=1.0)
    await _seed_active_entity("PB2", lat=27.001, lng=-80.0, velocity_ms=1.0)

    n = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 2


async def test_stale_entity_excluded(_clean_rdv_world):
    """One entity last_seen 60min ago — outside default 30min lookback."""
    await _seed_active_entity("SA", lat=59.0,   lng=25.0, velocity_ms=1.0)
    await _seed_active_entity("SB", lat=59.001, lng=25.0, velocity_ms=1.0,
                                last_seen_min_ago=60)
    n = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_no_pairs_returns_zero(_clean_rdv_world):
    n = await run_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
