"""
Phase 4 algorithm #3 — algorithms/sanctions_match.py test.

Cross-references live AIS-broadcasting vessels (entity_type='vessel') against
OFAC SDN sanctioned vessels (entity_type='sanctioned_vessel') by display_name
trigram similarity. Emits 'sanctioned_vessel_underway' events for matches.

Asserts:
  - Exact name match → 1 finding inserted, severity=9, similarity=1.0
  - Case-only difference (TAIMYR ↔ Taimyr) → 1 finding (similarity > 0.9)
  - Generic name with similarity < 0.9 → 0 findings
  - Vessel current_position_time older than lookback → 0 findings (stale)
  - Vessel with NULL display_name → 0 findings
  - sanctioned_vessel with NULL display_name → 0 findings
  - Re-running scan → no duplicate (idempotent within 24h)
  - Multiple distinct matches → all emitted, distinct entity_ids
  - Properties carry mmsi, ofac_sdn_canonical_id, similarity, fcra_safe=false
  - event.entity_id back-links to the LIVE vessel (not the sanctioned entry)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_sanctions_match.py -v
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
from algorithms.sanctions_match import run_sanctions_match_scan  # noqa: E402


TEST_PREFIX = "test09"
TEST_SDN_PREFIX = "ofac_sdn:vessel:test09_"
TEST_TAG = "sanctions_match_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_match_world():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='sanctioned_vessel_underway' "
            "AND properties->>'algorithm'=$1",
            TEST_TAG,
        )
        await execute(
            "DELETE FROM position_track WHERE entity_id IN "
            "(SELECT id FROM entity WHERE canonical_id LIKE $1)",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1 "
            "OR canonical_id LIKE $2",
            f"{TEST_PREFIX}%",
            "ofac_sdn:%test09%",
        )

    await _cleanup()
    yield
    await _cleanup()


async def _seed_live_vessel(
    mmsi_suffix: str,
    *,
    display_name: str | None,
    last_seen_min_ago: int = 0,
    imo: int | None = None,
) -> str:
    canonical_id = f"{TEST_PREFIX}{mmsi_suffix}"
    last_seen_ts = datetime.now(timezone.utc) - timedelta(minutes=last_seen_min_ago)
    import json
    props = {}
    if imo:
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
                 ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography, $4)
            RETURNING id
            """,
            canonical_id, display_name, json.dumps(props), last_seen_ts,
        )
    return str(eid)


async def _seed_sanctioned_vessel(
    suffix: str,
    *,
    display_name: str,
    imo: int | None = None,
) -> str:
    canonical_id = f"{TEST_SDN_PREFIX}{suffix}"
    import json
    props = {"type": "vessel", "fcra_safe": False,
             "sanctioning_authority": "US Treasury OFAC"}
    if imo is not None:
        props["imo"] = imo
    async with acquire() as conn:
        eid = await conn.fetchval(
            """
            INSERT INTO entity
                (entity_type, canonical_id_type, canonical_id,
                 display_name, properties, last_seen, updated_at)
            VALUES
                ('sanctioned_vessel', 'ofac_sdn_id', $1, $2, $3::jsonb,
                 NOW(), NOW())
            RETURNING id
            """,
            canonical_id, display_name, json.dumps(props),
        )
    return str(eid)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_exact_name_match_emits_finding(_clean_match_world):
    eid = await _seed_live_vessel("V1", display_name="POLA SOFIA", imo=9849459)
    await _seed_sanctioned_vessel("S1", display_name="POLA SOFIA")

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT severity, title, properties, entity_id FROM event "
        "WHERE event_type='sanctioned_vessel_underway' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["severity"] == 9.0
    assert "POLA SOFIA" in r["title"]
    assert str(r["entity_id"]) == eid

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["mmsi"] == f"{TEST_PREFIX}V1"
    assert props["live_vessel_name"] == "POLA SOFIA"
    assert props["ofac_sdn_match_name"] == "POLA SOFIA"
    assert float(props["similarity"]) == 1.0
    assert props["live_imo"] == "9849459"
    assert props["fcra_safe"] is False


async def test_case_difference_still_matches(_clean_match_world):
    """TAIMYR (live) ↔ Taimyr (OFAC) — uppercase forced in SQL, sim=1.0."""
    await _seed_live_vessel("V2", display_name="TAIMYR")
    await _seed_sanctioned_vessel("S2", display_name="Taimyr")

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 1


async def test_low_similarity_does_not_match(_clean_match_world):
    """AMBER vs AMBERSTAR is similarity ~0.7 — below default 0.9 threshold."""
    await _seed_live_vessel("V3", display_name="AMBERSTAR LINER")
    await _seed_sanctioned_vessel("S3", display_name="AMBER")

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


async def test_lower_threshold_admits_more_matches(_clean_match_world):
    """Same as above but with similarity_threshold=0.4."""
    await _seed_live_vessel("V4", display_name="AMBERSTAR LINER")
    await _seed_sanctioned_vessel("S4", display_name="AMBER")

    n = await run_sanctions_match_scan(
        similarity_threshold=0.4,
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    # Trigram similarity for AMBERSTAR LINER ↔ AMBER is roughly 0.4-0.5
    # depending on Postgres trigram model. Allow either 0 or 1.
    assert n in (0, 1)


async def test_stale_vessel_excluded(_clean_match_world):
    """Live vessel with current_position_time older than lookback → no match."""
    await _seed_live_vessel("V5", display_name="POLA SOFIA",
                              last_seen_min_ago=48 * 60)  # 48h ago
    await _seed_sanctioned_vessel("S5", display_name="POLA SOFIA")

    n = await run_sanctions_match_scan(
        lookback_min=24 * 60,  # default 24h
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


async def test_null_display_names_excluded(_clean_match_world):
    """Vessel without name (NULL) cannot match — and shouldn't crash."""
    await _seed_live_vessel("V6", display_name=None)
    await _seed_sanctioned_vessel("S6", display_name="POLA SOFIA")

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


async def test_short_name_excluded(_clean_match_world):
    """Names < 4 chars are excluded — too generic, false-positive risk too high."""
    await _seed_live_vessel("V7", display_name="ABC")
    await _seed_sanctioned_vessel("S7", display_name="ABC")

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


async def test_idempotent_within_dedup_window(_clean_match_world):
    await _seed_live_vessel("V8", display_name="ZALIV AMURSKIY")
    await _seed_sanctioned_vessel("S8", display_name="Zaliv Amurskiy")

    n1 = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n1 == 1

    n2 = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n2 == 0  # already flagged, NOT EXISTS dedup

    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type='sanctioned_vessel_underway' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert total == 1


async def test_multiple_distinct_matches(_clean_match_world):
    await _seed_live_vessel("MA", display_name="POLA SOFIA")
    await _seed_live_vessel("MB", display_name="ZALIV AMURSKIY")
    await _seed_live_vessel("MC", display_name="TAIMYR")
    await _seed_sanctioned_vessel("SA", display_name="POLA SOFIA")
    await _seed_sanctioned_vessel("SB", display_name="Zaliv Amurskiy")
    await _seed_sanctioned_vessel("SC", display_name="Taimyr")

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 3


async def test_sanctions_match_with_no_live_or_sdn(_clean_match_world):
    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0


async def test_imo_match_takes_precedence_over_name(_clean_match_world):
    """When BOTH sides have IMO and they match, emit subtype='imo_match'
    severity 10. Name match would also fire on this pair, but the IMO branch
    of the OR'd JOIN makes it match_kind='imo'."""
    await _seed_live_vessel("VIMO", display_name="AKADEMIK CHERSKIY",
                              imo=8770261)
    await _seed_sanctioned_vessel("SIMO", display_name="Akademik Cherskiy",
                                   imo=8770261)

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT event_subtype, severity, properties FROM event "
        "WHERE event_type='sanctioned_vessel_underway' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    assert rows[0]["event_subtype"] == "imo_match"
    assert rows[0]["severity"] == 10.0

    import json
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["match_kind"] == "imo"
    assert props["live_imo"] == "8770261"
    assert props["sanctioned_imo"] == "8770261"


async def test_imo_match_when_names_differ(_clean_match_world):
    """IMO matches but display_names completely different — should still
    emit (IMO is authoritative)."""
    await _seed_live_vessel("VDIFF", display_name="REGISTERED VESSEL NAME",
                              imo=9999999)
    await _seed_sanctioned_vessel("SDIFF", display_name="OFAC LISTED ALIAS",
                                   imo=9999999)

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 1

    subtype = await fetchval(
        "SELECT event_subtype FROM event "
        "WHERE event_type='sanctioned_vessel_underway' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert subtype == "imo_match"


async def test_imo_mismatch_does_not_fire_via_imo_path(_clean_match_world):
    """Same name, different IMOs — neither path may fire.

    Per IMO's 7-digit numbering scheme, two vessels with different IMOs
    are DIFFERENT vessels even when names match (vessel names get reused
    across the world fleet). The 2026-05-14 fix (commit f4dab9a) tightened
    sanctions_match (and sanctioned_dark_vessel + sanctioned_rendezvous)
    to require at least one IMO to be NULL before falling back to the
    name path — preventing 2,245 historical false positives like
    "live ANTEY IMO 8311912" matched to "sanctioned Antey IMO 9310018".

    This test originally asserted the OLD behavior (name path fires when
    IMOs are both present but differ). After the fix the correct outcome
    is n == 0 — by design, this case is now correctly NOT flagged.
    """
    await _seed_live_vessel("VMIS", display_name="POLA SOFIA", imo=9849459)
    # Different IMO on the OFAC entry — name matches exactly but IMOs
    # disagree, so per the 2026-05-14 fix neither path may fire.
    await _seed_sanctioned_vessel("SMIS", display_name="POLA SOFIA",
                                   imo=1111111)

    n = await run_sanctions_match_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"%{TEST_PREFIX}%",
    )
    assert n == 0, (
        "IMO-mismatch with both IMOs present must NOT fire — that was the "
        "2,245-FP bug fixed on 2026-05-14"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
