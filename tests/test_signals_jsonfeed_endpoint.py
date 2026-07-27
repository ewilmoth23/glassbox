"""
/api/v1/signals.json — JSON Feed v1.1 endpoint tests.

Asserts:
  - Spec compliance: required top-level fields per
    https://www.jsonfeed.org/version/1.1/ (version, title, items).
  - content-type is application/feed+json (the spec mandates this).
  - Items have id (stable), url (deep-link), title, content_html,
    date_published, tags, and our _glassbox extension carrying
    structured facts + authority.
  - min_severity floor + window_hours + limit work the same as the
    RSS endpoint.
  - Cache-Control public, max-age=60 so feed pollers don't hammer.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_signals_jsonfeed_endpoint.py -v
"""

from __future__ import annotations

import json as jsonlib
import sys
import uuid
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


_TEST_TAG = "signals_jsonfeed_endpoint_test"


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
                entity_id: uuid.UUID = None,
                extra_props: dict = None) -> uuid.UUID:
    eid = uuid.uuid4()
    if ts is None:
        ts = datetime.now(timezone.utc)
    props = {"_test_tag": _TEST_TAG, "external_id": f"json_test:{eid}"}
    if extra_props:
        props.update(extra_props)
    await execute(
        """
        INSERT INTO event
            (id, event_type, event_subtype, event_time,
             geom, severity, title, description, properties,
             entity_id,
             domain, decay_half_life_min)
        VALUES
            ($1::uuid, $2, NULL, $3,
             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
             $6, $7, '', $8::jsonb,
             $9::uuid,
             'geo', 60)
        """,
        eid, event_type, ts, lng, lat, severity, title,
        jsonlib.dumps(props), entity_id,
    )
    return eid


# ─── Spec compliance ────────────────────────────────────────────────────


async def test_jsonfeed_returns_correct_content_type_and_required_fields(_clean):
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="JSON-CT vessel",
                extra_props={"live_vessel_name": "JSONCT"})
    async with _client() as c:
        r = await c.get("/api/v1/signals.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/feed+json")
    assert "max-age=60" in r.headers.get("cache-control", "")
    body = jsonlib.loads(r.text)
    # Required JSON Feed v1.1 top-level fields
    assert body["version"] == "https://jsonfeed.org/version/1.1"
    assert body["title"]
    assert isinstance(body["items"], list)
    assert body["feed_url"].endswith("/api/v1/signals.json")


async def test_jsonfeed_item_carries_structured_glassbox_extension(_clean):
    eid = uuid.uuid4()
    await _seed(
        event_type="sanctioned_vessel_went_dark",
        title="JSONEXT vessel",
        entity_id=eid,
        extra_props={
            "live_vessel_name": "JSONEXT", "mmsi": "999999111",
            "hours_dark": 7.0, "match_kind": "name",
            "sanctioning_authority": "US Treasury OFAC",
            "sanctioned_canonical_id": "ofac_sdn:vessel:11111",
        },
    )
    async with _client() as c:
        r = await c.get("/api/v1/signals.json")
    body = jsonlib.loads(r.text)
    item = next(i for i in body["items"] if "JSONEXT" in i["title"])
    # Standard fields
    assert item["id"]
    assert item["url"].endswith(f"/api/v1/event/{item['id']}")
    assert item["date_published"]
    assert item["content_html"]
    assert "Sanctioned vessels gone dark" in item["tags"]
    # _glassbox extension
    g = item["_glassbox"]
    assert g["severity"] == "critical"
    assert g["category_id"] == "sanctioned_dark"
    assert g["event_type"] == "sanctioned_vessel_went_dark"
    assert g["facts"]["vessel"] == "JSONEXT"
    assert g["facts"]["mmsi"] == "999999111"
    assert abs(g["facts"]["hours_dark"] - 7.0) < 0.01
    assert g["authority"]["name"] == "US Treasury OFAC"
    assert g["authority"]["canonical_id"] == "ofac_sdn:vessel:11111"
    assert g["entity_id"] == str(eid)
    assert g["entity_url"].endswith(f"/entity/{eid}")


# ─── Filters mirror the RSS endpoint ────────────────────────────────────


async def test_jsonfeed_min_severity_critical_excludes_lower(_clean):
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="JCRIT-row",
                extra_props={"live_vessel_name": "JCRIT"})
    await _seed(event_type="military_aircraft_underway",
                title="JMED-row",
                extra_props={"callsign": "JMED"})
    async with _client() as c:
        r = await c.get("/api/v1/signals.json?min_severity=critical")
    titles = [it["title"] for it in jsonlib.loads(r.text)["items"]]
    assert any("JCRIT-row" in t for t in titles)
    assert not any("JMED-row" in t for t in titles)


async def test_jsonfeed_limit_and_window(_clean):
    for i in range(6):
        await _seed(event_type="sanctioned_vessel_went_dark",
                    title=f"JLIM{i}",
                    extra_props={"live_vessel_name": f"V{i}"})
    old = datetime.now(timezone.utc) - timedelta(hours=72)
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="JOLD-row", ts=old,
                extra_props={"live_vessel_name": "JOLD"})
    async with _client() as c:
        r = await c.get("/api/v1/signals.json?limit=4&window_hours=24"
                         "&min_severity=critical")
    items = jsonlib.loads(r.text)["items"]
    assert len(items) <= 4
    # Old row outside window must be excluded
    assert not any("JOLD-row" in i["title"] for i in items)


async def test_jsonfeed_validation():
    """Bad min_severity → 422; same regex as RSS."""
    async with _client() as c:
        r = await c.get("/api/v1/signals.json?min_severity=bogus")
    assert r.status_code == 422
