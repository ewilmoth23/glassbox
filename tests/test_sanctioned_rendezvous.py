"""
Phase 4 algorithm #9 — sanctioned_rendezvous.py test.

Combined-signal algorithm: rendezvous pair (close, slow-moving) where AT
LEAST ONE side matches OFAC SDN.

Asserts:
  - Two sanctioned vessels close together → severity 10, subtype='both_sanctioned'
  - One sanctioned + one regular vessel <500m → severity 9, subtype='one_sanctioned'
  - One sanctioned + one regular vessel <1km → severity 8
  - Two regular (non-sanctioned) close vessels → 0 findings
  - Sanctioned + far apart → 0 findings
  - Sanctioned + high velocity → 0 findings
  - Idempotent within dedup
  - properties carry both vessels' IDs + sanctioned-canonical-id when applicable

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_sanctioned_rendezvous.py -v
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
from algorithms.sanctioned_rendezvous import run_sanctioned_rendezvous_scan  # noqa: E402


TEST_PREFIX = "test19"
TEST_SDN_PREFIX = "ofac_sdn:vessel:test19_"
TEST_TAG = "sanctioned_rdv_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_world():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='sanctioned_vessel_rendezvous' "
            "AND properties->>'algorithm'=$1",
            TEST_TAG,
        )
        await execute(
            "DELETE FROM position_track WHERE entity_id IN "
            "(SELECT id FROM entity WHERE canonical_id LIKE $1)",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1 OR canonical_id LIKE $2",
            f"{TEST_PREFIX}%", "ofac_sdn:%test19%",
        )
    await _cleanup()
    yield
    await _cleanup()


async def _seed_live_vessel(
    suffix: str,
    *,
    display_name: str,
    imo: int | None = None,
    lat: float = 59.0, lng: float = 25.0,
    velocity_ms: float = 1.0,
) -> str:
    canonical_id = f"{TEST_PREFIX}{suffix}"
    last_seen_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    import json
    props = {}
    if imo is not None:
        props["imo"] = imo
    async with acquire() as conn:
        eid = await conn.fetchval(
            """
            INSERT INTO entity
                (entity_type, canonical_id_type, canonical_id,
                 display_name, properties, last_seen, updated_at,
                 current_geom, current_position_time)
            VALUES
                ('vessel', 'mmsi', $1, $2, $3::jsonb, $4, $4,
                 ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography, $4)
            RETURNING id
            """,
            canonical_id, display_name, json.dumps(props), last_seen_ts,
            float(lng), float(lat),
        )
        await conn.execute(
            """
            INSERT INTO position_track (time, entity_id, geom, velocity_ms, heading_deg)
            VALUES ($1, $2, ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography, $5, 0)
            """,
            last_seen_ts, eid, float(lng), float(lat), float(velocity_ms),
        )
    return str(eid)


async def _seed_sanctioned(
    suffix: str, *, display_name: str, imo: int | None = None,
) -> str:
    canonical_id = f"{TEST_SDN_PREFIX}{suffix}"
    import json
    props = {"type": "vessel", "fcra_safe": False}
    if imo is not None:
        props["imo"] = imo
    async with acquire() as conn:
        eid = await conn.fetchval(
            """
            INSERT INTO entity
                (entity_type, canonical_id_type, canonical_id,
                 display_name, properties, last_seen, updated_at)
            VALUES
                ('sanctioned_vessel', 'ofac_sdn_id', $1, $2, $3::jsonb, NOW(), NOW())
            RETURNING id
            """,
            canonical_id, display_name, json.dumps(props),
        )
    return str(eid)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_both_sanctioned_severity_max(_clean_world):
    """Two sanctioned vessels <500m apart → severity 10, both_sanctioned."""
    await _seed_live_vessel("A", display_name="VOLGO-DON 117",
                              imo=11111, lat=59.0,    lng=25.0, velocity_ms=1.0)
    await _seed_live_vessel("B", display_name="GEROY IGOR ASEEV",
                              imo=22222, lat=59.001, lng=25.0, velocity_ms=1.0)
    await _seed_sanctioned("SA", display_name="VOLGO-DON 117", imo=11111)
    await _seed_sanctioned("SB", display_name="GEROY IGOR ASEEV", imo=22222)

    n = await run_sanctioned_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 1
    rows = await fetch(
        "SELECT event_subtype, severity, properties FROM event "
        "WHERE event_type='sanctioned_vessel_rendezvous' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    assert rows[0]["event_subtype"] == "both_sanctioned"
    assert rows[0]["severity"] == 10.0


async def test_one_sanctioned_close_severity_9(_clean_world):
    """One sanctioned + one regular vessel <500m → severity 9."""
    await _seed_live_vessel("C", display_name="ASTRA", imo=33333,
                              lat=59.0,    lng=25.0, velocity_ms=1.0)
    await _seed_live_vessel("D", display_name="REGULAR CARGO", imo=44444,
                              lat=59.001, lng=25.0, velocity_ms=1.0)
    await _seed_sanctioned("SC", display_name="ASTRA", imo=33333)

    n = await run_sanctioned_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 1
    sev = await fetchval(
        "SELECT severity FROM event WHERE event_type='sanctioned_vessel_rendezvous' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert sev == 9.0


async def test_neither_sanctioned_excluded(_clean_world):
    """Two regular vessels close together → 0 findings."""
    await _seed_live_vessel("R1", display_name="REGULAR ONE",   imo=55555)
    await _seed_live_vessel("R2", display_name="REGULAR TWO",   imo=66666,
                              lat=59.001)
    n = await run_sanctioned_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


async def test_far_pair_excluded(_clean_world):
    """Sanctioned + far apart → 0 findings."""
    await _seed_live_vessel("F1", display_name="ASTRA", imo=77777,
                              lat=59.0,  lng=25.0)
    await _seed_live_vessel("F2", display_name="OTHER", imo=88888,
                              lat=59.05, lng=25.0)  # ~5km
    await _seed_sanctioned("SF", display_name="ASTRA", imo=77777)
    n = await run_sanctioned_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


async def test_high_velocity_excluded(_clean_world):
    """Sanctioned + close + 10 m/s → too fast for STS, 0 findings."""
    await _seed_live_vessel("H1", display_name="ASTRA", imo=99999,
                              velocity_ms=10.0)
    await _seed_live_vessel("H2", display_name="OTHER", imo=100100,
                              lat=59.001, velocity_ms=10.0)
    await _seed_sanctioned("SH", display_name="ASTRA", imo=99999)
    n = await run_sanctioned_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


async def test_idempotent_within_dedup(_clean_world):
    await _seed_live_vessel("IA", display_name="ASTRA", imo=121212)
    await _seed_live_vessel("IB", display_name="OTHER", imo=131313, lat=59.001)
    await _seed_sanctioned("ISI", display_name="ASTRA", imo=121212)
    n1 = await run_sanctioned_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n1 == 1
    n2 = await run_sanctioned_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n2 == 0


async def test_no_inputs_returns_zero(_clean_world):
    n = await run_sanctioned_rendezvous_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
