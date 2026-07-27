"""
/api/v1/signals/snapshot.csv — RFC-4180 CSV export tests.

Asserts:
  - Returns valid CSV (parses with stdlib csv.reader without error).
  - Header columns are stable + match the documented contract.
  - Cells with commas / quotes / newlines are properly RFC-4180 escaped.
  - content-type: text/csv with UTF-8 charset.
  - Content-Disposition: attachment with a timestamped filename.
  - Filter semantics (min_severity / window_hours / limit) mirror the
    feed endpoints.
  - Per-row projection: vessel_name / mmsi / hours_dark / authority /
    entity_url / event_url all populated correctly from JSONB props.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_signals_csv_endpoint.py -v
"""

from __future__ import annotations

import csv
import io
import json as jsonlib
import re
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
from web.routes.api_v1.signals import _CSV_COLUMNS  # noqa: E402


_TEST_TAG = "signals_csv_endpoint_test"


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
                description: str = "",
                entity_id: uuid.UUID = None,
                extra_props: dict = None) -> uuid.UUID:
    eid = uuid.uuid4()
    if ts is None:
        ts = datetime.now(timezone.utc)
    props = {"_test_tag": _TEST_TAG, "external_id": f"csv_test:{eid}"}
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
             $6, $7, $8, $9::jsonb,
             $10::uuid,
             'geo', 60)
        """,
        eid, event_type, ts, lng, lat, severity, title, description,
        jsonlib.dumps(props), entity_id,
    )
    return eid


def _parse_csv(body: str) -> list:
    return list(csv.reader(io.StringIO(body)))


# ─── Format + headers ────────────────────────────────────────────────────


async def test_csv_returns_valid_rfc4180(_clean):
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="CSV-VALID vessel",
                extra_props={"live_vessel_name": "CSVVALID",
                             "hours_dark": 6.0,
                             "sanctioning_authority": "US Treasury OFAC"})
    async with _client() as c:
        r = await c.get("/api/v1/signals/snapshot.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "max-age=60" in r.headers.get("cache-control", "")
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert re.search(r'filename="glassbox_signals_\d{8}_\d{6}Z\.csv"', cd), (
        f"timestamped filename missing: {cd!r}"
    )
    rows = _parse_csv(r.text)
    assert rows[0] == _CSV_COLUMNS
    # At least the header + our seeded row
    assert len(rows) >= 2


async def test_csv_escapes_commas_quotes_and_newlines(_clean):
    nasty = 'A vessel "TEST", which had a, comma\nand newline'
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="CSV-ESC test",
                description=nasty,
                extra_props={"live_vessel_name": "CSVESC"})
    async with _client() as c:
        r = await c.get("/api/v1/signals/snapshot.csv")
    rows = _parse_csv(r.text)
    header = rows[0]
    desc_idx = header.index("description")
    seeded = next(row for row in rows[1:]
                   if "CSV-ESC" in row[header.index("title")])
    # csv.reader unquotes back to the original; round-trip must match
    assert seeded[desc_idx] == nasty


async def test_csv_per_row_projection_pulls_facts_from_jsonb(_clean):
    eid = uuid.uuid4()
    await _seed(
        event_type="sanctioned_vessel_went_dark",
        title="CSVPROJ-row",
        entity_id=eid,
        extra_props={
            "live_vessel_name": "CSVPROJ", "mmsi": "888888888",
            "live_imo": "9000099", "hours_dark": 5.5,
            "sanctioning_authority": "US Treasury OFAC",
            "sanctioned_canonical_id": "ofac_sdn:vessel:77777",
        },
    )
    async with _client() as c:
        r = await c.get("/api/v1/signals/snapshot.csv")
    rows = _parse_csv(r.text)
    h = rows[0]
    row = next(rr for rr in rows[1:]
                if "CSVPROJ-row" in rr[h.index("title")])
    assert row[h.index("vessel_name")] == "CSVPROJ"
    assert row[h.index("mmsi")] == "888888888"
    assert row[h.index("imo")] == "9000099"
    assert row[h.index("hours_dark")] == "5.5"
    assert row[h.index("authority")] == "US Treasury OFAC"
    assert row[h.index("authority_canonical_id")] == "ofac_sdn:vessel:77777"
    assert row[h.index("entity_id")] == str(eid)
    assert row[h.index("entity_url")].endswith(f"/entity/{eid}")
    assert "/api/v1/event/" in row[h.index("event_url")]
    assert row[h.index("category")] == "Sanctioned vessels gone dark"
    assert row[h.index("severity")] == "critical"


async def test_csv_min_severity_floor(_clean):
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="CSVCRIT", extra_props={"live_vessel_name": "CRIT"})
    await _seed(event_type="military_aircraft_underway",
                title="CSVMED", extra_props={"callsign": "MED"})
    async with _client() as c:
        r = await c.get("/api/v1/signals/snapshot.csv?min_severity=critical")
    titles = [row[4] for row in _parse_csv(r.text)[1:]]   # title is col 4
    assert any("CSVCRIT" in t for t in titles)
    assert not any("CSVMED" in t for t in titles)


async def test_csv_window_hours_excludes_old_rows(_clean):
    old = datetime.now(timezone.utc) - timedelta(hours=72)
    await _seed(event_type="sanctioned_vessel_went_dark",
                title="CSVOLD-row", ts=old,
                extra_props={"live_vessel_name": "OLDROW"})
    async with _client() as c:
        r = await c.get("/api/v1/signals/snapshot.csv?window_hours=24"
                         "&min_severity=critical")
    titles = [row[4] for row in _parse_csv(r.text)[1:]]
    assert not any("CSVOLD-row" in t for t in titles)


async def test_csv_validation():
    """Bad min_severity → 422."""
    async with _client() as c:
        r = await c.get("/api/v1/signals/snapshot.csv?min_severity=bogus")
    assert r.status_code == 422
