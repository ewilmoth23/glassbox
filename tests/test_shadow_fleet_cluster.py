"""
Shadow-fleet cluster detector tests.

Asserts:
  - 3+ sanctioned vessels within 10km → 1 cluster event.
  - 2 vessels (below threshold) → 0 events.
  - Vessels >10km apart don't cluster.
  - Multi-jurisdictional cluster (mixed authorities) flagged correctly.
  - Idempotent re-emit on same member set.
  - Authority-set + member-set captured in properties.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_shadow_fleet_cluster.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetch, execute, acquire  # noqa: E402
from algorithms.shadow_fleet_cluster import run_shadow_fleet_cluster_scan  # noqa: E402


_PFX = "test_sfc_"
_TAG = "shadow_fleet_cluster_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM event WHERE event_type='shadow_fleet_cluster' "
            "AND properties->>'algorithm'=$1",
            _TAG,
        )
        await execute(
            "DELETE FROM event WHERE event_type='sanctioned_vessel_underway' "
            "AND properties->>'mmsi' LIKE $1",
            f"{_PFX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"{_PFX}%",
        )
    await _do()
    yield
    await _do()


async def _seed_vessel(suffix: str, *, name: str = "TEST", lat: float = 59.0,
                        lng: float = 25.0) -> str:
    cid = f"{_PFX}{suffix}"
    async with acquire() as conn:
        eid = await conn.fetchval(
            """
            INSERT INTO entity
                (entity_type, canonical_id_type, canonical_id,
                 display_name, properties, last_seen, updated_at,
                 current_geom, current_position_time)
            VALUES ('vessel', 'mmsi', $1, $2, '{}'::jsonb, NOW(), NOW(),
                    ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography, NOW())
            RETURNING id
            """,
            cid, name, lng, lat,
        )
    return str(eid)


async def _seed_underway(*, entity_id: str, mmsi: str, name: str,
                          authority: str, lat: float, lng: float) -> None:
    """Insert a sanctioned_vessel_underway event for the given entity."""
    props = {
        "algorithm": "sanctions_match",
        "mmsi": mmsi,
        "live_vessel_name": name,
        "fcra_safe": False,
        "sanctioning_authority": authority,
    }
    await execute(
        """
        INSERT INTO event
            (event_type, event_subtype, event_time, geom, severity,
             title, description, properties, domain, decay_half_life_min,
             entity_id)
        VALUES
            ('sanctioned_vessel_underway', 'imo_match', NOW(),
             ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
             10.0, 'seed', 'seed', $3::jsonb, 'maritime', 1440, $4)
        """,
        lng, lat, json.dumps(props), entity_id,
    )


# ─── Tests ──────────────────────────────────────────────────────────────


async def test_three_vessels_within_radius_emit_cluster(_clean):
    """3 vessels within 10km → 1 cluster event."""
    # Three points within ~5km of each other (Baltic ~25E 59N)
    e1 = await _seed_vessel("V1", name="GHOST A", lat=59.000, lng=25.000)
    e2 = await _seed_vessel("V2", name="GHOST B", lat=59.020, lng=25.000)  # ~2.2km N
    e3 = await _seed_vessel("V3", name="GHOST C", lat=59.000, lng=25.040)  # ~2.3km E
    for (eid, name, lat, lng) in [(e1, "GHOST A", 59.000, 25.000),
                                    (e2, "GHOST B", 59.020, 25.000),
                                    (e3, "GHOST C", 59.000, 25.040)]:
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}M{name[-1]}",
                              name=name, authority="US Treasury OFAC",
                              lat=lat, lng=lng)

    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG,
        entity_canonical_id_like=f"{_PFX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT severity, event_subtype, properties FROM event "
        "WHERE event_type='shadow_fleet_cluster' "
        "AND properties->>'algorithm'=$1",
        _TAG,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["severity"] == 10.0
    assert r["event_subtype"] == "cluster"
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["cluster_size"] == 3


async def test_two_vessels_below_threshold(_clean):
    """Default min_cluster_size=3; 2 vessels → 0 events."""
    e1 = await _seed_vessel("S1", lat=59.0, lng=25.0)
    e2 = await _seed_vessel("S2", lat=59.01, lng=25.0)
    await _seed_underway(entity_id=e1, mmsi=f"{_PFX}MA", name="A",
                          authority="US Treasury OFAC", lat=59.0, lng=25.0)
    await _seed_underway(entity_id=e2, mmsi=f"{_PFX}MB", name="B",
                          authority="US Treasury OFAC", lat=59.01, lng=25.0)
    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG,
        entity_canonical_id_like=f"{_PFX}%",
    )
    assert n == 0


async def test_vessels_outside_radius_dont_cluster(_clean):
    """3 vessels separated by 50+km → no cluster (radius 10km)."""
    e1 = await _seed_vessel("F1", lat=59.0, lng=25.0)
    e2 = await _seed_vessel("F2", lat=60.0, lng=25.0)   # ~111km away
    e3 = await _seed_vessel("F3", lat=58.0, lng=25.0)   # ~111km away
    for eid, name, lat in [(e1, "A", 59.0), (e2, "B", 60.0), (e3, "C", 58.0)]:
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}M{name}",
                              name=name, authority="US Treasury OFAC",
                              lat=lat, lng=25.0)
    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG,
        entity_canonical_id_like=f"{_PFX}%",
    )
    assert n == 0


async def test_multi_jurisdictional_cluster_tagged(_clean):
    """Cluster of 3 vessels from 3 different authorities → properties
    record all three + flag multi_jurisdictional."""
    e1 = await _seed_vessel("M1", lat=59.0, lng=25.0)
    e2 = await _seed_vessel("M2", lat=59.01, lng=25.0)
    e3 = await _seed_vessel("M3", lat=59.0, lng=25.02)
    await _seed_underway(entity_id=e1, mmsi=f"{_PFX}M1", name="USA",
                          authority="US Treasury OFAC", lat=59.0, lng=25.0)
    await _seed_underway(entity_id=e2, mmsi=f"{_PFX}M2", name="UK",
                          authority="UK OFSI", lat=59.01, lng=25.0)
    await _seed_underway(entity_id=e3, mmsi=f"{_PFX}M3", name="EU",
                          authority="EU CFSP", lat=59.0, lng=25.02)

    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG,
        entity_canonical_id_like=f"{_PFX}%",
    )
    assert n == 1
    rows = await fetch(
        "SELECT properties FROM event WHERE event_type='shadow_fleet_cluster' "
        "AND properties->>'algorithm'=$1",
        _TAG,
    )
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["authority_count"] == 3
    assert props["multi_jurisdictional"] is True
    auths = props["authorities"]
    if isinstance(auths, str):
        auths = json.loads(auths)
    assert sorted(auths) == ["EU CFSP", "UK OFSI", "US Treasury OFAC"]


async def test_idempotent_re_emit(_clean):
    """Same member set re-emits as 0."""
    e1 = await _seed_vessel("I1", lat=59.0, lng=25.0)
    e2 = await _seed_vessel("I2", lat=59.01, lng=25.0)
    e3 = await _seed_vessel("I3", lat=59.0, lng=25.02)
    for (eid, lat, lng) in [(e1, 59.0, 25.0), (e2, 59.01, 25.0), (e3, 59.0, 25.02)]:
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}M{eid[:3]}",
                              name="X", authority="US Treasury OFAC",
                              lat=lat, lng=lng)
    n1 = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG, entity_canonical_id_like=f"{_PFX}%",
    )
    n2 = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG, entity_canonical_id_like=f"{_PFX}%",
    )
    assert n1 == 1
    assert n2 == 0


async def test_dense_cluster_emits_one_event_not_per_pivot(_clean):
    """REGRESSION: 8 sanctioned vessels packed into a single 5km area
    must produce exactly 1 cluster event, NOT 8 (one per pivot anchor).
    This was the bug the operator flagged on the daily digest: the same
    physical gathering of vessels was being reported as 8-11 separate
    'Shadow-fleet cluster' findings, one per anchor whose neighborhood
    happened to vary slightly in membership.
    """
    eids = []
    # Pack 8 vessels into a tight ~3km grid
    for i in range(8):
        lat = 59.000 + (i // 4) * 0.015  # 2 rows ~1.7km apart
        lng = 25.000 + (i %  4) * 0.020  # 4 cols ~1.1km apart at this latitude
        eid = await _seed_vessel(f"P{i}", name=f"PIVOT{i}", lat=lat, lng=lng)
        eids.append(eid)
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}P{i}",
                              name=f"PIVOT{i}", authority="US Treasury OFAC",
                              lat=lat, lng=lng)

    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG,
        entity_canonical_id_like=f"{_PFX}%",
    )
    # The bug: any number > 1 means we're emitting per-pivot again.
    # The fix: ST_ClusterDBSCAN groups all 8 into a single cluster_id.
    assert n == 1, f"expected exactly 1 event for 1 physical cluster, got {n}"

    rows = await fetch(
        "SELECT properties FROM event WHERE event_type='shadow_fleet_cluster' "
        "AND properties->>'algorithm'=$1",
        _TAG,
    )
    assert len(rows) == 1
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    # All 8 vessels should be reflected in the cluster_size + member list.
    assert props["cluster_size"] == 8


async def test_two_separate_clusters_emit_two_events(_clean):
    """When two distinct physical clusters exist (>10km apart from each
    other but each tight internally), each should emit its own event."""
    # Cluster A: 3 vessels near 59.0, 25.0
    for i in range(3):
        eid = await _seed_vessel(f"A{i}", name=f"CLA{i}",
                                  lat=59.0 + i*0.005, lng=25.0)
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}A{i}",
                              name=f"CLA{i}", authority="US Treasury OFAC",
                              lat=59.0 + i*0.005, lng=25.0)
    # Cluster B: 3 vessels near 50.0, 5.0 (~1000+ km away)
    for i in range(3):
        eid = await _seed_vessel(f"B{i}", name=f"CLB{i}",
                                  lat=50.0 + i*0.005, lng=5.0)
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}B{i}",
                              name=f"CLB{i}", authority="EU CFSP",
                              lat=50.0 + i*0.005, lng=5.0)

    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG,
        entity_canonical_id_like=f"{_PFX}%",
    )
    assert n == 2


async def test_dbscan_chain_does_not_produce_global_cluster(_clean):
    """REGRESSION (2026-05-19 P0-C audit): DBSCAN density-reachability MUST
    NOT produce a "cluster" whose actual geographic diameter exceeds 3× eps.

    Pre-fix: 5 vessels in a chain (each ~8.9 km Mercator from the next,
    total chain length ~35.6 km) all shared the same DBSCAN cluster_id
    because each was within eps=10 km of the next via transitive density-
    reachability. The algorithm emitted a single 5-vessel cluster titled
    "within 10.0 km" even though endpoints were 35.6 km apart. Verified in
    production: 2,225 of 2,748 historical findings (81%) were this FP class,
    some spanning 15,000+ km (e.g. event 04c76094 = 130 vessels across
    240° of longitude).

    Post-fix: the bounding-diameter cap (ST_MaxDistance ≤ 3× eps = 30 km
    Mercator, applied in the `clusters` CTE) rejects the chain. n == 0.

    Audit doc: 21_GLASSBOX_AI/docs/ALGORITHM_FP_AUDIT_shadow_fleet_cluster_2026_05_19.md
    """
    # 5 vessels in an east-west chain. 0.08° longitude = ~8,907 m on the
    # Mercator x-axis at ANY latitude (Mercator x is linear in longitude:
    # x = R × λ). So adjacent pairs are within DBSCAN eps=10 km, but the
    # chain's endpoint-to-endpoint diameter is 4 × 8.9 ≈ 35.6 km, above
    # the 3× eps = 30 km cap. Pre-fix → 1 cluster. Post-fix → 0.
    longitudes = [25.000, 25.080, 25.160, 25.240, 25.320]
    for i, lng in enumerate(longitudes):
        eid = await _seed_vessel(f"CHAIN{i}", name=f"CHAIN{i}",
                                 lat=59.0, lng=lng)
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}CHAIN{i}",
                             name=f"CHAIN{i}", authority="US Treasury OFAC",
                             lat=59.0, lng=lng)

    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG,
        entity_canonical_id_like=f"{_PFX}%",
    )
    assert n == 0, (
        f"DBSCAN chain artifact: 5 vessels spanning ~35.6 km Mercator "
        f"must NOT form a cluster claiming 'within 10 km'. got n={n}"
    )


async def test_cluster_emits_diameter_m_in_properties(_clean):
    """A compact cluster (well under the 30 km cap) must still emit, AND
    the new `diameter_m` property added in the 2026-05-19 fix should be
    present with a sensible value. Forward-compatible with downstream
    consumers that may want to surface cluster geometry."""
    e1 = await _seed_vessel("D1", name="D1", lat=59.0,  lng=25.0)
    e2 = await _seed_vessel("D2", name="D2", lat=59.02, lng=25.0)
    e3 = await _seed_vessel("D3", name="D3", lat=59.0,  lng=25.04)
    for (eid, lat, lng, nm) in [(e1, 59.0,  25.0,  "D1"),
                                  (e2, 59.02, 25.0,  "D2"),
                                  (e3, 59.0,  25.04, "D3")]:
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}{nm}", name=nm,
                              authority="US Treasury OFAC", lat=lat, lng=lng)
    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG,
        entity_canonical_id_like=f"{_PFX}%",
    )
    assert n == 1
    rows = await fetch(
        "SELECT properties FROM event WHERE event_type='shadow_fleet_cluster' "
        "AND properties->>'algorithm'=$1",
        _TAG,
    )
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert "diameter_m" in props, "diameter_m must be present for debuggability"
    diameter = float(props["diameter_m"])
    # Compact triangle: max pairwise Mercator distance well under the cap.
    assert 0 < diameter < 30_000, f"diameter_m {diameter} outside sensible range"


async def test_large_fleet_subtype(_clean):
    """6+ vessels in cluster → event_subtype='large_fleet'."""
    eids = []
    for i in range(6):
        eid = await _seed_vessel(f"L{i}", name=f"LV{i}",
                                  lat=59.0 + i*0.005, lng=25.0)
        eids.append(eid)
        await _seed_underway(entity_id=eid, mmsi=f"{_PFX}L{i}",
                              name=f"LV{i}", authority="US Treasury OFAC",
                              lat=59.0 + i*0.005, lng=25.0)
    n = await run_shadow_fleet_cluster_scan(
        algorithm_tag=_TAG, entity_canonical_id_like=f"{_PFX}%",
    )
    assert n == 1
    rows = await fetch(
        "SELECT event_subtype, properties FROM event "
        "WHERE event_type='shadow_fleet_cluster' "
        "AND properties->>'algorithm'=$1",
        _TAG,
    )
    assert rows[0]["event_subtype"] == "large_fleet"
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["cluster_size"] == 6
