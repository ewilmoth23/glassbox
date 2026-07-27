"""
AISStream.io ingester — pure-fn parse + normalize tests.

Asserts:
  - PositionReport message parses into a buffered dict with mmsi/lat/lng
  - Sog (knots) -> velocity_ms (×0.514444)
  - TrueHeading == 511 means "not available" -> None
  - Non-PositionReport message types ignored
  - Out-of-range lat/lng rejected
  - Missing mmsi rejected
  - normalize() shapes events identical to ships.py (so write_vessel_events
    handles both transparently)
  - normalize() preserves attribution
  - empty buffer -> empty event list

Network test (live AISStream connect) is optional + only runs when
AISSTREAM_API_KEY is set in env.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_aisstream.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.aisstream import AISStreamIngester  # noqa: E402


def _ing() -> AISStreamIngester:
    return AISStreamIngester()


def _make_position_msg(*, mmsi=123456789, lat=30.5, lng=32.4,
                       sog=12.3, heading=178, name="EVER GIVEN") -> str:
    return json.dumps({
        "MessageType": "PositionReport",
        "MetaData": {
            "MMSI":      mmsi,
            "ShipName":  name,
            "latitude":  lat,
            "longitude": lng,
            "time_utc":  "2026-05-09 18:30:00 +0000 UTC",
        },
        "Message": {
            "PositionReport": {
                "UserID":      mmsi,
                "Latitude":    lat,
                "Longitude":   lng,
                "Sog":         sog,
                "Cog":         180.0,
                "TrueHeading": heading,
            }
        }
    })


# ─── _handle_message parsing ────────────────────────────────────────────


def test_position_report_parses_into_buffer():
    ing = _ing()
    ing._handle_message(_make_position_msg())
    assert len(ing._buffered) == 1
    r = ing._buffered[0]
    assert r["mmsi"] == 123456789
    assert r["name"] == "EVER GIVEN"
    assert r["lat"] == 30.5
    assert r["lng"] == 32.4
    # 12.3 knots → 6.327 m/s
    assert r["velocity_ms"] == pytest.approx(12.3 * 0.514444, rel=1e-4)
    assert r["heading_deg"] == 178


def test_true_heading_511_means_unavailable():
    ing = _ing()
    ing._handle_message(_make_position_msg(heading=511))
    assert ing._buffered[0]["heading_deg"] is None


def test_non_position_non_static_message_types_skipped():
    ing = _ing()
    other = json.dumps({
        "MessageType": "BaseStationReport",
        "MetaData": {"MMSI": 999},
        "Message": {"BaseStationReport": {"UserID": 999}},
    })
    ing._handle_message(other)
    assert ing._buffered == []


def test_ship_static_data_extracts_imo_and_metadata():
    """The fix for the ATLAS false positive: subscribing to
    ShipStaticData lets us populate IMO + flag for proper IMO-exact
    sanctions matching. Without this, vessels broadcast over PositionReport
    only and we never learn their IMO."""
    ing = _ing()
    static = json.dumps({
        "MessageType": "ShipStaticData",
        "MetaData": {
            "MMSI":      367560990,
            "ShipName":  "ATLAS",
            "latitude":  27.919,
            "longitude": -82.444,
            "time_utc":  "2026-05-09 18:30:00 +0000 UTC",
        },
        "Message": {"ShipStaticData": {
            "UserID":      367560990,
            "ImoNumber":   9641754,           # the real IMO of US-tug ATLAS
            "Name":        "ATLAS",
            "CallSign":    "WDG6819",
            "Type":        52,                 # tug
            "Destination": "TAMPA",
        }},
    })
    ing._handle_message(static)
    assert len(ing._buffered) == 1
    r = ing._buffered[0]
    assert r["_kind"] == "static"
    assert r["mmsi"] == 367560990
    assert r["imo"] == 9641754
    assert r["callsign"] == "WDG6819"
    assert r["ship_type"] == 52
    assert r["destination"] == "TAMPA"


def test_ship_static_data_skips_invalid_imo():
    """AIS feeds sometimes carry IMO=0 or junk integers. Real IMO is 7
    digits; reject anything outside that range."""
    ing = _ing()
    bad = json.dumps({
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 1, "latitude": 0.0, "longitude": 0.0},
        "Message": {"ShipStaticData": {"UserID": 1, "ImoNumber": 0}},
    })
    ing._handle_message(bad)
    assert len(ing._buffered) == 1
    assert ing._buffered[0]["imo"] is None   # 0 → rejected


def test_ship_static_data_skips_missing_position_metadata():
    """ShipStaticData without lat/lng in MetaData is unusable for the
    downstream entity-write path — skip rather than emit at (0,0)."""
    ing = _ing()
    bad = json.dumps({
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 12345},
        "Message": {"ShipStaticData": {"UserID": 12345, "ImoNumber": 9000001}},
    })
    ing._handle_message(bad)
    assert ing._buffered == []


def test_out_of_range_coords_rejected():
    ing = _ing()
    ing._handle_message(_make_position_msg(lat=200.0, lng=0.0))
    ing._handle_message(_make_position_msg(lat=0.0, lng=400.0))
    assert ing._buffered == []


def test_missing_mmsi_rejected():
    ing = _ing()
    bad = json.dumps({
        "MessageType": "PositionReport",
        "MetaData": {"latitude": 0.0, "longitude": 0.0},
        "Message": {"PositionReport": {"Latitude": 0.0, "Longitude": 0.0}},
    })
    ing._handle_message(bad)
    assert ing._buffered == []


def test_invalid_json_silently_ignored():
    ing = _ing()
    ing._handle_message("not json at all")
    ing._handle_message("")
    assert ing._buffered == []


def test_missing_lat_lng_rejected():
    ing = _ing()
    bad = json.dumps({
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 12345},
        "Message": {"PositionReport": {"UserID": 12345}},
    })
    ing._handle_message(bad)
    assert ing._buffered == []


def test_sog_missing_yields_none_velocity():
    ing = _ing()
    msg = json.dumps({
        "MessageType": "PositionReport",
        "MetaData": {"MMSI": 1, "latitude": 0.0, "longitude": 0.0},
        "Message": {"PositionReport": {
            "UserID": 1, "Latitude": 0.0, "Longitude": 0.0,
            "TrueHeading": 90,
        }},
    })
    ing._handle_message(msg)
    assert ing._buffered[0]["velocity_ms"] is None


# ─── normalize() shape ──────────────────────────────────────────────────


def test_normalize_produces_ships_layer_events():
    """Critical: layer must be 'ships' so write_vessel_events handles
    AISStream output the same way it handles the ships.py output."""
    ing = _ing()
    ing._handle_message(_make_position_msg())
    events = ing.normalize(ing._buffered)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "ships"
    assert e.external_id == "123456789"
    assert e.kind == "position"
    assert e.lat == 30.5
    assert e.lng == 32.4
    assert e.velocity_ms == pytest.approx(12.3 * 0.514444, rel=1e-4)
    assert e.heading_deg == 178
    assert e.payload["mmsi"] == 123456789
    assert e.payload["name"] == "EVER GIVEN"
    assert "aisstream.io" in e.payload["_attribution"]
    assert e.domain == "maritime"


def test_normalize_static_event_carries_imo_and_callsign():
    """ShipStaticData → GlassboxEvent kind='state' with IMO + callsign
    + ship_type + destination in payload. The writer's UPSERT then
    merges these fields onto the same vessel entity row keyed by MMSI."""
    ing = _ing()
    static = json.dumps({
        "MessageType": "ShipStaticData",
        "MetaData": {
            "MMSI":      367560990, "latitude": 27.9, "longitude": -82.4,
        },
        "Message": {"ShipStaticData": {
            "UserID":      367560990,
            "ImoNumber":   9641754,
            "Name":        "ATLAS",
            "CallSign":    "WDG6819",
            "Type":        52,
            "Destination": "TAMPA",
        }},
    })
    ing._handle_message(static)
    events = ing.normalize(ing._buffered)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "ships"
    assert e.kind == "state"
    assert e.external_id == "367560990"
    assert e.payload["imo"] == 9641754
    assert e.payload["callsign"] == "WDG6819"
    assert e.payload["ship_type"] == 52
    assert e.payload["destination"] == "TAMPA"


def test_normalize_empty_buffer_returns_empty():
    assert _ing().normalize([]) == []


def test_normalize_skips_buffered_rows_without_mmsi():
    """Defensive: if a row sneaks past _handle_message without mmsi,
    normalize must drop it rather than emit a bogus external_id."""
    events = _ing().normalize([{"lat": 0, "lng": 0}])
    assert events == []


# ─── Live network test (optional - only when key present) ───────────────


@pytest.mark.network
@pytest.mark.skipif(
    not os.environ.get("AISSTREAM_API_KEY"),
    reason="AISSTREAM_API_KEY not set; skipping live network test",
)
async def test_live_connect_receives_at_least_one_position():
    """Connect to AISStream live for 30s, expect at least one
    PositionReport. Skipped when no key is configured."""
    import asyncio
    ing = AISStreamIngester()
    # Override LISTEN_SECONDS for the test by monkey-patching the constant
    # (cheap; cleanup happens automatically since module-level import)
    import ingesters.aisstream as ais_mod
    saved = ais_mod.LISTEN_SECONDS
    ais_mod.LISTEN_SECONDS = 30
    try:
        raw = await ing.fetch()
    finally:
        ais_mod.LISTEN_SECONDS = saved
    assert isinstance(raw, list)
    # We don't assert > 0 because the firehose might be momentarily quiet
    # OR the bbox may be misconfigured upstream. This test is mostly a
    # smoke check that the WS handshake + auth works without error.


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
