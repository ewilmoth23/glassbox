"""
maritime_mid.py — MMSI MID → flag/country lookup + regime-consistency
filter for the sanctions_match flag safety net.

The motivating false positive that triggered this module's creation:
US-flagged tug ATLAS (MMSI 367560990, MarineTraffic-confirmed
US-Tampa-Port-Manatee) was being flagged as a Ukraine-regime sanctioned
vessel because OFAC has a sanctioned vessel literally named "Atlas"
(IMO 9413573, totally different vessel) and the live ATLAS doesn't
broadcast its IMO in PositionReport messages. is_flag_consistent_with_
regime suppresses this kind of name-only match.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_maritime_mid.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maritime_mid import (  # noqa: E402
    lookup, country_iso2,
    is_flag_consistent_with_regime,
    FLAG_OF_CONVENIENCE,
    REGIME_EXPECTED_FLAGS,
)


# ─── lookup() ────────────────────────────────────────────────────────────


def test_us_mid_resolves_to_us():
    """ATLAS MMSI 367560990 (the false-positive case) resolves to US."""
    assert lookup(367560990) == ("United States", "US")
    assert country_iso2(367560990) == "US"


def test_china_range_resolves():
    assert country_iso2(412000001) == "CN"
    assert country_iso2(414000001) == "CN"
    assert country_iso2(477123456) == "HK"   # HK SAR


def test_russia_iran_dprk_resolve():
    assert country_iso2(273000001) == "RU"
    assert country_iso2(422000001) == "IR"
    assert country_iso2(445000001) == "KP"


def test_string_mmsi_accepted():
    assert country_iso2("367560990") == "US"


def test_invalid_mmsi_returns_none():
    assert lookup(None) is None
    assert lookup("not-a-number") is None
    assert lookup(0) is None
    assert lookup(99_999_999) is None      # too short
    assert lookup(1_000_000_000) is None   # too long
    assert lookup(99) is None              # negative MID


def test_unknown_mid_returns_none():
    """A MID not in our table returns None — better to be safe (no
    opinion) than to guess wrong."""
    # 999 is not a valid MID per ITU
    assert lookup(999000001) is None


# ─── is_flag_consistent_with_regime() ────────────────────────────────────


def test_us_vessel_does_NOT_match_ukraine_regime_by_name():
    """The motivating bug: ATLAS US-tug should NOT name-match Ukraine
    sanctioned vessel."""
    # ATLAS (MMSI 367560990) name-matched against Ukraine OFAC entry
    assert is_flag_consistent_with_regime(367560990, "UKRAINE") is False
    assert is_flag_consistent_with_regime(367560990, "RUSSIA") is False


def test_us_vessel_does_NOT_match_iran_or_dprk_regime():
    assert is_flag_consistent_with_regime(367560990, "IRAN") is False
    assert is_flag_consistent_with_regime(367560990, "DPRK") is False
    assert is_flag_consistent_with_regime(367560990, "NORTH KOREA") is False


def test_russia_vessel_DOES_match_russia_regime():
    """Russian-MMSI vessel matched against Russia regime → consistent."""
    assert is_flag_consistent_with_regime(273000001, "RUSSIA") is True
    assert is_flag_consistent_with_regime(273000001, "UKRAINE") is True


def test_iran_vessel_matches_iran_regime():
    assert is_flag_consistent_with_regime(422000001, "IRAN") is True


def test_flag_of_convenience_always_passes():
    """Liberia-flagged vessels (FOC) host vessels owned in any country;
    the flag check must not disqualify a name match for them."""
    # 636 = Liberia (flag of convenience)
    assert is_flag_consistent_with_regime(636000001, "RUSSIA") is True
    assert is_flag_consistent_with_regime(636000001, "IRAN") is True
    # 538 = Marshall Islands FOC
    assert is_flag_consistent_with_regime(538000001, "DPRK") is True
    # 351 = Panama FOC
    assert is_flag_consistent_with_regime(351000001, "RUSSIA") is True


def test_no_regime_passes():
    """When the OFAC entry has no regime tag set, no flag opinion."""
    assert is_flag_consistent_with_regime(367560990, None) is True
    assert is_flag_consistent_with_regime(367560990, "") is True


def test_unknown_regime_passes():
    """A regime the table doesn't know about → no opinion → pass.
    Better to fail open than over-suppress real matches."""
    assert is_flag_consistent_with_regime(367560990, "ATLANTIS") is True
    # Programs without a hard country focus
    assert is_flag_consistent_with_regime(367560990, "TERRORISM") is True
    assert is_flag_consistent_with_regime(367560990, "GLOBAL MAGNITSKY") is True


def test_no_mmsi_flag_info_passes():
    """If we can't decode the MMSI, don't filter."""
    assert is_flag_consistent_with_regime(None, "RUSSIA") is True
    assert is_flag_consistent_with_regime("invalid", "RUSSIA") is True


def test_regime_lookup_case_insensitive():
    """OFAC writes regimes in various cases (RUSSIA / russia / Russia)."""
    assert is_flag_consistent_with_regime(367560990, "russia") is False
    assert is_flag_consistent_with_regime(367560990, "Russia") is False
    assert is_flag_consistent_with_regime(367560990, "  RUSSIA  ") is False


# ─── Coverage of the regime table ────────────────────────────────────────


def test_regime_table_has_top_sanctions_targets():
    """Every regime that appears in OFAC SDN program metadata should
    have an expected-flag entry (even if empty for global programs)."""
    for r in ("RUSSIA", "UKRAINE", "IRAN", "NORTH KOREA", "VENEZUELA",
              "CUBA", "SYRIA"):
        assert r in REGIME_EXPECTED_FLAGS, f"missing regime: {r}"


def test_foc_set_has_main_offenders():
    for c in ("LR", "PA", "MH", "MT", "SG", "HK"):
        assert c in FLAG_OF_CONVENIENCE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
