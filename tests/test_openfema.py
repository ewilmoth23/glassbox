"""
OpenFEMA disaster-declarations ingester tests.

Asserts: state-centroid lookup, severity ladder, multi-county dedup
into one event per disaster, writer dual-write idempotency.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_openfema.py -v
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
from ingesters.openfema import (  # noqa: E402
    OpenFemaIngester, _state_coords, _severity, _STATE_CENTROIDS,
)
from writers import write_fema_events  # noqa: E402


def _make_ingester() -> OpenFemaIngester:
    return OpenFemaIngester(broadcaster=None, classifier=None,
                             db_writer=None, logger=None)


def test_severity_ladder():
    """Hurricane (base 9) + DR (+1) → 10 (capped). FM Fire (base 6) + FM (-1) = 5."""
    assert _severity("Hurricane", "DR") == 10        # 9 + 1, capped
    assert _severity("Earthquake", "DR") == 10        # 9 + 1, capped
    assert _severity("Flood", "EM") == 7              # 7 + 0
    assert _severity("Fire", "FM") == 5               # 6 - 1
    assert _severity("Snowstorm", "DR") == 6          # 5 + 1
    assert _severity("Other", "DR") == 5              # 4 + 1
    assert _severity(None, None) == 5                 # both default
    assert _severity("Unknown Type", "FM") == 4       # default 5 - 1


def test_state_coords_known_states():
    """California should give coords in CA's lat/lng band."""
    lat, lng = _state_coords("CA")
    assert 32 <= lat <= 42
    assert -125 <= lng <= -114
    # Territory: Northern Mariana Islands
    lat, lng = _state_coords("MP")
    assert 14 <= lat <= 20
    assert 144 <= lng <= 148


def test_state_coords_handles_unknown_and_none():
    assert _state_coords(None) == (0.0, 0.0)
    assert _state_coords("XX") == (0.0, 0.0)
    assert _state_coords("") == (0.0, 0.0)


def test_normalize_groups_multi_county_into_one_disaster():
    """139 raw rows for the same disasterNumber → 1 event."""
    ing = _make_ingester()
    raw = [
        {
            "disasterNumber": 4900, "state": "TX",
            "declarationType": "DR", "incidentType": "Hurricane",
            "declarationTitle": "TEST HURRICANE",
            "femaDeclarationString": "DR-4900-TX",
            "declarationDate": "2026-04-01T00:00:00.000Z",
            "designatedArea": f"County {i}",
        } for i in range(50)
    ]
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.payload["disaster_number"] == 4900
    assert e.payload["designated_area_count"] == 50
    assert e.payload["state"] == "TX"
    assert e.severity == 10           # Hurricane + DR → capped at 10


def test_normalize_emits_one_event_per_disaster():
    """Two distinct disasterNumbers → two events even if same state."""
    ing = _make_ingester()
    raw = [
        {"disasterNumber": 5001, "state": "FL", "declarationType": "DR",
         "incidentType": "Hurricane", "femaDeclarationString": "DR-5001-FL",
         "declarationDate": "2026-04-01T00:00:00.000Z"},
        {"disasterNumber": 5002, "state": "FL", "declarationType": "EM",
         "incidentType": "Tropical Storm", "femaDeclarationString": "EM-5002-FL",
         "declarationDate": "2026-04-02T00:00:00.000Z"},
    ]
    events = ing.normalize(raw)
    assert len(events) == 2
    by_id = {e.payload["disaster_number"]: e for e in events}
    assert 5001 in by_id and 5002 in by_id


def test_normalize_uses_state_centroid_for_coords():
    """A FL declaration should pin somewhere in FL's lat/lng."""
    ing = _make_ingester()
    raw = [{
        "disasterNumber": 6000, "state": "FL", "declarationType": "DR",
        "incidentType": "Hurricane", "femaDeclarationString": "DR-6000-FL",
        "declarationDate": "2026-04-01T00:00:00.000Z",
    }]
    events = ing.normalize(raw)
    e = events[0]
    fl_lat, fl_lng = _STATE_CENTROIDS["FL"]
    assert e.lat == fl_lat
    assert e.lng == fl_lng
    assert e.geocode_quality == "approximate"


def test_normalize_drops_rows_missing_disaster_number():
    ing = _make_ingester()
    raw = [
        {"state": "TX", "incidentType": "Fire"},                     # no disasterNumber
        {"disasterNumber": "not-a-number", "state": "TX",            # invalid
         "incidentType": "Fire"},
        {"disasterNumber": 7000, "state": "TX", "declarationType": "FM",
         "incidentType": "Fire", "femaDeclarationString": "FM-7000-TX",
         "declarationDate": "2026-04-01T00:00:00.000Z"},
    ]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert events[0].payload["disaster_number"] == 7000


# ─── Writer integration ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


_TEST_PFX = "openfema:DR-99999"


@pytest.fixture
async def _clean():
    async def _do():
        # Cover both DR-99999 (write_fema_persists test) and DR-99998
        # (idempotency test) and DR-99997 (filter test) — any sentinel
        # disaster number we use in tests gets the same prefix-match.
        await execute(
            "DELETE FROM event WHERE event_type='fema_declaration' "
            "AND (properties->>'fema_declaration' LIKE 'DR-9999%' "
            "  OR properties->>'fema_declaration' LIKE 'EM-9999%' "
            "  OR properties->>'fema_declaration' LIKE 'FM-9999%')",
        )
    await _do()
    yield
    await _do()


async def test_write_fema_persists_row(_clean):
    ing = _make_ingester()
    raw = [{
        "disasterNumber": 99999, "state": "FL", "declarationType": "DR",
        "incidentType": "Hurricane",
        "declarationTitle": "TEST FEMA HURRICANE",
        "femaDeclarationString": "DR-99999-FL",
        "declarationDate": "2026-04-01T00:00:00.000Z",
        "ihProgramDeclared": True,
        "paProgramDeclared": True,
    }]
    events = ing.normalize(raw)
    written = await write_fema_events(events)
    assert written == 1

    rows = await fetch(
        "SELECT severity, title, properties FROM event "
        "WHERE event_type='fema_declaration' "
        "AND properties->>'fema_declaration' = 'DR-99999-FL'",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["severity"] == 10.0   # Hurricane + DR
    assert "TEST FEMA HURRICANE" in r["title"] or "Hurricane" in r["title"]
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["state"] == "FL"
    assert props["ih_program"] is True
    assert props["pa_program"] is True


async def test_write_fema_idempotent(_clean):
    ing = _make_ingester()
    raw = [{
        "disasterNumber": 99998, "state": "TX", "declarationType": "FM",
        "incidentType": "Fire", "femaDeclarationString": "DR-99998-TX",
        "declarationDate": "2026-04-01T00:00:00.000Z",
    }]
    # Note: femaDeclarationString uses DR-99998 prefix to match the test
    # cleanup pattern; declarationType=FM is what gets stored as event_subtype.
    events = ing.normalize(raw)
    n1 = await write_fema_events(events)
    n2 = await write_fema_events(events)
    assert n1 == 1
    assert n2 == 0


async def test_write_filters_to_fema_layer(_clean):
    """Wrong-layer events skip silently."""
    from ingesters.base import GlassboxEvent
    ev = GlassboxEvent(
        layer="not_fema",
        external_id="foo",
        kind="other",
        lat=27.0, lng=-82.0, ts="2026-04-01T00:00:00+00:00",
        severity=5, source="x",
        payload={"disaster_number": 99997, "fema_declaration": "DR-99999-XX"},
        domain="atmospheric", geocode_quality="approximate",
        decay_half_life_min=10080,
    )
    n = await write_fema_events([ev])
    assert n == 0
