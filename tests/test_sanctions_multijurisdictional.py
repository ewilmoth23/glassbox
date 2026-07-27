"""
Phase 4 algorithm #11 — algorithms/sanctions_multijurisdictional.py test.

Detects live AIS vessels that have been independently flagged by ≥2
sanctioning authorities (OFAC, EU CFSP, UK OFSI in any combination)
within the dedup window. Emits a higher-priority
`sanctioned_vessel_multijurisdictional` event that the brief + UI
surface as CRITICAL.

Test strategy: hand-seed `sanctioned_vessel_underway` events with
properties.sanctioning_authority set to different authorities for the
same live entity_id, then run the scan. Multi-jurisdictional events
must fire only when ≥2 distinct authorities are present.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_sanctions_multijurisdictional.py -v
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetch, execute, acquire  # noqa: E402
from algorithms.sanctions_multijurisdictional import (  # noqa: E402
    run_sanctions_multijurisdictional_scan,
)


TEST_PREFIX = "test_smj"
TEST_TAG = "sanctions_multi_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _cleanup():
        # The scan reads from sanctioned_vessel_underway and writes to
        # sanctioned_vessel_multijurisdictional. Clean both, plus the seed
        # vessel rows.
        await execute(
            "DELETE FROM event WHERE event_type='sanctioned_vessel_multijurisdictional' "
            "AND properties->>'algorithm'=$1",
            TEST_TAG,
        )
        await execute(
            "DELETE FROM event WHERE event_type='sanctioned_vessel_underway' "
            "AND properties->>'mmsi' LIKE $1",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"{TEST_PREFIX}%",
        )

    await _cleanup()
    yield
    await _cleanup()


async def _seed_live_vessel(suffix: str, *, name: str = "TEST VESSEL") -> str:
    cid = f"{TEST_PREFIX}{suffix}"
    async with acquire() as conn:
        eid = await conn.fetchval(
            """
            INSERT INTO entity
                (entity_type, canonical_id_type, canonical_id,
                 display_name, properties, last_seen, updated_at,
                 current_geom, current_position_time)
            VALUES ('vessel', 'mmsi', $1, $2, '{}'::jsonb, NOW(), NOW(),
                    ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography, NOW())
            RETURNING id
            """,
            cid, name,
        )
    return str(eid)


async def _seed_underway_event(
    *,
    entity_id: str,
    authority: str,
    mmsi: str,
    name: str = "TEST VESSEL",
    match_kind: str = "imo_match",
) -> None:
    """Insert a sanctioned_vessel_underway event with the given authority,
    mimicking what sanctions_match would emit."""
    props = {
        "algorithm": "sanctions_match",
        "mmsi": mmsi,
        "live_vessel_name": name,
        "live_imo": "9999999",
        "fcra_safe": False,
        "sanctioning_authority": authority,
        "ofac_sdn_match_name": name,
        "ofac_sdn_canonical_id": f"{authority}:vessel:{mmsi}",
    }
    await execute(
        """
        INSERT INTO event
            (event_type, event_subtype, event_time, geom, severity,
             title, description, properties, domain, decay_half_life_min,
             entity_id)
        VALUES
            ('sanctioned_vessel_underway', $1, NOW(),
             ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography,
             10.0, 'seed', 'seed', $2::jsonb, 'maritime', 1440, $3)
        """,
        match_kind, json.dumps(props), entity_id,
    )


# ─── Tests ─────────────────────────────────────────────────────────────────


async def test_dual_authority_match_fires(_clean):
    """A live vessel with sanctioned_vessel_underway events from two distinct
    authorities → one multi-jurisdictional event with severity=10."""
    eid = await _seed_live_vessel("V1", name="POLA SOFIA")
    await _seed_underway_event(entity_id=eid, authority="US Treasury OFAC",
                                mmsi=f"{TEST_PREFIX}V1")
    await _seed_underway_event(entity_id=eid, authority="UK OFSI",
                                mmsi=f"{TEST_PREFIX}V1")

    n = await run_sanctions_multijurisdictional_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT severity, event_subtype, title, properties, entity_id "
        "FROM event WHERE event_type='sanctioned_vessel_multijurisdictional' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["severity"] == 10.0
    assert r["event_subtype"] == "dual_listed"
    # Title is sourced from the seeded underway-event payload's
    # live_vessel_name (default "TEST VESSEL"), not the entity's display_name.
    assert "TEST VESSEL" in r["title"] or "MMSI" in r["title"]
    assert str(r["entity_id"]) == eid

    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["authority_count"] == 2
    assert props["multi_jurisdictional"] is True
    auths = props["authorities"]
    if isinstance(auths, str):
        auths = json.loads(auths)
    assert sorted(auths) == ["UK OFSI", "US Treasury OFAC"]


async def test_tri_authority_match_subtype(_clean):
    """OFAC + UK + EU all flag same vessel → subtype='tri_listed'."""
    eid = await _seed_live_vessel("V2", name="TRI LISTED")
    for authority in ("US Treasury OFAC", "UK OFSI", "EU CFSP"):
        await _seed_underway_event(entity_id=eid, authority=authority,
                                    mmsi=f"{TEST_PREFIX}V2")

    n = await run_sanctions_multijurisdictional_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1
    rows = await fetch(
        "SELECT event_subtype, properties FROM event "
        "WHERE event_type='sanctioned_vessel_multijurisdictional' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert rows[0]["event_subtype"] == "tri_listed"
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["authority_count"] == 3


async def test_single_authority_does_not_fire(_clean):
    """A vessel with one authority's underway event → no multi-jurisdictional
    event. (The single-authority event already exists from sanctions_match.)"""
    eid = await _seed_live_vessel("V3", name="SOLO LISTED")
    await _seed_underway_event(entity_id=eid, authority="US Treasury OFAC",
                                mmsi=f"{TEST_PREFIX}V3")

    n = await run_sanctions_multijurisdictional_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_idempotent_within_dedup_window(_clean):
    """Same authority set re-emits as a no-op."""
    eid = await _seed_live_vessel("V4", name="DUAL VESSEL")
    await _seed_underway_event(entity_id=eid, authority="US Treasury OFAC",
                                mmsi=f"{TEST_PREFIX}V4")
    await _seed_underway_event(entity_id=eid, authority="EU CFSP",
                                mmsi=f"{TEST_PREFIX}V4")

    n1 = await run_sanctions_multijurisdictional_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    n2 = await run_sanctions_multijurisdictional_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1
    assert n2 == 0


async def test_new_authority_joining_re_emits(_clean):
    """When a third authority joins (set changes from {OFAC,UK} to
    {OFAC,UK,EU}), authority_set_key changes → fresh event fires."""
    eid = await _seed_live_vessel("V5", name="GROWING SET")
    await _seed_underway_event(entity_id=eid, authority="US Treasury OFAC",
                                mmsi=f"{TEST_PREFIX}V5")
    await _seed_underway_event(entity_id=eid, authority="UK OFSI",
                                mmsi=f"{TEST_PREFIX}V5")

    n1 = await run_sanctions_multijurisdictional_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1

    # EU joins later
    await _seed_underway_event(entity_id=eid, authority="EU CFSP",
                                mmsi=f"{TEST_PREFIX}V5")

    n2 = await run_sanctions_multijurisdictional_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 1   # tri-listed event fires fresh

    rows = await fetch(
        "SELECT event_subtype FROM event "
        "WHERE event_type='sanctioned_vessel_multijurisdictional' "
        "AND properties->>'algorithm'=$1 "
        "ORDER BY event_time",
        TEST_TAG,
    )
    assert len(rows) == 2
    assert [r["event_subtype"] for r in rows] == ["dual_listed", "tri_listed"]


async def test_only_recent_underway_events_count(_clean):
    """If the underway events are older than lookback_hours, no multi event."""
    eid = await _seed_live_vessel("V6", name="STALE VESSEL")
    # Insert events but backdate them by 48h
    await execute(
        """
        INSERT INTO event
            (event_type, event_subtype, event_time, geom, severity,
             title, description, properties, domain, decay_half_life_min,
             entity_id)
        VALUES
            ('sanctioned_vessel_underway', 'imo_match',
             NOW() - INTERVAL '48 hours',
             ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography,
             10.0, 'seed', 'seed', $1::jsonb, 'maritime', 1440, $2),
            ('sanctioned_vessel_underway', 'imo_match',
             NOW() - INTERVAL '48 hours',
             ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography,
             10.0, 'seed', 'seed', $3::jsonb, 'maritime', 1440, $2)
        """,
        json.dumps({"algorithm": "sanctions_match", "mmsi": f"{TEST_PREFIX}V6",
                    "sanctioning_authority": "US Treasury OFAC", "fcra_safe": False}),
        eid,
        json.dumps({"algorithm": "sanctions_match", "mmsi": f"{TEST_PREFIX}V6",
                    "sanctioning_authority": "UK OFSI", "fcra_safe": False}),
    )

    n = await run_sanctions_multijurisdictional_scan(
        lookback_hours=24,   # default
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0
