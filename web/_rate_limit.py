"""Shared rate-limit helpers for route modules.

Two flavors live here:

  llm_rate_check(request, ...)       — function called INSIDE handler bodies
                                        to gate heavy Ollama LLM calls.
                                        Lifted from glassbox_server.py
                                        2026-05-22 (commit `1039777`) for
                                        P3-H Phase 1 extractions #9, #10.

  request_rate_limit(*, max_per_window, window_sec, scope)
                                      — DECORATOR applied at @ position
                                        on FastAPI handlers, gates by IP
                                        + scope. Lifted from api_v1.py
                                        2026-05-27 for P3-H Phase 2
                                        extraction #5 prep.

Both share the IP-resolution order:
  CF-Connecting-IP → X-Forwarded-For first hop → request.client.host

Different bucket dicts (`_BUCKET` for LLM scope, `_REQUEST_BUCKETS` for
the request-decorator). Could have shared but separate scopes make
debugging easier and a stray `.clear()` from one test won't blow away
the other's state.

Underscore-prefixed module name signals web-internal plumbing; public
function names drop the underscore because they're cross-module API.

Dropped along with the LLM lift: `_LLM_SEMAPHORE = asyncio.Semaphore(2)`
which was defined alongside `_llm_rate_check` but never imported or
referenced anywhere in the codebase. Concurrency control would be a
nice-to-have around heavy LLM calls but adding it back is a separate
task — leaving a dead symbol in place adds nothing.
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Dict, List, Optional

from fastapi import HTTPException, Request


# ─── LLM scope (called inside handlers) ───────────────────────────────


# Shared per-(scope, IP) bucket. Process-local, reset on restart;
# acceptable for the use case (rate-limiting client-side LLM curl loops,
# not durable abuse defense).
_BUCKET: Dict[str, List[float]] = {}


def llm_rate_check(
    request: Request,
    scope: str = "llm",
    max_per_window: int = 10,
    window_sec: int = 300,
) -> None:
    """Raise HTTP 429 if this IP has exceeded `max_per_window` LLM calls
    in `window_sec` seconds.

    Client-IP resolution order (matches the production CDN setup):
      1. `CF-Connecting-IP` header — what Cloudflare sets for the real
         client when traffic passes through their edge
      2. `X-Forwarded-For` header, first hop — fallback for non-CF
         proxies
      3. `request.client.host` — direct connection (local dev,
         localhost smoke tests)
    """
    ip = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
    key = f"{scope}:{ip}"
    now = time.time()
    cutoff = now - window_sec
    bucket = [t for t in _BUCKET.get(key, []) if t > cutoff]
    if len(bucket) >= max_per_window:
        retry = int(window_sec - (now - bucket[0]))
        raise HTTPException(
            status_code=429,
            detail=(
                f"LLM rate limit: max {max_per_window} per "
                f"{window_sec // 60}min. Try again in ~{max(1, retry)}s."
            ),
            headers={"Retry-After": str(max(1, retry))},
        )
    bucket.append(now)
    _BUCKET[key] = bucket


# ─── Request scope (decorator) ────────────────────────────────────────


# Separate bucket for the @request_rate_limit decorator. See module
# docstring for why this isn't shared with _BUCKET.
# `test_signals_subscribe_endpoint.py` calls `.clear()` on this dict
# (via the `api_v1._RATE_BUCKETS` re-export alias) between tests, so any
# caller that needs the bucket MUST reference the live module-level dict,
# not a snapshot.
_REQUEST_BUCKETS: Dict[str, List[float]] = {}


def request_rate_limit(*, max_per_window: int, window_sec: int, scope: str):
    """Decorator. Returns HTTP 429 if the requesting IP has exceeded
    `max_per_window` calls to `scope` in the last `window_sec` seconds.

    Cloudflare-friendly IP resolution (CF-Connecting-IP first, then
    X-Forwarded-For first hop, then request.client.host).

    Lazy GC: when the bucket dict crosses 5000 entries, prune buckets
    older than the current window in a single pass. Sized so a handful
    of pathological IPs can't unbounded-grow the dict, while normal
    traffic stays well below the threshold.

    Lifted from `api_v1.py` 2026-05-27 (P3-H Phase 2 extraction #5
    prep). Still callable via `api_v1._rate_limit` (re-export shim)
    so inline `@_rate_limit(...)` decorators inside `build_router`
    continue to work until `/analytics/*` + `/signals/subscribe`
    extract.
    """
    def deco(handler):
        @wraps(handler)
        async def wrapper(*args, **kwargs):
            request: Optional[Request] = kwargs.get("request") or (
                args[0] if args and isinstance(args[0], Request) else None
            )
            if request is not None:
                ip = (request.headers.get("cf-connecting-ip")
                      or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                      or (request.client.host if request.client else "unknown"))
                key = f"{scope}:{ip}"
                now = time.time()
                cutoff = now - window_sec
                bucket = _REQUEST_BUCKETS.get(key, [])
                bucket = [t for t in bucket if t > cutoff]
                if len(bucket) >= max_per_window:
                    retry_after = int(window_sec - (now - bucket[0]))
                    raise HTTPException(
                        status_code=429,
                        detail=f"Rate limit: max {max_per_window} per {window_sec}s. "
                               f"Try again in ~{max(1, retry_after)}s.",
                        headers={"Retry-After": str(max(1, retry_after))},
                    )
                bucket.append(now)
                _REQUEST_BUCKETS[key] = bucket
                # Lazy GC — prune buckets older than the window when the
                # dict crosses 5000 entries.
                if len(_REQUEST_BUCKETS) > 5000:
                    for k in list(_REQUEST_BUCKETS.keys()):
                        if not [t for t in _REQUEST_BUCKETS[k] if t > cutoff]:
                            _REQUEST_BUCKETS.pop(k, None)
            return await handler(*args, **kwargs)
        return wrapper
    return deco
