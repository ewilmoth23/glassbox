"""
Phase 4 algorithm #4 — algorithms/military_flights.py test.

Algorithm finds aircraft entities where properties.military=true that have
broadcast within the lookback window. Emits one
'military_aircraft_underway' event per (aircraft, dedup-window).

Asserts:
  - Military aircraft, recently broadcasting → 1 finding (severity 5)
  - Civilian aircraft (military=false) → 0 findings
  - Military aircraft, beyond lookback → 0 findings (stale)
  - Military aircraft, NULL current_geom → 0 findings
  - Re-running the scan → no duplicates (idempotent within dedup window)
  - Multiple distinct mil aircraft → all emitted
  - event.entity_id back-links to the aircraft
  - event_subtype = callsign-prefix family (GAF, VIPR, ...) when callsign present

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_military_flights.py -v
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
from algorithms.military_flights import run_military_flights_scan  # noqa: E402


TEST_PREFIX = "test11"
TEST_TAG = "mil_flights_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_world():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='military_aircraft_underway' "
            "AND properties->>'algorithm'=$1",
            TEST_TAG,
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


async def _seed_aircraft(
    icao_suffix: str,
    *,
    callsign: str | None = "VIPR76",
    military: bool = True,
    last_seen_min_ago: int = 5,
    has_geom: bool = True,
) -> str:
    canonical_id = f"{TEST_PREFIX}{icao_suffix}"
    last_seen_ts = datetime.now(timezone.utc) - timedelta(minutes=last_seen_min_ago)
    import json
    props = {"military": military}
    async with acquire() as conn:
        if has_geom:
            eid = await conn.fetchval(
                """
                INSERT INTO entity
                    (entity_type, canonical_id_type, canonical_id,
                     display_name, properties, last_seen, updated_at,
                     current_geom, current_position_time)
                VALUES
                    ('aircraft', 'icao24', $1, $2, $3::jsonb, $4, $4,
                     ST_SetSRID(ST_MakePoint(-100.0, 35.0), 4326)::geography, $4)
                RETURNING id
                """,
                canonical_id, callsign, json.dumps(props), last_seen_ts,
            )
        else:
            eid = await conn.fetchval(
                """
                INSERT INTO entity
                    (entity_type, canonical_id_type, canonical_id,
                     display_name, properties, last_seen, updated_at,
                     current_geom, current_position_time)
                VALUES
                    ('aircraft', 'icao24', $1, $2, $3::jsonb, $4, $4, NULL, $4)
                RETURNING id
                """,
                canonical_id, callsign, json.dumps(props), last_seen_ts,
            )
    return str(eid)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_military_aircraft_underway_emits_finding(_clean_world):
    eid = await _seed_aircraft("M1", callsign="VIPR76", military=True)
    n = await run_military_flights_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT event_subtype, severity, title, properties, entity_id FROM event "
        "WHERE event_type='military_aircraft_underway' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_subtype"] == "VIPR"
    assert r["severity"] == 5.0
    assert "VIPR76" in r["title"]
    assert str(r["entity_id"]) == eid

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["icao24"] == f"{TEST_PREFIX}M1"
    assert props["callsign"] == "VIPR76"


async def test_civilian_aircraft_excluded(_clean_world):
    await _seed_aircraft("C1", callsign="UAL100", military=False)
    n = await run_military_flights_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_stale_military_aircraft_excluded(_clean_world):
    """Beyond lookback (default 60 min) → not flagged."""
    await _seed_aircraft("S1", callsign="VIPR76", last_seen_min_ago=120)
    n = await run_military_flights_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_no_geom_excluded(_clean_world):
    await _seed_aircraft("G1", callsign="VIPR76", has_geom=False)
    n = await run_military_flights_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_idempotent_within_dedup_window(_clean_world):
    await _seed_aircraft("ID1", callsign="VIPR76")
    n1 = await run_military_flights_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1
    n2 = await run_military_flights_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='military_aircraft_underway' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert total == 1


async def test_multiple_distinct_mil_aircraft(_clean_world):
    await _seed_aircraft("MA", callsign="VIPR76")
    await _seed_aircraft("MB", callsign="GAF648")
    await _seed_aircraft("MC", callsign="SHWK412")
    await _seed_aircraft("MD", callsign=None)  # no callsign → subtype='unknown'

    n = await run_military_flights_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 4

    rows = await fetch(
        "SELECT event_subtype, properties->>'icao24' AS icao24 "
        "FROM event WHERE event_type='military_aircraft_underway' "
        "AND properties->>'algorithm'=$1 "
        "ORDER BY icao24",
        TEST_TAG,
    )
    subtypes = {r["icao24"]: r["event_subtype"] for r in rows}
    assert subtypes[f"{TEST_PREFIX}MA"] == "VIPR"
    assert subtypes[f"{TEST_PREFIX}MB"] == "GAF"
    assert subtypes[f"{TEST_PREFIX}MC"] == "SHWK"
    assert subtypes[f"{TEST_PREFIX}MD"] == "unknown"


async def test_no_military_aircraft_returns_zero(_clean_world):
    n = await run_military_flights_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
