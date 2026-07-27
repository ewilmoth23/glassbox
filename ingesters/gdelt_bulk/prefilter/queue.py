# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Bounded priority queue for the GDELT prefilter — feeds the LLM extraction
worker. v1.0 backing is in-process (single-Mac single-process backend, same
architectural call as the deferred NATS streaming spine). The interface is
the one a future Redis sorted-set implementation would use without any
caller change.

Tail-drop on overflow: when the queue is at ``max_depth`` and a new
``FilteredEvent`` would push us past, the LOWEST-priority entry currently
in the queue is evicted. If the new event itself has the lowest priority,
the new event is dropped instead. Either way, ``enqueue()`` returns the
``FilteredEvent`` that was dropped so the engine can record metrics.
"""

from __future__ import annotations

import heapq
import threading
from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import FilteredEvent


@dataclass
class QueueStats:
    enqueued_total: int = 0
    popped_total: int = 0
    tail_dropped_total: int = 0
    new_event_dropped_total: int = 0


class BoundedPriorityQueue:
    """In-process bounded priority queue with tail-drop on overflow.

    Highest ``priority`` pops first. ``enqueue`` returns the FilteredEvent
    that was dropped (the new entry, the displaced floor entry, or
    ``None`` if there was room).

    Thread-safe via a single instance lock — the prefilter engine itself
    is single-threaded, but the LLM worker pulling from the queue may run
    on a different task / thread.
    """

    def __init__(self, max_depth: int) -> None:
        if max_depth < 1:
            raise ValueError("max_depth must be ≥ 1")
        self._max_depth = max_depth
        # Max-heap via negated priority; secondary key = monotonic seq for
        # stable ordering of equal-priority events (FIFO within a tier).
        self._heap: List[tuple] = []
        self._seq = 0
        self._lock = threading.Lock()
        self.stats = QueueStats()

    @property
    def max_depth(self) -> int:
        return self._max_depth

    def __len__(self) -> int:
        with self._lock:
            return len(self._heap)

    def depth(self) -> int:
        return len(self)

    def enqueue(self, event: "FilteredEvent") -> Optional["FilteredEvent"]:
        with self._lock:
            self._seq += 1
            seq = self._seq
            if len(self._heap) < self._max_depth:
                heapq.heappush(self._heap, (-event.priority, seq, event))
                self.stats.enqueued_total += 1
                return None

            # Linear scan to find the lowest-priority entry. At max_depth=500
            # this is ~500 comparisons — well below any meaningful budget.
            floor_idx = 0
            floor_priority = -self._heap[0][0]
            for i in range(1, len(self._heap)):
                p = -self._heap[i][0]
                if p < floor_priority:
                    floor_priority = p
                    floor_idx = i

            if event.priority <= floor_priority:
                # New event is lowest — drop it.
                self.stats.new_event_dropped_total += 1
                return event

            dropped = self._heap[floor_idx][2]
            self._heap[floor_idx] = (-event.priority, seq, event)
            heapq.heapify(self._heap)
            self.stats.tail_dropped_total += 1
            self.stats.enqueued_total += 1
            return dropped

    def pop_highest(self) -> Optional["FilteredEvent"]:
        with self._lock:
            if not self._heap:
                return None
            _neg_priority, _seq, event = heapq.heappop(self._heap)
            self.stats.popped_total += 1
            return event

    def peek_priorities(self) -> List[float]:
        """Snapshot of current priorities, sorted descending. For
        diagnostics; not a hot-path call."""
        with self._lock:
            return sorted((-p for p, _, _ in self._heap), reverse=True)
