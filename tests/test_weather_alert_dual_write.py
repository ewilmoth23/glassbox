"""
Phase 2F — noaa_nws.py dual-write to the `event` hypertable.

Asserts:
  - GlassboxEvent (layer='weather_alerts') in → event row with event_type='noaa_alert'
  - event_subtype = the alert kind ('Tornado Warning', 'Flash Flood Warning', etc.)
  - title = headline; description = area_desc
  - properties carries event/severity_raw/urgency/certainty/area_desc/sender_name/effective/expires/instruction
  - Idempotent re-runs (NOAA alerts have stable IDs across emissions)
  - Multiple distinct alerts → all persist
  - Empty input → no-op
  - End-to-end via NoaaNwsIngester with db_writer hook (mocked HTTP)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_weather_alert_dual_write.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from ingesters.noaa_nws import NoaaNwsIngester  # noqa: E402
from writers import write_weather_alert_events  # noqa: E402


TEST_PREFIX = "test08"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_alerts():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type = 'noaa_alert' "
            "AND properties->>'external_id' LIKE $1",
            f"%{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _alert_event(external_id: str, *, lat: float = 35.0, lng: float = -97.0,
                 alert_kind: str = "Tornado Warning",
                 headline: str = "Tornado Warning issued for Grady County",
                 area_desc: str = "Grady County, OK",
                 severity: int = 8,
                 urgency: str = "Immediate",
                 certainty: str = "Observed") -> GlassboxEvent:
    """Build a GlassboxEvent in the shape NoaaNwsIngester.normalize() emits."""
    payload = {
        "event": alert_kind,
        "headline": headline,
        "severity_raw": "Severe",
        "urgency": urgency,
        "certainty": certainty,
        "area_desc": area_desc,
        "sender_name": "NWS Norman OK",
        "effective": "2026-05-08T00:00:00Z",
        "expires": "2026-05-08T03:00:00Z",
        "instruction": "Take shelter immediately.",
        "_attribution": "NOAA NWS (US public domain)",
    }
    return GlassboxEvent(
        layer="weather_alerts",
        external_id=external_id,
        kind="alert",
        lat=lat,
        lng=lng,
        ts="2026-05-08T00:00:00+00:00",   # alert sent time
        severity=severity,
        source="NOAA National Weather Service (api.weather.gov)",
        payload=payload,
        domain="geo",
        geocode_quality="polygon_centroid",
        decay_half_life_min=30,
    )


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_weather_alert_creates_event_row(_clean_test_alerts):
    ev = _alert_event(f"urn:oid:2.49.0.1.840.0.{TEST_PREFIX}_tor_1")
    n = await write_weather_alert_events([ev])
    assert n == 1

    row = await fetch(
        "SELECT event_type, event_subtype, severity, "
        "       title, description, "
        "       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "       properties, domain, decay_half_life_min "
        "FROM event WHERE properties->>'external_id' = $1",
        f"urn:oid:2.49.0.1.840.0.{TEST_PREFIX}_tor_1",
    )
    assert len(row) == 1
    r = row[0]
    assert r["event_type"] == "noaa_alert"
    assert r["event_subtype"] == "Tornado Warning"
    assert r["title"] == "Tornado Warning issued for Grady County"
    assert r["description"] == "Grady County, OK"
    assert abs(r["lat"] - 35.0) < 1e-4
    assert abs(r["lng"] - (-97.0)) < 1e-4
    assert r["severity"] == pytest.approx(8.0)
    assert r["domain"] == "geo"
    assert r["decay_half_life_min"] == 30

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["event"] == "Tornado Warning"
    assert props["urgency"] == "Immediate"
    assert props["certainty"] == "Observed"
    assert props["sender_name"] == "NWS Norman OK"
    assert props["instruction"].startswith("Take shelter")


async def test_write_weather_alert_is_idempotent(_clean_test_alerts):
    ev = _alert_event(f"urn:{TEST_PREFIX}_dedup")
    n1 = await write_weather_alert_events([ev])
    assert n1 == 1
    n2 = await write_weather_alert_events([ev])
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"urn:{TEST_PREFIX}_dedup",
    )
    assert total == 1


async def test_write_weather_alert_multiple_distinct(_clean_test_alerts):
    evs = [
        _alert_event(f"urn:{TEST_PREFIX}_multi{i}", lat=35 + i * 0.1, lng=-97 + i * 0.1)
        for i in range(5)
    ]
    n = await write_weather_alert_events(evs)
    assert n == 5

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' LIKE $1",
        f"urn:{TEST_PREFIX}_multi%",
    )
    assert total == 5


async def test_write_weather_alert_zero_events_is_noop():
    n = await write_weather_alert_events([])
    assert n == 0


async def test_write_weather_alert_skips_non_weather_layer(_clean_test_alerts):
    bogus = [GlassboxEvent(
        layer="planes",
        external_id=f"{TEST_PREFIX}_wrong",
        kind="position",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
    )]
    n = await write_weather_alert_events(bogus)
    assert n == 0


async def test_write_weather_alert_skips_empty_external_id(_clean_test_alerts):
    ev = _alert_event(f"{TEST_PREFIX}_emptied")
    ev.external_id = ""
    n = await write_weather_alert_events([ev])
    assert n == 0


async def test_write_weather_alert_distinct_subtypes(_clean_test_alerts):
    """Different alert kinds → different event_subtype values."""
    evs = [
        _alert_event(f"urn:{TEST_PREFIX}_tor", alert_kind="Tornado Warning"),
        _alert_event(f"urn:{TEST_PREFIX}_flo", alert_kind="Flash Flood Warning",
                     headline="Flash Flood for X", area_desc="X County"),
        _alert_event(f"urn:{TEST_PREFIX}_hur", alert_kind="Hurricane Warning",
                     headline="Hurricane for Y", area_desc="Y Parish"),
    ]
    await write_weather_alert_events(evs)

    rows = await fetch(
        "SELECT event_subtype FROM event WHERE properties->>'external_id' LIKE $1 "
        "ORDER BY event_subtype",
        f"urn:{TEST_PREFIX}_%",
    )
    seen = sorted(r["event_subtype"] for r in rows)
    assert seen == ["Flash Flood Warning", "Hurricane Warning", "Tornado Warning"]


async def test_full_noaa_nws_cycle_with_db_writer_hook(_clean_test_alerts):
    """End-to-end: NoaaNwsIngester with db_writer hook, mocked fetch().
    Confirms cycle() wiring."""
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_weather_alert_events(events)

    broadcast_log = []
    def noop_b(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = NoaaNwsIngester(broadcaster=noop_b, db_writer=capture_writer)

    # Mock fetch() — supplies one NWS-shaped alert feature
    fake_raw = [{
        "id": f"urn:oid:2.49.0.1.840.0.{TEST_PREFIX}_cycle",
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-97.5, 34.5], [-96.5, 34.5], [-96.5, 35.5],
                [-97.5, 35.5], [-97.5, 34.5],
            ]],
        },
        "properties": {
            "id": f"urn:oid:2.49.0.1.840.0.{TEST_PREFIX}_cycle",
            "event": "Tornado Warning",
            "headline": "Tornado Warning for Test County",
            "severity": "Severe",
            "urgency": "Immediate",
            "certainty": "Observed",
            "areaDesc": "Test County, OK",
            "senderName": "NWS Test",
            "effective": "2026-05-08T00:00:00Z",
            "expires": "2026-05-08T03:00:00Z",
            "instruction": "Take shelter.",
            "sent": "2026-05-08T00:00:00Z",
        },
    }]

    async def fake_fetch():
        return fake_raw

    with patch.object(ingester, "fetch", side_effect=fake_fetch):
        broadcast_count = await ingester.cycle()

    assert broadcast_count == 1
    assert len(db_writer_calls) == 1

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"urn:oid:2.49.0.1.840.0.{TEST_PREFIX}_cycle",
    )
    assert total == 1

    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
