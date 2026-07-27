# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Tests for the schema-validated LLM-JSON parsing helper.

The helper consolidates a tolerant-parse + Pydantic-validate pattern that
used to live duplicated inside forecaster.py and intelligence_loop.py.
Tests target the helper's contract directly, not those callers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm_json import extract_json_object, parse_with_schema  # noqa: E402


# ─── Test schemas ────────────────────────────────────────────────────────


class _Forecast(BaseModel):
    forecast: str = ""
    escalation_likelihood: float = Field(default=0.0, ge=0.0, le=1.0)
    category: str = "other"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class _Sitrep(BaseModel):
    headline: str = ""
    brief: str = ""
    priorities: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ─── extract_json_object ─────────────────────────────────────────────────


def test_extract_returns_clean_json_unchanged():
    txt = '{"forecast": "x", "confidence": 0.7}'
    assert extract_json_object(txt) == txt


def test_extract_strips_markdown_code_fence():
    txt = '```json\n{"forecast": "x"}\n```'
    out = extract_json_object(txt)
    assert out is not None
    assert out.startswith("{")
    assert "forecast" in out


def test_extract_strips_unlabeled_code_fence():
    txt = '```\n{"forecast": "x"}\n```'
    out = extract_json_object(txt)
    assert out is not None
    assert "forecast" in out


def test_extract_handles_prefix_text():
    """LLMs sometimes prepend 'Here is the JSON:' or similar — bracket
    slicing must still find the JSON."""
    txt = 'Here is the forecast: {"forecast": "x", "confidence": 0.5}'
    out = extract_json_object(txt)
    assert out is not None
    assert out.startswith("{")
    assert out.endswith("}")


def test_extract_returns_none_for_non_json():
    assert extract_json_object("just some prose with no braces") is None
    assert extract_json_object("") is None
    assert extract_json_object(None) is None  # type: ignore[arg-type]


def test_extract_handles_trailing_garbage_after_object():
    """If there's a nested object, we'll capture the whole outer one
    via first-{ / last-} slicing, which is what we want."""
    txt = '{"a": {"b": 1}, "c": 2} -- end of analyst note'
    out = extract_json_object(txt)
    assert out == '{"a": {"b": 1}, "c": 2}'


# ─── parse_with_schema ───────────────────────────────────────────────────


def test_parse_returns_validated_instance_on_clean_input():
    txt = '{"forecast": "Increasing tension", "escalation_likelihood": 0.7, "category": "armed_conflict", "confidence": 0.85}'
    parsed, err = parse_with_schema(txt, _Forecast)
    assert err is None
    assert parsed is not None
    assert parsed.forecast == "Increasing tension"
    assert parsed.escalation_likelihood == 0.7
    assert parsed.confidence == 0.85


def test_parse_returns_fallback_on_no_json():
    fb = _Forecast(forecast="default")
    parsed, err = parse_with_schema("just prose", _Forecast, fallback=fb)
    assert parsed is fb
    assert err == "no_json_object_found"


def test_parse_returns_fallback_on_decode_error():
    """Truncated JSON — closing brace exists but content is malformed."""
    txt = '{"forecast": "x", "confidence":}'
    parsed, err = parse_with_schema(txt, _Forecast, fallback=None)
    assert parsed is None
    assert err is not None and "json_decode_error" in err


def test_parse_extracts_inner_object_from_array_wrap():
    """Models occasionally wrap the response in a JSON array. Our
    LLM-tolerant slicing captures the first '{' to the last '}', so
    this naturally extracts the inner object — better DX than
    flatly failing on outer-array shape."""
    txt = '[{"forecast": "wrapped", "confidence": 0.7}]'
    parsed, err = parse_with_schema(txt, _Forecast)
    assert err is None
    assert parsed is not None
    assert parsed.forecast == "wrapped"


def test_parse_returns_fallback_on_schema_validation_failure():
    """escalation_likelihood out of [0, 1] range — Pydantic rejects."""
    txt = '{"forecast": "x", "escalation_likelihood": 1.5, "confidence": 0.5}'
    fb = _Forecast()
    parsed, err = parse_with_schema(txt, _Forecast, fallback=fb)
    assert parsed is fb
    assert err is not None and "schema_invalid" in err
    # Error should name the failing field
    assert "escalation_likelihood" in err


def test_parse_extra_fields_silently_ignored():
    """Pydantic's default is to drop unknown fields, not reject. The LLM
    sometimes adds 'reasoning' or 'sources' alongside the schema."""
    txt = '{"forecast": "x", "confidence": 0.5, "reasoning": "long chain..."}'
    parsed, err = parse_with_schema(txt, _Forecast)
    assert err is None
    assert parsed is not None
    assert parsed.forecast == "x"


def test_parse_missing_optional_fields_uses_defaults():
    """Pydantic fills in defaults for missing fields."""
    txt = '{"forecast": "x"}'
    parsed, err = parse_with_schema(txt, _Forecast)
    assert err is None
    assert parsed is not None
    assert parsed.escalation_likelihood == 0.0
    assert parsed.confidence == 0.0
    assert parsed.category == "other"


def test_parse_handles_sitrep_shape():
    """Larger schema with a list field — Pydantic enforces element types
    too, not just the top-level shape."""
    txt = ('{"headline": "Tension rising", "brief": "...", '
           '"priorities": ["region A", "region B"], "confidence": 0.6}')
    parsed, err = parse_with_schema(txt, _Sitrep)
    assert err is None
    assert parsed is not None
    assert parsed.priorities == ["region A", "region B"]


def test_parse_sitrep_priorities_wrong_type_rejected():
    txt = ('{"headline": "x", "brief": "x", '
           '"priorities": "should be a list", "confidence": 0.5}')
    parsed, err = parse_with_schema(txt, _Sitrep, fallback=None)
    assert parsed is None
    assert err is not None and "priorities" in err
