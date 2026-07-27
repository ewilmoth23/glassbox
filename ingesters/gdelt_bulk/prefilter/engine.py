# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
PreFilterEngine — runs the rule chain in cheap-first order, computes a
priority score for events that pass, and returns ``FilteredEvent`` (or
``None`` on drop).

This first slice runs all rules synchronously and tracks pass/drop counts
in-memory. Redis dedup is still in-process. The prometheus-client metrics
shim landed as a follow-up: pass an optional ``metrics`` constructor arg
and the engine will increment counters/gauges on each event.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import GDELTEventForPrefilter, PreFilterConfig
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
)


@dataclass(frozen=True)
class FilteredEvent:
    event: GDELTEventForPrefilter
    priority: float
    rules_version: str
    duplicate_of: Optional[str] = None


@dataclass
class PreFilterStats:
    """In-process counters surfaced via ``engine.health()``. The
    Prometheus shim mirrors this state on every event."""
    pass_count: int = 0
    drop_count: int = 0
    drops_by_rule: Counter = field(default_factory=Counter)
    drops_by_reason: Counter = field(default_factory=Counter)
    # Queue-overflow accounting. Distinct from drops_by_reason because
    # these events PASSED the rule chain — they were dropped purely by
    # the bounded queue's tail-drop policy.
    tail_dropped_count: int = 0
    new_event_dropped_count: int = 0
    # Set on every dedup reject so callers can correlate the rejected
    # event with the prior occurrence it duplicated.
    last_duplicate_of: Optional[str] = None
    # A/B shadow comparison — set when the engine has a shadow chain
    # configured. Each event the engine sees is also evaluated by the
    # shadow; the four outcome-pair counts surface the confusion
    # matrix to an operator deciding whether to promote shadow to
    # primary. Stays 0 when no shadow is wired.
    shadow_agree_pass: int = 0
    shadow_agree_drop: int = 0
    shadow_primary_pass_only: int = 0  # primary passed, shadow dropped
    shadow_primary_drop_only: int = 0  # primary dropped, shadow passed


class PreFilterEngine:
    """Stateful (only via stats counters) prefilter chain. Thread-unsafe.

    Construct with the loaded config and the absolute path to the
    ``data/`` directory containing source_quality.json. Rules are
    instantiated once at construction; ``process()`` is hot-path."""

    def __init__(
        self,
        config: PreFilterConfig,
        data_dir: Path,
        *,
        rules: Optional[List[BaseRule]] = None,
        scorer: Optional[PriorityScorer] = None,
        queue: Optional[BoundedPriorityQueue] = None,
        metrics: Optional[PrefilterMetrics] = None,
        shadow_engine: Optional["PreFilterEngine"] = None,
    ) -> None:
        self._cfg = config
        self._data_dir = Path(data_dir)

        if rules is None:
            sq_rule = SourceQualityRule(
                config.rules.source_quality_filter, self._data_dir
            )
            # Cheap-first ordering. Dedup runs LAST among reject-rules
            # because it's the only stateful one — we don't want to pay
            # tokenization + cache lookup for events the cheap rules
            # would have dropped anyway.
            rules = [
                CategoryRule(config.rules.category_filter),
                SeverityRule(config.rules.severity_filter),
                GeographyRule(config.rules.geography_filter),
                sq_rule,
                RecencyRule(config.rules.recency_filter),
                DedupRule(config.rules.dedup_filter),
            ]
            self._sq_rule = sq_rule
        else:
            # Caller-supplied rule chain (test path): find the
            # SourceQualityRule for the scorer to share.
            sq = next((r for r in rules if isinstance(r, SourceQualityRule)), None)
            self._sq_rule = sq

        self._rules = rules
        self._scorer = scorer or (
            PriorityScorer(config.priority, self._sq_rule)
            if self._sq_rule is not None
            else None
        )
        # Default to a queue sized per the config. Tests/operators can
        # pass their own (e.g. with hooks for the LLM extraction worker).
        self._queue = queue or BoundedPriorityQueue(config.queue.max_depth)
        self.stats = PreFilterStats()

        # Default to a metrics shim that pre-registers labels for the
        # rules in this chain. ``PrefilterMetrics`` is a no-op when
        # prometheus-client isn't installed, so this never blows up.
        if metrics is None:
            metrics = PrefilterMetrics([r.name for r in self._rules])
        self._metrics = metrics

        # A/B shadow chain. When set, each event the primary engine
        # sees is ALSO evaluated by the shadow (with ``enqueue=False``
        # so the shadow's queue never participates in primary
        # admission). Confusion-matrix counts land on
        # ``self.stats.shadow_*``. The shadow is itself a full
        # PreFilterEngine — operators stand one up with experimental
        # config and compare for a day before deciding to promote.
        self._shadow_engine = shadow_engine

    # ─── Hot path ────────────────────────────────────────────────────

    @property
    def queue(self) -> BoundedPriorityQueue:
        return self._queue

    @property
    def metrics(self) -> PrefilterMetrics:
        return self._metrics

    @property
    def shadow_engine(self) -> Optional["PreFilterEngine"]:
        return self._shadow_engine

    def process(
        self,
        event: GDELTEventForPrefilter,
        *,
        enqueue: bool = True,
    ) -> Optional[FilteredEvent]:
        """Run the rule chain. Return ``FilteredEvent`` on pass, ``None``
        on drop. Always updates stats counters.

        When ``enqueue=True`` (default), passing events are enqueued into
        the bounded priority queue. Tail-drop on overflow is recorded in
        ``stats.tail_dropped_count`` (and on the queue's own stats); the
        FilteredEvent is still returned to the caller either way so
        callers that ARE the queue consumer (tests, manual harnesses) can
        inspect what was produced.
        """
        # Per-event duplicate metadata is set by the DedupRule (if it
        # rejects) or remains None (if it passes / is disabled).
        duplicate_of: Optional[str] = None

        for rule in self._rules:
            outcome = rule.check(event)
            if outcome is not None:
                self.stats.drop_count += 1
                self.stats.drops_by_rule[rule.name] += 1
                self.stats.drops_by_reason[outcome.reason] += 1
                if outcome.reason == "duplicate":
                    duplicate_of = outcome.metadata.get("duplicate_of")
                    self.stats.last_duplicate_of = duplicate_of
                self._metrics.record_drop(rule.name, outcome.reason)
                self._evaluate_shadow(event, primary_passed=False)
                return None

        self.stats.pass_count += 1
        if self._scorer is None:
            priority = 0.5
        else:
            # First-occurrence events always get duplicate_count=0; the
            # Dedup rule increments the prior entry's counter for any
            # future re-prioritization but does not retroactively boost
            # already-enqueued items in this slice.
            priority = self._scorer.score(event, duplicate_count=0)
        filtered = FilteredEvent(
            event=event,
            priority=priority,
            rules_version=self._cfg.version,
            duplicate_of=duplicate_of,
        )
        self._metrics.record_pass(priority)

        if enqueue:
            dropped = self._queue.enqueue(filtered)
            if dropped is not None:
                if dropped is filtered:
                    self.stats.new_event_dropped_count += 1
                    self._metrics.record_new_event_drop()
                else:
                    self.stats.tail_dropped_count += 1
                    self._metrics.record_tail_drop()
            self._metrics.record_queue_state(
                self._queue.depth(), self._queue.max_depth,
            )

        self._evaluate_shadow(event, primary_passed=True)
        return filtered

    def _evaluate_shadow(self, event: GDELTEventForPrefilter,
                         *, primary_passed: bool) -> None:
        """When a shadow engine is configured, run it on the same event
        with no queue side-effects (``enqueue=False``) and record the
        4-cell confusion matrix on ``self.stats.shadow_*`` + the
        Prometheus shim. Shadow's own stats counters still update —
        operators inspect ``shadow_engine.health()`` directly to see
        what the candidate config would have done in aggregate."""
        if self._shadow_engine is None:
            return
        shadow_result = self._shadow_engine.process(event, enqueue=False)
        shadow_passed = shadow_result is not None
        if primary_passed and shadow_passed:
            self.stats.shadow_agree_pass += 1
            outcome = "agree_pass"
        elif (not primary_passed) and (not shadow_passed):
            self.stats.shadow_agree_drop += 1
            outcome = "agree_drop"
        elif primary_passed and (not shadow_passed):
            self.stats.shadow_primary_pass_only += 1
            outcome = "primary_pass_only"
        else:
            self.stats.shadow_primary_drop_only += 1
            outcome = "primary_drop_only"
        self._metrics.record_shadow_outcome(outcome)

    # ─── Diagnostics ─────────────────────────────────────────────────

    def health(self) -> dict:
        total = self.stats.pass_count + self.stats.drop_count
        pass_rate = (self.stats.pass_count / total) if total else 0.0
        out = {
            "rules_version":    self._cfg.version,
            "rules_in_chain":   [r.name for r in self._rules],
            "pass_count":       self.stats.pass_count,
            "drop_count":       self.stats.drop_count,
            "pass_rate":        round(pass_rate, 4),
            "drops_by_rule":    dict(self.stats.drops_by_rule),
            "drops_by_reason":  dict(self.stats.drops_by_reason),
            "queue": {
                "depth":                  self._queue.depth(),
                "max_depth":              self._queue.max_depth,
                "enqueued_total":         self._queue.stats.enqueued_total,
                "popped_total":           self._queue.stats.popped_total,
                "tail_dropped_total":     self._queue.stats.tail_dropped_total,
                "new_event_dropped_total": self._queue.stats.new_event_dropped_total,
            },
        }
        if self._shadow_engine is not None:
            agree = (self.stats.shadow_agree_pass +
                     self.stats.shadow_agree_drop)
            disagree = (self.stats.shadow_primary_pass_only +
                        self.stats.shadow_primary_drop_only)
            seen = agree + disagree
            out["shadow"] = {
                "agree_pass":         self.stats.shadow_agree_pass,
                "agree_drop":         self.stats.shadow_agree_drop,
                "primary_pass_only":  self.stats.shadow_primary_pass_only,
                "primary_drop_only":  self.stats.shadow_primary_drop_only,
                "agreement_rate":     round(agree / seen, 4) if seen else 0.0,
                "shadow_pass_rate":   round(
                    (self.stats.shadow_agree_pass +
                     self.stats.shadow_primary_drop_only) / seen, 4
                ) if seen else 0.0,
                "shadow_rules_version": self._shadow_engine._cfg.version,
            }
        return out
