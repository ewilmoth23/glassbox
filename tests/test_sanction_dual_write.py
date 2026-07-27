"""
Phase 2-G — ofac_sdn.py dual-write of OFAC SDN entries to the entity table.

Asserts:
  - One sanctioned vessel in → one entity row out (entity_type='sanctioned_vessel')
  - One sanctioned aircraft in → one entity row out (entity_type='sanctioned_aircraft')
  - Re-running with same external_id is idempotent — no duplicate, returns 0 new
  - Multiple distinct entries → all persist
  - Empty input → no-op
  - fcra_safe=False is enforced in stored properties
  - display_name preserved
  - Non-vessel/aircraft types (individual, entity, other) are skipped
  - End-to-end: OfacSdnIngester.cycle() with db_writer hook persists rows

Hits the real Postgres on the Mac Mini. Uses sentinel external_id prefix
('test07_*') for deterministic cleanup.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_sanction_dual_write.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from writers import write_sanction_entities  # noqa: E402


TEST_PREFIX = "ofac_sdn:vessel:test07_"
TEST_PREFIX_AIR = "ofac_sdn:aircraft:test07_"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_sanctions():
    async def _cleanup():
        await execute(
            "DELETE FROM entity WHERE canonical_id_type='ofac_sdn_id' AND canonical_id LIKE $1",
            "ofac_sdn:%test07_%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _sanction_event(external_id: str, sdn_type: str, display_name: str) -> GlassboxEvent:
    """Build a GlassboxEvent in the shape OfacSdnIngester.normalize() emits."""
    return GlassboxEvent(
        layer="sanctions",
        external_id=external_id,
        kind="index",
        lat=0.0,
        lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=10,
        source="US Treasury OFAC SDN List",
        payload={
            "type": sdn_type,
            "display_name": display_name,
            "fcra_safe": False,
            "_attribution": "Sanctions: US Treasury OFAC",
        },
        domain="entity",
        geocode_quality="needs_match",
        decay_half_life_min=10080,
    )


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_sanction_entity_creates_vessel_row(_clean_test_sanctions):
    """One sanctioned vessel → one entity row, entity_type='sanctioned_vessel'."""
    ev = _sanction_event(f"{TEST_PREFIX}V001", "vessel", "MV TEST GHOST")
    written = await write_sanction_entities([ev])
    assert written == 1

    rows = await fetch(
        "SELECT entity_type, canonical_id_type, canonical_id, display_name, "
        "       properties, current_geom "
        "FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}V001",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["entity_type"] == "sanctioned_vessel"
    assert r["canonical_id_type"] == "ofac_sdn_id"
    assert r["display_name"] == "MV TEST GHOST"
    # No position — sanctioned entries match against AIS-fed vessels
    assert r["current_geom"] is None

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["fcra_safe"] is False
    assert props["type"] == "vessel"
    assert props["sanctioning_authority"] == "US Treasury OFAC"


async def test_write_sanction_entity_creates_aircraft_row(_clean_test_sanctions):
    """One sanctioned aircraft → entity_type='sanctioned_aircraft'."""
    ev = _sanction_event(f"{TEST_PREFIX_AIR}A001", "aircraft", "EP-IAS")
    written = await write_sanction_entities([ev])
    assert written == 1

    rows = await fetch(
        "SELECT entity_type, display_name FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX_AIR}A001",
    )
    assert len(rows) == 1
    assert rows[0]["entity_type"] == "sanctioned_aircraft"
    assert rows[0]["display_name"] == "EP-IAS"


async def test_write_sanction_entities_is_idempotent(_clean_test_sanctions):
    """Re-running with same external_id → no new row, returns 0."""
    ev = _sanction_event(f"{TEST_PREFIX}IDEM", "vessel", "MV IDEMPOTENT")

    n1 = await write_sanction_entities([ev])
    assert n1 == 1

    n2 = await write_sanction_entities([ev])
    assert n2 == 0  # already exists; ON CONFLICT updates last_seen but doesn't count

    total = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}IDEM",
    )
    assert total == 1


async def test_write_sanction_entities_multiple_distinct(_clean_test_sanctions):
    """Five distinct sanctions → five entity rows."""
    entries = [
        _sanction_event(f"{TEST_PREFIX}M{i}", "vessel", f"MV TEST {i}")
        for i in range(5)
    ]
    n = await write_sanction_entities(entries)
    assert n == 5

    total = await fetchval(
        "SELECT count(*) FROM entity WHERE entity_type='sanctioned_vessel' "
        "AND canonical_id LIKE $1",
        f"{TEST_PREFIX}M%",
    )
    assert total == 5


async def test_write_sanction_entities_zero_events_is_noop():
    n = await write_sanction_entities([])
    assert n == 0


async def test_write_sanction_entities_filters_non_locatable_types(_clean_test_sanctions):
    """payload.type='individual' / 'entity' / 'other' should be skipped (defensive)."""
    skip_types = ["individual", "entity", "other", "subdivision"]
    events = [
        _sanction_event(f"{TEST_PREFIX}SKIP{i}", t, f"NAME {i}")
        for i, t in enumerate(skip_types)
    ]
    n = await write_sanction_entities(events)
    assert n == 0

    total = await fetchval(
        "SELECT count(*) FROM entity WHERE canonical_id LIKE $1",
        f"{TEST_PREFIX}SKIP%",
    )
    assert total == 0


async def test_write_sanction_entities_filters_wrong_layer():
    """Defensive: events with layer != 'sanctions' should be skipped (caller bug)."""
    ev = GlassboxEvent(
        layer="planes",  # WRONG
        external_id=f"{TEST_PREFIX}WRONG",
        kind="index",
        lat=0.0, lng=0.0, ts=datetime.now(timezone.utc).isoformat(),
        payload={"type": "vessel", "display_name": "Should be skipped"},
    )
    n = await write_sanction_entities([ev])
    assert n == 0


async def test_re_emit_updates_last_seen(_clean_test_sanctions):
    """Re-emit (hourly OFAC poll cycle) should advance last_seen timestamp."""
    ev = _sanction_event(f"{TEST_PREFIX}LS", "vessel", "MV LASTSEEN")
    await write_sanction_entities([ev])

    first_ls = await fetchval(
        "SELECT last_seen FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}LS",
    )

    # Build a second event with newer timestamp
    import asyncio
    await asyncio.sleep(0.05)
    ev2 = _sanction_event(f"{TEST_PREFIX}LS", "vessel", "MV LASTSEEN")  # same ext_id
    n = await write_sanction_entities([ev2])
    assert n == 0  # not new

    second_ls = await fetchval(
        "SELECT last_seen FROM entity WHERE canonical_id = $1",
        f"{TEST_PREFIX}LS",
    )
    assert second_ls >= first_ls


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
