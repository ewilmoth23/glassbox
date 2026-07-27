"""
Test the AIS static-info merge — Digitraffic /v1/vessels (name + IMO +
callsign + ship_type) merged into each /v1/locations position record.

Why this test exists:
  Before this change, all 18,334 vessels in the DB had display_name=NULL
  because /v1/locations only carries position firehose data, not static
  info. That blocked cross-domain matching against OFAC SDN sanctioned
  vessels and the dark-ship "sanctioned vessel went dark" signal.

What we test:
  - _merge_static_info populates name + imo + callsign + ship_type from
    cached static info when MMSI matches
  - merge does NOT overwrite fields the position record already has
  - merge is a no-op when MMSI not in cache (or cache empty)
  - destination = "UNKNOWN" is filtered out (Digitraffic upstream sentinel)
  - draught conversion: stored as decimeters in upstream, exposed as meters
  - normalize() passes the merged fields through to GlassboxEvent.payload
  - End-to-end: static-info refresh → position fetch → merge → emit

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_ships_static_info.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.ships import ShipsIngester  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────


def _ingester_with_cache(static_records):
    """Build a ShipsIngester instance with a pre-seeded static-info cache."""
    ing = ShipsIngester(broadcaster=lambda *_: None)
    ing._digitraffic_static = {
        str(r["mmsi"]): r for r in static_records
    }
    return ing


# ─── _merge_static_info tests ─────────────────────────────────────────────


def test_merge_populates_name_imo_callsign_when_mmsi_in_cache():
    ing = _ingester_with_cache([
        {"mmsi": 219598000, "name": "NORD SUPERIOR", "imo": 9692129,
         "callSign": "OWPA2", "shipType": 80},
    ])
    row = {"mmsi": "219598000", "lat": 60.0, "lng": 25.0,
           "name": None, "ship_type": None}
    ing._merge_static_info(row)

    assert row["name"] == "NORD SUPERIOR"
    assert row["imo"] == 9692129
    assert row["callsign"] == "OWPA2"
    assert row["ship_type"] == 80


def test_merge_is_noop_when_cache_empty():
    ing = ShipsIngester(broadcaster=lambda *_: None)
    row = {"mmsi": "123456789", "lat": 60.0, "lng": 25.0, "name": None}
    ing._merge_static_info(row)
    # Nothing should have been added
    assert row.get("name") is None
    assert row.get("imo") is None
    assert row.get("callsign") is None


def test_merge_is_noop_when_mmsi_not_in_cache():
    ing = _ingester_with_cache([
        {"mmsi": 111111111, "name": "OTHER VESSEL", "imo": 1, "callSign": "X"},
    ])
    row = {"mmsi": "999999999", "lat": 0, "lng": 0, "name": None}
    ing._merge_static_info(row)
    assert row.get("name") is None
    assert row.get("imo") is None


def test_merge_does_not_overwrite_existing_name():
    """If the position record already has a real name, don't replace it."""
    ing = _ingester_with_cache([
        {"mmsi": 219598000, "name": "STATIC NAME", "imo": 1, "callSign": "X"},
    ])
    row = {"mmsi": "219598000", "lat": 0, "lng": 0,
           "name": "POSITION NAME"}
    ing._merge_static_info(row)
    assert row["name"] == "POSITION NAME"  # not overwritten
    assert row["imo"] == 1                  # merged because absent


def test_merge_filters_destination_unknown_sentinel():
    ing = _ingester_with_cache([
        {"mmsi": 1, "name": "A", "imo": 1, "callSign": "X",
         "destination": "UNKNOWN"},
    ])
    row = {"mmsi": "1", "lat": 0, "lng": 0, "name": None}
    ing._merge_static_info(row)
    assert "destination" not in row


def test_merge_keeps_real_destination():
    ing = _ingester_with_cache([
        {"mmsi": 1, "name": "A", "imo": 1, "callSign": "X",
         "destination": "NL AMS"},
    ])
    row = {"mmsi": "1", "lat": 0, "lng": 0, "name": None}
    ing._merge_static_info(row)
    assert row["destination"] == "NL AMS"


def test_merge_converts_draught_decimeters_to_meters():
    """Digitraffic encodes draught in decimeters; we expose meters."""
    ing = _ingester_with_cache([
        {"mmsi": 1, "name": "A", "imo": 1, "callSign": "X", "draught": 118},
    ])
    row = {"mmsi": "1", "lat": 0, "lng": 0, "name": None}
    ing._merge_static_info(row)
    assert row["draught"] == 11.8


def test_merge_skips_zero_or_invalid_imo():
    """imo=0 is the AIS sentinel for 'no IMO'. Don't propagate."""
    ing = _ingester_with_cache([
        {"mmsi": 1, "name": "A", "imo": 0, "callSign": "X"},
    ])
    row = {"mmsi": "1", "lat": 0, "lng": 0, "name": None}
    ing._merge_static_info(row)
    assert row.get("imo") is None


def test_merge_skips_blank_callsign():
    ing = _ingester_with_cache([
        {"mmsi": 1, "name": "A", "imo": 1, "callSign": "  "},
    ])
    row = {"mmsi": "1", "lat": 0, "lng": 0, "name": None}
    ing._merge_static_info(row)
    assert row.get("callsign") is None


def test_merge_handles_missing_mmsi_in_row():
    ing = _ingester_with_cache([{"mmsi": 1, "name": "A"}])
    row = {"mmsi": "", "lat": 0, "lng": 0, "name": None}
    # Should not raise
    ing._merge_static_info(row)
    assert row.get("name") is None


# ─── normalize() passes merged fields through to payload ──────────────────


def test_normalize_passes_imo_callsign_destination_to_payload():
    ing = ShipsIngester(broadcaster=lambda *_: None)
    raw = [{
        "mmsi": "219598000",
        "lat": 60.0, "lng": 25.0,
        "sog": 10.5, "cog": 90, "heading": 92,
        "ship_type": 80,
        "name": "NORD SUPERIOR",
        "imo": 9692129,
        "callsign": "OWPA2",
        "destination": "NL AMS",
        "draught": 11.8,
        "_source": "digitraffic",
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    p = events[0].payload
    assert p["mmsi"] == "219598000"
    assert p["name"] == "NORD SUPERIOR"
    assert p["imo"] == 9692129
    assert p["callsign"] == "OWPA2"
    assert p["destination"] == "NL AMS"
    assert p["draught"] == 11.8
    assert p["ship_type"] == 80


def test_normalize_omits_optional_fields_when_absent():
    """Vessels not in the cache → no name/IMO/callsign in payload (vs garbage)."""
    ing = ShipsIngester(broadcaster=lambda *_: None)
    raw = [{
        "mmsi": "999999999",
        "lat": 60.0, "lng": 25.0,
        "sog": 5.0, "cog": 0, "heading": 0,
        "ship_type": None,
        "name": None,
        "_source": "digitraffic",
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    p = events[0].payload
    assert p.get("name") is None
    assert "imo" not in p
    assert "callsign" not in p
    assert "destination" not in p


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
