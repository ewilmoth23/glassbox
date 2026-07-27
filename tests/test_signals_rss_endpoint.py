"""
/api/v1/signals.rss endpoint tests.

Asserts:
  - Returns valid RSS 2.0 with the expected channel metadata.
  - Items round-trip through xml.etree.ElementTree (no malformed XML).
  - Each item has a stable GUID = the event UUID, a same-origin link
    to /api/v1/event/{id}, an RFC-822 pubDate, and a category.
  - The min_severity floor filters items correctly (critical excludes
    high+medium+low).
  - The limit param caps the number of <item> elements.
  - Cache-Control header is set so feed readers don't hammer.
  - Authority + facts make it into the description body.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_signals_rss_endpoint.py -v
"""

from __future__ import annotations

import json
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, execute  # noqa: E402
from api_v1 import build_router  # noqa: E402


_TEST_TAG = "signals_rss_endpoint_test"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM event WHERE properties->>'_test_tag' = $1",
            _TEST_TAG,
        )
    await _do()
    yield
    await _do()


def _client():
    app = FastAPI()
    app.include_router(build_router())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed(*, event_type: str, title: str = "test event",
                lat: float = 47.6, lng: float = -122.3,
                ts: datetime = None, severity: float = 5.0,
                extra_props: dict = None) -> uuid.UUID:
    eid = uuid.uuid4()
    if ts is None:
        ts = datetime.now(timezone.utc)
    props = {"_test_tag": _TEST_TAG, "external_id": f"rss_test:{eid}"}
    if extra_props:
        props.update(extra_props)
    await execute(
        """
        INSERT INTO event
            (id, event_type, event_subtype, event_time,
             geom, severity, title, description, properties,
             domain, decay_half_life_min)
        VALUES
            ($1::uuid, $2, NULL, $3,
             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
             $6, $7, '', $8::jsonb,
             'geo', 60)
        """,
        eid, event_type, ts, lng, lat, severity, title,
        json.dumps(props),
    )
    return eid


def _parse(body: str) -> ET.Element:
    """Parse the RSS body and return the <channel> element. Fails if
    the doc is malformed XML (bad escape, mismatched tags, etc.)."""
    root = ET.fromstring(body)
    assert root.tag == "rss"
    channel = root.find("channel")
    assert channel is not None
    return channel


# ─── Format + headers ────────────────────────────────────────────────────


async def test_rss_returns_valid_xml_and_correct_content_type(_clean):
    await _seed(
        event_type="sanctioned_vessel_went_dark",
        title="CRITICAL — sample dark vessel",
        extra_props={"live_vessel_name": "RSSTEST", "hours_dark": 6.0,
                     "sanctioning_authority": "US Treasury OFAC",
                     "sanctioned_canonical_id": "ofac_sdn:vessel:99999"},
    )
    async with _client() as c:
        r = await c.get("/api/v1/signals.rss")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/rss+xml")
    assert "max-age=60" in r.headers.get("cache-control", "")
    channel = _parse(r.text)
    assert channel.findtext("title").startswith("Glassbox")
    items = channel.findall("item")
    assert len(items) >= 1


async def test_rss_item_has_stable_guid_link_pubdate_category(_clean):
    eid = await _seed(
        event_type="sanctioned_vessel_went_dark",
        title="CRITICAL — RSSGUID vessel",
        extra_props={"live_vessel_name": "RSSGUID",
                     "sanctioning_authority": "US Treasury OFAC"},
    )
    async with _client() as c:
        r = await c.get("/api/v1/signals.rss")
    channel = _parse(r.text)
    item = next(i for i in channel.findall("item")
                if "RSSGUID" in (i.findtext("title") or ""))

    assert item.findtext("guid") == str(eid)
    assert item.findtext("link").endswith(f"/api/v1/event/{eid}")
    pub = item.findtext("pubDate")
    assert re.match(
        r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} "
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
        r"\d{4} \d{2}:\d{2}:\d{2} \+0000$",
        pub,
    ), f"bad RFC-822 pubDate: {pub!r}"
    cats = [c.text for c in item.findall("category")]
    assert "Sanctioned vessels gone dark" in cats


async def test_rss_description_carries_facts_and_authority(_clean):
    await _seed(
        event_type="sanctioned_vessel_went_dark",
        title="CRITICAL — RSSFACT vessel",
        extra_props={"live_vessel_name": "RSSFACT", "mmsi": "999999999",
                     "hours_dark": 6.5, "match_kind": "name",
                     "sanctioning_authority": "US Treasury OFAC",
                     "sanctioned_canonical_id": "ofac_sdn:vessel:88888"},
    )
    async with _client() as c:
        r = await c.get("/api/v1/signals.rss")
    body = r.text
    assert "vessel=RSSFACT" in body
    assert "mmsi=999999999" in body
    assert "Authority:" in body
    assert "US Treasury OFAC" in body
    assert "ofac_sdn:vessel:88888" in body


# ─── Filters ─────────────────────────────────────────────────────────────


async def test_min_severity_critical_excludes_high_medium_low(_clean):
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="CRIT-row",
                extra_props={"live_vessel_name": "CRITROW"})
    await _seed(event_type="sanctioned_vessel_underway",
                title="HIGH-row",
                extra_props={"live_vessel_name": "HIGHROW"})
    await _seed(event_type="military_aircraft_underway",
                title="MED-row",
                extra_props={"callsign": "MEDROW"})
    await _seed(event_type="loitering_detected",
                title="LOW-row",
                extra_props={"entity_name": "LOWROW"})

    async with _client() as c:
        r = await c.get("/api/v1/signals.rss?min_severity=critical")
    titles = "\n".join(i.findtext("title") or ""
                        for i in _parse(r.text).findall("item"))
    assert "CRIT-row" in titles
    assert "HIGH-row" not in titles
    assert "MED-row" not in titles
    assert "LOW-row" not in titles


async def test_min_severity_high_includes_critical_and_high(_clean):
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="CRIT2-row",
                extra_props={"live_vessel_name": "CRIT2"})
    await _seed(event_type="sanctioned_vessel_underway",
                title="HIGH2-row",
                extra_props={"live_vessel_name": "HIGH2"})
    await _seed(event_type="military_aircraft_underway",
                title="MED2-row",
                extra_props={"callsign": "MED2"})
    async with _client() as c:
        r = await c.get("/api/v1/signals.rss?min_severity=high")
    titles = "\n".join(i.findtext("title") or ""
                        for i in _parse(r.text).findall("item"))
    assert "CRIT2-row" in titles
    assert "HIGH2-row" in titles
    assert "MED2-row" not in titles


async def test_limit_caps_number_of_items(_clean):
    for i in range(8):
        await _seed(event_type="sanctioned_vessel_went_dark",
                    title=f"LIM{i}",
                    extra_props={"live_vessel_name": f"V{i}"})
    async with _client() as c:
        r = await c.get("/api/v1/signals.rss?limit=3&min_severity=critical")
    items = _parse(r.text).findall("item")
    assert len(items) <= 3


async def test_window_hours_excludes_old_rows(_clean):
    old = datetime.now(timezone.utc) - timedelta(hours=72)
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="OLD-row", ts=old,
                extra_props={"live_vessel_name": "OLDROW"})
    async with _client() as c:
        r = await c.get("/api/v1/signals.rss?window_hours=24&min_severity=critical")
    titles = "\n".join(i.findtext("title") or ""
                        for i in _parse(r.text).findall("item"))
    assert "OLD-row" not in titles


async def test_min_severity_validation():
    """Invalid min_severity → 422 from FastAPI's regex validator."""
    async with _client() as c:
        r = await c.get("/api/v1/signals.rss?min_severity=bogus")
    assert r.status_code == 422
