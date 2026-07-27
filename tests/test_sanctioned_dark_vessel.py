"""
Phase 4 algorithm #8 — sanctioned_dark_vessel.py test.

Combined-signal algorithm: vessel matches OFAC SDN AND has gone dark
on AIS while moving. Severity 10. Catches the textbook sanctions-evasion
in real time.

Asserts:
  - Vessel that matches sanctioned_vessel by IMO + dark 12h + moving → 1 finding
  - Vessel that matches by name (no IMO) + dark + moving → 1 finding
  - Vessel that's dark but NOT sanctioned → 0 findings
  - Vessel that's sanctioned but currently broadcasting → 0 findings
  - Vessel anchored (vel ~0) when last seen → 0 findings
  - IMO match takes precedence over name match — subtype='imo_match'
  - Severity always 10
  - Idempotent within 24h dedup window
  - properties.live_imo + sanctioned_imo + sanctioned_canonical_id all populated

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_sanctioned_dark_vessel.py -v
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
from algorithms.sanctioned_dark_vessel import run_sanctioned_dark_scan  # noqa: E402


TEST_PREFIX = "test18"
TEST_SDN_PREFIX = "ofac_sdn:vessel:test18_"
TEST_TAG = "sanctioned_dark_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_world():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='sanctioned_vessel_went_dark' "
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
            f"{TEST_PREFIX}%", "ofac_sdn:%test18%",
        )
    await _cleanup()
    yield
    await _cleanup()


async def _seed_dark_vessel(
    suffix: str,
    *,
    display_name: str | None = None,
    imo: int | None = None,
    last_seen_hours_ago: float = 12.0,
    velocity_ms: float = 4.0,
) -> str:
    canonical_id = f"{TEST_PREFIX}{suffix}"
    last_seen_ts = datetime.now(timezone.utc) - timedelta(hours=last_seen_hours_ago)
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
                 ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography, $4)
            RETURNING id
            """,
            canonical_id, display_name, json.dumps(props), last_seen_ts,
        )
        await conn.execute(
            """
            INSERT INTO position_track (time, entity_id, geom, velocity_ms, heading_deg)
            VALUES ($1, $2, ST_SetSRID(ST_MakePoint(25.0, 59.0), 4326)::geography, $3, 0)
            """,
            last_seen_ts, eid, float(velocity_ms),
        )
    return str(eid)


async def _seed_sanctioned_vessel(
    suffix: str,
    *,
    display_name: str = "TEST SANCTIONED",
    imo: int | None = None,
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
                ('sanctioned_vessel', 'ofac_sdn_id', $1, $2, $3::jsonb,
                 NOW(), NOW())
            RETURNING id
            """,
            canonical_id, display_name, json.dumps(props),
        )
    return str(eid)


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_imo_match_dark_emits_critical(_clean_world):
    """Vessel matched by IMO + dark 12h + was moving → severity 10 finding."""
    await _seed_dark_vessel("V1", display_name="ASTRA",
                              imo=8770261, last_seen_hours_ago=12, velocity_ms=4.0)
    await _seed_sanctioned_vessel("S1", display_name="Astra", imo=8770261)

    n = await run_sanctioned_dark_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1
    rows = await fetch(
        "SELECT event_subtype, severity, properties FROM event "
        "WHERE event_type='sanctioned_vessel_went_dark' "
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


async def test_name_match_dark_emits_finding(_clean_world):
    """Vessel matched by name (no IMO) + dark → finding with subtype=name_match."""
    await _seed_dark_vessel("V2", display_name="POLA SOFIA",
                              imo=None, last_seen_hours_ago=10, velocity_ms=3.0)
    await _seed_sanctioned_vessel("S2", display_name="POLA SOFIA")

    n = await run_sanctioned_dark_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 1
    subtype = await fetchval(
        "SELECT event_subtype FROM event WHERE event_type='sanctioned_vessel_went_dark' "
        "AND properties->>'algorithm'=$1",
        TEST_TAG,
    )
    assert subtype == "name_match"


async def test_dark_but_not_sanctioned_skipped(_clean_world):
    """Vessel went dark but isn't sanctioned → no finding."""
    await _seed_dark_vessel("V3", display_name="REGULAR FREIGHTER",
                              imo=9999999, last_seen_hours_ago=10, velocity_ms=4.0)
    # No sanctioned_vessel created
    n = await run_sanctioned_dark_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_sanctioned_but_currently_broadcasting_skipped(_clean_world):
    """Sanctioned vessel still active (last_seen 5min ago) → not "went dark"."""
    await _seed_dark_vessel("V4", display_name="ASTRA",
                              imo=8770261, last_seen_hours_ago=0.1, velocity_ms=4.0)
    await _seed_sanctioned_vessel("S4", display_name="Astra", imo=8770261)
    n = await run_sanctioned_dark_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_anchored_vessel_excluded(_clean_world):
    """Sanctioned vessel that was at velocity ~0 last → anchored, not 'went dark'."""
    await _seed_dark_vessel("V5", display_name="ASTRA",
                              imo=8770261, last_seen_hours_ago=12, velocity_ms=0.1)
    await _seed_sanctioned_vessel("S5", display_name="Astra", imo=8770261)
    n = await run_sanctioned_dark_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


async def test_idempotent_within_dedup(_clean_world):
    await _seed_dark_vessel("V6", display_name="ASTRA",
                              imo=8770261, last_seen_hours_ago=12, velocity_ms=4.0)
    await _seed_sanctioned_vessel("S6", display_name="Astra", imo=8770261)
    n1 = await run_sanctioned_dark_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n1 == 1
    n2 = await run_sanctioned_dark_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n2 == 0


async def test_no_inputs_returns_zero(_clean_world):
    n = await run_sanctioned_dark_scan(
        algorithm_tag=TEST_TAG,
        entity_canonical_id_like=f"{TEST_PREFIX}%",
    )
    assert n == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
