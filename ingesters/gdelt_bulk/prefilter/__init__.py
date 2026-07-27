# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GDELT pre-filter rules engine — picks the ~0.6% of incoming events worth
sending to the LLM extractor and drops the rest, with metrics and
prioritization. Built per HANDOFF_03.

This first slice ships the stateless pipeline (Category, Severity,
Geography, SourceQuality, Recency rules + priority scoring + the engine
runner). The Dedup rule (Redis sliding window), queue tail-drop on
overflow, A/B variant routing, and Prometheus metrics are layered on in
follow-up commits per the handoff's Day 2 / Day 3 split.
"""

from .config import (
    DedupFilterConfig,
    GDELTEventForPrefilter,
    PreFilterConfig,
    PriorityWeights,
)
from .engine import FilteredEvent, PreFilterEngine
from .metrics import PrefilterMetrics
from .priority import PriorityScorer
from .queue import BoundedPriorityQueue
from .rules import (
    BaseRule,
    CategoryRule,
    DedupRule,
    GeographyRule,
    RecencyRule,
    Rejected,
    SeverityRule,
    SourceQualityRule,
    haversine_km,
    token_set_jaccard,
)

__all__ = [
    "BaseRule",
    "BoundedPriorityQueue",
    "CategoryRule",
    "DedupFilterConfig",
    "DedupRule",
    "FilteredEvent",
    "GDELTEventForPrefilter",
    "GeographyRule",
    "PreFilterConfig",
    "PreFilterEngine",
    "PrefilterMetrics",
    "PriorityScorer",
    "PriorityWeights",
    "RecencyRule",
    "Rejected",
    "SeverityRule",
    "SourceQualityRule",
    "haversine_km",
    "token_set_jaccard",
]
