"""
Glassbox — Citizen OSINT + Traffic Cams Adapter
================================================
Wraps CitizenOSINTIngester and TrafficCamsIngester with the server interface
(layer, source, status(), run_forever(), stop()) so they plug into
glassbox_server.py _startup() identically to the base Ingester subclasses.

These orchestrators produce dicts rather than GlassboxEvent objects, so this
adapter also handles the dict → GlassboxEvent conversion and broadcasting.

Wire-up in glassbox_server.py:
    from ingesters.citizen_adapter import CitizenOSINTAdapter, TrafficCamsAdapter
    ...
    CitizenOSINTAdapter(broadcaster=_broadcast, logger=...),
    TrafficCamsAdapter(broadcaster=_broadcast, logger=...),
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .base import GlassboxEvent, BroadcastFn

# ─── Feed file paths ──────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent.parent
CITIZEN_FEED_FILE = _HERE / "citizen_sentinel_feed.json"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict_to_event(ev: Dict[str, Any]) -> Optional[GlassboxEvent]:
    """Convert a citizen-OSINT or traffic-cam dict into a GlassboxEvent."""
    try:
        lat = float(ev.get("lat", 0.0))
        lng = float(ev.get("lng", 0.0))
        return GlassboxEvent(
            layer=ev.get("layer", "citizen_osint"),
            external_id=ev["external_id"],
            kind=ev.get("kind", "alert"),
            lat=lat,
            lng=lng,
            ts=ev.get("timestamp") or ev.get("ts") or _now_iso(),
            severity=int(ev.get("severity", 3)),
            source=ev.get("source", ""),
            payload={
                "title":            ev.get("title", ""),
                "summary":          ev.get("summary", ""),
                "url":              ev.get("url", ""),
                "platform":         ev.get("platform", ev.get("layer", "")),
                "has_coords":       bool(ev.get("has_coords", lat != 0 or lng != 0)),
                "confidence_score": ev.get("confidence_score", 0.35),
                "confidence_label": ev.get("confidence_label", "LOW"),
                "media_type":       ev.get("media_type", ""),
                "has_media":        bool(ev.get("has_media", False)),
                "media_url":        ev.get("media_url", ""),
            },
        )
    except Exception:
        return None


# ─── Citizen OSINT Adapter ────────────────────────────────────────────────────

class CitizenOSINTAdapter:
    """
    Wraps CitizenOSINTIngester with the glassbox_server ingester interface.
    Polls all citizen sources every 30 minutes, broadcasts to SSE subscribers,
    and writes the feed file so harvester_runner.py + news-manifest can use it.
    """

    layer = "citizen_osint"
    source = "YouTube / Bluesky / Reddit / Telegram / Nitter"
    # 2026-05-04: gated as a compound. Per-platform yaml rows pending.
    # Will refuse at the gate until each underlying source verified individually.
    source_id = "citizen_osint_aggregated"
    poll_interval_sec = 1800.0  # 30 min — respectful of API quotas

    def __init__(
        self,
        broadcaster: Optional[BroadcastFn] = None,
        logger: Optional[logging.Logger] = None,
        classifier: Optional[Any] = None,
    ) -> None:
        self._broadcaster = broadcaster
        # Loop integration (Step 3 fix, 2026-04-26): accept classifier kwarg.
        # If present, run classifier.apply(ev) before broadcasting so events
        # from this layer also get the 5-dim prediction-market classification.
        self._classifier = classifier
        self.log = logger or logging.getLogger("ingester.citizen_osint")
        self._running = False
        self.last_fetch_ts: Optional[str] = None
        self.last_fetch_count: int = 0
        # Parity with base.Ingester so /api/glassbox/diagnostic gets uniform fields.
        self.last_emit_ts: Optional[str] = None
        self.last_emit_count: int = 0
        self.last_error: Optional[str] = None
        self.last_cycle_ms: int = 0
        self._total_events: int = 0
        # Loop diagnostic counters (parity with base.Ingester)
        self.classifier_failures: int = 0
        self.last_classifier_error: Optional[str] = None

        # Lazy import to avoid circular import at module level
        from .citizen_osint import CitizenOSINTIngester
        self._ingester = CitizenOSINTIngester()

    def stop(self) -> None:
        self._running = False

    def status(self) -> Dict[str, Any]:
        return {
            "layer":           self.layer,
            "source":          self.source,
            "health":          "ok" if self.last_error is None else "degraded",
            "tracked_entities": self.last_fetch_count,
            "last_fetch_ts":   self.last_fetch_ts,
            "last_fetch_count": self.last_fetch_count,
            "last_emit_ts":    self.last_emit_ts,
            "last_emit_count": self.last_emit_count,
            "last_cycle_ms":   self.last_cycle_ms,
            "total_events":    self._total_events,
            "error":           self.last_error,
        }

    async def run_forever(self) -> None:
        self._running = True
        self.log.info("CitizenOSINT adapter starting — interval=%.0fs", self.poll_interval_sec)

        while self._running:
            t0 = time.time()
            try:
                run_result = await self._ingester.run()
                all_events = self._ingester.all_events(run_result)

                # Convert dicts → GlassboxEvent and broadcast (with classification if wired)
                broadcast_count = 0
                for ev_dict in all_events:
                    ev = _dict_to_event(ev_dict)
                    if not ev or not self._broadcaster:
                        continue
                    # Apply Loop classification (Step 3 fix, 2026-04-26).
                    # Defensive: classifier failure must NEVER block broadcast.
                    if self._classifier is not None:
                        try:
                            self._classifier.apply(ev)
                        except Exception as cexc:
                            self.classifier_failures += 1
                            self.last_classifier_error = f"{type(cexc).__name__}: {cexc}"
                            level = "warning" if self.classifier_failures == 1 else "debug"
                            getattr(self.log, level)(f"classifier failed for {getattr(ev, 'id', '?')}: {cexc}")
                    try:
                        self._broadcaster(ev)
                        broadcast_count += 1
                    except Exception as exc:
                        self.log.debug("broadcast error: %s", exc)

                # Write feed file (polled by news-manifest endpoint + harvester_runner)
                _write_citizen_feed(all_events)

                self.last_fetch_ts = _now_iso()
                self.last_fetch_count = len(all_events)
                if broadcast_count > 0:
                    self.last_emit_ts = self.last_fetch_ts
                    self.last_emit_count = broadcast_count
                self._total_events += len(all_events)
                self.last_error = None
                self.last_cycle_ms = int((time.time() - t0) * 1000)

                self.log.info(
                    "CitizenOSINT: %d events harvested, %d broadcast, %.1fs",
                    len(all_events), broadcast_count, time.time() - t0
                )

            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_cycle_ms = int((time.time() - t0) * 1000)
                self.log.warning("CitizenOSINT cycle failed: %s", self.last_error)

            await asyncio.sleep(self.poll_interval_sec)


# ─── Traffic Cams Adapter ─────────────────────────────────────────────────────

class TrafficCamsAdapter:
    """
    Wraps TrafficCamsIngester with the glassbox_server ingester interface.
    Polls all traffic/camera sources every 10 minutes and broadcasts to SSE.
    """

    layer = "traffic_cams"
    source = "DOT 511 Cameras / HERE Traffic / TomTom / Webcams"
    # HERE + TomTom are paid commercial APIs — deferred to v1.2 Pro.
    # DOT 511 is per-state public domain; needs per-state row before shipping.
    source_id = "traffic_cams_aggregated"
    poll_interval_sec = 600.0  # 10 min

    def __init__(
        self,
        broadcaster: Optional[BroadcastFn] = None,
        logger: Optional[logging.Logger] = None,
        classifier: Optional[Any] = None,
    ) -> None:
        self._broadcaster = broadcaster
        # Loop integration (Step 3 fix, 2026-04-26): accept classifier kwarg.
        self._classifier = classifier
        self.log = logger or logging.getLogger("ingester.traffic_cams")
        self._running = False
        self.last_fetch_ts: Optional[str] = None
        self.last_fetch_count: int = 0
        # Parity with base.Ingester for diagnostic uniformity.
        self.last_emit_ts: Optional[str] = None
        self.last_emit_count: int = 0
        self.last_error: Optional[str] = None
        self.last_cycle_ms: int = 0
        self._total_events: int = 0
        self.classifier_failures: int = 0
        self.last_classifier_error: Optional[str] = None

        from .traffic_cams import TrafficCamsIngester
        self._ingester = TrafficCamsIngester()

    def stop(self) -> None:
        self._running = False

    def status(self) -> Dict[str, Any]:
        return {
            "layer":           self.layer,
            "source":          self.source,
            "health":          "ok" if self.last_error is None else "degraded",
            "tracked_entities": self.last_fetch_count,
            "last_fetch_ts":   self.last_fetch_ts,
            "last_fetch_count": self.last_fetch_count,
            "last_emit_ts":    self.last_emit_ts,
            "last_emit_count": self.last_emit_count,
            "last_cycle_ms":   self.last_cycle_ms,
            "total_events":    self._total_events,
            "error":           self.last_error,
        }

    async def run_forever(self) -> None:
        self._running = True
        self.log.info("TrafficCams adapter starting — interval=%.0fs", self.poll_interval_sec)

        while self._running:
            t0 = time.time()
            try:
                run_result = await self._ingester.run()
                all_events = self._ingester.all_events(run_result)

                broadcast_count = 0
                for ev_dict in all_events:
                    ev = _dict_to_event(ev_dict)
                    if not ev or not self._broadcaster:
                        continue
                    if self._classifier is not None:
                        try:
                            self._classifier.apply(ev)
                        except Exception as cexc:
                            self.classifier_failures += 1
                            self.last_classifier_error = f"{type(cexc).__name__}: {cexc}"
                    try:
                        self._broadcaster(ev)
                        broadcast_count += 1
                    except Exception as exc:
                        self.log.debug("broadcast error: %s", exc)

                self.last_fetch_ts = _now_iso()
                self.last_fetch_count = len(all_events)
                if broadcast_count > 0:
                    self.last_emit_ts = self.last_fetch_ts
                    self.last_emit_count = broadcast_count
                self._total_events += len(all_events)
                self.last_error = None
                self.last_cycle_ms = int((time.time() - t0) * 1000)

                self.log.info(
                    "TrafficCams: %d events harvested, %d broadcast, %.1fs",
                    len(all_events), broadcast_count, time.time() - t0
                )

            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.last_cycle_ms = int((time.time() - t0) * 1000)
                self.log.warning("TrafficCams cycle failed: %s", self.last_error)

            await asyncio.sleep(self.poll_interval_sec)


# ─── Feed file writer ─────────────────────────────────────────────────────────

def _write_citizen_feed(events: List[Dict[str, Any]]) -> None:
    """Write the citizen OSINT feed file for news-manifest + harvester_runner."""
    try:
        geolocated = [e for e in events if e.get("has_coords")]
        payload = {
            "generated_at": _now_iso(),
            "source":        "citizen_osint_adapter",
            "count":         len(geolocated),
            "events":        geolocated[:200],
        }
        with open(CITIZEN_FEED_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
    except Exception as exc:
        logging.getLogger("ingester.citizen_osint").debug(
            "citizen feed write error (non-fatal): %s", exc
        )
