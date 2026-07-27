"""
Phase 1.4 — algorithms/proximity.py test.

The algorithm scans entities (aircraft for Phase 1; any with position in DB)
+ events. For each entity-event pair within radius + time window, it writes
a `detected_proximity` row to the event table, recording both ids.

Asserts:
  - Pair within radius+window → 1 finding inserted
  - Pair outside radius → 0 findings
  - Pair outside time window → 0 findings
  - Re-running with same data → no duplicate findings (idempotent within window)
  - Multiple distinct pairs → each finding has correct properties.entity_ids/event_ids
  - Entity with no position → not flagged (no position_track row)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_proximity.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute, acquire  # noqa: E402
from algorithms.proximity import (  # noqa: E402
    run_proximity_scan,
    run_cross_entity_proximity_scan,
)


TEST_PREFIX = "test04"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_world():
    async def _cleanup():
        # Delete any proximity findings the test may have produced (both
        # entity↔event and entity↔entity tag families)
        await execute(
            "DELETE FROM event WHERE event_type = 'detected_proximity' "
            "AND properties->>'algorithm' IN ('proximity_test', 'proximity_cross_test')",
        )
        await execute(
            "DELETE FROM event WHERE event_subtype LIKE $1",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM position_track WHERE entity_id IN ("
            "  SELECT id FROM entity WHERE canonical_id LIKE $1)",
            f"{TEST_PREFIX}%",
        )
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"{TEST_PREFIX}%",
        )

    await _cleanup()
    yield
    await _cleanup()


async def _seed_aircraft(label: str, lat: float, lng: float, age_min: int = 0):
    """Insert one aircraft entity + one recent position_track row.

    Also populates the Phase 2.5 denormalized current_geom +
    current_position_time on entity, mirroring what writers.py does in prod.
    """
    now = datetime.now(timezone.utc)
    t = now - timedelta(minutes=age_min)
    async with acquire() as conn:
        async with conn.transaction():
            eid = await conn.fetchval(
                """
                INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                                    display_name, properties, last_seen,
                                    current_geom, current_position_time)
                VALUES ('aircraft', 'icao24', $1, $2, '{}'::jsonb, $3,
                        ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography, $3)
                RETURNING id
                """,
                f"{TEST_PREFIX}_{label}",
                f"AC_{label.upper()}",
                t,
                lng,
                lat,
            )
            await conn.execute(
                """
                INSERT INTO position_track (time, entity_id, geom, altitude_m,
                                            velocity_ms, heading_deg)
                VALUES ($1, $2,
                        ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                        10000, 250, 90)
                """,
                t,
                eid,
                lng,
                lat,
            )
    return eid


async def _seed_vessel(label: str, lat: float, lng: float, age_min: int = 0):
    """Insert one vessel entity + one position_track row.

    Also populates Phase 2.5 denormalized current_geom + current_position_time.
    """
    now = datetime.now(timezone.utc)
    t = now - timedelta(minutes=age_min)
    async with acquire() as conn:
        async with conn.transaction():
            eid = await conn.fetchval(
                """
                INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                                    display_name, properties, last_seen,
                                    current_geom, current_position_time)
                VALUES ('vessel', 'mmsi', $1, $2, '{}'::jsonb, $3,
                        ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography, $3)
                RETURNING id
                """,
                f"{TEST_PREFIX}_{label}",
                f"VS_{label.upper()}",
                t,
                lng,
                lat,
            )
            await conn.execute(
                """
                INSERT INTO position_track (time, entity_id, geom, altitude_m,
                                            velocity_ms, heading_deg)
                VALUES ($1, $2,
                        ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                        NULL, 6, 90)
                """,
                t,
                eid,
                lng,
                lat,
            )
    return eid


async def _seed_event(label: str, lat: float, lng: float, age_min: int = 0,
                      event_type: str = "usgs_quake",
                      decay_half_life_min: int = 60):
    """Insert one event with geom + decay_half_life_min.

    Phase 4 algorithm enhancement: the proximity scan now uses each event's
    decay_half_life_min as its freshness window. Tests that exercise that
    behavior pass an explicit value (e.g. 720 for EONET-style slow events,
    60 for fast quakes/weather)."""
    now = datetime.now(timezone.utc)
    t = now - timedelta(minutes=age_min)
    eid = uuid4()
    await execute(
        """
        INSERT INTO event (id, event_type, event_subtype, event_time, geom, severity,
                          title, description, decay_half_life_min)
        VALUES ($1, $2, $3, $4,
                ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography,
                5.0, $7, 'test event', $8)
        """,
        eid,
        event_type,
        f"{TEST_PREFIX}_{label}",
        t,
        lng,
        lat,
        f"Event {label}",
        decay_half_life_min,
    )
    return eid


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_proximity_finds_pair_within_radius_and_window(_clean_test_world):
    """Aircraft at (40.6,-73.8) + event ~2km away → 1 finding."""
    aircraft_id = await _seed_aircraft("a", 40.6, -73.8)
    event_id = await _seed_event("ev1", 40.62, -73.82)  # ~2.5km

    findings = await run_proximity_scan(radius_m=50_000, window_min=60,
                                        algorithm_tag="proximity_test",
                                        entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert findings == 1

    rows = await fetch(
        "SELECT properties FROM event WHERE event_type = 'detected_proximity' "
        "AND properties->>'algorithm' = 'proximity_test'",
    )
    assert len(rows) == 1
    import json
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert str(aircraft_id) in props["entity_ids"]
    assert str(event_id) in props["event_ids"]
    assert props["radius_m"] == 50_000
    assert props["distance_m"] is not None
    assert props["distance_m"] < 5000


async def test_proximity_excludes_pair_outside_radius(_clean_test_world):
    """Same aircraft + event 100km away → 0 findings with radius=50km."""
    await _seed_aircraft("a", 40.6, -73.8)
    await _seed_event("ev_far", 41.5, -73.8)  # ~100km north

    findings = await run_proximity_scan(radius_m=50_000, window_min=60,
                                        algorithm_tag="proximity_test",
                                        entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert findings == 0


async def test_proximity_excludes_event_outside_window(_clean_test_world):
    """Aircraft + close event but event is 10 hours old → 0 findings with 60min window."""
    await _seed_aircraft("a", 40.6, -73.8)
    await _seed_event("ev_old", 40.62, -73.82, age_min=600)  # 10h old

    findings = await run_proximity_scan(radius_m=50_000, window_min=60,
                                        algorithm_tag="proximity_test",
                                        entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert findings == 0


async def test_proximity_excludes_aircraft_with_no_recent_position(_clean_test_world):
    """Aircraft has only an old (10h ago) position → not flagged with 60min window."""
    await _seed_aircraft("a", 40.6, -73.8, age_min=600)
    await _seed_event("ev1", 40.62, -73.82)

    findings = await run_proximity_scan(radius_m=50_000, window_min=60,
                                        algorithm_tag="proximity_test",
                                        entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert findings == 0


async def test_proximity_idempotent_within_window(_clean_test_world):
    """Re-run with same data → no new findings."""
    await _seed_aircraft("a", 40.6, -73.8)
    await _seed_event("ev1", 40.62, -73.82)

    n1 = await run_proximity_scan(radius_m=50_000, window_min=60,
                                  algorithm_tag="proximity_test",
                                  entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert n1 == 1

    n2 = await run_proximity_scan(radius_m=50_000, window_min=60,
                                  algorithm_tag="proximity_test",
                                  entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert n2 == 0, "second run should not produce duplicate findings"

    total = await fetchval(
        "SELECT count(*) FROM event WHERE event_type = 'detected_proximity' "
        "AND properties->>'algorithm' = 'proximity_test'",
    )
    assert total == 1


async def test_proximity_multiple_pairs(_clean_test_world):
    """Two aircraft + two events, all in radius. Expect 4 findings (Cartesian)."""
    await _seed_aircraft("ac1", 40.6, -73.8)
    await _seed_aircraft("ac2", 40.65, -73.85)  # close to ac1
    await _seed_event("evA", 40.62, -73.82)
    await _seed_event("evB", 40.61, -73.81)

    n = await run_proximity_scan(radius_m=50_000, window_min=60,
                                 algorithm_tag="proximity_test",
                                 entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert n == 4


async def test_proximity_skips_findings_themselves(_clean_test_world):
    """Detected_proximity events should not be matched as targets in subsequent scans.

    Property under test: NOT EXISTS dedup correctly suppresses re-emission of
    the SAME (entity_id, event_id) pair. When the radius widens on a 2nd scan,
    the test aircraft may legitimately match additional production events that
    exist in the wider radius — that's correct, not a bug. We count duplicate
    findings PER PAIR instead of total findings to isolate the dedup property.
    """
    await _seed_aircraft("a", 40.6, -73.8)
    await _seed_event("ev1", 40.62, -73.82)

    n1 = await run_proximity_scan(radius_m=50_000, window_min=60,
                                  algorithm_tag="proximity_test",
                                  entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert n1 == 1

    # Re-run with a wider radius (500km). The test pair is already flagged so
    # NOT EXISTS suppresses it; the wider radius may pick up additional REAL
    # production events, but it should never produce a SECOND finding for the
    # same (entity, event) pair.
    n2 = await run_proximity_scan(radius_m=500_000, window_min=60,
                                  algorithm_tag="proximity_test",
                                  entity_canonical_id_like=f"{TEST_PREFIX}%")

    # Property assertion: no (entity_id, event_id) pair appears twice.
    rows = await fetch(
        "SELECT properties->'entity_ids'->>0 AS eid, "
        "       properties->'event_ids'->>0  AS evid "
        "FROM event WHERE event_type = 'detected_proximity' "
        "AND properties->>'algorithm' = 'proximity_test'",
    )
    pairs = [(r["eid"], r["evid"]) for r in rows]
    assert len(pairs) == len(set(pairs)), \
        f"Duplicate (entity, event) pair detected: {pairs}"
    # Sanity: at minimum the original test pair is among findings.
    assert len(pairs) >= 1


# ─── Per-event-type decay window tests (Phase 4 algorithm enhancement) ───


async def test_proximity_catches_slow_event_within_its_decay_window(_clean_test_world):
    """EONET-style event (decay=720min/12h) at 5h old (300min) → CAUGHT
    even though the global window_min default is 60min. Without the
    per-event-decay enhancement, this event would have been filtered out."""
    await _seed_aircraft("ac_slow", 40.6, -73.8)
    await _seed_event("slow_event", 40.62, -73.82, age_min=300,
                      event_type="nasa_eonet",
                      decay_half_life_min=720)

    findings = await run_proximity_scan(radius_m=50_000, window_min=60,
                                        algorithm_tag="proximity_test",
                                        entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert findings == 1, (
        "5h-old EONET event with decay=720 should be caught regardless of the "
        "60min global window_min — that's the Phase 4 per-event-type window fix"
    )

    # Confirm it was the EONET event, not stale quake leftovers
    rows = await fetch(
        "SELECT event_subtype FROM event "
        "WHERE event_type='detected_proximity' "
        "AND properties->>'algorithm' = 'proximity_test'"
    )
    assert any("nasa_eonet" in (r["event_subtype"] or "") for r in rows)


async def test_proximity_excludes_fast_event_past_its_decay_window(_clean_test_world):
    """USGS-style event (decay=60min) at 5h old → EXCLUDED. Past its own
    decay window even though global window_min is wide enough.

    Tests the ASYMMETRIC nature of the fix: the per-event decay can both
    INCLUDE slow events AND EXCLUDE fast events that are past their freshness."""
    await _seed_aircraft("ac_fast", 40.6, -73.8)
    await _seed_event("fast_event", 40.62, -73.82, age_min=300,
                      event_type="usgs_quake",
                      decay_half_life_min=60)

    # Use a generous window_min (480min/8h) — the OLD algorithm would have
    # caught this event. With the per-event-decay enhancement it's filtered
    # because the EVENT itself is past its 60min decay.
    findings = await run_proximity_scan(radius_m=50_000, window_min=480,
                                        algorithm_tag="proximity_test",
                                        entity_canonical_id_like=f"{TEST_PREFIX}%")
    assert findings == 0, (
        "5h-old USGS quake with decay=60 should be excluded — past its own "
        "freshness window regardless of how generous the global window_min is"
    )


# ─── Cross-entity (entity↔entity) proximity tests ────────────────────────


async def test_cross_entity_proximity_finds_aircraft_vessel_pair(_clean_test_world):
    """Aircraft + vessel within radius/window → 1 cross-entity finding."""
    await _seed_aircraft("ac1", 40.6, -73.8)
    await _seed_vessel("vs1", 40.62, -73.82)  # ~2.5km

    n = await run_cross_entity_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_cross_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1

    rows = await fetch(
        "SELECT event_subtype, properties FROM event "
        "WHERE event_type='detected_proximity' "
        "AND properties->>'algorithm' = 'proximity_cross_test'"
    )
    assert len(rows) == 1
    # event_subtype is lex-ordered: 'aircraft' < 'vessel' so subtype = 'aircraft_vessel'
    assert rows[0]["event_subtype"] == "aircraft_vessel"
    import json
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["pair_kind"] == "entity_to_entity"
    assert sorted(props["entity_types"]) == ["aircraft", "vessel"]
    assert len(props["entity_ids"]) == 2


async def test_cross_entity_does_not_pair_same_type(_clean_test_world):
    """Two aircraft close together → 0 cross-entity findings (same type filter
    via entity_type < entity_type means aircraft-aircraft pairs don't match)."""
    await _seed_aircraft("ac1", 40.6, -73.8)
    await _seed_aircraft("ac2", 40.61, -73.81)  # close

    n = await run_cross_entity_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_cross_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_cross_entity_idempotent_within_window(_clean_test_world):
    """Re-running with same data → no duplicate cross-entity findings."""
    await _seed_aircraft("ac1", 40.6, -73.8)
    await _seed_vessel("vs1", 40.62, -73.82)

    n1 = await run_cross_entity_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_cross_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1

    n2 = await run_cross_entity_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_cross_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 0


async def test_cross_entity_pair_outside_radius_excluded(_clean_test_world):
    await _seed_aircraft("ac1", 40.6, -73.8)
    await _seed_vessel("vs_far", 41.6, -73.8)  # ~111km north

    n = await run_cross_entity_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_cross_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


# ─── Satellite exclusion (2026-05-09 fix) ────────────────────────────────


async def _seed_satellite(label: str, lat: float, lng: float, age_min: int = 0):
    """Seed a satellite entity. Satellites store their ground track in
    current_geom but operate at orbital altitude — they should NOT be
    flagged as 'proximate' to anything on the surface."""
    now = datetime.now(timezone.utc)
    t = now - timedelta(minutes=age_min)
    async with acquire() as conn:
        async with conn.transaction():
            eid = await conn.fetchval(
                """
                INSERT INTO entity (entity_type, canonical_id_type, canonical_id,
                                    display_name, properties, last_seen,
                                    current_geom, current_position_time)
                VALUES ('satellite', 'norad_id', $1, $2, '{}'::jsonb, $3,
                        ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography, $3)
                RETURNING id
                """,
                f"{TEST_PREFIX}_{label}",
                f"SAT_{label.upper()}",
                t,
                lng,
                lat,
            )
            await conn.execute(
                """
                INSERT INTO position_track (time, entity_id, geom, altitude_m,
                                            velocity_ms, heading_deg)
                VALUES ($1, $2,
                        ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                        500000, 7800, 90)
                """,
                t, eid, lng, lat,
            )
    return eid


async def test_satellite_excluded_from_entity_event_proximity(_clean_test_world):
    """Satellite ground-track over an event must NOT trigger a proximity
    finding — they're separated by ~500 km of altitude and the 2D ground-
    distance check is meaningless across that vertical regime."""
    await _seed_satellite("sat1", 40.6, -73.8)
    await _seed_event("ev_quake", 40.62, -73.82)  # ~2.5 km ground distance

    findings = await run_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert findings == 0, (
        "satellite should not produce entity↔event proximity findings; "
        "they operate in a different vertical regime than surface events"
    )


async def test_satellite_excluded_from_cross_entity_proximity_either_side(_clean_test_world):
    """Aircraft below a satellite ground-track must NOT generate a
    cross-entity proximity finding. Tests both orderings since the
    SQL uses entity_type lex-ordering."""
    await _seed_satellite("sat2", 40.6, -73.8)
    await _seed_aircraft("ac_below", 40.61, -73.81)
    await _seed_vessel("vs_below", 40.62, -73.82)

    n = await run_cross_entity_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_cross_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    # Aircraft↔vessel pair should still fire (both surface) → 1 finding.
    # Satellite↔aircraft and satellite↔vessel must NOT fire.
    assert n == 1

    rows = await fetch(
        "SELECT properties->>'entity_types' AS types "
        "FROM event WHERE event_type = 'detected_proximity' "
        "AND properties->>'algorithm' = 'proximity_cross_test' "
        "ORDER BY event_time DESC",
    )
    assert len(rows) == 1
    types_str = rows[0]["types"] or ""
    assert "satellite" not in types_str, (
        f"satellite must not appear in any cross-entity proximity finding; "
        f"got entity_types={types_str}"
    )


async def test_aircraft_vessel_proximity_still_works_with_satellite_present(_clean_test_world):
    """Confirm the satellite exclusion didn't accidentally block legitimate
    surface-to-surface proximity — aircraft↔vessel should still fire."""
    await _seed_satellite("sat3", 40.6, -73.8)        # noise — should be ignored
    await _seed_aircraft("ac_sw", 40.6, -73.8)
    await _seed_vessel("vs_sw", 40.605, -73.805)

    n = await run_cross_entity_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_cross_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1


async def test_proximity_excludes_algorithm_derived_event_types(_clean_test_world):
    """P0-C audit #7 (2026-05-19): proximity must NOT match against
    algorithm-derived event types (rendezvous_detected, dark_vessel_detected,
    loitering_detected, port_*, sanctioned_*, military_aircraft_underway,
    shadow_fleet_cluster_detected, sanctions_match,
    sanctions_multijurisdictional_match) — only against raw external events
    (earthquakes, news, fires, weather, etc.).

    Pre-fix behavior: a 30-sample audit over the last 7d (May 12-19) found
    16/30 = 53% FP rate. A single rendezvous_detected in a busy lane was
    producing up to 27,034 'vessel near it' proximity findings within 50km.
    Total scope: 12,072,548 algo-derived-match findings over the corpus.

    This test seeds (1) a raw external event (usgs_quake) and (2) a
    representative algorithm-derived event (rendezvous_detected, with the
    same default 1440-min decay), both within radius of the same aircraft.
    Expected: 1 finding (the quake), NOT 2.
    """
    await _seed_aircraft("acdb", 40.6, -73.8)

    # Raw external event — should match.
    await _seed_event("quake", 40.61, -73.81, event_type="usgs_quake")

    # Algorithm-derived events — must NOT match. Cover every type the
    # 2026-05-19 fix denies, with each event at the same location so we
    # can prove they were geometry-eligible but type-excluded.
    DENIED = [
        "rendezvous_detected",
        "dark_vessel_detected",
        "loitering_detected",
        "port_call",
        "port_arrival",
        "port_departure",
        "sanctioned_vessel_went_dark",
        "sanctioned_vessel_rendezvous",
        "sanctioned_vessel_underway",
        "sanctioned_port_arrival",
        "aircraft_in_sanctioned_airspace",
        "military_aircraft_underway",
        "shadow_fleet_cluster_detected",
        "sanctions_match",
        "sanctions_multijurisdictional_match",
    ]
    for i, etype in enumerate(DENIED):
        # Use a 1440-min decay (matches real algorithm-derived findings)
        # and stagger lat by 0.001° so each event has a unique geom.
        await _seed_event(
            f"denied_{i}",
            40.6 + 0.001 * i, -73.8,
            event_type=etype,
            decay_half_life_min=1440,
        )

    n = await run_proximity_scan(
        radius_m=50_000, window_min=60,
        algorithm_tag="proximity_test",
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )

    # Expect EXACTLY 1 finding: the usgs_quake. If the deny-list is dropped
    # or any algo-derived type leaks through, this assertion fails.
    assert n == 1, (
        f"Expected exactly 1 proximity finding (usgs_quake only); got {n}. "
        f"This regression catches the P0-C 2026-05-19 fix being dropped — "
        f"the fanout FPs (16M+ rows in production) would return."
    )

    rows = await fetch(
        "SELECT event_subtype FROM event "
        "WHERE event_type = 'detected_proximity' "
        "AND properties->>'algorithm' = 'proximity_test'",
    )
    assert len(rows) == 1
    # The subtype is '{entity_type}_{src_event_type}' — confirm it's the quake.
    assert rows[0]["event_subtype"].endswith("usgs_quake"), (
        f"Wrong source event matched: {rows[0]['event_subtype']!r}; "
        f"expected something ending in 'usgs_quake'"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
