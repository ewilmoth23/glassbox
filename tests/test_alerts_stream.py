"""
/api/v1/alerts/stream SSE endpoint test.

Asserts (helper-level — full SSE plumbing is exercised by the live server):
  - _poll_new_tier1_events returns 0 rows when no tier-1 events newer than
    the watermark exist
  - When a fresh tier-1 event is inserted, the next poll picks it up
  - Bbox filter excludes events outside the bbox
  - Non-tier-1 event types are NOT included even if they match the bbox+time

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_alerts_stream.py -v
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

from db import init_pool, close_pool, execute  # noqa: E402
from web.routes.api_v1.alerts import _poll_new_tier1_events  # noqa: E402


TEST_PREFIX = "test20"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_alerts():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_subtype LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


async def _seed_event(
    *,
    event_type: str = "sanctioned_vessel_rendezvous",
    subtype: str = f"{TEST_PREFIX}_a",
    lat: float = 59.0,
    lng: float = 25.0,
    severity: float = 9.0,
    minutes_ago: float = 0.0,
):
    eid = uuid4()
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    await execute(
        """
        INSERT INTO event (id, event_type, event_subtype, event_time, geom,
                          severity, title, description, properties,
                          domain, decay_half_life_min)
        VALUES ($1::uuid, $2, $3, $4,
                ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography,
                $7, 'test', 'test', '{}'::jsonb, 'maritime', 1440)
        """,
        eid, event_type, subtype, ts,
        float(lng), float(lat), float(severity),
    )
    return eid


async def test_poll_returns_empty_when_no_new_events(_clean_alerts):
    """No tier-1 events newer than `since` → empty list."""
    since = datetime.now(timezone.utc) + timedelta(minutes=1)
    rows = await _poll_new_tier1_events(since=since, bbox=None)
    assert rows == [] or len(rows) == 0


async def test_poll_picks_up_new_event(_clean_alerts):
    """Insert a fresh tier-1 event, poll with watermark before it → 1 row."""
    since = datetime.now(timezone.utc) - timedelta(seconds=10)
    await _seed_event(event_type="sanctioned_vessel_rendezvous",
                       subtype=f"{TEST_PREFIX}_a")

    rows = await _poll_new_tier1_events(since=since, bbox=None)
    relevant = [r for r in rows if r["event_subtype"] == f"{TEST_PREFIX}_a"]
    assert len(relevant) == 1
    assert relevant[0]["event_type"] == "sanctioned_vessel_rendezvous"


async def test_poll_bbox_filter_excludes_far_event(_clean_alerts):
    """Event outside bbox → not returned even though it matches event_type+time."""
    since = datetime.now(timezone.utc) - timedelta(seconds=10)
    # Insert event in Mediterranean area (lat 36, lng 14)
    await _seed_event(subtype=f"{TEST_PREFIX}_med", lat=36.0, lng=14.0)

    # Query with Baltic bbox — should NOT include the Mediterranean event
    bbox = (10.0, 53.0, 30.0, 66.0)
    rows = await _poll_new_tier1_events(since=since, bbox=bbox)
    relevant = [r for r in rows if r["event_subtype"] == f"{TEST_PREFIX}_med"]
    assert len(relevant) == 0


async def test_poll_bbox_filter_includes_in_range(_clean_alerts):
    since = datetime.now(timezone.utc) - timedelta(seconds=10)
    await _seed_event(subtype=f"{TEST_PREFIX}_baltic", lat=59.0, lng=25.0)

    bbox = (10.0, 53.0, 30.0, 66.0)
    rows = await _poll_new_tier1_events(since=since, bbox=bbox)
    relevant = [r for r in rows if r["event_subtype"] == f"{TEST_PREFIX}_baltic"]
    assert len(relevant) == 1


async def test_poll_excludes_non_tier1_event_types(_clean_alerts):
    """An event_type that's not in the tier-1 list is NOT pushed via SSE."""
    since = datetime.now(timezone.utc) - timedelta(seconds=10)
    await _seed_event(event_type="usgs_quake", subtype=f"{TEST_PREFIX}_quake")

    rows = await _poll_new_tier1_events(since=since, bbox=None)
    relevant = [r for r in rows if r["event_subtype"] == f"{TEST_PREFIX}_quake"]
    assert len(relevant) == 0


async def test_poll_excludes_old_events_relative_to_watermark(_clean_alerts):
    """An event with event_time BEFORE the watermark must not be picked up."""
    # Insert an event timestamped 10 minutes ago
    await _seed_event(subtype=f"{TEST_PREFIX}_old", minutes_ago=10)

    # Then poll with a watermark of "1 minute ago" — old event predates it.
    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = await _poll_new_tier1_events(since=since, bbox=None)
    relevant = [r for r in rows if r["event_subtype"] == f"{TEST_PREFIX}_old"]
    assert len(relevant) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
