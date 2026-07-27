"""Shared jsonb-coercion helper for route modules that touch
asyncpg-returned jsonb columns.

Lifted from `api_v1.py` 2026-05-27 as the P3-H Phase 2 #7 prep
(`core` extraction). Already deferred-imported by `sanctions.py`
and `alerts.py`; will be the dominant consumer in `core.py`
(used by /viewport, /entity/{id}, /event/{id}, query_viewport,
query_entity_detail).

Public name `coerce_jsonb`. api_v1.py keeps an underscore-prefixed
re-export alias `_coerce_jsonb` because the 5 inline call sites
inside `build_router()` still use the legacy name (signals routes
that haven't extracted yet).
"""

from __future__ import annotations

import json
from typing import Any


def coerce_jsonb(value: Any) -> Any:
    """asyncpg sometimes returns jsonb as str (depends on version). Normalize to dict."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value
