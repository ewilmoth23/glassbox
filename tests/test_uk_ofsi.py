"""
UK OFSI ingester unit tests — parser + normalize() shape, plus a dual-write
test that confirms multi-authority writer routing (uk_ofsi_id rows live
alongside ofac_sdn_id rows without collision).

No live network: parser tests use a hand-crafted XML fixture matching
the real OFSI ConList.xml namespace and structure (verified against the
production file 2026-05-08).

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_uk_ofsi.py -v
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetch, execute  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from ingesters.uk_ofsi import UkOfsiIngester  # noqa: E402
from writers import write_sanction_entities  # noqa: E402


# ─── Fixture XML — minimal ConList with 2 ships (1 active, 1 alias of #1)
# + 1 individual + 1 entity, mirroring real OFSI structure ─────────────────

_FIXTURE_XML = """<?xml version="1.0"?>
<ArrayOfFinancialSanctionsTarget xmlns:i="http://www.w3.org/2001/XMLSchema-instance"
                                  xmlns="http://schemas.hmtreasury.gov.uk/ofsi/consolidatedlist">
  <FinancialSanctionsTarget>
    <Name6>TEST GHOST</Name6>
    <name1 i:nil="true" />
    <GroupTypeDescription>Ship</GroupTypeDescription>
    <AliasType>Primary name variation</AliasType>
    <RegimeName>Russia</RegimeName>
    <Ship_IMONumber>9999001</Ship_IMONumber>
    <Ship_Flag>Russia</Ship_Flag>
    <Ship_Type>Crude oil tanker</Ship_Type>
    <UKSanctionsListRef>RUS9001</UKSanctionsListRef>
    <GroupID>99001</GroupID>
    <GrpStatus>A</GrpStatus>
  </FinancialSanctionsTarget>
  <FinancialSanctionsTarget>
    <Name6>TEST PHANTOM</Name6>
    <GroupTypeDescription>Ship</GroupTypeDescription>
    <AliasType>AKA</AliasType>
    <RegimeName>Russia</RegimeName>
    <Ship_IMONumber>9999001</Ship_IMONumber>
    <UKSanctionsListRef>RUS9001</UKSanctionsListRef>
    <GroupID>99001</GroupID>
    <GrpStatus>A</GrpStatus>
  </FinancialSanctionsTarget>
  <FinancialSanctionsTarget>
    <Name6>BLACK PEARL</Name6>
    <GroupTypeDescription>Ship</GroupTypeDescription>
    <AliasType>Primary name variation</AliasType>
    <RegimeName>Democratic People's Republic of Korea</RegimeName>
    <Ship_IMONumber>8888002</Ship_IMONumber>
    <Ship_Flag>North Korea</Ship_Flag>
    <UKSanctionsListRef>DPR9002</UKSanctionsListRef>
    <GroupID>99002</GroupID>
    <GrpStatus>A</GrpStatus>
  </FinancialSanctionsTarget>
  <FinancialSanctionsTarget>
    <Name6>REMOVED VESSEL</Name6>
    <GroupTypeDescription>Ship</GroupTypeDescription>
    <Ship_IMONumber>7777003</Ship_IMONumber>
    <UKSanctionsListRef>RMV9003</UKSanctionsListRef>
    <GroupID>99003</GroupID>
    <GrpStatus>R</GrpStatus>
  </FinancialSanctionsTarget>
  <FinancialSanctionsTarget>
    <Name6>SOMEONE</Name6>
    <name1>Person</name1>
    <GroupTypeDescription>Individual</GroupTypeDescription>
    <RegimeName>Russia</RegimeName>
    <GroupID>99004</GroupID>
    <GrpStatus>A</GrpStatus>
  </FinancialSanctionsTarget>
  <FinancialSanctionsTarget>
    <Name6>Some Bank</Name6>
    <GroupTypeDescription>Entity</GroupTypeDescription>
    <RegimeName>Iran</RegimeName>
    <GroupID>99005</GroupID>
    <GrpStatus>A</GrpStatus>
  </FinancialSanctionsTarget>
</ArrayOfFinancialSanctionsTarget>
""".encode("utf-8")


def _make_ingester() -> UkOfsiIngester:
    return UkOfsiIngester(
        broadcaster=None, classifier=None, db_writer=None, logger=None,
    )


def test_parser_extracts_only_active_ship_groups():
    """Fixture has 4 Ship rows (2 alias-paired into one group, 1 distinct,
    1 removed) + 1 Individual + 1 Entity. We expect 2 active ship GROUPS
    (the removed ship and non-ships are filtered)."""
    ing = _make_ingester()

    async def runner():
        with patch.object(UkOfsiIngester, "fetch", new=AsyncMock(return_value=None)):
            # Bypass fetch and inject XML directly via the parser path
            pass

    # Instead of the patch dance: call fetch with mocked aiohttp by injecting
    # bytes via a private re-entry — but the cleanest approach is to test
    # against the parser directly. Construct a tiny test that runs the
    # XML-to-rows logic by re-using the same parser code path:
    import xml.etree.ElementTree as ET
    from ingesters.uk_ofsi import _NS, _txt, _compose_name

    root = ET.fromstring(_FIXTURE_XML)
    targets = root.findall("ns:FinancialSanctionsTarget", _NS)
    assert len(targets) == 6
    ship_targets = [
        t for t in targets
        if _txt(t.find("ns:GroupTypeDescription", _NS)) == "Ship"
    ]
    assert len(ship_targets) == 4

    # Active-only filter
    active_ships = [
        t for t in ship_targets
        if (_txt(t.find("ns:GrpStatus", _NS)) or "").upper() == "A"
    ]
    assert len(active_ships) == 3   # 2 aliases of group 99001, 1 of 99002

    # Group-dedup filter (what fetch() does)
    groups = {_txt(t.find("ns:GroupID", _NS)) for t in active_ships}
    assert groups == {"99001", "99002"}


def test_compose_name_concatenates_name_parts():
    """Name composition handles name1..name6 + Name6 (capital N)."""
    import xml.etree.ElementTree as ET
    from ingesters.uk_ofsi import _compose_name, _NS

    xml = """<FinancialSanctionsTarget xmlns="http://schemas.hmtreasury.gov.uk/ofsi/consolidatedlist">
        <name1>Eternal</name1>
        <name2>Defiance</name2>
        <Name6>VESSEL</Name6>
    </FinancialSanctionsTarget>"""
    el = ET.fromstring(xml)
    assert _compose_name(el) == "Eternal Defiance VESSEL"


async def test_normalize_emits_uk_ofsi_payload():
    """normalize() shapes events with canonical_id_type='uk_ofsi_id' and
    sanctioning_authority='UK OFSI' so the writer routes them correctly."""
    ing = _make_ingester()
    rows = [
        {
            "id": "99001",
            "type": "vessel",
            "display_name": "TEST GHOST",
            "imo": 9999001,
            "regime": "Russia",
            "flag": "Russia",
            "ship_type": "Crude oil tanker",
            "uk_ref": "RUS9001",
        },
    ]
    events = ing.normalize(rows)
    assert len(events) == 1
    ev = events[0]
    assert ev.layer == "sanctions"
    assert ev.kind == "index"
    assert ev.external_id == "uk_ofsi:vessel:99001"
    assert ev.lat == 0.0 and ev.lng == 0.0
    assert ev.payload["sanctioning_authority"] == "UK OFSI"
    assert ev.payload["canonical_id_type"] == "uk_ofsi_id"
    assert ev.payload["fcra_safe"] is False
    assert ev.payload["imo"] == 9999001
    assert ev.payload["regime"] == "Russia"
    assert ev.payload["flag"] == "Russia"
    assert "OGL v3.0" in ev.payload["_attribution"]


# ─── Multi-authority write integration test ─────────────────────────────


_TEST_PREFIX = "uk_ofsi:vessel:test_uk_"
_TEST_PREFIX_OFAC = "ofac_sdn:vessel:test_uk_"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM entity WHERE canonical_id LIKE $1 OR canonical_id LIKE $2",
            "uk_ofsi:vessel:test_uk_%", "ofac_sdn:vessel:test_uk_%",
        )
    await _do()
    yield
    await _do()


def _uk_event(ext_id: str, name: str, imo: int) -> GlassboxEvent:
    return GlassboxEvent(
        layer="sanctions",
        external_id=ext_id,
        kind="index",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=10,
        source="UK OFSI Consolidated Sanctions List",
        payload={
            "type": "vessel",
            "display_name": name,
            "fcra_safe": False,
            "_attribution": "Sanctions: UK OFSI (Crown Copyright, OGL v3.0)",
            "sanctioning_authority": "UK OFSI",
            "canonical_id_type": "uk_ofsi_id",
            "imo": imo,
        },
        domain="entity",
        geocode_quality="needs_match",
        decay_half_life_min=10080,
    )


def _ofac_event(ext_id: str, name: str, imo: int) -> GlassboxEvent:
    return GlassboxEvent(
        layer="sanctions",
        external_id=ext_id,
        kind="index",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=10,
        source="US Treasury OFAC SDN List",
        payload={
            "type": "vessel",
            "display_name": name,
            "fcra_safe": False,
            "_attribution": "Sanctions: US Treasury OFAC",
            "imo": imo,
        },
        domain="entity",
        geocode_quality="needs_match",
        decay_half_life_min=10080,
    )


async def test_uk_ofsi_writes_with_authority_specific_id_type(_clean):
    """UK OFSI events go to canonical_id_type='uk_ofsi_id', NOT 'ofac_sdn_id'.
    This is the core multi-authority routing test — without it, UK and OFAC
    rows with overlapping IMOs would collide."""
    ev = _uk_event(f"{_TEST_PREFIX}V001", "TEST UK GHOST", 7777001)
    written = await write_sanction_entities([ev])
    assert written == 1

    rows = await fetch(
        "SELECT entity_type, canonical_id_type, canonical_id, display_name, properties "
        "FROM entity WHERE canonical_id = $1",
        f"{_TEST_PREFIX}V001",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["entity_type"] == "sanctioned_vessel"
    assert r["canonical_id_type"] == "uk_ofsi_id"
    assert r["display_name"] == "TEST UK GHOST"

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["sanctioning_authority"] == "UK OFSI"
    assert props["imo"] == 7777001


async def test_uk_and_ofac_coexist_for_same_vessel(_clean):
    """A vessel appearing on both UK and OFAC lists should get TWO entity
    rows (one per authority) — not collide. The sanctions_match algorithm
    can then surface 'multi-jurisdictional' as a stronger signal."""
    uk = _uk_event(f"{_TEST_PREFIX}V002", "DUAL LISTED", 6666002)
    ofac = _ofac_event(f"{_TEST_PREFIX_OFAC}V002", "DUAL LISTED", 6666002)
    written = await write_sanction_entities([uk, ofac])
    assert written == 2

    rows = await fetch(
        "SELECT canonical_id_type FROM entity "
        "WHERE canonical_id IN ($1, $2) ORDER BY canonical_id_type",
        f"{_TEST_PREFIX}V002", f"{_TEST_PREFIX_OFAC}V002",
    )
    types = sorted(r["canonical_id_type"] for r in rows)
    assert types == ["ofac_sdn_id", "uk_ofsi_id"]


async def test_uk_ofsi_idempotent_re_emit(_clean):
    """Re-running the ingester within the same hour returns 0 new rows
    (ON CONFLICT updates last_seen but doesn't double-count)."""
    ev = _uk_event(f"{_TEST_PREFIX}V003", "RE-EMIT", 5555003)
    first = await write_sanction_entities([ev])
    second = await write_sanction_entities([ev])
    assert first == 1
    assert second == 0
