# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Schema-validated LLM-JSON parsing.

Glassbox calls Ollama's ``/api/generate`` with ``"format": "json"`` from
several modules (``forecaster.py``, ``intelligence_loop.py``, etc.). Until
this module each call site re-implemented the same tolerant pattern —
strip code fences, json.loads, fall back to bracket-extraction, fall
back to defaults.

This helper consolidates that pattern AND adds a Pydantic-schema-bound
validation layer per HANDOFF_03 R3 ("tighter JSON validity than
retry+repair"). On parse + validation failure callers get a typed
fallback instance so downstream code never has to defensively check
shape.

Why not ``outlines.from_openai`` directly? The empire's Ollama call
sites use the native ``/api/generate`` endpoint, not the
OpenAI-compatible ``/v1/chat/completions``. ``outlines.from_openai``
requires the latter, so a full swap is more invasive than this slice.
``outlines>=0.0.40`` is added to requirements.txt as a registered
runtime dep so a future commit can wire ``outlines.from_openai``
behind this same helper interface without breaking callers.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Tuple, Type, TypeVar

from pydantic import BaseModel, ValidationError


_log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def extract_json_object(text: str) -> Optional[str]:
    """Return the first ``{...}`` block in ``text``, sliced from the
    first ``{`` to the last ``}``. Returns None if no candidate exists.

    Tolerant to:
      * leading / trailing whitespace
      * markdown ``"```json ...```"`` code fences (any language tag)
      * the LLM occasionally prepending "Here is the JSON:" prose
      * trailing analyst-note prose after the JSON body
      * the JSON being wrapped in an outer array (we capture the inner
        object — sufficient for LLM-tolerance, callers that want strict
        array vs object semantics should validate post-parse)

    Naive first-``{`` / last-``}`` slicing — the LLM rarely emits
    multiple top-level objects, and json.loads will reject anything
    malformed in the slice.
    """
    if not text:
        return None
    s = text.strip()

    # Strip markdown code fences if present — pull the first chunk that
    # starts with `{` after a fence boundary.
    if s.startswith("```"):
        for chunk in s.split("```"):
            chunk = chunk.strip()
            if chunk and not chunk.startswith("{"):
                head, _, tail = chunk.partition("\n")
                if head and not head.startswith("{") and len(head) <= 8:
                    chunk = tail.strip()
            if chunk.startswith("{"):
                s = chunk
                break

    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    return s[start : end + 1]


def parse_with_schema(
    text: str,
    schema: Type[T],
    *,
    fallback: Optional[T] = None,
) -> Tuple[Optional[T], Optional[str]]:
    """Parse ``text`` as JSON and validate it against ``schema``.

    Returns ``(instance, None)`` on success; ``(fallback, err_msg)`` on
    any failure (parse or validation). Pure function — no I/O, no
    raise. Caller decides whether ``fallback=None`` is meaningful or
    constructs a sentinel default.

    The error message is short, actionable, and safe to log — never
    contains the full LLM output (which can be 700+ tokens).
    """
    candidate = extract_json_object(text)
    if candidate is None:
        return fallback, "no_json_object_found"

    try:
        raw = json.loads(candidate)
    except json.JSONDecodeError as e:
        return fallback, f"json_decode_error: {e.msg}"

    if not isinstance(raw, dict):
        return fallback, f"json_root_not_object: got {type(raw).__name__}"

    try:
        return schema.model_validate(raw), None
    except ValidationError as e:
        # Compact error: just the first failing field name + reason.
        errs = e.errors()
        if errs:
            first = errs[0]
            loc = ".".join(str(p) for p in first.get("loc", ())) or "<root>"
            return fallback, f"schema_invalid: {loc}: {first.get('msg', 'invalid')}"
        return fallback, "schema_invalid"
