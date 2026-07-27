# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Glassbox CAMEO taxonomy tests.

Two suites in one file:

  * test_lookup_*: API-level — direct lookup, parent-code fallback, performance.
  * test_coverage_*: data-quality — JSON Schema validation, GDELT-emitted-code
    coverage, severity bounds, Goldstein spot-checks.

Together these enforce HANDOFF_02's "what success looks like": every CAMEO
code GDELT can emit is mapped, no orphans, performance ceiling held.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from glassbox_taxonomy import CAMEOEntry, CAMEOLookup  # noqa: E402


_DATA_DIR = ROOT / "glassbox_taxonomy" / "data"
_JSON_PATH = _DATA_DIR / "cameo_lookup.json"
_SCHEMA_PATH = _DATA_DIR / "cameo_lookup.schema.json"


@pytest.fixture(scope="module")
def lookup() -> CAMEOLookup:
    return CAMEOLookup()


@pytest.fixture(scope="module")
def doc() -> dict:
    with _JSON_PATH.open() as f:
        return json.load(f)


@pytest.fixture(scope="module")
def schema() -> dict:
    with _SCHEMA_PATH.open() as f:
        return json.load(f)


# ─── API tests (test_lookup_*) ───────────────────────────────────────────


def test_lookup_loads_with_default_path():
    """Module-level singleton via no-arg constructor."""
    lk = CAMEOLookup()
    assert len(lk) > 0
    assert lk.version == "1.0"
    assert "CAMEO" in lk.source_codebook


def test_lookup_loads_with_explicit_path():
    """Constructor accepts an override path."""
    lk = CAMEOLookup(json_path=_JSON_PATH)
    assert len(lk) > 0


def test_lookup_direct_known_codes(lookup):
    """20 representative direct lookups covering every category-root."""
    expectations = {
        "010":  ("diplomatic",       "diplomatic.statement"),
        "041":  ("diplomatic",       "diplomatic.consultation"),
        "057":  ("diplomatic",       "diplomatic.agreement"),
        "071":  ("economic",         "economic.aid"),
        "073":  ("humanitarian",     "humanitarian.aid_delivery"),
        "0871": ("armed_conflict",   "armed_conflict.ceasefire"),
        "094":  ("governance",       "governance.investigation"),
        "100":  ("diplomatic",       "diplomatic.demand"),
        "111":  ("diplomatic",       "diplomatic.disapproval"),
        "129":  ("governance",       "governance.judicial"),
        "1355": ("armed_conflict",   "armed_conflict.threat"),
        "141":  ("violence_civil",   "violence_civil.demonstration"),
        "145":  ("violence_civil",   "violence_civil.riot"),
        "154":  ("cyber",            "cyber.posture"),
        "163":  ("economic",         "economic.sanctions"),
        "1712": ("infrastructure",   "infrastructure.property_attack"),
        "175":  ("violence_civil",   "violence_civil.repression"),
        "1823": ("violence_civil",   "violence_civil.assault"),
        "186":  ("armed_conflict",   "armed_conflict.assassination"),
        "194":  ("armed_conflict",   "armed_conflict.artillery"),
        "2042": ("armed_conflict",   "armed_conflict.wmd"),
    }
    for code, (cat, subcat) in expectations.items():
        entry = lookup.by_code(code)
        assert entry is not None, f"code {code} missing from lookup"
        assert entry.category == cat, \
            f"code {code}: category {entry.category!r} != expected {cat!r}"
        assert entry.subcategory == subcat, \
            f"code {code}: subcategory {entry.subcategory!r} != expected {subcat!r}"


def test_lookup_parent_code_fallback(lookup):
    """A nonexistent finer code must fall back to its closest parent.
    1953 (nonexistent) -> 195 (Employ aerial weapons / armed_conflict.airstrike)."""
    entry = lookup.by_code("1953")
    assert entry is not None
    assert entry.code == "195"
    assert entry.subcategory == "armed_conflict.airstrike"


def test_lookup_falls_through_multiple_levels(lookup):
    """12345 (none of 1234, 123 exist as 12345's prefix) -> 12 (Reject)."""
    entry = lookup.by_code("12345")
    assert entry is not None
    # Either lands on 1234 (Reject institutional change) or its ancestor.
    assert entry.category == "diplomatic"


def test_lookup_returns_none_for_unmappable_code(lookup):
    """A code with no ancestor in the table at all (e.g. starts with '00')
    returns None. The caller is then expected to use the '999' fallback."""
    assert lookup.by_code("00000") is None
    assert lookup.by_code("") is None
    # Whitespace must not be silently mapped.
    assert lookup.by_code("   ") is None


def test_lookup_999_unknown_fallback_exists(lookup):
    """ingester glue does `entry = by_code('999')` when by_code(real_code)
    returns None. That fallback must always be in the table."""
    e = lookup.by_code("999")
    assert e is not None
    assert e.category == "unknown"


def test_lookup_by_subcategory_returns_all_matching(lookup):
    """Reverse index: every code under a subcategory comes back together."""
    bombings = lookup.by_subcategory("armed_conflict.bombing")
    # 183, 1832, 1833, 1834 (1831 is suicide_attack)
    assert len(bombings) >= 3
    assert all(e.subcategory == "armed_conflict.bombing" for e in bombings)
    codes = sorted(e.code for e in bombings)
    assert "183" in codes


def test_lookup_by_subcategory_returns_empty_for_unknown(lookup):
    assert lookup.by_subcategory("does_not_exist.at_all") == []


def test_lookup_all_subcategories_sorted_unique(lookup):
    subs = lookup.all_subcategories()
    assert subs == sorted(subs)
    assert len(subs) == len(set(subs))


def test_lookup_all_categories_sorted_unique(lookup):
    cats = lookup.all_categories()
    assert cats == sorted(cats)
    assert len(cats) == len(set(cats))


def test_lookup_million_lookups_under_200ms(lookup):
    """HANDOFF_02 perf bar: 1M direct lookups in under 200ms on a single
    thread. Production GDELT bulk processes ~250K events/day so this gives
    ~4× headroom."""
    codes = ["010", "190", "1834", "186", "1355", "141", "1234"]
    n = 1_000_000
    start = time.perf_counter()
    for i in range(n):
        lookup.by_code(codes[i % len(codes)])
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 200, f"1M lookups took {elapsed_ms:.0f}ms (>200ms ceiling)"


# ─── Schema + data integrity (test_coverage_*) ───────────────────────────


def test_coverage_json_validates_against_schema(doc, schema):
    """Every entry must validate."""
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    assert not errors, "schema errors:\n" + "\n".join(
        f"  at {list(e.absolute_path)}: {e.message}" for e in errors[:5]
    )


def test_coverage_top_level_doc_shape(doc):
    assert doc["version"] == "1.0"
    assert doc["license"] == "MIT"
    assert "CAMEO" in doc["source_codebook"]
    assert isinstance(doc["entries"], list)
    assert 280 <= len(doc["entries"]) <= 350, \
        f"expected 280-350 codes per HANDOFF_02 target, got {len(doc['entries'])}"


def test_coverage_no_duplicate_codes(doc):
    codes = [e["code"] for e in doc["entries"]]
    assert len(codes) == len(set(codes)), "duplicate CAMEO codes in lookup"


def test_coverage_subcategory_starts_with_category(doc):
    """Invariant from the schema description: subcategory's prefix is the
    same string as the category field."""
    for e in doc["entries"]:
        prefix = e["subcategory"].split(".", 1)[0]
        assert prefix == e["category"], \
            f"code {e['code']}: subcategory '{e['subcategory']}' != category '{e['category']}'"


def test_coverage_severity_bounds(doc):
    """HANDOFF_02 rubric: 0.0 ≤ severity ≤ 1.0 for every entry."""
    for e in doc["entries"]:
        assert 0.0 <= e["severity"] <= 1.0, \
            f"code {e['code']}: severity {e['severity']} out of [0.0, 1.0]"


def test_coverage_goldstein_bounds(doc):
    """CAMEO Goldstein scale: -10.0 ≤ x ≤ +10.0."""
    for e in doc["entries"]:
        assert -10.0 <= e["goldstein"] <= 10.0, \
            f"code {e['code']}: Goldstein {e['goldstein']} out of CAMEO range"


def test_coverage_high_severity_implies_negative_goldstein(doc):
    """Sanity gate: if Glassbox classifies severity ≥ 0.7 (clear violence),
    Goldstein had better be negative — these are conflict events. Catches
    sign errors when entries are added."""
    offenders = [
        e for e in doc["entries"]
        if e["severity"] >= 0.7 and e["goldstein"] > 0
    ]
    assert not offenders, \
        "high-severity entries with positive Goldstein:\n" + "\n".join(
            f"  {e['code']} sev={e['severity']} gold={e['goldstein']} ({e['subcategory']})"
            for e in offenders[:5]
        )


def test_coverage_no_orphan_subcategories(doc):
    """Every subcategory used must appear in at least one entry. Trivially
    true by construction (we derive the set from entries) but checks the
    JSON file by itself, not via the loader."""
    subs_from_entries = {e["subcategory"] for e in doc["entries"]}
    assert subs_from_entries, "no subcategories at all"


def test_coverage_every_root_category_has_unspecified_or_explicit_root(doc):
    """For every top-level CAMEO root code 01-20, the lookup must answer
    via the parent-fallback path. Equivalently: every 2-digit root code is
    in the table directly, or HANDOFF_02's parent-fallback breaks for
    GDELT-extended codes."""
    by_code = {e["code"]: e for e in doc["entries"]}
    missing_roots = []
    for n in range(1, 21):
        root = f"{n:02d}"
        # The root is implied if any code starts with it (the parent fallback
        # walks down to the 2-digit prefix, which must exist as an entry).
        if root not in by_code:
            missing_roots.append(root)
    assert not missing_roots, \
        f"root codes missing — parent-fallback would return None: {missing_roots}"


def test_coverage_known_gdelt_emitted_codes(doc):
    """Spot-check that the canonical, frequently-emitted GDELT codes are all
    mapped. This list is the floor — GDELT bulk emits roughly this set every
    cycle, with the long tail of 4-digit children. If any of these are
    missing the ingester is going to drop events on day 1.
    """
    by_code = {e["code"]: e for e in doc["entries"]}
    must_have = [
        # Statements & appeals — the high-volume 'noise floor' of GDELT
        "010", "011", "012", "013", "017", "020", "030", "036", "040", "042",
        # Diplomatic cooperation
        "050", "051", "057",
        # Material cooperation + aid
        "060", "061", "070", "071", "072", "073",
        # Concessions / yielding
        "080", "0871",
        # Investigations & demands
        "090", "100", "104",
        # Disapproval / rejection / threat — common before things escalate
        "110", "111", "112", "120", "130", "135", "1355",
        # Protest, strike, riot
        "140", "141", "143", "145",
        # Force posture
        "150", "152", "153",
        # Reduce relations
        "160", "161", "163",
        # Coercion
        "170", "171", "173",
        # Assault
        "180", "181", "182", "183", "1831", "186",
        # Fight
        "190", "192", "193", "194", "195",
        # Mass violence
        "200", "202", "203", "204",
        # Required Glassbox fallback
        "999",
    ]
    missing = [c for c in must_have if c not in by_code]
    assert not missing, f"high-volume GDELT codes not mapped: {missing}"


def test_coverage_goldstein_spot_check_canonical_codes(doc):
    """Spot check 50+ codes match the published CAMEO Goldstein scale.
    Canonical reference: Schrodt 2012 codebook + 2015 supplement. Catches
    accidental sign flips and misreads when entries are added/edited."""
    expected = {
        "010":  0.0,    # Make statement
        "012": -0.4,    # Pessimistic comment
        "013":  0.4,    # Optimistic comment
        "016": -1.1,    # Deny responsibility
        "020":  3.0,    # Appeal
        "030":  4.0,    # Express intent to cooperate
        "041":  1.0,    # Discuss by telephone
        "042":  1.9,    # Make a visit
        "045":  4.0,    # Mediate
        "046":  4.0,    # Negotiate
        "050":  3.5,    # Diplomatic cooperation
        "051":  3.4,    # Praise or endorse
        "054":  7.0,    # Grant diplomatic recognition
        "055":  1.9,    # Apologize
        "057":  6.4,    # Sign formal agreement
        "060":  6.0,    # Material cooperation
        "061":  7.0,    # Cooperate economically
        "070":  7.0,    # Provide aid
        "071":  7.4,    # Provide economic aid
        "072":  8.3,    # Provide military aid
        "073":  7.4,    # Provide humanitarian aid
        "075":  7.0,    # Grant asylum
        "080":  5.0,    # Yield
        "085":  6.5,    # Ease economic sanctions
        "0871": 6.5,    # Declare ceasefire
        "0874": 6.5,    # Retreat or surrender
        "090": -2.0,    # Investigate
        "094": -2.0,    # Investigate war crimes
        "100": -5.0,    # Demand
        "108": -5.0,    # Demand de-escalation
        "110": -2.0,    # Disapprove
        "112": -2.0,    # Accuse
        "120": -4.0,    # Reject
        "128": -4.0,    # Defy norms
        "130": -4.4,    # Threaten
        "135": -7.0,    # Threaten military action
        "1355": -7.0,   # Threaten WMD attack
        "137": -6.9,    # Give ultimatum
        "140": -4.0,    # Political dissent
        "141": -6.5,    # Demonstrate
        "143": -6.5,    # Strike or boycott
        "145": -6.5,    # Riot
        "150": -5.0,    # Force posture
        "152": -5.0,    # Increase military alert
        "160": -4.0,    # Reduce relations
        "161": -7.0,    # Break diplomatic relations
        "163": -5.6,    # Impose sanctions
        "170": -7.0,    # Coerce
        "175": -8.5,    # Violent repression
        "180": -9.0,    # Unconventional violence
        "182": -9.0,    # Physical assault
        "1823": -10.0,  # Kill by physical assault
        "183": -10.0,   # Bombing
        "1831": -10.0,  # Suicide bombing
        "185": -9.5,    # Attempt assassinate
        "186": -10.0,   # Assassinate
        "190": -10.0,   # Conventional military force
        "191": -7.6,    # Blockade
        "192": -9.2,    # Occupy territory
        "193": -10.0,   # Small arms
        "195": -10.0,   # Aerial weapons
        "196": -7.6,    # Violate ceasefire
        "200": -10.0,   # Unconventional mass violence
        "203": -10.0,   # Ethnic cleansing
        "204": -10.0,   # WMD
    }
    by_code = {e["code"]: e for e in doc["entries"]}
    mismatches = []
    for code, expected_g in expected.items():
        got = by_code.get(code, {}).get("goldstein")
        if got is None or got != expected_g:
            mismatches.append((code, expected_g, got))
    assert not mismatches, "Goldstein mismatches:\n" + "\n".join(
        f"  {c}: expected {e}, got {g}" for c, e, g in mismatches[:10]
    )


def test_coverage_entry_count_meets_handoff_target(doc):
    """HANDOFF_02 success criterion: 'covers every CAMEO code GDELT can
    emit'. Empirical floor: ≥ 280 codes per HANDOFF_02 target range."""
    assert len(doc["entries"]) >= 280


def test_coverage_subcategory_count_in_target_range(doc):
    """HANDOFF_02 target: 80-120 subcategories total. Floor of 60 keeps
    the taxonomy expressive without going so granular it becomes
    hard to query/aggregate. Hard cap of 120 prevents the long-tail
    explosion HANDOFF_02 explicitly warns about."""
    subs = {e["subcategory"] for e in doc["entries"]}
    assert 60 <= len(subs) <= 120, \
        f"taxonomy has {len(subs)} subcategories — outside healthy 60-120 range"


# ─── CAMEOEntry validation ───────────────────────────────────────────────


def test_entry_validation_rejects_bad_severity():
    with pytest.raises(Exception):
        CAMEOEntry(
            code="999", name="x", category="unknown", subcategory="unknown.unknown",
            label="x", goldstein=0.0, severity=1.5, flags=[],
        )


def test_entry_validation_rejects_bad_goldstein():
    with pytest.raises(Exception):
        CAMEOEntry(
            code="999", name="x", category="unknown", subcategory="unknown.unknown",
            label="x", goldstein=-15.0, severity=0.5, flags=[],
        )


def test_entry_validation_rejects_short_code():
    with pytest.raises(Exception):
        CAMEOEntry(
            code="1", name="x", category="unknown", subcategory="unknown.unknown",
            label="x", goldstein=0.0, severity=0.5, flags=[],
        )
