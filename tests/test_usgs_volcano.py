"""
USGS Volcano Hazards Program ingester tests.

Asserts ingester filters + normalize() shape + severity ladder +
coordinate lookup + writer dual-write idempotency.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_usgs_volcano.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetch, execute  # noqa: E402
from ingesters.usgs_volcano import (  # noqa: E402
    UsgsVolcanoIngester, _coords_for_vnum, _severity, _VOLCANO_COORDS,
)
from writers import write_volcanic_events  # noqa: E402


def _make_ingester() -> UsgsVolcanoIngester:
    return UsgsVolcanoIngester(broadcaster=None, classifier=None,
                                db_writer=None, logger=None)


def test_severity_ladder():
    """Composition: alert_level base + color_code boost, clamped 1..10."""
    assert _severity("ADVISORY", "YELLOW") == 6      # 5 base + 1
    assert _severity("WATCH",    "ORANGE") == 9      # 7 base + 2
    assert _severity("WARNING",  "RED")    == 10     # 10 base + 3 → clamped
    assert _severity("ADVISORY", "GREEN")  == 5      # 5 + 0
    # Unknown level falls through to default 5
    assert _severity("UNKNOWN",  "YELLOW") == 6      # 5 + 1
    assert _severity(None, None)            == 5     # both default


def test_coords_lookup_known_volcano():
    """Great Sitkin has a hardcoded entry; Aleutian-AK lat/lng makes sense."""
    lat, lng = _coords_for_vnum("311120")
    assert 50 <= lat <= 56              # Aleutian latitude band
    assert -180 <= lng <= -160          # Aleutian longitude band
    assert lat != 0.0 and lng != 0.0    # not sentinel


def test_coords_unknown_vnum_returns_sentinel():
    lat, lng = _coords_for_vnum("999999")
    assert (lat, lng) == (0.0, 0.0)


def test_coords_lookup_handles_none():
    """Defensive: None input doesn't crash."""
    assert _coords_for_vnum(None) == (0.0, 0.0)
    assert _coords_for_vnum("") == (0.0, 0.0)


def test_normalize_emits_event_with_canonical_payload():
    ing = _make_ingester()
    raw = [{
        "volcano_name":      "Great Sitkin",
        "vnum":              "311120",
        "alert_level":       "WATCH",
        "color_code":        "ORANGE",
        "obs_fullname":      "Alaska Volcano Observatory",
        "obs_abbr":          "avo",
        "notice_identifier": "DOI-USGS-AVO-TEST-001",
        "notice_url":        "https://example/test001",
        "sent_utc":          "2026-05-08 18:00:00",
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    ev = events[0]
    assert ev.layer == "volcanic_activity"
    assert ev.kind == "volcanic_alert"
    assert ev.severity == 9
    assert ev.lat != 0.0 and ev.lng != 0.0   # Great Sitkin has real coords
    assert ev.payload["volcano_name"] == "Great Sitkin"
    assert ev.payload["alert_level"] == "WATCH"
    assert ev.payload["color_code"] == "ORANGE"
    assert ev.payload["observatory_abbr"] == "avo"
    assert "test001" in ev.payload["notice_url"]
    assert ev.geocode_quality == "point"


def test_normalize_drops_rows_missing_id_or_name():
    ing = _make_ingester()
    raw = [
        {"volcano_name": "", "vnum": "311120", "alert_level": "WATCH", "color_code": "ORANGE"},
        {"volcano_name": "Test", "vnum": "", "alert_level": "WATCH", "color_code": "ORANGE"},
        {"volcano_name": "Valid", "vnum": "999998", "alert_level": "ADVISORY", "color_code": "YELLOW"},
    ]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert events[0].payload["volcano_name"] == "Valid"


def test_normalize_uses_sentinel_for_unknown_vnum():
    """Unknown vnum → sentinel coords + geocode_quality='needs_match'."""
    ing = _make_ingester()
    raw = [{
        "volcano_name": "Unmapped Volcano",
        "vnum":         "999998",
        "alert_level":  "ADVISORY",
        "color_code":   "YELLOW",
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert events[0].lat == 0.0 and events[0].lng == 0.0
    assert events[0].geocode_quality == "needs_match"


# ─── Writer dual-write integration test ────────────────────────────────


_TEST_NOTICE_PREFIX = "DOI-USGS-TEST-"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM event WHERE event_type='volcanic_alert' "
            "AND properties->>'notice_identifier' LIKE $1",
            f"{_TEST_NOTICE_PREFIX}%",
        )
    await _do()
    yield
    await _do()


async def test_write_volcanic_events_persists_row(_clean):
    """Single event in → one event row out with severity, geom, properties."""
    ing = _make_ingester()
    raw = [{
        "volcano_name": "Great Sitkin",
        "vnum":         "311120",
        "alert_level":  "WATCH",
        "color_code":   "ORANGE",
        "obs_abbr":     "avo",
        "notice_identifier": f"{_TEST_NOTICE_PREFIX}001",
        "notice_url":   "https://example/001",
        "sent_utc":     "2026-05-08 18:00:00",
    }]
    events = ing.normalize(raw)
    written = await write_volcanic_events(events)
    assert written == 1

    rows = await fetch(
        "SELECT severity, title, properties FROM event "
        "WHERE event_type='volcanic_alert' "
        "AND properties->>'notice_identifier' = $1",
        f"{_TEST_NOTICE_PREFIX}001",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["severity"] == 9.0
    assert "Great Sitkin" in r["title"]
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["volcano_name"] == "Great Sitkin"
    assert props["alert_level"] == "WATCH"


async def test_write_volcanic_idempotent_within_same_notice(_clean):
    """Re-emitting the same notice → no new row (UUID5 stable on
    external_id which already encodes vnum + notice_id)."""
    ing = _make_ingester()
    raw = [{
        "volcano_name": "Great Sitkin",
        "vnum":         "311120",
        "alert_level":  "ADVISORY",
        "color_code":   "YELLOW",
        "notice_identifier": f"{_TEST_NOTICE_PREFIX}002",
        "sent_utc":     "2026-05-08 18:00:00",
    }]
    events = ing.normalize(raw)
    n1 = await write_volcanic_events(events)
    n2 = await write_volcanic_events(events)
    assert n1 == 1
    assert n2 == 0


async def test_write_filters_to_volcanic_layer(_clean):
    """Events with the wrong layer are skipped — defensive against
    accidentally crossing wires with another writer."""
    from ingesters.base import GlassboxEvent
    ev = GlassboxEvent(
        layer="not_volcanic",
        external_id="foo",
        kind="other",
        lat=0.0, lng=0.0, ts="2026-05-08T18:00:00+00:00",
        severity=9, source="x", payload={"volcano_name": "x", "vnum": "x",
                                          "notice_identifier": f"{_TEST_NOTICE_PREFIX}003"},
        domain="atmospheric", geocode_quality="needs_match",
        decay_half_life_min=1440,
    )
    n = await write_volcanic_events([ev])
    assert n == 0
