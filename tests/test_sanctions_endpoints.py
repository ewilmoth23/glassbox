"""
/api/v1/sanctions/{breakdown,by-regime} endpoint tests.

These endpoints query the live entity table for sanctioned-vessel +
sanctioned-aircraft rows and aggregate by authority + regime, or list
matching entities for a given regime. Tests use the production DB and
seed authority-prefixed sentinel rows for deterministic cleanup.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_sanctions_endpoints.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, execute, acquire  # noqa: E402
from api_v1 import build_router  # noqa: E402


_PFX = "test_se_"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1",
            f"%{_PFX}%",
        )
    await _do()
    yield
    await _do()


async def _seed(*, ext_id: str, cid_type: str, name: str, regime: str,
                authority: str, etype: str = "sanctioned_vessel") -> None:
    payload = {
        "type": "vessel" if etype == "sanctioned_vessel" else "aircraft",
        "fcra_safe": False,
        "sanctioning_authority": authority,
        "regime": regime,
    }
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity
                (entity_type, canonical_id_type, canonical_id,
                 display_name, properties, last_seen, updated_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, NOW(), NOW())
            """,
            etype, cid_type, ext_id, name, json.dumps(payload),
        )


def _client():
    app = FastAPI()
    app.include_router(build_router())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_breakdown_aggregates_per_authority_and_regime(_clean):
    """Seed 2 OFAC + 1 UK + 1 EU sentinel rows; breakdown groups
    them correctly with vessel/aircraft totals."""
    await _seed(ext_id=f"ofac_sdn:vessel:{_PFX}001",
                 cid_type="ofac_sdn_id", name="OFAC RUSS A",
                 regime="RUSSIA", authority="US Treasury OFAC")
    await _seed(ext_id=f"ofac_sdn:vessel:{_PFX}002",
                 cid_type="ofac_sdn_id", name="OFAC RUSS B",
                 regime="RUSSIA", authority="US Treasury OFAC")
    await _seed(ext_id=f"uk_ofsi:vessel:{_PFX}003",
                 cid_type="uk_ofsi_id", name="UK NK",
                 regime="Democratic People's Republic of Korea",
                 authority="UK OFSI")
    await _seed(ext_id=f"eu_cfsp:vessel:{_PFX}004",
                 cid_type="eu_cfsp_id", name="EU UKR",
                 regime="Russia/Ukraine", authority="EU CFSP")

    async with _client() as c:
        r = await c.get("/api/v1/sanctions/breakdown")
    assert r.status_code == 200
    data = r.json()
    # Totals include real production rows + our 4 sentinels
    assert data["totals"]["vessels"] >= 4
    auths = {a["authority"]: a for a in data["authorities"]}
    assert "US Treasury OFAC" in auths
    assert "UK OFSI" in auths
    assert "EU CFSP" in auths
    # OFAC has the most rows (it's the production authority + 2 sentinels)
    ofac = auths["US Treasury OFAC"]
    assert ofac["canonical_id_type"] == "ofac_sdn_id"
    assert ofac["totals"]["sanctioned_vessel"] >= 2


async def test_by_regime_lists_matching_entities(_clean):
    """RUSSIA query returns the 2 OFAC sentinels (and any production rows)."""
    await _seed(ext_id=f"ofac_sdn:vessel:{_PFX}010",
                 cid_type="ofac_sdn_id", name="SENTINEL RUSS 1",
                 regime="RUSSIA", authority="US Treasury OFAC")
    await _seed(ext_id=f"ofac_sdn:vessel:{_PFX}011",
                 cid_type="ofac_sdn_id", name="SENTINEL RUSS 2",
                 regime="RUSSIA", authority="US Treasury OFAC")

    async with _client() as c:
        r = await c.get("/api/v1/sanctions/by-regime?regime=RUSSIA&limit=2000")
    assert r.status_code == 200
    data = r.json()
    assert data["regime"] == "RUSSIA"
    sentinel_names = {"SENTINEL RUSS 1", "SENTINEL RUSS 2"}
    found_names = {e["display_name"] for e in data["entities"]}
    assert sentinel_names.issubset(found_names)


async def test_by_regime_is_case_insensitive(_clean):
    """Lowercase / mixed-case input matches uppercase regime values
    in the DB (stored as 'RUSSIA', queried as 'russia')."""
    await _seed(ext_id=f"ofac_sdn:vessel:{_PFX}020",
                 cid_type="ofac_sdn_id", name="CASE TEST",
                 regime="RUSSIA", authority="US Treasury OFAC")

    async with _client() as c:
        r1 = await c.get("/api/v1/sanctions/by-regime?regime=russia&limit=500")
        r2 = await c.get("/api/v1/sanctions/by-regime?regime=Russia&limit=500")
    assert r1.status_code == 200 and r2.status_code == 200
    n1 = sum(1 for e in r1.json()["entities"] if e["display_name"] == "CASE TEST")
    n2 = sum(1 for e in r2.json()["entities"] if e["display_name"] == "CASE TEST")
    assert n1 == 1 and n2 == 1


async def test_by_regime_returns_empty_for_unknown_regime(_clean):
    async with _client() as c:
        r = await c.get("/api/v1/sanctions/by-regime?regime=NONEXISTENT_REGIME_XYZ_TEST&limit=10")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["entities"] == []


async def test_by_regime_requires_regime_param():
    """Missing `regime` query param → 422 (FastAPI validation)."""
    async with _client() as c:
        r = await c.get("/api/v1/sanctions/by-regime")
    assert r.status_code == 422


# ─── /sanctions/search tests ───────────────────────────────────────────


async def test_search_finds_by_imo(_clean):
    """All-digit query of length ≥ 6 matches properties.imo precisely."""
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity
                (entity_type, canonical_id_type, canonical_id,
                 display_name, properties, last_seen, updated_at)
            VALUES ('sanctioned_vessel', 'ofac_sdn_id', $1, 'IMO TEST GHOST',
                    $2::jsonb, NOW(), NOW())
            """,
            f"ofac_sdn:vessel:{_PFX}IMO_001",
            json.dumps({"type": "vessel", "fcra_safe": False, "imo": 7777810,
                         "sanctioning_authority": "US Treasury OFAC"}),
        )
    async with _client() as c:
        r = await c.get("/api/v1/sanctions/search?q=7777810")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 1
    # First result should be our IMO match
    found = [x for x in data["results"] if x["display_name"] == "IMO TEST GHOST"]
    assert len(found) == 1
    assert found[0]["match_kind"] == "imo_match"


async def test_search_fuzzy_name(_clean):
    """A partial name like 'GHOSTLI' matches 'TEST GHOSTLINER' via trigram."""
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO entity
                (entity_type, canonical_id_type, canonical_id,
                 display_name, properties, last_seen, updated_at)
            VALUES ('sanctioned_vessel', 'eu_cfsp_id', $1, 'TEST GHOSTLINER',
                    $2::jsonb, NOW(), NOW())
            """,
            f"eu_cfsp:vessel:{_PFX}NAME_001",
            json.dumps({"type": "vessel", "fcra_safe": False,
                         "sanctioning_authority": "EU CFSP"}),
        )
    async with _client() as c:
        r = await c.get("/api/v1/sanctions/search?q=GHOSTLI&limit=10")
    assert r.status_code == 200
    data = r.json()
    found = [x for x in data["results"] if x["display_name"] == "TEST GHOSTLINER"]
    assert len(found) == 1
    assert found[0]["match_kind"] in ("name_fuzzy", "name_substring")
    assert found[0]["canonical_id_type"] == "eu_cfsp_id"


async def test_search_minimum_length_validation():
    """q < 2 chars → 422."""
    async with _client() as c:
        r = await c.get("/api/v1/sanctions/search?q=A")
    assert r.status_code == 422


async def test_search_returns_empty_for_unmatched_query(_clean):
    async with _client() as c:
        r = await c.get("/api/v1/sanctions/search?q=ZZZZQQQQNONEXISTENT")
    assert r.status_code == 200
    assert r.json()["count"] == 0
