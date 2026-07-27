"""
Phase 5c — cascade-rule firing-rate audit tests.

Asserts:
  - _count_rule_firings tallies labels across clusters
  - audit_firing_rates flags a rule as 'elevated' when today's rate is
    materially above the historical baseline AND the binomial test
    rejects the null at p < 0.05
  - audit_firing_rates does NOT flag a rule that fires at the same rate
    as baseline
  - audit_firing_rates does NOT flag a rule with too few today firings
    (the min_today_firings guard prevents single-cluster spurious flags)
  - audit_firing_rates omits rules that never fired in either window
  - results are sorted: elevated-first, then descending elevation,
    ascending p-value
  - existing find_correlations behavior unchanged (cascade rules still
    fire correctly for cells with multi-layer events)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_correlator_firing_rates.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from correlator import (  # noqa: E402
    _count_rule_firings,
    audit_firing_rates,
    find_correlations,
)


def _cluster_with_rule(rule_label: str) -> dict:
    """Build a minimal cluster dict that records a single fired rule."""
    return {
        "center_lat": 30.0,
        "center_lng": -90.0,
        "layers_present": ["a", "b"],
        "event_count": 2,
        "cascade_rules_fired": [{"layers": ["a", "b"], "severity": 8, "rule": rule_label}],
        "severity": 8,
        "sample_events": [],
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── Pure-fn helpers ──────────────────────────────────────────────────────


def test_count_rule_firings_counts_labels():
    clusters = [
        _cluster_with_rule("Tsunami generation risk"),
        _cluster_with_rule("Tsunami generation risk"),
        _cluster_with_rule("Hybrid warfare indicators"),
        {"cascade_rules_fired": []},  # empty fires nothing
        {},                            # missing key fires nothing
    ]
    counts = _count_rule_firings(clusters)
    assert counts["Tsunami generation risk"] == 2
    assert counts["Hybrid warfare indicators"] == 1
    assert "anything else" not in counts


# ─── audit_firing_rates ───────────────────────────────────────────────────


def test_audit_flags_elevated_rule():
    """
    Today: 50 clusters, "Tsunami" fires in 30 (rate=0.6).
    History: 200 clusters, "Tsunami" fires in 4 (rate=0.02).
    elevation = 30 baseline -> well above 3x; p<<0.05.
    """
    today = (
        [_cluster_with_rule("Tsunami generation risk") for _ in range(30)]
        + [{"cascade_rules_fired": []} for _ in range(20)]
    )
    history = (
        [_cluster_with_rule("Tsunami generation risk") for _ in range(4)]
        + [{"cascade_rules_fired": []} for _ in range(196)]
    )
    audit = audit_firing_rates(today, history)
    rules = {r["rule"]: r for r in audit}
    flagged = rules["Tsunami generation risk"]
    assert flagged["today_firings"] == 30
    assert flagged["history_firings"] == 4
    assert flagged["elevation"] > 3.0
    assert flagged["p_value"] < 0.05
    assert flagged["elevated"] is True


def test_audit_does_not_flag_steady_rate():
    """If today's rate matches baseline within noise, no flag."""
    # 4/100 today, 8/200 baseline -> both 0.04
    today = (
        [_cluster_with_rule("Aviation weather disruption") for _ in range(4)]
        + [{"cascade_rules_fired": []} for _ in range(96)]
    )
    history = (
        [_cluster_with_rule("Aviation weather disruption") for _ in range(8)]
        + [{"cascade_rules_fired": []} for _ in range(192)]
    )
    audit = audit_firing_rates(today, history)
    rules = {r["rule"]: r for r in audit}
    r = rules["Aviation weather disruption"]
    assert r["elevated"] is False
    # Roughly equal rates -> elevation around 1.0
    assert 0.5 < r["elevation"] < 2.0


def test_audit_skips_low_today_firings():
    """A single firing today should not flag, even with zero history baseline."""
    today = [_cluster_with_rule("Hybrid warfare indicators")] + [
        {"cascade_rules_fired": []} for _ in range(99)
    ]
    history = [{"cascade_rules_fired": []} for _ in range(500)]
    audit = audit_firing_rates(today, history, min_today_firings=2)
    rules = {r["rule"]: r for r in audit}
    r = rules["Hybrid warfare indicators"]
    assert r["today_firings"] == 1
    assert r["elevated"] is False


def test_audit_omits_rules_with_zero_signal():
    """Rules that never fire in either window are not in the output."""
    today = [_cluster_with_rule("Tsunami generation risk")]
    history = [_cluster_with_rule("Tsunami generation risk")]
    audit = audit_firing_rates(today, history)
    rules = {r["rule"] for r in audit}
    # Only the one rule is in the output; the other 19 cascade rules are absent
    assert "Tsunami generation risk" in rules
    assert "Aviation weather disruption" not in rules


def test_audit_sort_order_elevated_first_then_by_elevation():
    """
    Build two elevated rules with different elevation magnitudes + one
    non-elevated rule. Verify ordering: elevated-first, then descending
    elevation.
    """
    # Rule A: massive elevation
    # Rule B: modest elevation but still flagged
    # Rule C: same as baseline (not elevated)
    today = (
        [_cluster_with_rule("A") for _ in range(40)]      # 40/100
        + [_cluster_with_rule("B") for _ in range(15)]    # 15/100
        + [_cluster_with_rule("C") for _ in range(5)]     # 5/100
        + [{"cascade_rules_fired": []} for _ in range(40)]
    )
    history = (
        [_cluster_with_rule("A") for _ in range(2)]       # 2/200 = 0.01
        + [_cluster_with_rule("B") for _ in range(8)]     # 8/200 = 0.04
        + [_cluster_with_rule("C") for _ in range(10)]    # 10/200 = 0.05 (matches today)
        + [{"cascade_rules_fired": []} for _ in range(180)]
    )
    audit = audit_firing_rates(today, history)
    by_rule = {r["rule"]: r for r in audit}
    # A and B should both be flagged elevated
    assert by_rule["A"]["elevated"] is True
    assert by_rule["B"]["elevated"] is True
    assert by_rule["C"]["elevated"] is False

    # First entry should be the higher elevation (A)
    elevated = [r for r in audit if r["elevated"]]
    assert len(elevated) >= 2
    assert elevated[0]["rule"] == "A"
    assert elevated[0]["elevation"] >= elevated[1]["elevation"]


def test_audit_handles_empty_today_window():
    """No today clusters -> all entries have today_rate=0, none elevated."""
    history = [_cluster_with_rule("Tsunami generation risk") for _ in range(20)]
    audit = audit_firing_rates([], history)
    for r in audit:
        assert r["today_firings"] == 0
        assert r["today_rate"] == 0.0
        assert r["elevated"] is False


# ─── find_correlations regression check ───────────────────────────────────


def test_find_correlations_still_fires_cascade_rules():
    """Phase 5c didn't change find_correlations — verify it still works."""
    events = [
        {"layer": "earthquakes", "lat": 32.5, "lng": -87.5,
         "external_id": "q1", "severity": 8, "payload": {"place": "X"}},
        {"layer": "tsunamis", "lat": 32.7, "lng": -87.3,
         "external_id": "t1", "severity": 9, "payload": {"name": "Y"}},
    ]
    clusters = find_correlations(events)
    assert len(clusters) >= 1
    fired = clusters[0]["cascade_rules_fired"]
    rules = {r["rule"] for r in fired}
    assert "Tsunami generation risk" in rules
