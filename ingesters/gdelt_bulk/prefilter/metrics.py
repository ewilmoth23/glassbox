# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Prometheus metrics shim for the prefilter rule chain.

HANDOFF_03 Day-3 deliverable: per-rule pass/drop rates, per-reason
drop counters, and queue depth/overflow gauges so operators can chart
prefilter behavior over time and spot drift (e.g. a CAMEO category's
pass rate cratering after an upstream feed change).

Why a thin wrapper rather than letting callers touch prometheus-client
directly:

  1. **Optional dep.** If ``prometheus-client`` is missing — easy to
     happen on a fresh dev machine — the engine should still run.
     ``PrefilterMetrics`` becomes a no-op and the surrounding code
     never knows it was disabled.
  2. **Single registry per process.** The engine + the Prometheus
     scrape endpoint need to share the same registry. Putting it
     here keeps that decision out of the hot path.
  3. **Stable label set.** Rule names + drop reasons are enumerated
     at construction. Counter creation is paid once, not per event.

License: prometheus-client is Apache-2.0 (LICENSE_RISK_REGISTER §1.3).
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

_log = logging.getLogger(__name__)


# Drop reasons emitted by the rules today. The set is small enough that
# we enumerate them at construction so Prometheus's label cardinality
# doesn't drift unexpectedly.
_KNOWN_DROP_REASONS = (
    "category_not_allowed",
    "severity_below_floor",
    "geography_outside_allowed",
    "source_below_quality_floor",
    "stale",
    "duplicate",
)

# Shadow-A/B confusion-matrix outcomes. Fixed set so Prometheus
# cardinality is bounded.
_KNOWN_SHADOW_OUTCOMES = (
    "agree_pass",          # primary passed, shadow passed
    "agree_drop",          # primary dropped, shadow dropped
    "primary_pass_only",   # primary passed, shadow dropped (shadow stricter)
    "primary_drop_only",   # primary dropped, shadow passed (shadow looser)
)


class PrefilterMetrics:
    """Wraps prometheus_client primitives. No-op when the lib isn't
    importable; the engine is unaffected either way.

    Construct with the rule names that will fire in this engine instance
    (so the Counter labels are pre-allocated and a 0-count rule still
    shows up on a fresh scrape — easier to reason about empty
    pass-rate dashboards than missing labels)."""

    def __init__(
        self,
        rule_names: Iterable[str],
        *,
        registry=None,
    ) -> None:
        self._enabled = False
        self.registry = None
        self._pass_total = None
        self._drop_total = None
        self._drop_by_reason = None
        self._queue_depth = None
        self._queue_max_depth = None
        self._tail_dropped_total = None
        self._new_event_dropped_total = None
        self._priority_score = None
        self._shadow_outcome_total = None

        try:
            from prometheus_client import (  # noqa: WPS433
                CollectorRegistry, Counter, Gauge, Histogram,
            )
        except ImportError:
            _log.info("prometheus-client not installed — prefilter metrics disabled")
            return

        self.registry = registry if registry is not None else CollectorRegistry()
        self._pass_total = Counter(
            "glassbox_prefilter_pass_total",
            "Events that passed the prefilter rule chain.",
            registry=self.registry,
        )
        self._drop_total = Counter(
            "glassbox_prefilter_drop_total",
            "Events dropped by the prefilter rule chain.",
            ("rule",),
            registry=self.registry,
        )
        self._drop_by_reason = Counter(
            "glassbox_prefilter_drop_by_reason_total",
            "Events dropped, broken out by structured reason.",
            ("reason",),
            registry=self.registry,
        )
        self._queue_depth = Gauge(
            "glassbox_prefilter_queue_depth",
            "Current number of events in the bounded priority queue.",
            registry=self.registry,
        )
        self._queue_max_depth = Gauge(
            "glassbox_prefilter_queue_max_depth",
            "Configured queue capacity (constant per-process; gauge "
            "for completeness so a dashboard can compute % full).",
            registry=self.registry,
        )
        self._tail_dropped_total = Counter(
            "glassbox_prefilter_queue_tail_dropped_total",
            "Events evicted from the queue tail because a higher-priority "
            "event arrived after the queue was full.",
            registry=self.registry,
        )
        self._new_event_dropped_total = Counter(
            "glassbox_prefilter_queue_new_event_dropped_total",
            "Newly-arrived events dropped because the queue was full and "
            "their priority did not exceed the lowest-priority resident.",
            registry=self.registry,
        )
        self._priority_score = Histogram(
            "glassbox_prefilter_priority_score",
            "Priority score assigned to events that passed the rule chain.",
            buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5,
                     0.6, 0.7, 0.8, 0.9, 1.0),
            registry=self.registry,
        )
        self._shadow_outcome_total = Counter(
            "glassbox_prefilter_shadow_outcome_total",
            "A/B comparison of primary vs. shadow rule chain on every "
            "event. Four outcomes pre-allocated; ratios surface in "
            "engine.health()['shadow'].",
            ("outcome",),
            registry=self.registry,
        )

        # Pre-allocate label combinations so a 0-count rule still shows
        # up on the first scrape.
        for rule_name in rule_names:
            self._drop_total.labels(rule=rule_name)  # touches: registers 0
        for reason in _KNOWN_DROP_REASONS:
            self._drop_by_reason.labels(reason=reason)
        for outcome in _KNOWN_SHADOW_OUTCOMES:
            self._shadow_outcome_total.labels(outcome=outcome)

        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ─── Hot-path hooks (called by PreFilterEngine.process) ──────────

    def record_pass(self, priority: float) -> None:
        if not self._enabled:
            return
        self._pass_total.inc()
        self._priority_score.observe(priority)

    def record_drop(self, rule_name: str, reason: str) -> None:
        if not self._enabled:
            return
        self._drop_total.labels(rule=rule_name).inc()
        # Unknown reasons (a future rule emits a new reason string) are
        # silently dropped from this counter rather than minting a new
        # label and blowing cardinality. Caller's `drops_by_reason`
        # in-memory counter still tracks them for diagnostics.
        if reason in _KNOWN_DROP_REASONS:
            self._drop_by_reason.labels(reason=reason).inc()

    def record_queue_state(self, depth: int, max_depth: int) -> None:
        if not self._enabled:
            return
        self._queue_depth.set(depth)
        self._queue_max_depth.set(max_depth)

    def record_tail_drop(self) -> None:
        if not self._enabled:
            return
        self._tail_dropped_total.inc()

    def record_new_event_drop(self) -> None:
        if not self._enabled:
            return
        self._new_event_dropped_total.inc()

    def record_shadow_outcome(self, outcome: str) -> None:
        """Increment one cell of the primary-vs-shadow confusion matrix.
        Outcomes outside _KNOWN_SHADOW_OUTCOMES are silently dropped to
        protect Prometheus label cardinality."""
        if not self._enabled:
            return
        if outcome not in _KNOWN_SHADOW_OUTCOMES:
            return
        self._shadow_outcome_total.labels(outcome=outcome).inc()

    # ─── Read path ────────────────────────────────────────────────────

    def render_prometheus(self) -> bytes:
        """Return the Prometheus text-format exposition for the
        registered metrics. Returns ``b""`` when disabled — caller
        should also include a ``# prefilter metrics disabled`` comment
        in the response so a missing scrape doesn't look like a
        scraper-config bug."""
        if not self._enabled:
            return b""
        from prometheus_client import generate_latest  # noqa: WPS433
        return generate_latest(self.registry)
