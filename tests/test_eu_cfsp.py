"""
EU CFSP ingester tests — parser + normalize() shape, plus tri-authority
write coexistence (EU + UK + OFAC for the same vessel produce 3 distinct
entity rows with no collision).

Parser tests use a hand-crafted XML fixture matching the real EU FSF
namespace, verified against the production file 2026-05-08 (5,996
sanction entities, 35 with IMO).

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_eu_cfsp.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetch, execute  # noqa: E402
from ingesters import eu_cfsp as eu_cfsp_mod  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from ingesters.eu_cfsp import (  # noqa: E402
    EuCfspIngester, _imo_for_entity, _primary_name, _programme, _NS,
)
from writers import write_sanction_entities  # noqa: E402


_FIXTURE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<export xmlns="http://eu.europa.ec/fpi/fsd/export" generationDate="2026-05-08T16:00:00Z">
  <sanctionEntity logicalId="100001" euReferenceNumber="EU.PRK.1">
    <subjectType code="enterprise" classificationCode="E" />
    <nameAlias wholeName="EU TEST GHOST" nameLanguage="en" />
    <nameAlias wholeName="유럽 테스트" nameLanguage="ko" />
    <identification identificationTypeCode="imo" number="9999101" />
    <regulation programme="PRK" entryIntoForceDate="2017-08-12" />
  </sanctionEntity>
  <sanctionEntity logicalId="100002" euReferenceNumber="EU.UKR.99">
    <subjectType code="enterprise" classificationCode="E" />
    <nameAlias wholeName="SOVCOMFLOT TEST TANKER" nameLanguage="en" />
    <identification identificationTypeCode="imo" number="9999102" />
    <regulation programme="UKR" entryIntoForceDate="2022-04-08" />
  </sanctionEntity>
  <sanctionEntity logicalId="100003" euReferenceNumber="EU.PRK.2">
    <subjectType code="enterprise" classificationCode="E" />
    <nameAlias wholeName="ENTERPRISE WITHOUT IMO" nameLanguage="en" />
    <identification identificationTypeCode="other" number="ABC" />
    <regulation programme="PRK" entryIntoForceDate="2017-08-12" />
  </sanctionEntity>
  <sanctionEntity logicalId="100004" euReferenceNumber="EU.PERSON.7">
    <subjectType code="person" classificationCode="P" />
    <nameAlias wholeName="A PERSON" nameLanguage="en" />
    <identification identificationTypeCode="imo" number="9999104" />
    <regulation programme="PRK" entryIntoForceDate="2017-08-12" />
  </sanctionEntity>
</export>
""".encode("utf-8")


def _make_ingester() -> EuCfspIngester:
    return EuCfspIngester(broadcaster=None, classifier=None, db_writer=None, logger=None)


def test_imo_extraction_handles_digits_only():
    """IMO numbers may have prefixes/spaces — extractor strips to digits."""
    import xml.etree.ElementTree as ET
    xml = """<sanctionEntity xmlns="http://eu.europa.ec/fpi/fsd/export">
      <identification identificationTypeCode="imo" number="IMO 9999101"/>
    </sanctionEntity>"""
    el = ET.fromstring(xml)
    assert _imo_for_entity(el) == 9999101


def test_imo_returns_none_when_absent():
    import xml.etree.ElementTree as ET
    xml = """<sanctionEntity xmlns="http://eu.europa.ec/fpi/fsd/export">
      <identification identificationTypeCode="other" number="ABC"/>
    </sanctionEntity>"""
    el = ET.fromstring(xml)
    assert _imo_for_entity(el) is None


def test_primary_name_prefers_latin_script():
    """When entity has multiple <nameAlias>, prefer the en/fr/de form."""
    import xml.etree.ElementTree as ET
    xml = """<sanctionEntity xmlns="http://eu.europa.ec/fpi/fsd/export">
      <nameAlias wholeName="유럽 테스트" nameLanguage="ko" />
      <nameAlias wholeName="EU TEST GHOST" nameLanguage="en" />
    </sanctionEntity>"""
    el = ET.fromstring(xml)
    assert _primary_name(el) == "EU TEST GHOST"


def test_parser_filters_to_enterprises_with_imo():
    """Fixture has 4 entities: 2 valid vessels, 1 enterprise without IMO,
    1 person (with IMO but wrong subjectType). Only the 2 valid pass."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(_FIXTURE_XML)
    ents = root.findall("ns:sanctionEntity", _NS)
    assert len(ents) == 4

    enterprise_with_imo = []
    for e in ents:
        sj = e.find("ns:subjectType", _NS)
        if sj is None or sj.attrib.get("code") != "enterprise":
            continue
        if _imo_for_entity(e) is None:
            continue
        enterprise_with_imo.append(e)
    assert len(enterprise_with_imo) == 2
    ids = {e.attrib.get("logicalId") for e in enterprise_with_imo}
    assert ids == {"100001", "100002"}


def test_normalize_emits_eu_cfsp_payload():
    """normalize() shape: canonical_id_type='eu_cfsp_id', authority='EU CFSP',
    regime mapped from programme code."""
    ing = _make_ingester()
    rows = [{
        "id":           "100002",
        "type":         "vessel",
        "display_name": "SOVCOMFLOT TEST TANKER",
        "imo":          9999102,
        "regime":       "Russia/Ukraine",
        "programme":    "UKR",
        "eu_ref":       "EU.UKR.99",
    }]
    events = ing.normalize(rows)
    assert len(events) == 1
    ev = events[0]
    assert ev.layer == "sanctions"
    assert ev.kind == "index"
    assert ev.external_id == "eu_cfsp:vessel:100002"
    assert ev.lat == 0.0 and ev.lng == 0.0
    assert ev.payload["sanctioning_authority"] == "EU CFSP"
    assert ev.payload["canonical_id_type"] == "eu_cfsp_id"
    assert ev.payload["fcra_safe"] is False
    assert ev.payload["imo"] == 9999102
    assert ev.payload["programme"] == "UKR"
    assert ev.payload["regime"] == "Russia/Ukraine"
    assert ev.payload["eu_ref"] == "EU.UKR.99"


# ─── Tri-authority write integration test ───────────────────────────────


_PFX_EU = "eu_cfsp:vessel:test_eu_"
_PFX_UK = "uk_ofsi:vessel:test_eu_"
_PFX_OFAC = "ofac_sdn:vessel:test_eu_"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1 OR canonical_id LIKE $2 OR canonical_id LIKE $3",
            "eu_cfsp:vessel:test_eu_%",
            "uk_ofsi:vessel:test_eu_%",
            "ofac_sdn:vessel:test_eu_%",
        )
    await _do()
    yield
    await _do()


def _eu_event(ext_id: str, name: str, imo: int) -> GlassboxEvent:
    return GlassboxEvent(
        layer="sanctions",
        external_id=ext_id,
        kind="index",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=10,
        source="EU CFSP Consolidated Sanctions List",
        payload={
            "type": "vessel",
            "display_name": name,
            "fcra_safe": False,
            "_attribution": "EU sanctions: European Commission FSF",
            "sanctioning_authority": "EU CFSP",
            "canonical_id_type": "eu_cfsp_id",
            "imo": imo,
            "programme": "UKR",
            "regime": "Russia/Ukraine",
            "eu_ref": "EU.UKR.99",
        },
        domain="entity",
        geocode_quality="needs_match",
        decay_half_life_min=10080,
    )


async def test_eu_writes_with_authority_specific_id_type(_clean):
    """EU CFSP events go to canonical_id_type='eu_cfsp_id', preserving
    programme + eu_ref + regime in entity properties."""
    ev = _eu_event(f"{_PFX_EU}V001", "EU TEST GHOST", 7777501)
    written = await write_sanction_entities([ev])
    assert written == 1

    rows = await fetch(
        "SELECT entity_type, canonical_id_type, canonical_id, properties "
        "FROM entity WHERE canonical_id = $1",
        f"{_PFX_EU}V001",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["entity_type"] == "sanctioned_vessel"
    assert r["canonical_id_type"] == "eu_cfsp_id"

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["sanctioning_authority"] == "EU CFSP"
    assert props["imo"] == 7777501
    assert props["programme"] == "UKR"
    assert props["eu_ref"] == "EU.UKR.99"
    assert props["regime"] == "Russia/Ukraine"


async def test_eu_uk_ofac_coexist_for_same_vessel(_clean):
    """A vessel listed by all three authorities → three entity rows.
    sanctions_match can then surface 'tri-jurisdictional' as the
    strongest possible signal."""
    eu = _eu_event(f"{_PFX_EU}V002", "TRI LISTED", 6666502)
    uk = GlassboxEvent(
        layer="sanctions",
        external_id=f"{_PFX_UK}V002",
        kind="index",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=10,
        source="UK OFSI Consolidated Sanctions List",
        payload={
            "type": "vessel",
            "display_name": "TRI LISTED",
            "fcra_safe": False,
            "_attribution": "Sanctions: UK OFSI (Crown Copyright, OGL v3.0)",
            "sanctioning_authority": "UK OFSI",
            "canonical_id_type": "uk_ofsi_id",
            "imo": 6666502,
        },
        domain="entity",
        geocode_quality="needs_match",
        decay_half_life_min=10080,
    )
    ofac = GlassboxEvent(
        layer="sanctions",
        external_id=f"{_PFX_OFAC}V002",
        kind="index",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=10,
        source="US Treasury OFAC SDN List",
        payload={
            "type": "vessel",
            "display_name": "TRI LISTED",
            "fcra_safe": False,
            "_attribution": "Sanctions: US Treasury OFAC",
            "imo": 6666502,
        },
        domain="entity",
        geocode_quality="needs_match",
        decay_half_life_min=10080,
    )
    written = await write_sanction_entities([eu, uk, ofac])
    assert written == 3

    rows = await fetch(
        "SELECT canonical_id_type FROM entity "
        "WHERE canonical_id IN ($1, $2, $3) ORDER BY canonical_id_type",
        f"{_PFX_EU}V002", f"{_PFX_UK}V002", f"{_PFX_OFAC}V002",
    )
    types = sorted(r["canonical_id_type"] for r in rows)
    assert types == ["eu_cfsp_id", "ofac_sdn_id", "uk_ofsi_id"]


async def test_eu_idempotent_re_emit(_clean):
    """Re-running same event = 0 new rows (ON CONFLICT updates last_seen)."""
    ev = _eu_event(f"{_PFX_EU}V003", "RE EMIT", 5555503)
    first = await write_sanction_entities([ev])
    second = await write_sanction_entities([ev])
    assert first == 1
    assert second == 0


# ─── Stale-cache fallback (upstream resilience) ─────────────────────────


class _FakeRequestInfo:
    """Minimal stand-in for aiohttp.RequestInfo so ClientResponseError can be
    str()'d in our warning path. The real one carries url/method/headers."""
    def __init__(self, url: str = "https://webgate.ec.europa.eu/test"):
        self.real_url = url
        self.url = url
        self.method = "GET"
        self.headers = {}


def test_fetch_falls_back_to_cache_on_upstream_500(monkeypatch, tmp_path):
    """When EU webgate returns 5xx, the ingester replays the on-disk
    cached XML (if < 7 days old) and tags emitted rows with
    served_from_cache=True. Mirrors the real outage 2026-05-09 →
    2026-05-10 where webgate.ec.europa.eu/fsd/fsf/... 500'd for 12h+.
    """
    # Redirect cache dir into a clean tmp location for this test.
    cache_dir = tmp_path / "eu_cfsp"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "last_good.xml"
    cache_file.write_bytes(_FIXTURE_XML)
    monkeypatch.setattr(eu_cfsp_mod, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(eu_cfsp_mod, "_CACHE_FILE", cache_file)

    # Force every aiohttp.get to raise a 500 like the real webgate outage.
    class _FakeResp:
        status = 500
        request_info = None
        history = ()
        headers = {}
        def raise_for_status(self):
            raise eu_cfsp_mod.aiohttp.ClientResponseError(
                request_info=_FakeRequestInfo(), history=(),
                status=500, message="Internal Server Error",
            )
        async def read(self):
            return b""
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass
        def get(self, *a, **kw):
            return _FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(eu_cfsp_mod.aiohttp, "ClientSession", _FakeSession)

    ing = _make_ingester()
    rows = asyncio.run(ing.fetch())
    # Fixture has 2 enterprise+IMO entities (100001, 100002) — both should pass.
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"100001", "100002"}
    # Every row must be tagged as cache-served + carry an age in hours.
    assert all(r.get("served_from_cache") is True for r in rows)
    assert all(isinstance(r.get("cache_age_hours"), float) for r in rows)
    # Age must be ~0h (we just wrote the cache).
    assert all(r["cache_age_hours"] < 0.1 for r in rows)


def test_fetch_does_not_serve_stale_cache(monkeypatch, tmp_path):
    """A cache > 7 days old must be refused — better to alert loud than
    quietly serve drifted sanctions data."""
    cache_dir = tmp_path / "eu_cfsp"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "last_good.xml"
    cache_file.write_bytes(_FIXTURE_XML)
    # Force the file mtime back in time past the 7-day threshold.
    eight_days_ago = time.time() - (8 * 24 * 3600)
    os.utime(cache_file, (eight_days_ago, eight_days_ago))

    monkeypatch.setattr(eu_cfsp_mod, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(eu_cfsp_mod, "_CACHE_FILE", cache_file)

    class _FakeResp:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def raise_for_status(self):
            raise eu_cfsp_mod.aiohttp.ClientResponseError(
                request_info=_FakeRequestInfo(), history=(),
                status=500, message="Internal Server Error",
            )
        async def read(self):
            return b""

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass
        def get(self, *a, **kw):
            return _FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(eu_cfsp_mod.aiohttp, "ClientSession", _FakeSession)

    ing = _make_ingester()
    with pytest.raises(eu_cfsp_mod.aiohttp.ClientResponseError):
        asyncio.run(ing.fetch())


def test_fetch_does_not_mask_4xx_with_cache(monkeypatch, tmp_path):
    """A 4xx (auth / URL change) is a configuration problem, not an outage —
    we propagate the error rather than serving cached data so the operator
    notices and fixes the underlying config."""
    cache_dir = tmp_path / "eu_cfsp"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "last_good.xml"
    cache_file.write_bytes(_FIXTURE_XML)
    monkeypatch.setattr(eu_cfsp_mod, "_CACHE_DIR", cache_dir)
    monkeypatch.setattr(eu_cfsp_mod, "_CACHE_FILE", cache_file)

    class _FakeResp:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def raise_for_status(self):
            raise eu_cfsp_mod.aiohttp.ClientResponseError(
                request_info=_FakeRequestInfo(), history=(),
                status=403, message="Forbidden",
            )
        async def read(self):
            return b""

    class _FakeSession:
        def __init__(self, *a, **kw):
            pass
        def get(self, *a, **kw):
            return _FakeResp()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(eu_cfsp_mod.aiohttp, "ClientSession", _FakeSession)

    ing = _make_ingester()
    with pytest.raises(eu_cfsp_mod.aiohttp.ClientResponseError):
        asyncio.run(ing.fetch())


def test_normalize_propagates_cache_flag_to_payload():
    """When fetch() tags a row as cache-served, normalize() must surface
    that as payload.served_from_cache + payload.cache_age_hours so
    downstream readers can flag staleness."""
    ing = _make_ingester()
    rows = [{
        "id":                 "100002",
        "type":               "vessel",
        "display_name":       "SOVCOMFLOT TEST TANKER",
        "imo":                9999102,
        "regime":             "Russia/Ukraine",
        "programme":          "UKR",
        "eu_ref":             "EU.UKR.99",
        "served_from_cache":  True,
        "cache_age_hours":    3.5,
    }]
    events = ing.normalize(rows)
    assert len(events) == 1
    assert events[0].payload["served_from_cache"] is True
    assert events[0].payload["cache_age_hours"] == 3.5
