"""Shared SSE-subscriber delivery helper.

Lifted from `glassbox_server.py` 2026-05-22 EVE as the P3-H prerequisite
for extracting the `/api/glassbox/sitrep/publish` handler (extraction
#9) — the helper is called by both the in-file broadcaster and the
sitrep/publish route, so it must live somewhere the extracted route
module can import.

Same Option A pattern as `web/_assets.py` and `web/_rate_limit.py`:
underscore-prefixed module name signals web-internal plumbing; public
function name drops the underscore (`deliver_to_subscribers`) because
it's cross-module API.

The function takes a `state` argument (typically `request.app.state` or
the in-process `app.state` from glassbox_server.py) so callers don't
need to thread separate subscribers / drops / config arguments. State
shape required:

    state.subscribers              List[asyncio.Queue]
    state.subscriber_drops         Dict[int, int]
    state.broadcast_drop_limit     int
    state.broadcast_drop_log_every int
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

log = logging.getLogger("glassbox-server.broadcast")


def deliver_to_subscribers(state: Any, msg: Dict[str, Any]) -> None:
    """Push `msg` to every SSE subscriber on `state.subscribers`. Track
    consecutive-drop counts per subscriber; evict any subscriber that
    exceeds `state.broadcast_drop_limit`. Periodic warning log every
    `state.broadcast_drop_log_every` drops.

    Shared by `_broadcast()` in glassbox_server.py (batch path) and by
    the `/api/glassbox/sitrep/publish` route (single-message path)."""
    subscribers: List[asyncio.Queue] = state.subscribers
    drops_map: Dict[int, int] = state.subscriber_drops
    drop_limit: int = state.broadcast_drop_limit
    drop_log_every: int = state.broadcast_drop_log_every

    evicted: List[asyncio.Queue] = []
    for q in list(subscribers):
        qid = id(q)
        try:
            q.put_nowait(msg)
            if qid in drops_map:
                drops_map.pop(qid, None)
        except asyncio.QueueFull:
            drops = drops_map.get(qid, 0) + 1
            drops_map[qid] = drops
            if drops == 1 or drops % drop_log_every == 0:
                log.warning(
                    "SSE subscriber slow: %d consecutive drops (qsize=%d, limit=%d)",
                    drops, q.qsize(), drop_limit,
                )
            if drops >= drop_limit:
                evicted.append(q)
    for q in evicted:
        try:
            subscribers.remove(q)
        except ValueError:
            pass
        drops_map.pop(id(q), None)
        log.warning(
            "SSE subscriber evicted after %d consecutive drops",
            drop_limit,
        )
