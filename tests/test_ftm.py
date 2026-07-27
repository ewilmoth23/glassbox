# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
FollowTheMoney bridge tests — entity_to_ftm() round-trip + edge cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ftm import entity_to_ftm, supported_entity_types  # noqa: E402


def _row(entity_type: str, canonical_id: str, **props) -> dict:
    return {
        "entity_type":  entity_type,
        "canonical_id": canonical_id,
        "properties":   props,
    }


# ─── Supported-types surface ─────────────────────────────────────────────


def test_supported_entity_types_returns_canonical_set():
    types = supported_entity_types()
    assert "vessel" in types
    assert "aircraft" in types
    assert "sanctioned_vessel" in types
    assert "sanctioned_aircraft" in types
    # satellite is intentionally NOT supported in this slice
    assert "satellite" not in types


# ─── Vessel translation ──────────────────────────────────────────────────


def test_vessel_translates_to_ftm_vessel_schema():
    out = entity_to_ftm(_row(
        "vessel", "mmsi:273123456",
        display_name="MV TEST",
        imo=9123456, mmsi=273123456, flag="RU",
        callsign="UABCD", vessel_type="tanker",
    ))
    assert out is not None
    assert out["schema"] == "Vessel"
    assert out["id"] == "mmsi:273123456"
    p = out["properties"]
    assert p["name"] == ["MV TEST"]
    assert p["imoNumber"] == ["9123456"]
    assert p["mmsi"] == ["273123456"]
    assert p["callSign"] == ["UABCD"]
    assert p["type"] == ["tanker"]
    # FtM normalizes flag values to ISO-2 lowercase
    assert p["flag"] == ["ru"]


def test_vessel_minimal_only_canonical_id_and_type():
    """A vessel row with no payload still produces a valid FtM skeleton —
    consumers can attach attributes later."""
    out = entity_to_ftm(_row("vessel", "mmsi:000000001"))
    assert out is not None
    assert out["schema"] == "Vessel"
    assert out["id"] == "mmsi:000000001"
    assert out.get("properties") == {}


def test_vessel_alternate_field_names_supported():
    """Some ingesters use 'name' instead of 'display_name'; same for
    'flag_state' vs 'flag'. Translator tolerates both."""
    out = entity_to_ftm(_row(
        "vessel", "mmsi:1",
        name="ALT NAME", flag_state="DE", call_sign="DTEST",
    ))
    assert out is not None
    assert out["properties"]["name"] == ["ALT NAME"]
    assert out["properties"]["flag"] == ["de"]
    assert out["properties"]["callSign"] == ["DTEST"]


# ─── Sanctioned-vessel translation ───────────────────────────────────────


def test_sanctioned_vessel_adds_topic_and_program():
    out = entity_to_ftm(_row(
        "sanctioned_vessel", "ofac_sdn:vessel:12345",
        display_name="ATLAS", imo=9123456, flag="RU",
        regime="Russia/Ukraine",
        sanctioning_authority="OFAC",
        ofac_uid="12345",
    ))
    assert out is not None
    assert out["schema"] == "Vessel"
    p = out["properties"]
    assert "sanction" in p["topics"]
    assert "Russia/Ukraine" in p["program"]
    # Description carries provenance — authority + reference id
    desc = " ".join(p.get("description", []))
    assert "OFAC" in desc
    assert "12345" in desc


def test_sanctioned_vessel_with_eu_ref_carries_eu_authority():
    out = entity_to_ftm(_row(
        "sanctioned_vessel", "eu_cfsp:vessel:100002",
        display_name="SOVCOMFLOT", imo=9999102,
        regime="Russia/Ukraine",
        sanctioning_authority="EU CFSP",
        eu_ref="EU.UKR.99",
    ))
    assert out is not None
    desc = " ".join(out["properties"].get("description", []))
    assert "EU CFSP" in desc
    assert "EU.UKR.99" in desc


def test_sanctioned_vessel_without_authority_metadata_still_topics():
    """Bare-minimum sanctioned vessel — just the type + name. Topics
    must still be set so OCCRP-ecosystem consumers can filter on it."""
    out = entity_to_ftm(_row(
        "sanctioned_vessel", "x:1", display_name="MIN",
    ))
    assert out is not None
    assert out["properties"]["topics"] == ["sanction"]


# ─── Aircraft translation ────────────────────────────────────────────────


def test_aircraft_translates_to_ftm_airplane_schema():
    out = entity_to_ftm(_row(
        "aircraft", "icao24:a12345",
        display_name="BOEING 737",
        icao24="a12345", registration="N737AA",
        callsign="AAL123", model="B737-800",
    ))
    assert out is not None
    assert out["schema"] == "Airplane"
    p = out["properties"]
    assert p["name"] == ["BOEING 737"]
    assert p["icaoCode"] == ["a12345"]
    assert p["registrationNumber"] == ["N737AA"]
    assert p["model"] == ["B737-800"]
    # callSign isn't an Airplane property in FtM — translator should
    # silently skip rather than crash
    assert "callSign" not in p


def test_sanctioned_aircraft_topics_set():
    out = entity_to_ftm(_row(
        "sanctioned_aircraft", "ofac_sdn:aircraft:1",
        display_name="N1", icao24="abc123", regime="Iran",
        sanctioning_authority="OFAC",
    ))
    assert out is not None
    assert out["schema"] == "Airplane"
    assert "sanction" in out["properties"]["topics"]
    assert "Iran" in out["properties"]["program"]


# ─── Unsupported / malformed input ───────────────────────────────────────


def test_unsupported_entity_type_returns_none():
    assert entity_to_ftm(_row("satellite", "norad:25544")) is None
    assert entity_to_ftm(_row("unknown_kind", "x:1")) is None


def test_missing_required_fields_return_none():
    assert entity_to_ftm({}) is None
    assert entity_to_ftm({"entity_type": "vessel"}) is None
    assert entity_to_ftm({"canonical_id": "x"}) is None


def test_none_properties_handled_gracefully():
    """Some ORM rows arrive with properties=None instead of {}."""
    row = {
        "entity_type":  "vessel",
        "canonical_id": "x:1",
        "properties":   None,
    }
    out = entity_to_ftm(row)
    assert out is not None
    assert out["schema"] == "Vessel"


def test_invalid_imo_does_not_break_translation():
    """A malformed IMO (non-digit) shouldn't crash — FtM cleaned=True
    accepts any string. Consumers can validate."""
    out = entity_to_ftm(_row(
        "vessel", "x:1",
        display_name="X", imo="invalid-imo-string",
    ))
    assert out is not None
    # The translator passed it through; downstream FtM consumers can
    # decide whether to honor or drop
    assert "imoNumber" in out["properties"]
