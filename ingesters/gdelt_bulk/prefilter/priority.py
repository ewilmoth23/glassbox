# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Priority scorer for events that pass the rule chain.

Score is in [0, 1] (clamped) and feeds the Redis sorted-set queue keyed
by ``priority``. Components are independently bounded to [0, 1] before
weighted sum so changing one weight in production doesn't accidentally
let any single signal dominate.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable, Optional

from .config import GDELTEventForPrefilter, PriorityConfig
from .rules import SourceQualityRule


class PriorityScorer:
    """Pure function over ``(event, duplicate_count, in_aoi)``.

    ``source_quality_rule`` is shared with the SourceQualityRule (single
    source of truth for domain → score). ``aoi_checker`` is an optional
    callable; until analyst AOIs land it returns False and the geo_aoi
    component is always 0.
    """

    def __init__(
        self,
        cfg: PriorityConfig,
        source_quality_rule: SourceQualityRule,
        aoi_checker=None,
        *,
        now_fn=None,
    ) -> None:
        self._weights = cfg.weights
        self._category_bonuses = dict(cfg.category_priority_bonuses)
        self._sq = source_quality_rule
        self._aoi = aoi_checker or (lambda ev: False)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def score(
        self,
        event: GDELTEventForPrefilter,
        duplicate_count: int = 0,
    ) -> float:
        w = self._weights
        components = {
            "severity":    w.severity    * _clamp(event.severity),
            "source":      w.source      * _clamp(self._sq.score_for(event.source_url)),
            "duplication": w.duplication * _dup_curve(duplicate_count),
            "recency":     w.recency     * _recency_factor(event.timestamp, self._now_fn()),
            "category":    w.category    * _clamp(
                self._category_bonuses.get(event.subcategory, 0.0)
            ),
            "geo_aoi":     w.geo_aoi     * (1.0 if self._aoi(event) else 0.0),
        }
        weight_sum = sum([w.severity, w.source, w.duplication,
                          w.recency, w.category, w.geo_aoi])
        if weight_sum <= 0:
            return 0.0
        return _clamp(sum(components.values()) / weight_sum)

    def explain(
        self,
        event: GDELTEventForPrefilter,
        duplicate_count: int = 0,
    ) -> dict:
        """Return per-component contributions for debugging / dashboards."""
        w = self._weights
        return {
            "severity":    _clamp(event.severity),
            "source":      _clamp(self._sq.score_for(event.source_url)),
            "duplication": _dup_curve(duplicate_count),
            "recency":     _recency_factor(event.timestamp, self._now_fn()),
            "category":    _clamp(self._category_bonuses.get(event.subcategory, 0.0)),
            "geo_aoi":     1.0 if self._aoi(event) else 0.0,
            "weights":     w.model_dump(),
            "final":       self.score(event, duplicate_count),
        }


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _dup_curve(n: int) -> float:
    """log(1 + n) / log(1 + 10) — 0 dupes → 0.0, 10 dupes → 1.0, asymptotic.
    Matches HANDOFF_03's ``log(1 + duplicate_count)`` shape, normalized so
    the component stays in [0, 1] without dominating at high dup counts."""
    if n <= 0:
        return 0.0
    return min(1.0, math.log1p(n) / math.log1p(10))


def _recency_factor(event_ts: datetime, now: datetime) -> float:
    """1.0 at publication, decays linearly to 0 at 6h. Older events that
    survived the RecencyRule (e.g. with a longer max_age_hours override)
    still get a small floor of 0.0."""
    if event_ts.tzinfo is None:
        event_ts = event_ts.replace(tzinfo=timezone.utc)
    age_h = (now - event_ts).total_seconds() / 3600.0
    if age_h <= 0:
        return 1.0
    if age_h >= 6.0:
        return 0.0
    return 1.0 - (age_h / 6.0)
