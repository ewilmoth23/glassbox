"""
MMSI MID → flag/country lookup per ITU-R M.585.

Every MMSI starts with a 3-digit Maritime Identification Digit that
encodes the vessel's flag state. This is decoded by every commercial
AIS receiver and is the cheapest available signal to disqualify a
sanctions name-match against a country whose vessels can't possibly
be on the list (e.g. an MMSI-367 US-flagged vessel can't be a
Ukraine-regime sanctioned tanker).

Source: ITU-R M.585 Annex 1 (publicly published, public domain).
This file ships the official assignments + flag → ISO-3166 alpha-2
mapping. We use the alpha-2 codes downstream in sanctions_match.py
to compare against the OFAC sanctioning regime / origin field.

This is NOT a replacement for IMO matching — it's a SAFETY filter that
stops obvious false positives when name-only matching fires.
"""

from __future__ import annotations

from typing import Optional, Tuple


# MID → (country_name, ISO-3166 alpha-2). Most-traffic'd MIDs first,
# but the lookup is dict-based so order doesn't matter for performance.
# Ranges (e.g., 366-369 for USA) are stored as discrete entries below.
_MID_TO_COUNTRY: dict = {}


def _add_range(start: int, end: int, name: str, iso2: str) -> None:
    for mid in range(start, end + 1):
        _MID_TO_COUNTRY[mid] = (name, iso2)


def _add(mid: int, name: str, iso2: str) -> None:
    _MID_TO_COUNTRY[mid] = (name, iso2)


# ─── Major maritime nations ───────────────────────────────────────────────
# Source: ITU MID assignments (https://www.itu.int/en/ITU-R/terrestrial/fmd/Pages/mid.aspx)

# United States (366-369)
_add_range(366, 369, "United States", "US")
# China (412-413, 414, 477)
_add_range(412, 414, "China", "CN")
_add(477, "China (Hong Kong SAR)", "HK")
# Russia (273)
_add(273, "Russia", "RU")
# United Kingdom (232-235)
_add_range(232, 235, "United Kingdom", "GB")
# Germany (211, 218)
_add(211, "Germany", "DE")
_add(218, "Germany", "DE")
# Greece (237, 239-241)
_add(237, "Greece", "GR")
_add_range(239, 241, "Greece", "GR")
# Japan (431-432)
_add_range(431, 432, "Japan", "JP")
# Norway (257-259)
_add_range(257, 259, "Norway", "NO")
# Liberia (636-637) — common flag of convenience
_add_range(636, 637, "Liberia", "LR")
# Panama (351-357, 370-373) — flag of convenience
_add_range(351, 357, "Panama", "PA")
_add_range(370, 373, "Panama", "PA")
# Marshall Islands (538) — flag of convenience
_add(538, "Marshall Islands", "MH")
# Malta (215, 229, 248-249, 256)
_add(215, "Malta", "MT")
_add(229, "Malta", "MT")
_add_range(248, 249, "Malta", "MT")
_add(256, "Malta", "MT")
# Singapore (563-565)
_add_range(563, 565, "Singapore", "SG")
# India (419)
_add(419, "India", "IN")
# Iran (422)
_add(422, "Iran", "IR")
# DPRK / North Korea (445)
_add(445, "North Korea", "KP")
# Cuba (323)
_add(323, "Cuba", "CU")
# Venezuela (775)
_add(775, "Venezuela", "VE")
# Syria (468)
_add(468, "Syria", "SY")
# South Korea (440-441)
_add_range(440, 441, "South Korea", "KR")
# Turkey (271)
_add(271, "Turkey", "TR")
# Italy (247)
_add(247, "Italy", "IT")
# Spain (224-225, 269)
_add_range(224, 225, "Spain", "ES")
_add(269, "Spain", "ES")
# France (226-228)
_add_range(226, 228, "France", "FR")
# Netherlands (244-246)
_add_range(244, 246, "Netherlands", "NL")
# Belgium (205)
_add(205, "Belgium", "BE")
# Denmark (219-220)
_add_range(219, 220, "Denmark", "DK")
# Sweden (265-266)
_add_range(265, 266, "Sweden", "SE")
# Finland (230)
_add(230, "Finland", "FI")
# Estonia (276)
_add(276, "Estonia", "EE")
# Latvia (275)
_add(275, "Latvia", "LV")
# Lithuania (277)
_add(277, "Lithuania", "LT")
# Poland (261)
_add(261, "Poland", "PL")
# Ukraine (272)
_add(272, "Ukraine", "UA")
# Canada (316)
_add(316, "Canada", "CA")
# Brazil (710)
_add(710, "Brazil", "BR")
# Mexico (345)
_add(345, "Mexico", "MX")
# Australia (503)
_add(503, "Australia", "AU")
# UAE (470-471)
_add_range(470, 471, "United Arab Emirates", "AE")
# Saudi Arabia (403)
_add(403, "Saudi Arabia", "SA")
# Egypt (622)
_add(622, "Egypt", "EG")
# South Africa (601)
_add(601, "South Africa", "ZA")
# Bahrain (408)
_add(408, "Bahrain", "BH")
# Hong Kong (477) - already added above
# Taiwan (416)
_add(416, "Taiwan", "TW")
# Vietnam (574)
_add(574, "Vietnam", "VN")
# Thailand (567)
_add(567, "Thailand", "TH")
# Malaysia (533)
_add(533, "Malaysia", "MY")
# Indonesia (525)
_add(525, "Indonesia", "ID")
# Philippines (548)
_add(548, "Philippines", "PH")
# Argentina (701)
_add(701, "Argentina", "AR")
# Colombia (730)
_add(730, "Colombia", "CO")
# Sri Lanka (417)
_add(417, "Sri Lanka", "LK")


# ─── Public lookup ────────────────────────────────────────────────────────


def lookup(mmsi) -> Optional[Tuple[str, str]]:
    """Return (country_name, ISO-3166 alpha-2) for an MMSI, or None.

    Accepts MMSI as int OR str. MMSIs <100M are non-vessel (coastal
    stations / SAR / etc.) and return None — the SAR / aid-to-nav MIDs
    aren't useful for vessel-flag checks.
    """
    if mmsi is None:
        return None
    try:
        m = int(mmsi)
    except (TypeError, ValueError):
        return None
    if m < 100_000_000 or m > 999_999_999:
        # Not a valid vessel MMSI per ITU-R M.585 (vessels are 9 digits)
        return None
    mid = m // 1_000_000
    return _MID_TO_COUNTRY.get(mid)


def country_iso2(mmsi) -> Optional[str]:
    """Convenience: just the ISO-3166 alpha-2 code."""
    res = lookup(mmsi)
    return res[1] if res else None


# ─── Regime → expected flag-state hint ────────────────────────────────────
#
# OFAC sanctioning regime → the set of flag states whose MMSIs we'd
# expect a sanctioned vessel to broadcast under. A vessel broadcasting
# from outside this set, when name-matched only, is almost certainly a
# false positive (different vessel sharing the name).
#
# Two notes on use:
#   1. NOT a hard reject — flag-of-convenience countries (Liberia,
#      Panama, Marshall Islands, Malta) host vessels owned by parties
#      in regime countries. So "FOC + name match" still passes.
#   2. The check applies ONLY to name-only matches. IMO-exact matches
#      ALWAYS fire regardless of MMSI flag (IMO is globally unique;
#      flag is irrelevant once IMO matches).
#
# Empty set means "no opinion" — we don't filter further.

# Flag-of-convenience codes that host vessels owned anywhere; never
# disqualify a name-match purely because the MMSI is one of these.
FLAG_OF_CONVENIENCE: frozenset = frozenset({
    "LR", "PA", "MH", "MT", "BS", "CY", "BB", "AG", "BM", "VG", "KY",
    "LK", "GI", "VC", "SG", "HK",
})

REGIME_EXPECTED_FLAGS: dict = {
    # OFAC regime / EO short code → expected real-flag set (NOT the FOC
    # ones; those are always allowed). A name match where the vessel's
    # MMSI is a non-FOC NOT in this set is a probable false positive.
    "RUSSIA":          frozenset({"RU", "UA"}),
    "UKRAINE-/-RUSSIA": frozenset({"RU", "UA"}),
    "UKRAINE":          frozenset({"RU", "UA"}),
    "IRAN":            frozenset({"IR"}),
    "NORTH KOREA":     frozenset({"KP"}),
    "DPRK":            frozenset({"KP"}),
    "VENEZUELA":       frozenset({"VE"}),
    "CUBA":            frozenset({"CU"}),
    "SYRIA":           frozenset({"SY"}),
    "TERRORISM":       frozenset(),  # global, no flag hint
    "GLOBAL MAGNITSKY": frozenset(),
    "NON-PROLIFERATION": frozenset(),
}


def is_flag_consistent_with_regime(mmsi, regime: Optional[str]) -> bool:
    """True if the vessel's MMSI flag is consistent with the sanctioning
    regime (or we have no opinion either way). False ONLY when:
      - MMSI maps to a non-FOC country
      - regime has an expected-flag set
      - vessel's flag is NOT in that set

    Used by sanctions_match.py to suppress name-only false positives.
    """
    if not regime:
        return True
    expected = REGIME_EXPECTED_FLAGS.get(str(regime).upper().strip())
    if expected is None or len(expected) == 0:
        return True   # no opinion
    iso = country_iso2(mmsi)
    if iso is None:
        return True   # no MMSI flag info → don't filter
    if iso in FLAG_OF_CONVENIENCE:
        return True   # FOC vessels can be owned anywhere
    return iso in expected
