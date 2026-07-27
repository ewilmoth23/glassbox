"""
P3-N regression suite — `_with_confidence()` helper in writers.py.

Pins:
  - Mapped layers produce a numeric `confidence_score` in [0.0, 1.0] and
    a `confidence_label` from the LABEL_THRESHOLDS set.
  - Unmapped layers return the dict unchanged (no-op).
  - The helper mutates the input dict in-place AND returns it (chain-friendly).
  - Pre-existing keys in props_dict are preserved.

These tests are hermetic — no DB, no network. ~30ms total.

Adding more event writers to confidence scoring? Add the new layer to
`_LAYER_TO_PLATFORM` in writers.py, then add a test case to
`test_known_layer_mappings_produce_expected_baselines` below.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from writers import _with_confidence, _LAYER_TO_PLATFORM  # noqa: E402


# Sentinel set of all label values (from confidence_scorer.LABEL_THRESHOLDS).
_VALID_LABELS = {"SPECULATIVE", "LOW", "MODERATE", "HIGH", "CONFIRMED"}


def test_with_confidence_no_op_on_unmapped_layer():
    """Unmapped layer = no-op. Dict returned unchanged, no exceptions."""
    props = {"external_id": "x", "topic": "weather"}
    out = _with_confidence(props, "this_layer_does_not_exist")
    assert out is props  # mutated-in-place, same object returned
    assert "confidence_score" not in out
    assert "confidence_label" not in out
    assert out == {"external_id": "x", "topic": "weather"}


def test_with_confidence_preserves_existing_keys():
    """Adding confidence must NOT clobber any pre-existing dict entries."""
    props = {"external_id": "abc", "mag": 6.5, "place": "Iceland"}
    _with_confidence(props, "earthquakes")
    assert props["external_id"] == "abc"
    assert props["mag"] == 6.5
    assert props["place"] == "Iceland"


def test_with_confidence_mutates_in_place_and_returns_same_object():
    """Helper is chain-friendly: returns the same dict it received."""
    props = {"external_id": "x"}
    out = _with_confidence(props, "earthquakes")
    assert out is props


def test_with_confidence_earthquake_scores_high():
    """USGS earthquakes have a 0.95 baseline — should land in HIGH or CONFIRMED."""
    props = {"external_id": "us7000abcd"}
    _with_confidence(props, "earthquakes")
    assert "confidence_score" in props
    assert 0.0 <= props["confidence_score"] <= 1.0
    assert props["confidence_label"] in {"HIGH", "CONFIRMED"}
    # Earthquake baseline is 0.95; with has_coordinates=True the score
    # should stay quite high (>0.65, the HIGH threshold).
    assert props["confidence_score"] >= 0.65


def test_with_confidence_gdelt_news_scores_lower():
    """GDELT mention-geocoded news has a 0.55 baseline — should land
    in MODERATE or below."""
    props = {"external_id": "gdelt_abc"}
    _with_confidence(props, "news")
    assert props["confidence_score"] < 0.80  # never CONFIRMED for GDELT
    assert props["confidence_label"] in {"SPECULATIVE", "LOW", "MODERATE", "HIGH"}


def test_with_confidence_hn_scores_in_osint_range():
    """HackerNews posts have a 0.50 baseline — typically LOW to MODERATE."""
    props = {"external_id": "hn_123"}
    _with_confidence(props, "hacker_news")
    assert "confidence_score" in props
    # Sanity: HN should never come out CONFIRMED with just baseline + coords
    assert props["confidence_score"] < 0.80


def test_with_confidence_label_in_valid_set():
    """For every mapped layer, label must be one of the canonical set."""
    for layer in _LAYER_TO_PLATFORM:
        props = {"external_id": f"id_{layer}"}
        _with_confidence(props, layer)
        assert props.get("confidence_label") in _VALID_LABELS, (
            f"layer={layer!r} produced label "
            f"{props.get('confidence_label')!r} not in {_VALID_LABELS}"
        )


def test_with_confidence_score_in_unit_interval():
    """For every mapped layer, score must be in [0.0, 1.0]."""
    for layer in _LAYER_TO_PLATFORM:
        props = {"external_id": f"id_{layer}"}
        _with_confidence(props, layer)
        score = props.get("confidence_score")
        assert score is not None, f"layer={layer!r} produced no score"
        assert 0.0 <= score <= 1.0, (
            f"layer={layer!r} produced score {score} out of [0,1]"
        )


def test_known_layer_mappings_produce_expected_baselines():
    """Sanity-pin: the high-trust feeds outscore the OSINT feeds in
    the no-extra-signals case. If this regression catches, somebody
    swapped a PLATFORM_BASELINE key in _LAYER_TO_PLATFORM by mistake."""
    earthquake_props = {}
    _with_confidence(earthquake_props, "earthquakes")
    ads_b_props = {}
    _with_confidence(ads_b_props, "planes")
    ais_props = {}
    _with_confidence(ais_props, "ships")
    gdelt_props = {}
    _with_confidence(gdelt_props, "news")
    hn_props = {}
    _with_confidence(hn_props, "hacker_news")

    # High-trust > OSINT
    assert earthquake_props["confidence_score"] > gdelt_props["confidence_score"]
    assert ads_b_props["confidence_score"] > gdelt_props["confidence_score"]
    assert ais_props["confidence_score"] > hn_props["confidence_score"]
    # Within high-trust: earthquake > ADS-B > AIS (the established ordering)
    assert earthquake_props["confidence_score"] >= ads_b_props["confidence_score"]
    assert ads_b_props["confidence_score"] >= ais_props["confidence_score"]


def test_with_confidence_handles_empty_dict():
    """Edge case — empty input dict."""
    out = _with_confidence({}, "earthquakes")
    assert "confidence_score" in out
    assert "confidence_label" in out


@pytest.mark.parametrize("layer", [None, "", 0, False])
def test_with_confidence_no_op_on_falsy_or_invalid_layer(layer):
    """Defensive: garbage `layer` values don't raise — just no-op."""
    props = {"external_id": "x"}
    out = _with_confidence(props, layer)  # type: ignore[arg-type]
    assert out is props
    assert "confidence_score" not in out
