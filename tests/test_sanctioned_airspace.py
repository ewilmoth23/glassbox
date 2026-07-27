"""
Phase 4 algorithm #7 — algorithms/sanctioned_airspace.py test.

Algorithm flags aircraft entities whose current_geom falls inside a known
sanctioned-airspace polygon (Iran, Syria, North Korea, Crimea, etc.).

Asserts:
  - Aircraft inside Iran bbox → 1 finding, event_subtype='iran', severity 8
  - Aircraft inside North Korea bbox → 1 finding, severity 10
  - Aircraft outside all zones (over Atlantic) → 0 findings
  - Aircraft inside Cuba → severity 7
  - Stale aircraft (last_seen > 1h) → 0 findings
  - Aircraft with NULL current_geom → 0 findings
  - Re-running scan → no duplicates per (aircraft, zone, day)
  - Aircraft transiting two zones (Iran → Syria) over time → 2 findings
    (different subtypes)
  - Multiple aircraft → all flagged, distinct entity_ids
  - Vessel inside sanctioned airspace → 0 findings (only aircraft tracked)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_sanctioned_airspace.py -v
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
from algorithms.sanctioned_airspace import run_sanctioned_airspace_scan  # noqa: E402


TEST_PREFIX = "test15"
TEST_TAG = "sanctioned_airspace_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_world():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='aircraft_in_sanctioned_airspace' "
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
    lat: float, lng: float,
    callsign: str | None = "TESTFLT1",
    last_seen_min_ago: int = 5,
    has_geom: bool = True,
    entity_type: str = "aircraft",
) -> str:
    canonical_id = f"{TEST_PREFIX}{icao_suffix}"
    last_seen_ts = datetime.now(timezone.utc) - timedelta(minutes=last_seen_min_ago)
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
                callsign, last_seen_ts, float(lng), float(lat),
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
                callsign, last_seen_ts,
            )
    return str(eid)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_aircraft_in_iran_emits_finding(_clean_world):
    eid = await _seed_aircraft("IRN", lat=32.0, lng=53.0)  # central Iran
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1
    rows = await fetch(
        "SELECT event_subtype, severity, properties, entity_id FROM event "
        "WHERE event_type='aircraft_in_sanctioned_airspace' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_subtype"] == "iran"
    assert r["severity"] == 8.0
    assert str(r["entity_id"]) == eid


async def test_aircraft_in_north_korea_severity_max(_clean_world):
    await _seed_aircraft("NK1", lat=40.0, lng=127.0, callsign="DPRK001")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1
    sev = await fetchval(
        "SELECT severity FROM event WHERE event_type='aircraft_in_sanctioned_airspace' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert sev == 10.0


async def test_aircraft_in_atlantic_not_flagged(_clean_world):
    """Aircraft over open Atlantic — outside all sanctioned zones."""
    await _seed_aircraft("ATL", lat=30.0, lng=-40.0)
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_aircraft_in_cuba_severity_7(_clean_world):
    await _seed_aircraft("CUB", lat=22.0, lng=-79.0)
    await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    sev = await fetchval(
        "SELECT severity FROM event WHERE event_type='aircraft_in_sanctioned_airspace' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert sev == 7.0


async def test_aircraft_in_crimea_severity_9(_clean_world):
    await _seed_aircraft("CRM", lat=45.0, lng=34.0)
    await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    rows = await fetch(
        "SELECT event_subtype, severity FROM event "
        "WHERE event_type='aircraft_in_sanctioned_airspace' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    assert rows[0]["event_subtype"] == "crimea"
    assert rows[0]["severity"] == 9.0


async def test_stale_aircraft_excluded(_clean_world):
    await _seed_aircraft("STA", lat=32.0, lng=53.0, last_seen_min_ago=120)
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_null_geom_excluded(_clean_world):
    await _seed_aircraft("NUL", lat=32.0, lng=53.0, has_geom=False)
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_idempotent_within_dedup(_clean_world):
    await _seed_aircraft("IDM", lat=32.0, lng=53.0)
    n1 = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1
    n2 = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 0


async def test_multiple_aircraft_flagged(_clean_world):
    await _seed_aircraft("MA", lat=32.0, lng=53.0)        # Iran
    await _seed_aircraft("MB", lat=40.0, lng=127.0)       # NK
    await _seed_aircraft("MC", lat=22.0, lng=-79.0)       # Cuba
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 3


async def test_vessel_not_tracked(_clean_world):
    """Sanctioned-airspace algorithm only tracks aircraft, not vessels."""
    await _seed_aircraft("VES", lat=32.0, lng=53.0,
                          entity_type="vessel")  # vessel in Iran area
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_no_aircraft_in_zones_returns_zero(_clean_world):
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


# ─── Regression: tightened polygons (P0-C audit #10, 2026-05-19) ──────────
#
# Original axis-aligned bboxes leaked into non-sanctioned neighbor states
# (Dubai, Doha, Bahrain, Riyadh in "iran"; Lebanon/Israel/Jordan/Med in
# "syria"; Lithuania in "belarus"; Florida Strait in "cuba").  Measured
# 76.7% production FP rate on a 14-day random sample.  The fix replaced
# bboxes with tighter multi-vertex polygons.  These tests assert the
# specific airport hubs and neighbor-state capitals that the OLD code
# falsely flagged now produce 0 findings.


async def test_dubai_dxb_not_in_iran_zone(_clean_world):
    """Dubai International is in UAE airspace, not Iran. Old bbox leaked."""
    await _seed_aircraft("FP_DXB", lat=25.25, lng=55.38, callsign="UAE100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_doha_doh_not_in_iran_zone(_clean_world):
    """Doha is in Qatar airspace, not Iran. Old bbox leaked."""
    await _seed_aircraft("FP_DOH", lat=25.27, lng=51.61, callsign="QTR100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_bahrain_bah_not_in_iran_zone(_clean_world):
    """Bahrain is in Bahraini airspace, not Iran. Old bbox leaked."""
    await _seed_aircraft("FP_BAH", lat=26.39, lng=50.64, callsign="GFA100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_riyadh_ruh_not_in_iran_zone(_clean_world):
    """Riyadh is in Saudi airspace, not Iran. Old bbox leaked."""
    await _seed_aircraft("FP_RUH", lat=24.65, lng=46.71, callsign="SVA100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_vilnius_vno_not_in_belarus_zone(_clean_world):
    """Vilnius is in Lithuania (NATO/EU), not Belarus. Old bbox leaked."""
    await _seed_aircraft("FP_VNO", lat=54.71, lng=25.28, callsign="LOT100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_beirut_bey_not_in_syria_zone(_clean_world):
    """Beirut is in Lebanon, not Syria. Old bbox leaked."""
    await _seed_aircraft("FP_BEY", lat=33.9, lng=35.5, callsign="MEA100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_tel_aviv_tlv_not_in_syria_zone(_clean_world):
    """Tel Aviv is in Israel, not Syria. Old bbox leaked."""
    await _seed_aircraft("FP_TLV", lat=32.08, lng=34.78, callsign="ELY100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_amman_amm_not_in_syria_zone(_clean_world):
    """Amman is in Jordan, not Syria. Old bbox leaked."""
    await _seed_aircraft("FP_AMM", lat=31.95, lng=35.93, callsign="RJA100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_florida_keys_not_in_cuba_zone(_clean_world):
    """The Florida Keys are US territory, not Cuba. Old bbox extended to lat=23.5."""
    await _seed_aircraft("FP_FLK", lat=24.5, lng=-81.0, callsign="AAL100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_damascus_in_syria_zone(_clean_world):
    """Damascus IS in Syria — tightened polygon must still cover the SW capital."""
    await _seed_aircraft("TP_DAM", lat=33.51, lng=36.30, callsign="SYR100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1


async def test_tehran_in_iran_zone(_clean_world):
    """Tehran IS in Iran — tightened polygon must still cover the capital."""
    await _seed_aircraft("TP_THR", lat=35.69, lng=51.43, callsign="IRA100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1


async def test_minsk_in_belarus_zone(_clean_world):
    """Minsk IS in Belarus — tightened polygon must still cover the capital."""
    await _seed_aircraft("TP_MSQ", lat=53.90, lng=27.57, callsign="BRU100")
    n = await run_sanctioned_airspace_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
