# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
In-process token-bucket rate limiter for the MCP servers.

Per HANDOFF_04:
  Entities:      300 calls/min/agent
  Events:        300 calls/min/agent
  Investigation: 30 calls/min/agent (LLM-bearing tools count 5×)

In-process state per the empire's "single-Mac single-process for v1.0"
call (same logic as the deferred NATS streaming spine + the in-process
prefilter dedup). Per-agent isolation keeps one chatty agent from
starving others. Concurrency-safe via a single asyncio.Lock — token-
bucket math is cheap so contention is trivial.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class _Bucket:
    """One token bucket. tokens float so partial-second refills land
    exactly where math says, no integer rounding."""
    tokens: float
    last_refill_ts: float


@dataclass
class RateLimitDecision:
    """Outcome of try_consume. ``allowed`` True → caller proceeds.
    ``retry_after_sec`` > 0 → recommended sleep before retry. The MCP
    handler turns this into a tool error message."""
    allowed: bool
    retry_after_sec: float = 0.0
    bucket_tokens: float = 0.0


class TokenBucketRateLimiter:
    """Per-agent-id token bucket.

    capacity: max burst (tokens that can stack up while idle).
    refill_per_sec: sustained rate. e.g. 300/min => 5.0 tokens/sec.

    Anonymous calls (agent_id=None) get a dedicated shared bucket
    keyed on the literal sentinel string ``"<anonymous>"`` — they do
    NOT all bypass the limiter.
    """

    _ANON_KEY = "<anonymous>"

    def __init__(self, *, capacity: float, refill_per_sec: float) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_per_sec <= 0:
            raise ValueError("refill_per_sec must be > 0")
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._now_fn = time.monotonic

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def refill_per_sec(self) -> float:
        return self._refill

    def _key(self, agent_id: Optional[str]) -> str:
        return agent_id or self._ANON_KEY

    async def try_consume(
        self,
        agent_id: Optional[str],
        *,
        cost: float = 1.0,
    ) -> RateLimitDecision:
        """Attempt to consume ``cost`` tokens. Refills the bucket
        based on elapsed time since last touch, then checks if there
        are enough tokens. Cost > 1 lets investigation-server LLM
        tools count 5× per call."""
        if cost <= 0:
            raise ValueError("cost must be > 0")
        key = self._key(agent_id)
        async with self._lock:
            now = self._now_fn()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self._capacity, last_refill_ts=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.last_refill_ts)
                bucket.tokens = min(self._capacity,
                                    bucket.tokens + elapsed * self._refill)
                bucket.last_refill_ts = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return RateLimitDecision(allowed=True,
                                         bucket_tokens=bucket.tokens)
            # Not enough tokens. Compute how long until cost tokens are
            # available so the caller can honor a retry-after hint.
            shortfall = cost - bucket.tokens
            retry_after = shortfall / self._refill
            return RateLimitDecision(allowed=False,
                                     retry_after_sec=retry_after,
                                     bucket_tokens=bucket.tokens)

    def reset(self, agent_id: Optional[str] = None) -> None:
        """Drop one agent's bucket (or all of them if agent_id is None).
        Test-only helper; not meant for production hot-path use."""
        if agent_id is None:
            self._buckets.clear()
        else:
            self._buckets.pop(self._key(agent_id), None)


class RateLimited(Exception):
    """Raised by the MCP dispatcher when a tool call exceeds the per-
    agent token budget. The MCP server formats this as a tool error
    that the agent can see + back off from."""

    def __init__(self, retry_after_sec: float, agent_id: Optional[str],
                 cost: float):
        self.retry_after_sec = retry_after_sec
        self.agent_id = agent_id
        self.cost = cost
        super().__init__(
            f"rate limit exceeded for agent {agent_id!r}; "
            f"retry after {retry_after_sec:.1f}s (cost={cost})"
        )
