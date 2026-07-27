"""
OFAC SDN program-code extraction unit tests.

Asserts that ofac_sdn.py builds the LegalBasis index, walks
SanctionsEntry → EntryEvent → LegalBasisID chains, and emits
events with ofac_program / ofac_programs / regime fields.

Uses a hand-crafted XML fixture matching the OFAC SDN_Advanced
namespace (verified against the live 118MB file 2026-05-08).

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_ofac_program_extraction.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.ofac_sdn import OfacSdnIngester  # noqa: E402


# OFAC's namespace; partySubTypeID 1 = vessel
_FIXTURE_XML = """<?xml version="1.0"?>
<Sanctions xmlns="http://www.un.org/sanctions/1.0">
  <ReferenceValueSets>
    <SanctionsProgramValues />
  </ReferenceValueSets>
  <LegalBasis ID="1" LegalBasisShortRef="Unknown" LegalBasisTypeID="1" SanctionsProgramID="1"/>
  <LegalBasis ID="1811" LegalBasisShortRef="Executive Order 13382 (Non-proliferation)" LegalBasisTypeID="1" SanctionsProgramID="1"/>
  <LegalBasis ID="1812" LegalBasisShortRef="Executive Order 13662 (Russia)" LegalBasisTypeID="1" SanctionsProgramID="1"/>
  <LegalBasis ID="1813" LegalBasisShortRef="Executive Order 14024 (Russia)" LegalBasisTypeID="1" SanctionsProgramID="1"/>
  <LegalBasis ID="1814" LegalBasisShortRef="Executive Order 13599 (Iran)" LegalBasisTypeID="1" SanctionsProgramID="1"/>

  <SanctionsEntry ID="9001" ProfileID="P1" ListID="1550">
    <EntryEvent ID="E1" EntryEventTypeID="1" LegalBasisID="1812"/>
    <EntryEvent ID="E2" EntryEventTypeID="1" LegalBasisID="1813"/>
    <SanctionsMeasure ID="9101" SanctionsTypeID="1"/>
  </SanctionsEntry>
  <SanctionsEntry ID="9002" ProfileID="P2" ListID="1550">
    <EntryEvent ID="E3" EntryEventTypeID="1" LegalBasisID="1814"/>
    <SanctionsMeasure ID="9102" SanctionsTypeID="1"/>
  </SanctionsEntry>
  <SanctionsEntry ID="9003" ProfileID="P3" ListID="1550">
    <EntryEvent ID="E4" EntryEventTypeID="1" LegalBasisID="1"/>  <!-- "Unknown" — must be skipped -->
    <SanctionsMeasure ID="9103" SanctionsTypeID="1"/>
  </SanctionsEntry>

  <DistinctParty FixedRef="P1">
    <Profile ID="P1" PartySubTypeID="1">
      <Identity ID="I1" Primary="true">
        <Alias Primary="true">
          <DocumentedName>
            <DocumentedNamePart>
              <NamePartValue>RUSSIAN GHOST</NamePartValue>
            </DocumentedNamePart>
          </DocumentedName>
        </Alias>
      </Identity>
    </Profile>
  </DistinctParty>
  <DistinctParty FixedRef="P2">
    <Profile ID="P2" PartySubTypeID="1">
      <Identity ID="I2" Primary="true">
        <Alias Primary="true">
          <DocumentedName>
            <DocumentedNamePart>
              <NamePartValue>IRANIAN PHANTOM</NamePartValue>
            </DocumentedNamePart>
          </DocumentedName>
        </Alias>
      </Identity>
    </Profile>
  </DistinctParty>
  <DistinctParty FixedRef="P3">
    <Profile ID="P3" PartySubTypeID="1">
      <Identity ID="I3" Primary="true">
        <Alias Primary="true">
          <DocumentedName>
            <DocumentedNamePart>
              <NamePartValue>NO PROGRAM VESSEL</NamePartValue>
            </DocumentedNamePart>
          </DocumentedName>
        </Alias>
      </Identity>
    </Profile>
  </DistinctParty>
</Sanctions>
""".encode("utf-8")


def _run_fetch_with_fixture():
    """Stand up the ingester, mock the fetch path's local-file branch +
    network branch with the fixture XML."""
    ing = OfacSdnIngester(broadcaster=None, classifier=None,
                           db_writer=None, logger=None)

    # Patch the local file path / read path to deliver fixture bytes.
    with patch("ingesters.ofac_sdn._LOCAL_SDN_PATH") as mock_path:
        mock_path.exists.return_value = True
        mock_path.stat.return_value.st_mtime = 9999999999
        mock_path.read_bytes.return_value = _FIXTURE_XML
        return asyncio.run(ing.fetch())


def test_party_with_two_eos_emits_both_programs():
    """A party with EntryEvents pointing at EO13662 + EO14024 → both
    Russia programs in ofac_legal_basis_refs; ofac_program = primary
    (first encountered)."""
    rows = _run_fetch_with_fixture()
    by_id = {r["id"]: r for r in rows}
    p1 = by_id["P1"]
    assert p1["display_name"] == "RUSSIAN GHOST"
    assert "RUSSIA" in p1["ofac_programs"]
    # Both Russia EOs should appear in legal-basis refs
    refs = p1["ofac_legal_basis_refs"]
    assert any("13662" in r for r in refs)
    assert any("14024" in r for r in refs)


def test_party_with_iran_eo_emits_iran_program():
    rows = _run_fetch_with_fixture()
    by_id = {r["id"]: r for r in rows}
    p2 = by_id["P2"]
    assert p2["ofac_program"] == "IRAN"
    assert p2["ofac_programs"] == ["IRAN"]


def test_unknown_legal_basis_is_skipped():
    """A party whose only EntryEvent points at the placeholder 'Unknown'
    LegalBasis gets no programs — we don't want a useless 'UNKNOWN' label
    leaking into the regime field."""
    rows = _run_fetch_with_fixture()
    by_id = {r["id"]: r for r in rows}
    p3 = by_id["P3"]
    assert p3["ofac_program"] is None
    assert p3["ofac_programs"] == []


def test_normalize_propagates_programs_to_payload():
    """End-to-end: fetch → normalize must produce GlassboxEvents whose
    payload has ofac_program + regime + ofac_programs."""
    ing = OfacSdnIngester(broadcaster=None, classifier=None,
                           db_writer=None, logger=None)
    rows = _run_fetch_with_fixture()
    events = ing.normalize(rows)
    assert len(events) == 3
    by_ext = {ev.external_id: ev for ev in events}
    p1_ev = by_ext["ofac_sdn:vessel:P1"]
    assert p1_ev.payload["ofac_program"] == "RUSSIA"
    assert p1_ev.payload["regime"] == "RUSSIA"
    assert "RUSSIA" in p1_ev.payload["ofac_programs"]
    p2_ev = by_ext["ofac_sdn:vessel:P2"]
    assert p2_ev.payload["regime"] == "IRAN"
    p3_ev = by_ext["ofac_sdn:vessel:P3"]
    assert "ofac_program" not in p3_ev.payload   # no program → omitted
    assert "regime" not in p3_ev.payload
