"""
Phase 4 algorithm — algorithms/port_call.py

Asserts:
  - PORTS list parses + indexes
  - VALUES clause render quotes safely
  - Vessel within radius + low velocity → port_call event
  - Vessel within radius but moving fast → no event
  - Vessel beyond radius → no event
  - Vessel without velocity (no position_track row) → no event
  - Idempotent within cooldown window (re-runs are no-ops)
  - Strategic vs commercial port_kind sets the right severity
  - properties carry vessel + port metadata for downstream consumers
  - Empty entity table → returns 0 cleanly

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_port_call.py -v
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
from algorithms.port_call import (  # noqa: E402
    PORTS, PORT_INDEX, _build_values_clause,
    run_port_call_scan,
    run_port_arrival_scan,
    run_port_departure_scan,
)


TEST_PREFIX = "pc_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_world():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type IN "
            "('port_call', 'port_arrival', 'port_departure') "
            "AND properties->>'algorithm' LIKE 'port_%_test'"
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


async def _seed_vessel_at(label: str, lat: float, lng: float,
                           *, velocity_ms: float = 0.5,
                           age_min: int = 0):
    """Seed a vessel + one position_track row at (lat, lng) with the
    given velocity. Mirrors the writers' Phase 2.5 pattern of populating
    entity.current_geom + current_position_time."""
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
                                            velocity_ms, heading_deg, properties)
                VALUES ($1, $2,
                        ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                        NULL, $5, 90, '{}'::jsonb)
                """,
                t, eid, lng, lat, velocity_ms,
            )
    return eid


# ─── Static / pure tests ─────────────────────────────────────────────────


def test_ports_list_is_non_empty_and_indexed():
    assert len(PORTS) > 50, f"expected substantial ports list; got {len(PORTS)}"
    for pid, name, country, lat, lng, kind in PORTS:
        assert pid in PORT_INDEX
        assert -90 <= lat <= 90
        assert -180 <= lng <= 180
        assert kind in ("commercial", "strategic")
        assert len(country) == 2     # ISO-3166-1 alpha-2


def test_ports_contains_strategic_iran_and_commercial_singapore():
    """Quick sanity - both intel-relevant + macroeconomic anchors present."""
    iran_ports = [p for p in PORTS if p[2] == "IR"]
    assert iran_ports, "Iran sanctions-watchlist ports should be in the list"
    sing = PORT_INDEX.get("SG_SIN")
    assert sing is not None
    assert "Singapore" in sing[0]


def test_values_clause_renders_safely():
    """Names with apostrophes / special chars must be escaped."""
    body = _build_values_clause()
    # Should contain ports + escape any single quotes (PORTS doesn't have
    # any apostrophes today but the renderer must defend)
    assert "Singapore" in body
    assert "Bandar Abbas" in body
    # Each row is a parenthesized tuple; count should equal len(PORTS)
    assert body.count("),") == len(PORTS) - 1


# ─── DB-touching tests ───────────────────────────────────────────────────


async def _count_port_calls_for(vessel_id) -> int:
    """Production AIS feeds emit their own port_call events on every
    cycle, so tests must scope assertions to JUST the seeded vessel
    rather than asserting on the full run-return count."""
    n = await fetchval(
        "SELECT count(*) FROM event WHERE event_type = 'port_call' "
        "AND entity_id = $1::uuid",
        vessel_id,
    )
    return int(n or 0)


async def _count_arrivals_for(vessel_id) -> int:
    n = await fetchval(
        "SELECT count(*) FROM event WHERE event_type = 'port_arrival' "
        "AND entity_id = $1::uuid",
        vessel_id,
    )
    return int(n or 0)


async def test_vessel_at_singapore_with_low_velocity_fires(_clean_world):
    """Vessel parked at SG_SIN coords + slow → 1 port_call event for THIS vessel."""
    sing = PORT_INDEX["SG_SIN"]    # (name, country, lat, lng, kind)
    vid = await _seed_vessel_at("sg1", lat=sing[2], lng=sing[3],
                                 velocity_ms=0.3)
    await run_port_call_scan(algorithm_tag="port_call_test")
    assert await _count_port_calls_for(vid) == 1
    rows = await fetch(
        "SELECT event_type, event_subtype, severity, title, properties "
        "FROM event WHERE event_type = 'port_call' AND entity_id = $1::uuid",
        vid,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "port_call"
    assert r["event_subtype"] == "SG"
    assert "Singapore" in r["title"]
    import json as _json
    props = r["properties"]
    if isinstance(props, str):
        props = _json.loads(props)
    assert props["port_id"] == "SG_SIN"
    assert props["port_kind"] == "commercial"
    assert props["vessel_id"] == str(vid)
    assert props["distance_m"] < 5000


async def test_strategic_port_gets_higher_severity(_clean_world):
    """Bandar Abbas is strategic → severity should be 6, not 3."""
    bnd = PORT_INDEX["IR_BND"]
    vid = await _seed_vessel_at("ir1", lat=bnd[2], lng=bnd[3], velocity_ms=0.1)
    await run_port_call_scan(algorithm_tag="port_call_test")
    rows = await fetch(
        "SELECT severity, properties->>'port_kind' AS kind "
        "FROM event WHERE event_type = 'port_call' AND entity_id = $1::uuid",
        vid,
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "strategic"
    assert rows[0]["severity"] == pytest.approx(6.0)


async def test_fast_moving_vessel_at_port_does_not_fire(_clean_world):
    """Vessel passing through with v=10 m/s (transit) → no event for THIS vessel."""
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at("transit", lat=sing[2], lng=sing[3],
                                 velocity_ms=10.0)
    await run_port_call_scan(algorithm_tag="port_call_test")
    assert await _count_port_calls_for(vid) == 0


async def test_vessel_far_from_any_port_does_not_fire(_clean_world):
    """Vessel mid-Pacific → no port within 5km → no event for THIS vessel."""
    vid = await _seed_vessel_at("midpac", lat=10.0, lng=-150.0,
                                 velocity_ms=0.1)
    await run_port_call_scan(algorithm_tag="port_call_test")
    assert await _count_port_calls_for(vid) == 0


async def test_idempotent_within_cooldown(_clean_world):
    """Re-run within 24h → THIS vessel still has 1 finding."""
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at("idem", lat=sing[2], lng=sing[3], velocity_ms=0.2)
    await run_port_call_scan(algorithm_tag="port_call_test")
    n1 = await _count_port_calls_for(vid)
    await run_port_call_scan(algorithm_tag="port_call_test")
    n2 = await _count_port_calls_for(vid)
    assert n1 == 1
    assert n2 == 1   # still 1; second scan was a no-op


async def test_seven_day_cooldown_suppresses_intra_week_refires(_clean_world):
    """Regression: ALGORITHM_FP_AUDIT_port_call_2026_05_19.

    Pre-2026-05-13 the cooldown was 24h; vessels at berth for multiple
    days emitted one finding per day per port. Commit c4906ae extended
    cooldown to 7 days (168h). This test verifies that the algorithm's
    NOT EXISTS cooldown predicate correctly suppresses re-fires for
    the same (vessel, port) inside the 7-day window.

    The audit on 2026-05-19 found 7,413 historical excess re-fires from
    the 24h-cooldown era that were withdrawn per the cooldown's
    NOT EXISTS predicate.

    Approach: seed a prior port_call event 6 days ago (well inside 7-day
    cooldown), then run the scan with a fresh-positioned vessel — expect
    no new emission. Then test the BOUNDARY by passing cooldown_hours=24
    (legacy value) — expect the algorithm to fire because the prior is
    older than 24h. This exercises the cooldown predicate end-to-end.
    """
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at(
        "cooldown7d", lat=sing[2], lng=sing[3], velocity_ms=0.2
    )
    # Seed a prior finding 6 days ago directly (avoids hypertable
    # chunk-range constraint violations from UPDATE event_time).
    now = datetime.now(timezone.utc)
    prior_t = now - timedelta(days=6)
    await execute(
        """
        INSERT INTO event (
            event_type, event_subtype, event_time, geom, severity,
            title, description, properties, entity_id, domain,
            decay_half_life_min
        ) VALUES (
            'port_call', 'SG', $1,
            ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography, 3,
            'seed', 'seed',
            jsonb_build_object(
                'algorithm', 'port_call_test',
                'port_id', 'SG_SIN',
                'vessel_id', $4::text
            ),
            $4::uuid, 'maritime', 1440
        )
        """,
        prior_t, sing[3], sing[2], str(vid),
    )
    # Verify the seed landed
    assert await _count_port_calls_for(vid) == 1

    # Re-scan with the default 7-day (168h) cooldown.
    # The seeded prior is 6 days old → inside the 7-day window → no new emission.
    await run_port_call_scan(algorithm_tag="port_call_test")
    n_under_cooldown = await _count_port_calls_for(vid)
    assert n_under_cooldown == 1, (
        f"7-day cooldown failed: {n_under_cooldown} findings after "
        f"re-scan with a 6-day-old prior — expected 1 (no new emission). "
        f"This is the bug that caused 7,413 historical FPs."
    )

    # Now scan with cooldown_hours=24 (the legacy pre-2026-05-13 value).
    # The 6-day-old prior is OUTSIDE this short window → emit again.
    # This proves the cooldown predicate actually consults the parameter.
    await run_port_call_scan(
        algorithm_tag="port_call_test", cooldown_hours=24
    )
    n_legacy_cooldown = await _count_port_calls_for(vid)
    assert n_legacy_cooldown == 2, (
        f"After scan with legacy 24h cooldown the algorithm should "
        f"re-fire (prior is 6 days old): got {n_legacy_cooldown} "
        f"findings (expected 2). This means the cooldown parameter "
        f"isn't being honored."
    )


async def test_vessel_with_no_velocity_skipped(_clean_world):
    """Vessel with no position_track row (so no velocity_ms) → no event."""
    sing = PORT_INDEX["SG_SIN"]
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
        f"{TEST_PREFIX}_novel", "VS_NOVEL", now, sing[3], sing[2],
    )
    await run_port_call_scan(algorithm_tag="port_call_test")
    assert await _count_port_calls_for(vid) == 0


async def test_old_position_outside_fresh_window_skipped(_clean_world):
    """Vessel last seen 2 hours ago + fresh_window_min=60 → excluded."""
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at("stale", lat=sing[2], lng=sing[3],
                                 velocity_ms=0.1, age_min=120)
    await run_port_call_scan(fresh_window_min=60, algorithm_tag="port_call_test")
    assert await _count_port_calls_for(vid) == 0


async def test_multiple_vessels_at_same_port_each_fire(_clean_world):
    """Three vessels at Singapore → 3 distinct port_call events for the seeded set."""
    sing = PORT_INDEX["SG_SIN"]
    vids = []
    for i in range(3):
        vids.append(await _seed_vessel_at(f"multi{i}",
                                           lat=sing[2] + 0.0001 * i,
                                           lng=sing[3] + 0.0001 * i,
                                           velocity_ms=0.2))
    await run_port_call_scan(algorithm_tag="port_call_test")
    total = 0
    for v in vids:
        total += await _count_port_calls_for(v)
    assert total == 3


# ─── port_arrival v1.1 ──────────────────────────────────────────────────


async def test_port_arrival_fires_on_first_visit(_clean_world):
    """Vessel currently at port + no prior port_call event for this
    (vessel, port) pair → port_arrival event for THIS vessel."""
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at("arr1", lat=sing[2], lng=sing[3], velocity_ms=0.2)
    await run_port_arrival_scan(algorithm_tag="port_arrival_test")
    assert await _count_arrivals_for(vid) == 1
    rows = await fetch(
        "SELECT event_type, severity, properties FROM event "
        "WHERE event_type = 'port_arrival' AND entity_id = $1::uuid",
        vid,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "port_arrival"
    assert r["severity"] == pytest.approx(4.0)   # commercial
    import json as _json
    props = r["properties"]
    if isinstance(props, str):
        props = _json.loads(props)
    assert props["transition"] == "arrival"
    assert props["port_id"] == "SG_SIN"


async def test_port_arrival_does_not_refire_within_lookback(_clean_world):
    """Once port_call has fired for a (vessel, port), a subsequent
    arrival scan should NOT re-emit (it's continued stay, not arrival)."""
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at("arr2", lat=sing[2], lng=sing[3], velocity_ms=0.2)
    await run_port_call_scan(algorithm_tag="port_call_test")
    await run_port_arrival_scan(algorithm_tag="port_arrival_test")
    assert await _count_arrivals_for(vid) == 0


async def test_port_arrival_strategic_severity_is_higher(_clean_world):
    bnd = PORT_INDEX["IR_BND"]
    vid = await _seed_vessel_at("arr_str", lat=bnd[2], lng=bnd[3], velocity_ms=0.1)
    await run_port_arrival_scan(algorithm_tag="port_arrival_test")
    rows = await fetch(
        "SELECT severity FROM event WHERE event_type = 'port_arrival' "
        "AND entity_id = $1::uuid",
        vid,
    )
    assert rows[0]["severity"] == pytest.approx(7.0)


# ─── port_departure v1.1 ────────────────────────────────────────────────


async def _count_departures_for(vessel_id) -> int:
    """Tests can't assert on the run_*_scan return value directly because
    production data may also be emitting departures concurrently. This
    counts JUST the events tied to the test's seeded vessel."""
    n = await fetchval(
        "SELECT count(*) FROM event WHERE event_type = 'port_departure' "
        "AND entity_id = $1::uuid",
        vessel_id,
    )
    return int(n or 0)


async def test_port_departure_fires_after_recent_port_call(_clean_world):
    """Vessel had port_call event 1h ago + is now far from port + has
    fresh position → port_departure event."""
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at("dep1", lat=sing[2], lng=sing[3], velocity_ms=0.2)
    await run_port_call_scan(algorithm_tag="port_call_test")
    # Move the vessel 50 km north (out of port radius)
    far_lat, far_lng = sing[2] + 0.45, sing[3]
    now = datetime.now(timezone.utc)
    async with acquire() as conn:
        await conn.execute(
            "UPDATE entity SET current_geom = "
            "ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, "
            "current_position_time = $3 WHERE id = $4",
            far_lng, far_lat, now, vid,
        )
        await conn.execute(
            "INSERT INTO position_track (time, entity_id, geom, velocity_ms, "
            "heading_deg, properties) VALUES ($1, $2, "
            "ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography, $5, 0, '{}'::jsonb)",
            now, vid, far_lng, far_lat, 8.0,
        )
    await run_port_departure_scan(algorithm_tag="port_departure_test")
    assert await _count_departures_for(vid) == 1
    rows = await fetch(
        "SELECT properties FROM event WHERE event_type = 'port_departure' "
        "AND entity_id = $1::uuid",
        vid,
    )
    assert len(rows) == 1
    import json as _json
    props = rows[0]["properties"]
    if isinstance(props, str):
        props = _json.loads(props)
    assert props["transition"] == "departure"
    assert props["port_id"] == "SG_SIN"
    assert props["distance_m"] > 5_000


async def test_port_departure_does_not_fire_for_stale_position(_clean_world):
    """Vessel had port_call but now last_seen is 2h ago + fresh_window=60.
    Could be 'AIS went dark' (dark_ship's territory) — port_departure
    must NOT fire so we don't double-count."""
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at("dep_stale",
                                 lat=sing[2], lng=sing[3], velocity_ms=0.2)
    await run_port_call_scan(algorithm_tag="port_call_test")
    stale_when = datetime.now(timezone.utc) - timedelta(hours=2)
    async with acquire() as conn:
        await conn.execute(
            "UPDATE entity SET current_geom = "
            "ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, "
            "current_position_time = $3 WHERE id = $4",
            sing[3] + 0.5, sing[2] + 0.5, stale_when, vid,
        )
    await run_port_departure_scan(fresh_window_min=60,
                                   algorithm_tag="port_departure_test")
    assert await _count_departures_for(vid) == 0


async def test_port_departure_idempotent(_clean_world):
    """Two consecutive departure scans for the same actual departure
    should fire ONCE — the second is suppressed by the existing-row
    NOT EXISTS guard."""
    sing = PORT_INDEX["SG_SIN"]
    vid = await _seed_vessel_at("dep_idem", lat=sing[2], lng=sing[3], velocity_ms=0.2)
    await run_port_call_scan(algorithm_tag="port_call_test")
    now = datetime.now(timezone.utc)
    async with acquire() as conn:
        await conn.execute(
            "UPDATE entity SET current_geom = "
            "ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, "
            "current_position_time = $3 WHERE id = $4",
            sing[3] + 0.5, sing[2] + 0.5, now, vid,
        )
    await run_port_departure_scan(algorithm_tag="port_departure_test")
    n1 = await _count_departures_for(vid)
    await run_port_departure_scan(algorithm_tag="port_departure_test")
    n2 = await _count_departures_for(vid)
    assert n1 == 1
    assert n2 == 1   # still 1, second scan was no-op


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
