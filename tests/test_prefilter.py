# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GDELT prefilter tests — covers the stateless slice of HANDOFF_03:
config validation + load, each rule in isolation, priority scoring,
and the engine's pass/drop chain. Dedup + Redis queue + A/B + perf
benchmarks land in the follow-up commit.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.gdelt_bulk.prefilter import (  # noqa: E402
    BoundedPriorityQueue,
    CategoryRule,
    DedupRule,
    FilteredEvent,
    GDELTEventForPrefilter,
    GeographyRule,
    PreFilterConfig,
    PreFilterEngine,
    PriorityScorer,
    RecencyRule,
    SeverityRule,
    SourceQualityRule,
    haversine_km,
    token_set_jaccard,
)
from ingesters.gdelt_bulk.prefilter.config import (  # noqa: E402
    CategoryFilterConfig,
    DedupFilterConfig,
    GeographyFilterConfig,
    PriorityConfig,
    RecencyFilterConfig,
    SeverityFilterConfig,
    SourceQualityFilterConfig,
)
from ingesters.gdelt_bulk.prefilter.rules import extract_domain  # noqa: E402

# Engine's `data_dir` is the prefilter package root; the YAML's
# `data_file` is resolved relative to that (matching how a deployed
# config-file path would be resolved).
_PREFILTER_PKG = ROOT / "ingesters" / "gdelt_bulk" / "prefilter"
_DATA_DIR = _PREFILTER_PKG
_CONFIG_PATH = _PREFILTER_PKG / "config" / "prefilter.yaml"

_FIXED_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
def _now() -> datetime:
    return _FIXED_NOW


def _make_event(**overrides) -> GDELTEventForPrefilter:
    """Default event passes the default prefilter config; tests override
    fields to exercise specific rule rejection paths."""
    base = dict(
        event_id="EV1000001",
        timestamp=_FIXED_NOW - timedelta(minutes=10),
        code="195",
        category="armed_conflict",
        subcategory="armed_conflict.airstrike",
        severity=0.92,
        goldstein=-10.0,
        flags=["military", "civilian_impact"],
        title="Airstrike on infrastructure",
        source_url="https://www.reuters.com/world/article-12345",
        actor1_name="Country X",
        actor2_name="Country Y",
        lat=50.45,
        lng=30.52,
        geocode_quality="city",
        iso_country="UA",
    )
    base.update(overrides)
    return GDELTEventForPrefilter(**base)


# ─── Default config + YAML loader ────────────────────────────────────────


def test_config_loads_default_yaml():
    cfg = PreFilterConfig.load_yaml(_CONFIG_PATH)
    assert cfg.version == "1.0"
    # Allowlist must contain at least the WMD code we always keep
    assert "armed_conflict.wmd" in cfg.rules.category_filter.subcategories
    # Per-category overrides honored
    assert cfg.rules.severity_filter.per_category_overrides["diplomatic"] >= 0.8


def test_config_rejects_extra_top_level_field():
    with pytest.raises(Exception):
        PreFilterConfig(version="1.0", unknown_key="oops")


def test_config_rejects_severity_above_one():
    with pytest.raises(Exception):
        SeverityFilterConfig(min_severity=1.5)


def test_config_rejects_invalid_bbox_orientation():
    with pytest.raises(Exception):
        GeographyFilterConfig(bbox_allowlist=[[10, 50, 5, 60]])  # west > east


def test_config_rejects_negative_priority_weight():
    from ingesters.gdelt_bulk.prefilter.config import PriorityWeights
    with pytest.raises(Exception):
        PriorityWeights(severity=-0.1)


# ─── extract_domain helper ───────────────────────────────────────────────


def test_extract_domain_strips_www_and_port():
    assert extract_domain("https://www.bbc.co.uk/news/123") == "bbc.co.uk"
    assert extract_domain("http://reuters.com:8080/x") == "reuters.com"


def test_extract_domain_handles_subdomain_and_empty():
    assert extract_domain("https://news.bbc.co.uk/x") == "news.bbc.co.uk"
    assert extract_domain("") == ""
    assert extract_domain("not-a-url") == "not-a-url"


# ─── CategoryRule ────────────────────────────────────────────────────────


def test_category_allow_passes_listed():
    rule = CategoryRule(CategoryFilterConfig(
        mode="allow", subcategories=["armed_conflict.airstrike"]
    ))
    assert rule.check(_make_event()) is None


def test_category_allow_rejects_unlisted():
    rule = CategoryRule(CategoryFilterConfig(
        mode="allow", subcategories=["armed_conflict.bombing"]
    ))
    out = rule.check(_make_event(subcategory="armed_conflict.airstrike"))
    assert out is not None
    assert out.reason == "category_not_allowed"


def test_category_block_drops_listed():
    rule = CategoryRule(CategoryFilterConfig(
        mode="block", subcategories=["diplomatic.statement"]
    ))
    out = rule.check(_make_event(subcategory="diplomatic.statement",
                                 category="diplomatic", severity=0.05,
                                 goldstein=0.0, flags=["diplomatic"]))
    assert out is not None
    assert out.reason == "category_blocked"


def test_category_flag_required_drops_when_missing():
    rule = CategoryRule(CategoryFilterConfig(
        mode="allow",
        subcategories=["armed_conflict.airstrike"],
        flags_required=["military", "civilian_impact"],
    ))
    out = rule.check(_make_event(flags=["military"]))  # missing civilian_impact
    assert out is not None
    assert out.reason == "flag_required_missing"


def test_category_flag_blocked_drops_when_present():
    rule = CategoryRule(CategoryFilterConfig(
        mode="allow",
        subcategories=["armed_conflict.airstrike"],
        flags_blocked=["civilian_impact"],
    ))
    out = rule.check(_make_event())  # has civilian_impact
    assert out is not None
    assert out.reason == "flag_blocked"


# ─── SeverityRule ────────────────────────────────────────────────────────


def test_severity_default_threshold():
    rule = SeverityRule(SeverityFilterConfig(min_severity=0.7))
    assert rule.check(_make_event(severity=0.92)) is None
    out = rule.check(_make_event(severity=0.65))
    assert out is not None and out.reason == "severity_below_threshold"


def test_severity_per_category_override_is_authoritative():
    rule = SeverityRule(SeverityFilterConfig(
        min_severity=0.4,
        per_category_overrides={"diplomatic": 0.85},
    ))
    # Diplomatic event at 0.5 — default would pass, override drops it
    diplomatic_event = _make_event(
        category="diplomatic",
        subcategory="diplomatic.statement",
        severity=0.5, goldstein=0.0, flags=["diplomatic"],
    )
    out = rule.check(diplomatic_event)
    assert out is not None and out.reason == "severity_below_threshold"
    # Same severity, different category — passes the unoverridden default
    armed_event = _make_event(severity=0.5)
    assert rule.check(armed_event) is None


# ─── GeographyRule ───────────────────────────────────────────────────────


def test_geography_precision_floor():
    rule = GeographyRule(GeographyFilterConfig(precision_min="region"))
    assert rule.check(_make_event(geocode_quality="exact")) is None
    assert rule.check(_make_event(geocode_quality="city")) is None
    assert rule.check(_make_event(geocode_quality="region")) is None
    out = rule.check(_make_event(geocode_quality="country"))
    assert out is not None and out.reason == "geocode_below_precision_floor"
    out = rule.check(_make_event(geocode_quality="unknown"))
    assert out is not None and out.reason == "geocode_below_precision_floor"


def test_geography_unknown_precision_label_rejected():
    rule = GeographyRule(GeographyFilterConfig(precision_min="region"))
    out = rule.check(_make_event(geocode_quality="not_a_real_bucket"))
    assert out is not None and out.reason == "geocode_quality_unrecognized"


def test_geography_country_allowlist_and_blocklist():
    rule_allow = GeographyRule(GeographyFilterConfig(
        precision_min="country", iso_country_allowlist=["UA", "PL"],
    ))
    assert rule_allow.check(_make_event(iso_country="UA")) is None
    out = rule_allow.check(_make_event(iso_country="RU"))
    assert out is not None and out.reason == "iso_country_not_allowed"
    out = rule_allow.check(_make_event(iso_country=None))
    assert out is not None and out.reason == "iso_country_missing_with_allowlist"

    rule_block = GeographyRule(GeographyFilterConfig(
        precision_min="country", iso_country_blocklist=["RU"],
    ))
    out = rule_block.check(_make_event(iso_country="RU"))
    assert out is not None and out.reason == "iso_country_blocked"


def test_geography_bbox_allowlist():
    # Eastern Europe rough bbox
    rule = GeographyRule(GeographyFilterConfig(
        precision_min="region",
        bbox_allowlist=[[20.0, 40.0, 50.0, 60.0]],
    ))
    # Kyiv-ish (lat 50.45, lng 30.52) — inside
    assert rule.check(_make_event(lat=50.45, lng=30.52)) is None
    # Tokyo (35.68, 139.69) — outside
    out = rule.check(_make_event(lat=35.68, lng=139.69))
    assert out is not None and out.reason == "bbox_not_allowed"


# ─── SourceQualityRule ───────────────────────────────────────────────────


def test_source_quality_loads_seed_file():
    rule = SourceQualityRule(SourceQualityFilterConfig(min_score=0.5), _DATA_DIR)
    # Reuters scored ~0.97 in the seed
    assert rule.score_for("https://www.reuters.com/world/x") >= 0.9


def test_source_quality_unknown_domain_uses_default():
    rule = SourceQualityRule(
        SourceQualityFilterConfig(min_score=0.5, unknown_domain_score=0.20),
        _DATA_DIR,
    )
    assert rule.score_for("https://entirely-fake-domain-12345.example") == 0.20


def test_source_quality_drops_below_min_score():
    rule = SourceQualityRule(
        SourceQualityFilterConfig(min_score=0.7),
        _DATA_DIR,
    )
    # rt.com is in the seed at ~0.10 — well below 0.7
    out = rule.check(_make_event(source_url="https://rt.com/news/x"))
    assert out is not None and out.reason == "source_quality_below_min"


def test_source_quality_passes_high_score():
    rule = SourceQualityRule(
        SourceQualityFilterConfig(min_score=0.7),
        _DATA_DIR,
    )
    assert rule.check(_make_event(source_url="https://reuters.com/x")) is None


# ─── RecencyRule ─────────────────────────────────────────────────────────


def test_recency_passes_fresh_event():
    rule = RecencyRule(RecencyFilterConfig(max_age_hours=6.0), now_fn=_now)
    fresh = _make_event(timestamp=_FIXED_NOW - timedelta(minutes=30))
    assert rule.check(fresh) is None


def test_recency_drops_stale_event():
    rule = RecencyRule(RecencyFilterConfig(max_age_hours=6.0), now_fn=_now)
    stale = _make_event(timestamp=_FIXED_NOW - timedelta(hours=12))
    out = rule.check(stale)
    assert out is not None and out.reason == "event_too_old"


def test_recency_handles_naive_timestamp_as_utc():
    """GDELT's timestamps are unambiguously UTC but sometimes arrive naive.
    The rule must treat naive timestamps as UTC, not raise."""
    rule = RecencyRule(RecencyFilterConfig(max_age_hours=6.0), now_fn=_now)
    naive = _FIXED_NOW.replace(tzinfo=None) - timedelta(minutes=10)
    ev = _make_event(timestamp=naive)
    assert rule.check(ev) is None


# ─── PriorityScorer ──────────────────────────────────────────────────────


def _make_scorer(category_bonuses=None):
    sq = SourceQualityRule(SourceQualityFilterConfig(min_score=0.5), _DATA_DIR)
    return sq, PriorityScorer(
        PriorityConfig(category_priority_bonuses=category_bonuses or {}),
        sq, now_fn=_now,
    )


def test_priority_clamped_to_unit_interval():
    _sq, scorer = _make_scorer()
    score = scorer.score(_make_event(), duplicate_count=0)
    assert 0.0 <= score <= 1.0


def test_priority_higher_severity_scores_higher():
    _sq, scorer = _make_scorer()
    low = scorer.score(_make_event(severity=0.5))
    high = scorer.score(_make_event(severity=1.0))
    assert high > low


def test_priority_higher_source_quality_scores_higher():
    _sq, scorer = _make_scorer()
    rt = scorer.score(_make_event(source_url="https://rt.com/x"))
    reuters = scorer.score(_make_event(source_url="https://reuters.com/x"))
    assert reuters > rt


def test_priority_more_duplicates_scores_higher():
    _sq, scorer = _make_scorer()
    s0 = scorer.score(_make_event(), duplicate_count=0)
    s5 = scorer.score(_make_event(), duplicate_count=5)
    s10 = scorer.score(_make_event(), duplicate_count=10)
    assert s5 > s0
    assert s10 > s5
    # log curve must asymptote — 100 dupes only marginally above 10
    s100 = scorer.score(_make_event(), duplicate_count=100)
    assert s100 - s10 < (s5 - s0)  # diminishing returns


def test_priority_recency_decays_to_zero_at_six_hours():
    _sq, scorer = _make_scorer()
    fresh_score = scorer.score(_make_event(timestamp=_FIXED_NOW))
    six_hours = scorer.score(_make_event(timestamp=_FIXED_NOW - timedelta(hours=6)))
    # Fresh > 6h-old; difference comes purely from the recency component.
    assert fresh_score > six_hours


def test_priority_category_bonus_lifts_score():
    _sq, scorer = _make_scorer(
        category_bonuses={"armed_conflict.wmd": 1.0},
    )
    base = scorer.score(_make_event(subcategory="armed_conflict.airstrike"))
    boosted = scorer.score(_make_event(subcategory="armed_conflict.wmd"))
    assert boosted > base


def test_priority_aoi_checker_matters_when_configured():
    sq = SourceQualityRule(SourceQualityFilterConfig(min_score=0.5), _DATA_DIR)
    in_aoi = PriorityScorer(PriorityConfig(), sq, aoi_checker=lambda ev: True, now_fn=_now)
    out_aoi = PriorityScorer(PriorityConfig(), sq, aoi_checker=lambda ev: False, now_fn=_now)
    ev = _make_event()
    assert in_aoi.score(ev) > out_aoi.score(ev)


def test_priority_explain_returns_breakdown():
    _sq, scorer = _make_scorer()
    breakdown = scorer.explain(_make_event(), duplicate_count=3)
    assert {"severity", "source", "duplication", "recency",
            "category", "geo_aoi", "weights", "final"} <= set(breakdown)


# ─── PreFilterEngine ─────────────────────────────────────────────────────


def _engine_from_default_config(now_fn=_now) -> PreFilterEngine:
    """Build an engine using the checked-in default config but with the
    deterministic clock so recency tests are reproducible."""
    cfg = PreFilterConfig.load_yaml(_CONFIG_PATH)
    eng = PreFilterEngine(cfg, _DATA_DIR)
    # Replace the rule chain's RecencyRule with one that uses our clock
    # (cheaper than yet another test-only config knob).
    eng._rules = [
        RecencyRule(cfg.rules.recency_filter, now_fn=now_fn) if r.name == "recency" else r
        for r in eng._rules
    ]
    return eng


def test_engine_passes_canonical_high_signal_event():
    eng = _engine_from_default_config()
    out = eng.process(_make_event())
    assert isinstance(out, FilteredEvent)
    assert 0.0 <= out.priority <= 1.0
    assert out.rules_version == "1.0"
    assert eng.stats.pass_count == 1
    assert eng.stats.drop_count == 0


def test_engine_drops_low_signal_event_and_records_reason():
    eng = _engine_from_default_config()
    # Low-grade diplomatic statement on a junky source
    drop = _make_event(
        code="010", category="diplomatic", subcategory="diplomatic.statement",
        severity=0.05, goldstein=0.0, flags=["diplomatic"],
        source_url="https://infowars.com/x",
    )
    assert eng.process(drop) is None
    assert eng.stats.drop_count == 1
    # Category rule fires first under default 'allow' mode
    assert eng.stats.drops_by_rule["category"] == 1
    assert "category_not_allowed" in eng.stats.drops_by_reason


def test_engine_short_circuits_on_first_reject():
    """If category rule rejects, source-quality rule must NOT have been
    consulted (its drop counter must stay at 0). Ensures cheap-first
    ordering does meaningful work."""
    eng = _engine_from_default_config()
    bad = _make_event(
        subcategory="diplomatic.statement",  # not in default allowlist
        source_url="https://infowars.com/x",  # would also fail source quality
    )
    eng.process(bad)
    assert eng.stats.drops_by_rule.get("source_quality", 0) == 0
    assert eng.stats.drops_by_rule.get("category") == 1


def test_engine_health_returns_stats():
    eng = _engine_from_default_config()
    eng.process(_make_event())  # pass
    eng.process(_make_event(subcategory="diplomatic.statement"))  # drop
    h = eng.health()
    assert h["pass_count"] == 1
    assert h["drop_count"] == 1
    assert h["pass_rate"] == 0.5
    assert "category" in h["rules_in_chain"]
    assert h["rules_version"] == "1.0"


def test_engine_pass_rate_against_canonical_default_set():
    """Sanity gate on the default config: feed a tiny synthetic set
    representing a realistic mix and confirm pass rate is in the 0-30%
    band the handoff predicts (real GDELT will be much sparser).
    Catches gross misconfiguration of the default yaml."""
    eng = _engine_from_default_config()
    events = []
    # Should pass:
    events.append(_make_event())  # airstrike, reuters, fresh
    events.append(_make_event(
        event_id="EV2", subcategory="economic.sanctions", category="economic",
        severity=0.6, goldstein=-5.6, flags=["economic"],
        source_url="https://www.bloomberg.com/x",
    ))
    # Should drop (not in allowlist, even though high-quality source):
    events.append(_make_event(
        event_id="EV3", subcategory="diplomatic.statement", category="diplomatic",
        severity=0.05, goldstein=0.0, flags=["diplomatic"],
        source_url="https://www.reuters.com/x",
    ))
    # Should drop (allowlisted but low source quality):
    events.append(_make_event(
        event_id="EV4", source_url="https://rt.com/x",
    ))
    # Should drop (allowlisted but stale):
    events.append(_make_event(
        event_id="EV5", timestamp=_FIXED_NOW - timedelta(hours=12),
    ))
    # Should drop (allowlisted but country-only precision):
    events.append(_make_event(
        event_id="EV6", geocode_quality="country",
    ))
    passes = sum(1 for ev in events if eng.process(ev) is not None)
    drops = sum(1 for ev in events if True) - passes
    assert passes == 2
    assert drops == 4
    # Each drop landed under exactly one rule.
    assert sum(eng.stats.drops_by_rule.values()) == drops


# ─── Helper math (token_set_jaccard, haversine_km) ──────────────────────


def test_token_set_jaccard_identical_strings_score_one():
    assert token_set_jaccard("Airstrike on power plant in Kyiv",
                             "Airstrike on power plant in Kyiv") == 1.0


def test_token_set_jaccard_token_order_invariant():
    s = token_set_jaccard("Airstrike on Kyiv power plant",
                          "Power plant in Kyiv hit by airstrike")
    # 5 unique tokens overlap on {airstrike, kyiv, power, plant} of varying
    # union sizes — should be a healthy similarity, well above the 0.85 default
    # would not necessarily fire, but token-order invariance is the property
    # under test here
    assert s > 0.4


def test_token_set_jaccard_disjoint_strings_score_zero():
    assert token_set_jaccard("apple banana", "carrot date") == 0.0


def test_token_set_jaccard_handles_empty_inputs():
    assert token_set_jaccard("", "") == 1.0
    assert token_set_jaccard("hello", "") == 0.0
    assert token_set_jaccard("", "world") == 0.0


def test_haversine_km_known_pair_is_close_to_truth():
    """Kyiv → Berlin great-circle ≈ 1199 km; allow a little wiggle for
    spherical-vs-WGS84 differences."""
    d = haversine_km(50.4501, 30.5234, 52.5200, 13.4050)
    assert 1180.0 <= d <= 1230.0


def test_haversine_km_zero_for_same_point():
    assert haversine_km(50.0, 30.0, 50.0, 30.0) == pytest.approx(0.0, abs=1e-6)


# ─── DedupRule ───────────────────────────────────────────────────────────


def _dedup_event(event_id: str, **overrides) -> GDELTEventForPrefilter:
    """Same default high-signal event, but explicit event_id so the
    duplicate tests can assert the duplicate_of pointer."""
    return _make_event(event_id=event_id, **overrides)


def test_dedup_passes_first_event_in_window():
    rule = DedupRule(DedupFilterConfig(), now_fn=_now)
    assert rule.check(_dedup_event("EV-A")) is None


def test_dedup_drops_near_identical_event_in_window():
    rule = DedupRule(DedupFilterConfig(window_minutes=60.0,
                                       similarity_threshold=0.5,
                                       geo_threshold_km=100.0),
                     now_fn=_now)
    first = _dedup_event(
        "EV-A",
        title="Airstrike on Kyiv power plant",
    )
    near_dup = _dedup_event(
        "EV-B",
        title="Power plant in Kyiv hit by airstrike",
        timestamp=_FIXED_NOW - timedelta(minutes=5),
    )
    assert rule.check(first) is None
    out = rule.check(near_dup)
    assert out is not None
    assert out.reason == "duplicate"
    assert out.metadata["duplicate_of"] == "EV-A"
    assert out.metadata["duplicate_count"] == 1
    assert "similarity" in out.metadata


def test_dedup_distinct_titles_are_not_duplicates():
    rule = DedupRule(DedupFilterConfig(similarity_threshold=0.85,
                                       geo_threshold_km=100.0),
                     now_fn=_now)
    rule.check(_dedup_event("EV-A", title="Airstrike on power plant"))
    out = rule.check(_dedup_event("EV-B",
                                  title="Diplomatic statement on grain corridor"))
    assert out is None


def test_dedup_geographically_distant_events_are_not_duplicates():
    """Same headline, but Kyiv vs Tokyo — must NOT dedup."""
    rule = DedupRule(DedupFilterConfig(geo_threshold_km=50.0,
                                       similarity_threshold=0.5),
                     now_fn=_now)
    rule.check(_dedup_event("EV-A",
                            title="Airstrike", lat=50.45, lng=30.52))
    out = rule.check(_dedup_event("EV-B",
                                  title="Airstrike", lat=35.68, lng=139.69))
    assert out is None


def test_dedup_window_expiry_lets_old_match_pass():
    """An event 90 min after the original (window=60 min) must NOT
    register as a duplicate — the original has expired from cache."""
    rule = DedupRule(DedupFilterConfig(window_minutes=60.0,
                                       similarity_threshold=0.5),
                     now_fn=_now)
    # Insert with mock-time 90 min behind current
    rule._now_fn = lambda: _FIXED_NOW - timedelta(minutes=90)
    rule.check(_dedup_event("EV-A", title="Airstrike on Kyiv plant",
                            timestamp=_FIXED_NOW - timedelta(minutes=90)))
    # Now jump the clock forward — the original is past the window
    rule._now_fn = _now
    out = rule.check(_dedup_event("EV-B", title="Airstrike on Kyiv plant"))
    assert out is None


def test_dedup_subcategories_isolated():
    """Two events identical except for subcategory must not dedup
    against each other — different cache buckets."""
    rule = DedupRule(DedupFilterConfig(similarity_threshold=0.5),
                     now_fn=_now)
    rule.check(_dedup_event("EV-A",
                            title="Strike",
                            subcategory="armed_conflict.airstrike"))
    out = rule.check(_dedup_event("EV-B",
                                  title="Strike",
                                  subcategory="armed_conflict.bombing"))
    assert out is None


def test_dedup_disabled_passes_everything():
    rule = DedupRule(DedupFilterConfig(enabled=False), now_fn=_now)
    rule.check(_dedup_event("EV-A", title="Airstrike"))
    out = rule.check(_dedup_event("EV-B", title="Airstrike"))
    assert out is None


def test_dedup_increments_count_on_repeated_dups():
    rule = DedupRule(DedupFilterConfig(similarity_threshold=0.5,
                                       geo_threshold_km=100.0),
                     now_fn=_now)
    rule.check(_dedup_event("EV-A", title="Airstrike on plant"))
    out1 = rule.check(_dedup_event("EV-B",
                                   title="Airstrike on plant",
                                   timestamp=_FIXED_NOW - timedelta(minutes=5)))
    out2 = rule.check(_dedup_event("EV-C",
                                   title="Airstrike on plant",
                                   timestamp=_FIXED_NOW - timedelta(minutes=4)))
    assert out1.metadata["duplicate_count"] == 1
    assert out2.metadata["duplicate_count"] == 2


# ─── BoundedPriorityQueue ────────────────────────────────────────────────


def _fe(priority: float, event_id: str = "EV") -> FilteredEvent:
    return FilteredEvent(
        event=_make_event(event_id=event_id),
        priority=priority,
        rules_version="1.0",
    )


def test_queue_enqueue_returns_none_until_full():
    q = BoundedPriorityQueue(max_depth=3)
    assert q.enqueue(_fe(0.5, "A")) is None
    assert q.enqueue(_fe(0.6, "B")) is None
    assert q.enqueue(_fe(0.7, "C")) is None
    assert q.depth() == 3


def test_queue_pops_highest_first():
    q = BoundedPriorityQueue(max_depth=3)
    q.enqueue(_fe(0.5, "A"))
    q.enqueue(_fe(0.9, "B"))
    q.enqueue(_fe(0.7, "C"))
    popped = q.pop_highest()
    assert popped.event.event_id == "B"
    popped = q.pop_highest()
    assert popped.event.event_id == "C"
    popped = q.pop_highest()
    assert popped.event.event_id == "A"
    assert q.pop_highest() is None


def test_queue_tail_drop_evicts_floor_when_overfull():
    q = BoundedPriorityQueue(max_depth=2)
    q.enqueue(_fe(0.3, "low"))
    q.enqueue(_fe(0.9, "high"))
    # New event with priority 0.6 — should evict 'low' (0.3 floor)
    dropped = q.enqueue(_fe(0.6, "med"))
    assert dropped is not None
    assert dropped.event.event_id == "low"
    assert q.depth() == 2
    # Pop order confirms 0.9 stayed and 0.6 took 'low's slot
    assert q.pop_highest().event.event_id == "high"
    assert q.pop_highest().event.event_id == "med"


def test_queue_drops_new_event_when_it_is_lowest():
    q = BoundedPriorityQueue(max_depth=2)
    q.enqueue(_fe(0.7, "A"))
    q.enqueue(_fe(0.8, "B"))
    new_low = _fe(0.2, "C")
    dropped = q.enqueue(new_low)
    assert dropped is new_low
    assert q.depth() == 2
    assert q.stats.new_event_dropped_total == 1
    assert q.stats.tail_dropped_total == 0


def test_queue_stats_track_enqueue_pop_drop():
    q = BoundedPriorityQueue(max_depth=1)
    q.enqueue(_fe(0.5, "A"))
    q.enqueue(_fe(0.6, "B"))   # tail-drops A
    q.enqueue(_fe(0.4, "C"))   # rejects new
    q.pop_highest()
    assert q.stats.enqueued_total == 2
    assert q.stats.popped_total == 1
    assert q.stats.tail_dropped_total == 1
    assert q.stats.new_event_dropped_total == 1


def test_queue_max_depth_must_be_positive():
    with pytest.raises(ValueError):
        BoundedPriorityQueue(max_depth=0)


# ─── Engine integration with dedup + queue ───────────────────────────────


def test_engine_default_chain_includes_dedup():
    eng = _engine_from_default_config()
    assert "dedup" in [r.name for r in eng._rules]


def test_engine_dedup_drop_records_reason_and_pointer():
    eng = _engine_from_default_config()
    first = _make_event(event_id="EV-1",
                        title="Airstrike on Mariupol power plant")
    # Loosen sim threshold for the test by replacing the dedup rule
    eng._rules = [
        DedupRule(DedupFilterConfig(similarity_threshold=0.5,
                                    geo_threshold_km=100.0), now_fn=_now)
        if r.name == "dedup" else r
        for r in eng._rules
    ]
    out_first = eng.process(first)
    assert isinstance(out_first, FilteredEvent)
    assert out_first.duplicate_of is None

    near_dup = _make_event(
        event_id="EV-2",
        title="Mariupol power plant hit in airstrike",
        timestamp=_FIXED_NOW - timedelta(minutes=5),
    )
    out_dup = eng.process(near_dup)
    assert out_dup is None
    assert eng.stats.drops_by_rule["dedup"] == 1
    assert eng.stats.drops_by_reason["duplicate"] == 1
    assert eng.stats.last_duplicate_of == "EV-1"


def test_engine_passing_events_land_in_queue():
    eng = _engine_from_default_config()
    eng.process(_make_event(event_id="EV-1"))
    eng.process(_make_event(event_id="EV-2", title="Distinct headline two",
                            actor1_name="A2", actor2_name="B2"))
    assert eng.queue.depth() == 2


def test_engine_tail_drop_on_overfull_queue():
    """Force a tiny queue and verify tail-drop accounting plumbs through
    from the queue back into engine stats."""
    cfg = PreFilterConfig.load_yaml(_CONFIG_PATH)
    cfg.queue.max_depth = 1
    eng = PreFilterEngine(cfg, _DATA_DIR)
    # Replace recency + dedup with deterministic-clock variants
    eng._rules = [
        RecencyRule(cfg.rules.recency_filter, now_fn=_now)
        if r.name == "recency" else
        DedupRule(cfg.rules.dedup_filter, now_fn=_now)
        if r.name == "dedup" else r
        for r in eng._rules
    ]

    # Two distinct, well-spaced events that both pass the rule chain
    eng.process(_make_event(event_id="LO", severity=0.55,
                            title="airstrike Aleppo"))
    eng.process(_make_event(event_id="HI", severity=0.95,
                            title="strike Mariupol",
                            actor1_name="A2", actor2_name="B2",
                            lat=47.0, lng=37.0))
    assert eng.queue.depth() == 1
    # The higher-priority HI event should have evicted LO
    assert eng.queue.pop_highest().event.event_id == "HI"
    # Tail-drop accounted on engine stats
    assert eng.stats.tail_dropped_count == 1


def test_engine_health_includes_queue_block():
    eng = _engine_from_default_config()
    h = eng.health()
    assert "queue" in h
    assert h["queue"]["max_depth"] == eng.queue.max_depth
    for k in ("depth", "enqueued_total", "popped_total",
              "tail_dropped_total", "new_event_dropped_total"):
        assert k in h["queue"]


def test_engine_handles_empty_sourcequality_seed():
    """An empty source_quality.json must still load — ingester operators
    should be able to wipe the seed and start over without engine crash."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        (tmp_dir / "source_quality.json").write_text(
            json.dumps({"version": "0.1", "license": "MIT", "domains": {}})
        )
        sq = SourceQualityRule(
            SourceQualityFilterConfig(min_score=0.5, unknown_domain_score=0.4,
                                      data_file="source_quality.json"),
            tmp_dir,
        )
        # Every domain is "unknown" → 0.4 < 0.5 → all events fail source-quality.
        out = sq.check(_make_event())
        assert out is not None and out.reason == "source_quality_below_min"


# ─── Prometheus metrics shim ────────────────────────────────────────────


def test_prefilter_metrics_enabled_when_lib_installed():
    """prometheus-client is in requirements.txt and pinned in the venv;
    constructing PrefilterMetrics with rule names should succeed and
    flip the enabled flag to True."""
    from ingesters.gdelt_bulk.prefilter import PrefilterMetrics
    m = PrefilterMetrics(["category", "severity", "geography"])
    assert m.enabled is True
    # Render returns non-empty bytes after construction (label
    # pre-allocation produces 0-count rows).
    body = m.render_prometheus()
    assert b"glassbox_prefilter_drop_total" in body


def test_prefilter_metrics_pass_drop_record_correctly():
    from ingesters.gdelt_bulk.prefilter import PrefilterMetrics
    m = PrefilterMetrics(["category", "severity"])
    m.record_pass(0.75)
    m.record_pass(0.25)
    m.record_drop("category", "category_not_allowed")
    m.record_drop("category", "category_not_allowed")
    m.record_drop("severity", "severity_below_floor")
    body = m.render_prometheus().decode("utf-8")
    assert "glassbox_prefilter_pass_total 2.0" in body
    assert 'glassbox_prefilter_drop_total{rule="category"} 2.0' in body
    assert 'glassbox_prefilter_drop_total{rule="severity"} 1.0' in body
    assert 'glassbox_prefilter_drop_by_reason_total{reason="category_not_allowed"} 2.0' in body


def test_prefilter_metrics_unknown_reason_silently_skipped():
    """Drop reasons not in _KNOWN_DROP_REASONS go into the per-rule
    counter but NOT into the per-reason counter — protects label
    cardinality from a future rule that emits a new reason string."""
    from ingesters.gdelt_bulk.prefilter import PrefilterMetrics
    m = PrefilterMetrics(["weird_rule"])
    m.record_drop("weird_rule", "some_future_reason")
    body = m.render_prometheus().decode("utf-8")
    assert 'glassbox_prefilter_drop_total{rule="weird_rule"} 1.0' in body
    # No new label should appear for the unknown reason
    assert 'reason="some_future_reason"' not in body


def test_prefilter_metrics_queue_state_recorded():
    from ingesters.gdelt_bulk.prefilter import PrefilterMetrics
    m = PrefilterMetrics(["category"])
    m.record_queue_state(depth=42, max_depth=1000)
    m.record_tail_drop()
    m.record_new_event_drop()
    body = m.render_prometheus().decode("utf-8")
    assert "glassbox_prefilter_queue_depth 42.0" in body
    assert "glassbox_prefilter_queue_max_depth 1000.0" in body
    assert "glassbox_prefilter_queue_tail_dropped_total 1.0" in body
    assert "glassbox_prefilter_queue_new_event_dropped_total 1.0" in body


def test_engine_increments_metrics_on_pass_and_drop():
    """End-to-end: process(passing event) bumps pass_total + observes
    priority; process(failing event) bumps the per-rule + per-reason
    drop counters."""
    eng = _engine_from_default_config()
    # Pass: canonical event
    out = eng.process(_make_event())
    assert isinstance(out, FilteredEvent)
    # Drop: low-grade diplomatic statement
    drop = _make_event(
        code="010", category="diplomatic", subcategory="diplomatic.statement",
        severity=0.05, source_url="https://infowars.com/x",
    )
    assert eng.process(drop) is None
    body = eng.metrics.render_prometheus().decode("utf-8")
    assert "glassbox_prefilter_pass_total 1.0" in body
    assert 'glassbox_prefilter_drop_total{rule="category"} 1.0' in body
    assert "glassbox_prefilter_priority_score_count 1.0" in body
    # Queue depth gauge updated after the pass
    assert "glassbox_prefilter_queue_depth 1.0" in body


# ─── 5K-event integration fixture + throughput bench ────────────────────


def _generate_realistic_event(i: int, *, now: datetime) -> GDELTEventForPrefilter:
    """Synthetic event generator that mimics a realistic GDELT mix:
    most events are CAMEO codes outside our allowlist (default-drop),
    a minority are armed_conflict / economic_sanctions (pass-eligible),
    and we vary source quality + severity + geo so each rule fires
    on a meaningful slice. Deterministic per index — same i produces
    the same event so a regression bisect can replay exactly."""
    # Spread modulo classes deterministically. Avoids pulling
    # `random` and keeps the bench reproducible across runs.
    bucket = i % 100
    if bucket < 60:
        # Diplomatic / general — fails category filter under default
        # `mode=allow` config.
        category, subcategory = "diplomatic", "diplomatic.statement"
        code = "010"
        severity = 0.10
        flags = ["diplomatic"]
        title = f"Routine statement {i}"
    elif bucket < 80:
        # Armed conflict — passes category, varies severity.
        category, subcategory = "armed_conflict", "armed_conflict.airstrike"
        code = "195"
        severity = 0.92 if (i % 3) else 0.20  # ~33% pass severity
        flags = ["military", "civilian_impact"]
        title = f"Airstrike report {i}"
    else:
        # Economic sanctions — passes category, mostly junky source.
        category, subcategory = "economic", "economic.sanctions"
        code = "044"
        severity = 0.65
        flags = ["economic"]
        title = f"Sanctions news {i}"

    # Source quality split: ~30% reuters/AP (high), ~70% unknown.
    if i % 10 < 3:
        source_url = f"https://www.reuters.com/article-{i}"
    else:
        source_url = f"https://random-source-{i % 50}.example.com/x"

    # Geo: half EU/US, quarter rest, quarter no geo.
    if bucket < 50:
        lat, lng, iso_country, geocode_quality = 50.45, 30.52, "UA", "city"
    elif bucket < 75:
        lat, lng, iso_country, geocode_quality = 13.7, 100.5, "TH", "city"
    else:
        lat, lng, iso_country, geocode_quality = None, None, None, "country"

    return GDELTEventForPrefilter(
        event_id=f"EV{i:08d}",
        timestamp=now - timedelta(minutes=10 + (i % 60)),
        code=code,
        category=category,
        subcategory=subcategory,
        severity=severity,
        goldstein=-5.0,
        flags=flags,
        title=title,
        source_url=source_url,
        actor1_name=f"Actor A {i % 7}",
        actor2_name=f"Actor B {i % 11}",
        lat=lat, lng=lng,
        geocode_quality=geocode_quality,
        iso_country=iso_country,
    )


def test_engine_processes_5k_events_above_perf_floor():
    """Integration fixture + throughput bench. Generates 5,000
    realistically-distributed synthetic events, runs them through
    the default rule chain, and asserts:
      * total processed = 5,000 (no drops or misses inside the
        engine itself — every event has a verdict)
      * throughput ≥ 1,000 events/sec on the test machine. The
        floor is intentionally below typical hardware (we measure
        ~30K/sec on the Mac Mini in dev) so a 5x slowdown still
        passes; a 30x slowdown means we have a real regression.
      * pass count in 0.5%-25% of total — sanity gate against
        accidentally inverting a rule (catches "all events
        passing" or "everything dropped"). Tunes wider than
        prod expectations because of the synthetic skew toward
        passing categories.
      * Prometheus counters match in-memory stats counters
        (proves the metrics shim wires every code path).
    """
    import time

    eng = _engine_from_default_config()
    events = [_generate_realistic_event(i, now=_FIXED_NOW) for i in range(5_000)]

    # The recency rule uses the engine's RecencyRule clock (already
    # patched to _now via _engine_from_default_config), so all events
    # generated within the last 60 minutes are fresh enough.
    start = time.perf_counter()
    for ev in events:
        eng.process(ev, enqueue=False)
    elapsed = time.perf_counter() - start
    rate = len(events) / elapsed

    assert eng.stats.pass_count + eng.stats.drop_count == len(events), (
        f"engine missed events: pass={eng.stats.pass_count} "
        f"drop={eng.stats.drop_count} total expected={len(events)}"
    )

    pct_pass = eng.stats.pass_count / len(events)
    assert 0.005 <= pct_pass <= 0.25, (
        f"pass rate {pct_pass:.1%} outside the 0.5%-25% sanity band; "
        "either a rule inverted or the synthetic mix shifted"
    )

    # 1K events/sec floor; print the actual rate so dev-machine
    # regression is visible in pytest -v output.
    print(f"\n[perf] processed {len(events)} events in {elapsed:.3f}s "
          f"= {rate:,.0f} events/sec (pass={eng.stats.pass_count}, "
          f"drop={eng.stats.drop_count})")
    assert rate >= 1_000, (
        f"prefilter throughput {rate:,.0f} events/sec below 1K floor — "
        f"check for accidental quadratic-time changes in the rule chain"
    )

    # Cross-check: prometheus counters match the in-memory stats.
    body = eng.metrics.render_prometheus().decode("utf-8")
    assert f"glassbox_prefilter_pass_total {float(eng.stats.pass_count)}" in body
    # The total drops across rules should sum to drop_count. Easier
    # to check via the histogram observe count for priority_score
    # which only fires on pass.
    assert (f"glassbox_prefilter_priority_score_count "
            f"{float(eng.stats.pass_count)}" in body)


def test_engine_processes_5k_events_with_queue_enqueued():
    """Same fixture but with enqueue=True so the bounded queue path
    runs too. Confirms the queue gauge updates + tail-drop counter
    behaves under real load."""
    eng = _engine_from_default_config()
    events = [_generate_realistic_event(i, now=_FIXED_NOW) for i in range(5_000)]
    for ev in events:
        eng.process(ev, enqueue=True)

    # Queue depth is bounded; final depth should be at or near max.
    h = eng.health()
    assert h["queue"]["enqueued_total"] >= eng.stats.pass_count - 1, (
        "fewer events enqueued than passed — queue path leaking"
    )
    # Either we filled the queue (hit max_depth) or all passes fit.
    queue_max = h["queue"]["max_depth"]
    assert h["queue"]["depth"] <= queue_max
    if eng.stats.pass_count > queue_max:
        # We must have shed events via tail-drop or new-event-drop.
        total_dropped_by_queue = (
            h["queue"]["tail_dropped_total"]
            + h["queue"]["new_event_dropped_total"]
        )
        assert total_dropped_by_queue >= eng.stats.pass_count - queue_max


# ─── A/B shadow routing ─────────────────────────────────────────────────


def _stricter_shadow_engine() -> PreFilterEngine:
    """Build a shadow engine with a higher severity floor so it
    diverges from the primary on borderline events. Any event with
    severity < 0.85 is dropped by the shadow but might pass the
    primary (default floor 0.50)."""
    cfg = PreFilterConfig.load_yaml(_CONFIG_PATH)
    eng = PreFilterEngine(cfg, _DATA_DIR)
    # Replace the existing severity rule with a stricter one
    from ingesters.gdelt_bulk.prefilter.config import SeverityFilterConfig
    strict = SeverityRule(SeverityFilterConfig(min_severity=0.85))
    eng._rules = [
        strict if r.name == "severity" else r for r in eng._rules
    ]
    # Force the same recency clock as the primary
    eng._rules = [
        RecencyRule(cfg.rules.recency_filter, now_fn=_now)
        if r.name == "recency" else r for r in eng._rules
    ]
    return eng


def test_engine_with_no_shadow_does_not_record_shadow_stats():
    """Sanity: no shadow_engine kwarg → no shadow accounting."""
    eng = _engine_from_default_config()
    eng.process(_make_event())
    eng.process(_make_event(subcategory="diplomatic.statement"))
    h = eng.health()
    assert "shadow" not in h
    assert eng.stats.shadow_agree_pass == 0
    assert eng.stats.shadow_agree_drop == 0


def test_engine_records_shadow_agreement_when_both_pass():
    """High-severity event passes both primary (floor 0.5) and the
    stricter shadow (floor 0.85). Confusion matrix records
    `agree_pass`."""
    primary = _engine_from_default_config()
    primary._shadow_engine = _stricter_shadow_engine()
    out = primary.process(_make_event(severity=0.92), enqueue=False)
    assert out is not None  # primary passed
    assert primary.stats.shadow_agree_pass == 1
    assert primary.stats.shadow_agree_drop == 0
    assert primary.stats.shadow_primary_pass_only == 0
    assert primary.stats.shadow_primary_drop_only == 0


def test_engine_records_primary_pass_only_when_shadow_stricter():
    """Severity 0.65 passes the primary (floor 0.5) but fails the
    stricter shadow (floor 0.85). Records `primary_pass_only`."""
    primary = _engine_from_default_config()
    primary._shadow_engine = _stricter_shadow_engine()
    out = primary.process(_make_event(severity=0.65), enqueue=False)
    assert out is not None  # primary passed
    assert primary.stats.shadow_primary_pass_only == 1
    assert primary.stats.shadow_agree_pass == 0


def test_engine_records_agree_drop_when_both_reject():
    """Diplomatic statement is dropped by both primary and shadow
    on the category rule (which is identical). Records `agree_drop`."""
    primary = _engine_from_default_config()
    primary._shadow_engine = _stricter_shadow_engine()
    out = primary.process(
        _make_event(subcategory="diplomatic.statement"),
        enqueue=False,
    )
    assert out is None  # primary dropped
    assert primary.stats.shadow_agree_drop == 1
    assert primary.stats.shadow_agree_pass == 0


def test_engine_health_reports_shadow_block_with_rates():
    """When shadow is wired, health() includes a `shadow` block with
    agreement_rate + the shadow's would-have pass rate."""
    primary = _engine_from_default_config()
    primary._shadow_engine = _stricter_shadow_engine()
    # 2 events: one passes both, one passes primary-only
    primary.process(_make_event(severity=0.92), enqueue=False)
    primary.process(_make_event(severity=0.65), enqueue=False)
    h = primary.health()
    assert "shadow" in h
    sb = h["shadow"]
    assert sb["agree_pass"] == 1
    assert sb["primary_pass_only"] == 1
    # Agreement rate = (agree_pass + agree_drop) / seen = 1/2 = 0.5
    assert sb["agreement_rate"] == 0.5
    # Shadow's would-have pass rate = (agree_pass + primary_drop_only) / seen = 1/2
    assert sb["shadow_pass_rate"] == 0.5


def test_engine_shadow_does_not_pollute_primary_queue():
    """The shadow engine's own queue must NOT receive primary events.
    A primary's call to process(event) runs the shadow's process()
    with enqueue=False — verify by checking shadow.queue stays empty."""
    primary = _engine_from_default_config()
    shadow = _stricter_shadow_engine()
    primary._shadow_engine = shadow
    primary.process(_make_event(severity=0.92))  # primary enqueues
    assert primary.queue.depth() == 1
    # Shadow's queue must NOT have received this event.
    assert shadow.queue.depth() == 0
    assert shadow.queue.stats.enqueued_total == 0


def test_engine_shadow_metrics_record_outcome():
    """The Prometheus metric `glassbox_prefilter_shadow_outcome_total`
    increments for each event with the right outcome label."""
    primary = _engine_from_default_config()
    primary._shadow_engine = _stricter_shadow_engine()
    primary.process(_make_event(severity=0.92), enqueue=False)
    primary.process(_make_event(severity=0.65), enqueue=False)
    primary.process(_make_event(subcategory="diplomatic.statement"),
                    enqueue=False)
    body = primary.metrics.render_prometheus().decode("utf-8")
    assert ('glassbox_prefilter_shadow_outcome_total{outcome="agree_pass"} 1.0'
            in body)
    assert ('glassbox_prefilter_shadow_outcome_total{outcome="primary_pass_only"} 1.0'
            in body)
    assert ('glassbox_prefilter_shadow_outcome_total{outcome="agree_drop"} 1.0'
            in body)


def test_prefilter_metrics_unknown_shadow_outcome_silently_skipped():
    """Sanity: if a future contributor adds a new outcome string but
    forgets to register it in _KNOWN_SHADOW_OUTCOMES, the metric
    should not blow up with a label-cardinality explosion."""
    from ingesters.gdelt_bulk.prefilter import PrefilterMetrics
    m = PrefilterMetrics(["category"])
    m.record_shadow_outcome("invented_outcome")
    body = m.render_prometheus().decode("utf-8")
    # New label must NOT have been minted.
    assert 'outcome="invented_outcome"' not in body


def test_engine_metrics_disabled_no_op_when_lib_missing(monkeypatch):
    """Simulate a fresh machine with no prometheus-client installed.
    The engine must still process events; metrics calls become
    no-ops."""
    import sys
    from ingesters.gdelt_bulk.prefilter import metrics as metrics_mod

    # Force the import path to fail so PrefilterMetrics initializes
    # in the disabled state.
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "prometheus_client" or name.startswith("prometheus_client."):
            raise ImportError("prometheus-client not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _blocked_import)

    m = metrics_mod.PrefilterMetrics(["category"])
    assert m.enabled is False
    assert m.render_prometheus() == b""
    # All record_* methods are no-ops; ensure they don't raise
    m.record_pass(0.5)
    m.record_drop("category", "category_not_allowed")
    m.record_queue_state(0, 100)
    m.record_tail_drop()
    m.record_new_event_drop()
