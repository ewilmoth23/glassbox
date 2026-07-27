"""
Glassbox firehose endpoint — the public WebSocket interface for event consumers.

This module implements the contract specified in CONSUMER_API_CONTRACT.md:
  - WebSocket /events/subscribe accepts filter spec on connect
  - Server emits matching GlassboxEvents in real time
  - Heartbeat every 20s
  - Per-token rate limiting (concurrent connection cap)
  - Auth via api_token table

Design decisions:
  - Self-contained module — glassbox_server.py imports + wires into FastAPI
  - Subscriber state lives in this module (FirehoseManager singleton)
  - Filtering happens server-side BEFORE send, so consumers don't drown in events
  - Geographic filter uses simple bbox/circle math (no PostGIS dependency at this layer)
  - Each subscriber gets an asyncio.Queue (bounded) — slow consumers get evicted, not backpressure'd
  - Heartbeat task runs once globally, ticks all subscribers (vs one task per sub)

Does NOT depend on:
  - Postgres (auth lookup is async-pluggable; for v1.0 startup, accepts a token validator function)
  - Prediqt or any consumer
  - The 23_FULCRUM_MARKETS or 30_PREDIQT folders

Phase 0.5 Step 1 deliverable. Wired into glassbox_server.py during Step 4 of the decoupling plan.

Tests: see 21_GLASSBOX_AI/tests/test_firehose.py (parallel scope — write next).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

# WebSocket imports are gated to keep this module testable without FastAPI.
# Production wiring imports WebSocket / WebSocketDisconnect from FastAPI.
try:
    from fastapi import WebSocket, WebSocketDisconnect  # type: ignore
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    WebSocket = None  # type: ignore
    WebSocketDisconnect = Exception  # type: ignore

log = logging.getLogger("glassbox.firehose")


# ─────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────

# Token validator interface — caller-supplied. Returns user_id on success,
# None on invalid token. Async because v1.2 will hit Postgres for the lookup.
TokenValidator = Callable[[str], Awaitable[Optional[str]]]

# Default no-auth validator for v1.0 single-user mode. Accepts any non-empty
# token + maps to 'system' user. Replace with real validator at wire-up time.
async def _default_no_auth_validator(token: str) -> Optional[str]:
    if token and isinstance(token, str) and len(token) > 8:
        return "system"
    return None


@dataclass
class GeographicFilter:
    """One of: global / bbox / country / circle. See CONSUMER_API_CONTRACT.md."""
    type: str = "global"  # 'global' | 'bbox' | 'country' | 'circle'
    bbox: Optional[Tuple[float, float, float, float]] = None  # west, south, east, north
    iso: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    radius_km: Optional[float] = None

    def matches(self, ev_lat: Optional[float], ev_lng: Optional[float]) -> bool:
        """Cheap event-vs-filter check. Defaults to True if event has no geo
        (some events are non-geographic, e.g. macro announcements) — consumers
        that only want geo events should filter on event_type instead."""
        if ev_lat is None or ev_lng is None:
            return True

        if self.type == "global":
            return True

        if self.type == "bbox" and self.bbox:
            west, south, east, north = self.bbox
            # Handle antimeridian (west > east means crosses dateline)
            if west <= east:
                lng_in = west <= ev_lng <= east
            else:
                lng_in = ev_lng >= west or ev_lng <= east
            return south <= ev_lat <= north and lng_in

        if self.type == "country" and self.iso:
            # v1.0 stub: country filtering needs reverse-geocode lookup.
            # For now, accept everything and document the limitation.
            # Phase 1.5 adds proper reverse-geo via PostGIS country boundaries.
            return True

        if self.type == "circle" and self.lat is not None and self.lng is not None and self.radius_km:
            return _haversine_km(self.lat, self.lng, ev_lat, ev_lng) <= self.radius_km

        return True

    @classmethod
    def parse(cls, raw: Optional[Dict[str, Any]]) -> "GeographicFilter":
        if not raw or not isinstance(raw, dict):
            return cls(type="global")
        t = raw.get("type", "global")
        if t == "bbox":
            bb = raw.get("bbox")
            if isinstance(bb, list) and len(bb) == 4:
                return cls(type="bbox", bbox=tuple(float(x) for x in bb))  # type: ignore
            return cls(type="global")
        if t == "country":
            return cls(type="country", iso=str(raw.get("iso", "")))
        if t == "circle":
            return cls(
                type="circle",
                lat=float(raw["lat"]),
                lng=float(raw["lng"]),
                radius_km=float(raw["radius_km"]),
            )
        return cls(type="global")


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km. Cheap; avoids PostGIS dependency."""
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))


@dataclass
class SubscriptionFilter:
    """Server-side filter spec — applied to every event before send."""
    user_id: str
    api_version: str
    categories: Set[str] = field(default_factory=set)       # event_type values; empty = all
    subcategories: Set[str] = field(default_factory=set)    # event_subtype values
    min_confidence: float = 0.0
    min_severity: float = 0.0
    geographic_filter: GeographicFilter = field(default_factory=GeographicFilter)

    def matches(self, event: Dict[str, Any]) -> bool:
        """Return True if event should be delivered to this subscriber."""
        if self.categories:
            if event.get("event_type") not in self.categories:
                return False
        if self.subcategories:
            if event.get("event_subtype") not in self.subcategories:
                return False
        if (event.get("confidence") or 0.0) < self.min_confidence:
            return False
        if (event.get("severity") or 0.0) < self.min_severity:
            return False
        loc = event.get("location") or {}
        ev_lat = loc.get("lat")
        ev_lng = loc.get("lng")
        if not self.geographic_filter.matches(ev_lat, ev_lng):
            return False
        return True


@dataclass
class Subscriber:
    """One connected WebSocket consumer."""
    sub_id: str
    user_id: str
    filters: SubscriptionFilter
    queue: asyncio.Queue
    connected_at: float
    events_sent: int = 0
    events_dropped: int = 0  # incremented when queue is full (slow consumer)


# ─────────────────────────────────────────────────────────────────────
# Manager
# ─────────────────────────────────────────────────────────────────────

class FirehoseManager:
    """Owns subscriber registry + heartbeat task + broadcast fanout.

    Usage from glassbox_server.py:

        from firehose import FirehoseManager

        firehose = FirehoseManager(token_validator=my_token_validator)

        @app.on_event("startup")
        async def _startup():
            await firehose.start()

        @app.on_event("shutdown")
        async def _shutdown():
            await firehose.stop()

        @app.websocket("/events/subscribe")
        async def subscribe(websocket: WebSocket):
            await firehose.handle_connection(websocket)

        # In your existing _broadcast() function:
        def _broadcast(events):
            ...existing logic...
            firehose.broadcast(events)  # add this line — non-blocking
    """

    HEARTBEAT_INTERVAL_SEC = 20.0
    SUBSCRIBER_QUEUE_SIZE = 1000
    MAX_CONCURRENT_PER_USER = 10
    SUPPORTED_API_VERSIONS = {"v1"}

    def __init__(
        self,
        token_validator: Optional[TokenValidator] = None,
    ) -> None:
        self._token_validator = token_validator or _default_no_auth_validator
        self._subscribers: Dict[str, Subscriber] = {}
        self._subs_by_user: Dict[str, Set[str]] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False

    # ─── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn the heartbeat task. Called from FastAPI startup."""
        if self._running:
            return
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        log.info("FirehoseManager started")

    async def stop(self) -> None:
        """Stop heartbeat + close all subscribers. Called from FastAPI shutdown."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        # Close all subscribers
        for sub_id in list(self._subscribers.keys()):
            self._evict(sub_id, code=1001, reason="server shutdown")
        log.info("FirehoseManager stopped")

    # ─── Connection handler ────────────────────────────────────────

    async def handle_connection(self, websocket: Any) -> None:
        """Run for the lifetime of one WebSocket. FastAPI route handler
        is just a thin wrapper that calls this."""
        if not HAS_FASTAPI:
            raise RuntimeError("FastAPI not available — cannot handle WebSocket connections")

        await websocket.accept()
        sub_id: Optional[str] = None
        try:
            # Step 1 — receive subscription frame
            try:
                first_frame = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            except asyncio.TimeoutError:
                await self._send_error(websocket, "TIMEOUT", "Subscription frame not received within 10s")
                await websocket.close(code=4000)
                return

            try:
                sub_msg = json.loads(first_frame)
            except json.JSONDecodeError as e:
                await self._send_error(websocket, "INVALID_JSON", str(e))
                await websocket.close(code=4000)
                return

            # Step 2 — validate API version
            api_version = sub_msg.get("api_version", "v1")
            if api_version not in self.SUPPORTED_API_VERSIONS:
                await self._send_error(
                    websocket,
                    "VERSION_UNSUPPORTED",
                    f"API version {api_version} not supported. Use one of: {sorted(self.SUPPORTED_API_VERSIONS)}",
                )
                await websocket.close(code=4000)
                return

            # Step 3 — validate auth token
            token = sub_msg.get("auth_token")
            if not token:
                await self._send_error(websocket, "AUTH_FAILED", "auth_token required")
                await websocket.close(code=4001)
                return
            user_id = await self._token_validator(token)
            if not user_id:
                await self._send_error(websocket, "AUTH_FAILED", "Invalid api token")
                await websocket.close(code=4001)
                return

            # Step 4 — enforce per-user concurrent connection cap
            existing = self._subs_by_user.get(user_id, set())
            if len(existing) >= self.MAX_CONCURRENT_PER_USER:
                await self._send_error(
                    websocket,
                    "RATE_LIMITED",
                    f"Max {self.MAX_CONCURRENT_PER_USER} concurrent connections per user",
                )
                await websocket.close(code=4002)
                return

            # Step 5 — parse filters
            filters = SubscriptionFilter(
                user_id=user_id,
                api_version=api_version,
                categories=set(sub_msg.get("categories") or []),
                subcategories=set(sub_msg.get("subcategories") or []),
                min_confidence=float(sub_msg.get("min_confidence", 0.0)),
                min_severity=float(sub_msg.get("min_severity", 0.0)),
                geographic_filter=GeographicFilter.parse(sub_msg.get("geographic_filter")),
            )

            # Step 6 — register + send confirmation
            sub_id = uuid.uuid4().hex
            sub = Subscriber(
                sub_id=sub_id,
                user_id=user_id,
                filters=filters,
                queue=asyncio.Queue(maxsize=self.SUBSCRIBER_QUEUE_SIZE),
                connected_at=time.time(),
            )
            self._subscribers[sub_id] = sub
            self._subs_by_user.setdefault(user_id, set()).add(sub_id)

            await websocket.send_text(json.dumps({
                "type": "subscribed",
                "subscription_id": sub_id,
                "filters": {
                    "categories": sorted(filters.categories),
                    "subcategories": sorted(filters.subcategories),
                    "min_confidence": filters.min_confidence,
                    "min_severity": filters.min_severity,
                    "geographic_filter": filters.geographic_filter.type,
                },
            }))
            log.info(f"sub {sub_id[:8]} connected — user={user_id} filters={len(filters.categories)} cats")

            # Step 7 — drain queue → WS until disconnect
            await self._drain_to_ws(websocket, sub)

        except WebSocketDisconnect:
            log.debug(f"sub {sub_id[:8] if sub_id else '?'} disconnected (client)")
        except Exception as e:
            log.warning(f"sub {sub_id[:8] if sub_id else '?'} crashed: {e}")
        finally:
            if sub_id:
                self._evict(sub_id)

    async def _drain_to_ws(self, websocket: Any, sub: Subscriber) -> None:
        """Read from sub's queue, write to ws. Loop until ws errors."""
        while True:
            try:
                msg = await sub.queue.get()
            except asyncio.CancelledError:
                break
            try:
                await websocket.send_text(msg)
                sub.events_sent += 1
            except Exception as e:
                log.debug(f"sub {sub.sub_id[:8]} send failed: {e}")
                break

    async def _send_error(self, websocket: Any, code: str, message: str) -> None:
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "code": code,
                "message": message,
            }))
        except Exception:
            pass  # Best-effort

    def _evict(self, sub_id: str, code: int = 1000, reason: str = "") -> None:
        """Remove subscriber from registry. Idempotent."""
        sub = self._subscribers.pop(sub_id, None)
        if not sub:
            return
        user_subs = self._subs_by_user.get(sub.user_id)
        if user_subs:
            user_subs.discard(sub_id)
            if not user_subs:
                self._subs_by_user.pop(sub.user_id, None)
        log.info(f"sub {sub_id[:8]} evicted — sent={sub.events_sent} dropped={sub.events_dropped}")

    # ─── Broadcast (called from existing _broadcast in glassbox_server.py) ───

    def broadcast(self, events: Any) -> None:
        """Non-blocking broadcast to all matching subscribers.
        Accepts a single event dict OR a list of events.
        Slow consumers get events dropped (queue full) rather than backpressuring
        the ingester. evicted only after sustained drops (TODO Phase 6)."""
        if isinstance(events, dict):
            events_list = [events]
        else:
            events_list = list(events)
        if not events_list or not self._subscribers:
            return

        for ev in events_list:
            ev_v1 = self._to_event_v1(ev)
            msg = json.dumps({
                "type": "event",
                "api_version": "v1",
                "event": ev_v1,
            })
            for sub in list(self._subscribers.values()):
                if not sub.filters.matches(ev_v1):
                    continue
                try:
                    sub.queue.put_nowait(msg)
                except asyncio.QueueFull:
                    sub.events_dropped += 1
                    # TODO Phase 6: evict after N sustained drops

    @staticmethod
    def _to_event_v1(ev: Any) -> Dict[str, Any]:
        """Coerce an internal event into the EventV1 wire shape per CONSUMER_API_CONTRACT.
        Defensive: accepts dataclass, dict, or anything with to_dict().
        Future: tighten this once ingesters all emit consistent shapes."""
        if hasattr(ev, "to_dict"):
            d = ev.to_dict()
        elif isinstance(ev, dict):
            d = dict(ev)
        else:
            try:
                d = vars(ev)
            except TypeError:
                d = {"raw": str(ev)}

        # Map internal fields to the public EventV1 shape per CONSUMER_API_CONTRACT.
        location = None
        if d.get("lat") is not None and d.get("lng") is not None:
            location = {
                "lat": d.get("lat"),
                "lng": d.get("lng"),
                "altitude_m": d.get("altitude_m"),
            }

        sources_list = d.get("sources") or []
        if not sources_list and d.get("source"):
            sources_list = [{"source_type": d.get("source"), "fetched_at": d.get("ts")}]

        return {
            "id": d.get("id") or d.get("external_id"),
            "event_type": d.get("layer") or d.get("event_type") or "unknown",
            "event_subtype": d.get("payload", {}).get("topic") if isinstance(d.get("payload"), dict) else d.get("event_subtype"),
            "event_time": d.get("event_time") or d.get("ts"),
            "detected_at": d.get("ts"),
            "location": location,
            "geocode_quality": d.get("geocode_quality") or "unknown",
            "title": (d.get("payload") or {}).get("headline") if isinstance(d.get("payload"), dict) else d.get("title"),
            "description": d.get("description"),
            "severity": d.get("severity"),
            "domain": d.get("domain") or "unknown",
            "decay_half_life_min": d.get("decay_half_life_min") or 60,
            "involved_entities": d.get("involved_entities") or [],
            "sources": sources_list,
            "confidence": d.get("confidence", 1.0),
            "properties": d.get("payload") if isinstance(d.get("payload"), dict) else {},
            "api_version": "v1",
        }

    # ─── Heartbeat ─────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Tick all subscribers every HEARTBEAT_INTERVAL_SEC. If a sub's
        queue is full of pings, the consumer is dead — evict it."""
        while self._running:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL_SEC)
                ping_msg = json.dumps({
                    "type": "ping",
                    "ts": _utc_iso(),
                })
                for sub_id, sub in list(self._subscribers.items()):
                    try:
                        sub.queue.put_nowait(ping_msg)
                    except asyncio.QueueFull:
                        # 1000 pings backed up = consumer is dead
                        log.info(f"sub {sub_id[:8]} queue full on heartbeat — evicting")
                        self._evict(sub_id, code=4003, reason="consumer too slow")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"heartbeat loop error: {e}")

    # ─── Diagnostic ────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        """Snapshot of firehose state — surfaced via /api/glassbox/diagnostic."""
        return {
            "running": self._running,
            "subscriber_count": len(self._subscribers),
            "users_connected": len(self._subs_by_user),
            "subscribers": [
                {
                    "sub_id": s.sub_id[:8],
                    "user_id": s.user_id,
                    "connected_at": s.connected_at,
                    "events_sent": s.events_sent,
                    "events_dropped": s.events_dropped,
                    "queue_size": s.queue.qsize(),
                    "categories": sorted(s.filters.categories),
                    "min_severity": s.filters.min_severity,
                }
                for s in self._subscribers.values()
            ],
        }


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
