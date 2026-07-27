"""
Glassbox Publisher — Mac Mini → Cloudflare Worker edge state mirror

Loop Step 6. Lives next to glassbox_server.py on the Mac Mini.

What it does:
  - Subscribes to the in-process event stream + LoopBridge alerts
  - Buffers the last N events (default 200) and last M alerts (default 50)
  - Every PUBLISH_INTERVAL_SEC, POSTs an aggregated state blob to the Worker
  - Worker stashes it in KV for the GET /api/glassbox/state edge cache

Why this exists:
  Without a publisher, glassbox-markets.html visitors must hit the Mac Mini
  directly via SSE for first paint. That's slow (cold-start a long-running
  connection) and exposes the home network. The publisher pushes a digest
  to Cloudflare's edge so:
    1. Page TTI < 1s (read static-cached JSON, no SSE handshake needed)
    2. Mac Mini doesn't get hammered by every visitor
    3. Public visitors never directly touch the Mac Mini

  SSE then UPGRADES from edge state to live updates — best of both worlds.

Failure modes (all handled):
  - Worker unreachable → don't lose buffer, retry next flush
  - Worker returns non-2xx → log, don't lose buffer
  - HTTP timeout → ditto
  - Publisher crashes → ingester loop keeps going, /api/health surfaces
  - KV write fails on Worker side → publisher just retries

Backed by:
  - Tests in 29_MEWR_OS/core/agent_tests.py — TestGlassboxPublisher (7 tests)

Usage (from glassbox_server.py):
    from glassbox_publisher import GlassboxPublisher
    pub = GlassboxPublisher(
        worker_url=os.environ["GLASSBOX_PUBLISHER_URL"],   # https://mewr-news-api.../api/glassbox/state
        api_token=os.environ["NEWS_API_TOKEN"],
    )
    # Subscribe to broadcast pipeline:
    ingester._broadcaster = lambda batch: (
        # ... existing broadcast logic ...
        pub.on_event(ev) for ev in (batch if isinstance(batch, list) else [batch])
    )
    # Subscribe to LoopBridge:
    bridge = LoopBridge(registry=..., on_alert=[pub.on_alert, slack_notify])
    # Spin up flusher thread:
    pub.start_flusher_thread()  # background thread, flushes every 30s

Author: 2026-04-25 — Loop Step 6
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, is_dataclass
from typing import Any, Deque, Dict, Iterable, List, Optional

log = logging.getLogger("glassbox_publisher")


# ─── HTTP client interface (injectable for tests) ────────────────────────

class _DefaultHTTP:
    """urllib-based POSTer used in production. Tests inject their own mock."""

    def post(self, url: str, json_body: Dict[str, Any],
             headers: Optional[Dict[str, str]] = None,
             timeout_sec: float = 10.0) -> Dict[str, Any]:
        data = json.dumps(json_body).encode("utf-8")
        # 2026-04-28: Cloudflare's Browser Integrity Check returns HTTP 403
        # with error code 1010 for the default Python-urllib User-Agent.
        # A real-browser UA passes through cleanly. Caller can still override
        # via the `headers` kwarg if needed.
        hdrs = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) MEWR-Glassbox-Publisher/1.0",
            "Accept": "application/json",
        }
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as r:
                body_bytes = r.read()
                try:
                    body = json.loads(body_bytes.decode("utf-8"))
                except ValueError:
                    body = {"raw": body_bytes.decode("utf-8", errors="replace")}
                return {"status": r.status, "body": body}
        except urllib.error.HTTPError as e:
            raise ConnectionError(f"HTTP {e.code} from {url}: {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            raise ConnectionError(f"unreachable {url}: {e.reason}") from e


# ─── Event/Alert serialization ───────────────────────────────────────────

def _event_to_dict(ev: Any) -> Dict[str, Any]:
    """Convert a GlassboxEvent (or dict) to the wire shape."""
    if isinstance(ev, dict):
        return ev
    if hasattr(ev, "to_dict"):
        return ev.to_dict()
    if is_dataclass(ev):
        return asdict(ev)
    # Last resort: pull known attrs
    return {k: getattr(ev, k, None)
            for k in ("layer", "external_id", "kind", "lat", "lng", "ts",
                      "severity", "severity_for_market", "market_tags",
                      "domain", "geocode_quality", "decay_half_life_min", "payload")}


def _alert_to_dict(alert: Any) -> Dict[str, Any]:
    """Convert an EdgeAlert (or dict) to the wire shape."""
    if isinstance(alert, dict):
        return alert
    if hasattr(alert, "to_dict"):
        return alert.to_dict()
    if is_dataclass(alert):
        return asdict(alert)
    return {"matched_tag": getattr(alert, "matched_tag", None),
            "severity_for_market": getattr(alert, "severity_for_market", None),
            "reason": getattr(alert, "reason", None),
            "market": dict(getattr(alert, "market", {}) or {}),
            "event": _event_to_dict(getattr(alert, "event", {})),
            "timestamp": getattr(alert, "timestamp", time.time())}


# ─── Publisher ───────────────────────────────────────────────────────────

class GlassboxPublisher:
    """Buffers events + alerts, periodically POSTs the aggregated state to
    the Worker's /api/glassbox/state endpoint. Thread-safe via a single lock.

    The publisher is a passive component — it doesn't pull data, it consumes
    via on_event() / on_alert() callbacks. Caller wires it into the broadcast
    + LoopBridge pipelines.
    """

    def __init__(
        self,
        worker_url: str,
        api_token: str,
        http: Optional[Any] = None,
        max_events: int = 200,
        max_alerts: int = 50,
        publish_interval_sec: float = 90.0,
        # 2026-04-28: bumped from 30s -> 90s to stay under the Cloudflare
        # Workers FREE tier KV write limit (1,000 writes/day).
        #   30s = 2,880 writes/day → blows cap in ~8 hours (last seen 09:20 ET 4/28).
        #   90s =   960 writes/day → safe under cap with a little margin.
        # If we upgrade to Workers Paid ($5/mo, 10M writes/month) drop this
        # back to 30s for tighter latency.
    ) -> None:
        self.worker_url = worker_url
        self.api_token = api_token
        self.http = http or _DefaultHTTP()
        self.max_events = max_events
        self.max_alerts = max_alerts
        self.publish_interval_sec = publish_interval_sec

        # Buffers (FIFO eviction at max). Lock protects buffers + stats.
        self._lock = threading.Lock()

        # 2026-04-28: per-layer deques replace the single self._events deque.
        # Without this, high-volume layers (planes ~100 events/cycle) starve
        # low-volume layers (ships, sats) before flush — Worker KV would only
        # show planes. Each layer gets its own cap so all are represented in
        # the flushed snapshot.
        self._events_by_layer: Dict[str, Deque[Dict[str, Any]]] = {}
        # Per-layer caps tuned to typical volume. Unknown layers default to 40.
        self._layer_caps = {
            "planes":            150,
            "ships":             80,
            "satellites":        120,
            "earthquakes":       40,
            "gdelt":             40,
            "citizen_osint":     40,
            "traffic_cams":      40,
            "police_incidents":  40,
            "noaa_weather":      30,
            "fred_macro":        20,
            # acled_conflict removed 2026-05-09: ingester archived to
            # _archive/2026_05_09_research_integration/disabled_ingesters_no_commercial_license/
            # because ACLED requires a commercial license MEWR does not hold.
            # See 00_MASTER_DOCS/legal/LICENSE_RISK_REGISTER.md.
            "odds_api":          20,
        }
        self._default_layer_cap = 40
        # Legacy alias — points at a synthetic "all layers" view used by
        # snapshot()/tests. Always rebuilt from the per-layer deques.
        self._events: Deque[Dict[str, Any]] = deque(maxlen=max_events)
        self._alerts: Deque[Dict[str, Any]] = deque(maxlen=max_alerts)

        self._stats = {
            "events_received": 0,
            "alerts_received": 0,
            "flushes_attempted": 0,
            "flushes_succeeded": 0,
            "http_failures": 0,
            "last_flush_ts": None,
            "last_flush_payload_size": 0,
        }

        # Background flusher thread
        self._flush_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ─── Subscribe surface ────────────────────────────────────────────

    def _route_event(self, ev_dict: Dict[str, Any]) -> None:
        """Route one event-as-dict into its per-layer deque. Caller holds lock."""
        layer = ev_dict.get("layer") or "?"
        deq = self._events_by_layer.get(layer)
        if deq is None:
            cap = self._layer_caps.get(layer, self._default_layer_cap)
            deq = deque(maxlen=cap)
            self._events_by_layer[layer] = deq
        deq.append(ev_dict)
        # Mirror into legacy self._events for snapshot/tests
        self._events.append(ev_dict)
        self._stats["events_received"] += 1

    def on_event(self, event: Any) -> None:
        """Buffer one event for next flush. Wire into ingester broadcast."""
        with self._lock:
            self._route_event(_event_to_dict(event))

    def on_alert(self, alert: Any) -> None:
        """Buffer one EdgeAlert for next flush. Wire into LoopBridge."""
        with self._lock:
            self._alerts.append(_alert_to_dict(alert))
            self._stats["alerts_received"] += 1

    def on_event_batch(self, events: Iterable[Any]) -> None:
        """Convenience for batch broadcast — same lock acquired once."""
        with self._lock:
            for ev in events:
                self._route_event(_event_to_dict(ev))

    # ─── Snapshot + flush ────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Read-only view of current buffer contents. Used by /api/health
        and tests."""
        with self._lock:
            return {
                "events": list(self._events),
                "alerts": list(self._alerts),
                "stats": dict(self._stats),
            }

    def flush(self) -> bool:
        """POST current buffer contents to the Worker. Returns True on success.

        On success: clears the buffer (events not double-published).
        On HTTP failure: keeps the buffer intact for the next flush.
        """
        with self._lock:
            self._stats["flushes_attempted"] += 1
            # 2026-04-28: removed the early-return-on-empty-buffer.
            # Worker stores `glassbox:state` with a 10-minute TTL — if we skip
            # the POST when the buffer is empty (ingesters rate-limited, quiet
            # period, etc.), the KV value ages out and the public site goes
            # dark for the next 10 minutes. Always posting keeps the TTL fresh
            # and gives consumers a heartbeat. Empty-payload POSTs are cheap.
            #
            # Sample from per-layer deques so all layers are represented even
            # when one (planes) floods. Concatenate in deterministic order so
            # the Worker's `slice(-N)` truncation drops uniformly across the
            # tail rather than starving a single layer.
            sampled_events = []
            for layer_name, deq in self._events_by_layer.items():
                sampled_events.extend(list(deq))
            # Per-layer counts in stats for diagnostic visibility
            self._stats["per_layer_buffered"] = {
                k: len(v) for k, v in self._events_by_layer.items()
            }
            payload = {
                "events": sampled_events,
                "alerts": list(self._alerts),
                "stats": dict(self._stats),
                "ts": time.time(),
            }

        # Network call OUTSIDE the lock so on_event/on_alert from other
        # threads aren't blocked during the POST.
        size = len(json.dumps(payload))
        try:
            resp = self.http.post(
                self.worker_url,
                json_body=payload,
                headers={"Authorization": f"Bearer {self.api_token}"},
            )
            status = resp.get("status", 0) if isinstance(resp, dict) else 0
            if not (200 <= status < 300):
                raise ConnectionError(f"non-2xx status {status}")
        except Exception as e:
            with self._lock:
                self._stats["http_failures"] += 1
            log.warning(f"flush failed (buffer retained for retry): {e}")
            return False

        # Success — clear what we sent + record stats
        with self._lock:
            # Drain each per-layer deque by the count we sampled at flush
            # time. Per-layer counts captured pre-flush so new events that
            # arrived during the POST stay buffered for the next flush.
            for layer_name, deq in list(self._events_by_layer.items()):
                drain_count = self._stats.get("per_layer_buffered", {}).get(layer_name, 0)
                for _ in range(drain_count):
                    if deq:
                        deq.popleft()
            # Also drain the legacy mirror deque by the same amount
            for _ in range(len(payload["events"])):
                if self._events:
                    self._events.popleft()
            for _ in range(len(payload["alerts"])):
                if self._alerts:
                    self._alerts.popleft()
            self._stats["flushes_succeeded"] += 1
            self._stats["last_flush_ts"] = time.time()
            self._stats["last_flush_payload_size"] = size
        return True

    # ─── Background flusher thread ───────────────────────────────────

    def start_flusher_thread(self) -> None:
        """Start a daemon thread that flushes every publish_interval_sec.
        Idempotent — returns silently if already running."""
        if self._flush_thread and self._flush_thread.is_alive():
            return
        self._stop_event.clear()
        t = threading.Thread(target=self._flusher_loop, daemon=True,
                             name="glassbox-publisher")
        t.start()
        self._flush_thread = t

    def stop_flusher_thread(self, timeout_sec: float = 5.0) -> None:
        self._stop_event.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=timeout_sec)

    def _flusher_loop(self) -> None:
        log.info(f"publisher flusher up — interval={self.publish_interval_sec}s "
                 f"target={self.worker_url}")
        while not self._stop_event.is_set():
            try:
                self.flush()
            except Exception as e:
                # Defensive — flush() already swallows known failures, but
                # ANY exception here must not kill the thread.
                log.exception(f"unexpected flush error: {e}")
            # Sleep in 1-second slices so stop is responsive
            slept = 0.0
            while slept < self.publish_interval_sec and not self._stop_event.is_set():
                time.sleep(min(1.0, self.publish_interval_sec - slept))
                slept += 1.0

    # ─── Diagnostics ────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._stats)
