"""
Phase 4d-4 — sanctioned_port_arrival compound algorithm.

Asserts:
  - When a vessel has BOTH a recent port_arrival AND a recent
    sanctioned_vessel_underway, a compound event fires
  - Strategic-port hits get severity 10
  - Commercial-port hits get severity 8
  - Idempotent within dedup_hours per (vessel, port)
  - Vessel with arrival but no sanctions match → no event
  - Vessel with sanctions match but no arrival → no event
  - port_call (continuous) also pairs to make the alert (not just
    the arrival transition)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_sanctioned_port_arrival.py -v
"""

from __future__ import annotations

import json as _json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute, acquire  # noqa: E402
from algorithms.sanctioned_port_arrival import (  # noqa: E402
    run_sanctioned_port_arrival_scan,
)


TEST_PREFIX = "spa_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_world():
    async def _cleanup():
        # Cleanup matches every tag this test emits. The original `LIKE 'spa_%_test'`
        # pattern never matched the actual `sanctioned_port_arrival_test` tag produced
        # by `run_sanctioned_port_arrival_scan(algorithm_tag="sanctioned_port_arrival_test")`,
        # which let 115 fixture rows accumulate in production before P0-F.1's DB
        # isolation landed. See ALGORITHM_FP_AUDIT_sanctioned_port_arrival_2026_05_19.md.
        await execute(
            "DELETE FROM event WHERE event_type IN "
            "('port_call', 'port_arrival', 'sanctioned_vessel_underway', "
            " 'sanctioned_port_arrival') "
            "AND (properties->>'algorithm' LIKE 'spa_%_test' "
            "  OR properties->>'algorithm' = 'sanctioned_port_arrival_test')"
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


async def _seed_vessel(label: str, lat: float = 27.18, lng: float = 56.28):
    """Seed a vessel entity at given coords. Returns id."""
    now = datetime.now(timezone.utc)
    vid = await fetchval(
        """
        INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                            display_name, properties, last_seen,
                            current_geom, current_position_time)
        VALUES ('vessel', 'mmsi', $1, $2, '{}'::jsonb, $3,
                ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography, $3)
        RETURNING id
        """,
        f"{TEST_PREFIX}_{label}", f"VS_{label.upper()}", now, lng, lat,
    )
    return vid


async def _seed_port_arrival(vessel_id, *, port_id="IR_BND",
                              port_name="Bandar Abbas",
                              port_country="IR",
                              port_kind="strategic",
                              port_lat=27.1833, port_lng=56.2833,
                              event_type="port_arrival",
                              algorithm="spa_arr_test"):
    """Seed a port_arrival or port_call event for the given vessel."""
    eid = uuid4()
    now = datetime.now(timezone.utc)
    props = _json.dumps({
        "algorithm":    algorithm,
        "vessel_id":    str(vessel_id),
        "port_id":      port_id,
        "port_name":    port_name,
        "port_country": port_country,
        "port_kind":    port_kind,
        "transition":   "arrival" if event_type == "port_arrival" else "stay",
    })
    await execute(
        """
        INSERT INTO event (id, event_type, event_subtype, event_time, geom,
                          severity, title, description, properties, entity_id,
                          domain, decay_half_life_min)
        VALUES ($1, $2, $3, $4,
                ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography,
                7.0, $7, 'test', $8::jsonb, $9, 'maritime', 1440)
        """,
        eid, event_type, port_country, now,
        port_lng, port_lat, f"vessel at {port_name}", props, vessel_id,
    )
    return eid


async def _seed_sanc_match(vessel_id, *, mmsi="123456789",
                            vessel_name="ATLAS", regime="IRAN",
                            algorithm="spa_sanc_test"):
    """Seed a sanctioned_vessel_underway event for the given vessel."""
    eid = uuid4()
    now = datetime.now(timezone.utc)
    props = _json.dumps({
        "algorithm":            algorithm,
        "match_kind":           "imo",
        "mmsi":                 mmsi,
        "live_vessel_name":     vessel_name,
        "match_regime":         regime,
        "sanctioning_authority": "US Treasury OFAC",
    })
    await execute(
        """
        INSERT INTO event (id, event_type, event_subtype, event_time, geom,
                          severity, title, description, properties, entity_id,
                          domain, decay_half_life_min)
        VALUES ($1, 'sanctioned_vessel_underway', $2, $3,
                ST_SetSRID(ST_MakePoint(0, 0), 4326)::geography,
                10.0, $4, 'test', $5::jsonb, $6, 'maritime', 1440)
        """,
        eid, "imo_match", now, f"sanctioned {vessel_name}",
        props, vessel_id,
    )
    return eid


async def _count_compound_for(vessel_id) -> int:
    n = await fetchval(
        "SELECT count(*) FROM event WHERE event_type = 'sanctioned_port_arrival' "
        "AND entity_id = $1::uuid AND properties->>'algorithm' = $2",
        vessel_id, "sanctioned_port_arrival_test",
    )
    return int(n or 0)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_arrival_plus_sanctions_match_fires_compound(_clean_world):
    """The killer query: vessel with both port_arrival AND
    sanctioned_vessel_underway → compound tier-1 alert fires."""
    vid = await _seed_vessel("a")
    await _seed_port_arrival(vid)
    await _seed_sanc_match(vid)
    n = await run_sanctioned_port_arrival_scan(
        algorithm_tag="sanctioned_port_arrival_test",
    )
    assert n >= 1
    assert await _count_compound_for(vid) == 1
    rows = await fetch(
        "SELECT severity, properties FROM event "
        "WHERE event_type = 'sanctioned_port_arrival' "
        "AND entity_id = $1::uuid",
        vid,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["severity"] == pytest.approx(10.0)   # strategic port
    props = r["properties"]
    if isinstance(props, str):
        props = _json.loads(props)
    assert props["match_regime"] == "IRAN"
    assert props["port_country"] == "IR"
    assert props["port_kind"] == "strategic"


async def test_commercial_port_severity_is_8():
    pass  # placeholder; the next test handles it


async def test_commercial_port_gets_severity_8(_clean_world):
    vid = await _seed_vessel("b")
    await _seed_port_arrival(vid, port_id="SG_SIN", port_name="Singapore",
                              port_country="SG", port_kind="commercial",
                              port_lat=1.2655, port_lng=103.824)
    await _seed_sanc_match(vid)
    await run_sanctioned_port_arrival_scan(
        algorithm_tag="sanctioned_port_arrival_test",
    )
    rows = await fetch(
        "SELECT severity FROM event "
        "WHERE event_type = 'sanctioned_port_arrival' "
        "AND entity_id = $1::uuid",
        vid,
    )
    assert rows[0]["severity"] == pytest.approx(8.0)


async def test_idempotent_within_dedup_window(_clean_world):
    """Two scans → still 1 event for this vessel."""
    vid = await _seed_vessel("c")
    await _seed_port_arrival(vid)
    await _seed_sanc_match(vid)
    await run_sanctioned_port_arrival_scan(
        algorithm_tag="sanctioned_port_arrival_test",
    )
    n1 = await _count_compound_for(vid)
    await run_sanctioned_port_arrival_scan(
        algorithm_tag="sanctioned_port_arrival_test",
    )
    n2 = await _count_compound_for(vid)
    assert n1 == 1
    assert n2 == 1


async def test_arrival_only_no_sanctions_match_no_event(_clean_world):
    """Port arrival with no sanctions match → no compound event."""
    vid = await _seed_vessel("d")
    await _seed_port_arrival(vid)
    await run_sanctioned_port_arrival_scan(
        algorithm_tag="sanctioned_port_arrival_test",
    )
    assert await _count_compound_for(vid) == 0


async def test_sanctions_match_only_no_arrival_no_event(_clean_world):
    """Sanctions match with no port arrival → no compound event."""
    vid = await _seed_vessel("e")
    await _seed_sanc_match(vid)
    await run_sanctioned_port_arrival_scan(
        algorithm_tag="sanctioned_port_arrival_test",
    )
    assert await _count_compound_for(vid) == 0


async def test_port_call_also_pairs_for_compound(_clean_world):
    """A port_call (continuous-stay) event ALSO pairs with sanctions
    match — not just port_arrival. Vessel sitting at sanctioned port
    for a week still emits the alert."""
    vid = await _seed_vessel("f")
    await _seed_port_arrival(vid, event_type="port_call")
    await _seed_sanc_match(vid)
    await run_sanctioned_port_arrival_scan(
        algorithm_tag="sanctioned_port_arrival_test",
    )
    assert await _count_compound_for(vid) == 1


async def test_event_subtype_combines_regime_and_country(_clean_world):
    vid = await _seed_vessel("g")
    await _seed_port_arrival(vid, port_country="IR")
    await _seed_sanc_match(vid, regime="IRAN")
    await run_sanctioned_port_arrival_scan(
        algorithm_tag="sanctioned_port_arrival_test",
    )
    rows = await fetch(
        "SELECT event_subtype FROM event "
        "WHERE event_type = 'sanctioned_port_arrival' "
        "AND entity_id = $1::uuid",
        vid,
    )
    assert rows[0]["event_subtype"] == "IRAN:IR"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
