"""
AISStream.io ingester — global vessel-position firehose via WebSocket.

Source:  wss://stream.aisstream.io/v0/stream
License: free_with_attribution per sources.yaml. commercial_use_ok=true.
Auth:    AISSTREAM_API_KEY env var (registered free at https://aisstream.io)
Attribution: required to display 'Vessel positions: aisstream.io' in UI.

Why this exists alongside ships.py:
  ships.py polls Digitraffic + BarentsWatch + DMA — a Baltic / N. European
  bias because those are the only public free AIS feeds we'd cleared
  legally (2026-05-04 audit). The audit-flagged-and-cleared global
  free firehose is AISStream — but it requires a key. With the key,
  this ingester gives us global coverage and the 84 non-Baltic ports
  in algorithms/port_call.py PORTS list start firing.

  Both ingesters write to the same `vessel` entity rows via
  write_vessel_events (keyed by MMSI). Where they overlap (Baltic),
  the most recent broadcast wins via the upsert. Where AISStream covers
  a region the others don't (everywhere else), this is the only source.

Protocol summary (AISStream docs):
  1. WebSocket connect, no auth header — auth happens via the first
     message we send.
  2. Send a JSON subscription message:
       {"APIKey": "...", "BoundingBoxes": [[[lat1,lng1],[lat2,lng2]]],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
  3. Server then streams JSON messages of various types. Each message
     has MetaData (MMSI, ShipName, time_utc) + Message dict keyed by
     type ('PositionReport' / 'ShipStaticData' / etc.).

Filter strategy (v1):
  - Subscribe to PositionReport ONLY for v1. ShipStaticData is useful
    for IMO + name backfill but adds noise; defer to v1.1.
  - World bounding box [[-90,-180],[90,180]].
  - No MMSI filter — accept all.

Reconnect strategy:
  - WebSocket disconnect → wait 5 s → reconnect (capped at 60 s exp).
  - On unexpected message → log + skip (never crash the loop).

Output shape:
  layer='ships', external_id=str(mmsi), lat, lng, ts (ingester clock),
  velocity_ms (from Sog in knots × 0.514444), heading_deg from
  TrueHeading. payload preserves mmsi + name + ship_type. Same shape
  as ships.py so write_vessel_events handles both transparently.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import websockets
except ImportError:
    websockets = None    # ingester logs + skips when missing

from .base import GlassboxEvent, Ingester


WS_URL = "wss://stream.aisstream.io/v0/stream"
UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"
# AISStream is incompatible with aiohttp's WebSocket client (sends no
# data after subscribe; works fine with the `websockets` library that
# AISStream's own example uses). Don't switch back to aiohttp here.

# How long to listen per cycle before returning. Long-poll model: the
# base class re-runs cycle() after this returns; a return on disconnect
# triggers reconnect on the next tick.
LISTEN_SECONDS = 270

# Rate-limit our own emit pace so a flood doesn't overwhelm the
# downstream broadcaster. AISStream advertises ~300 msg/sec global; we
# accept all, then the writer handles the throughput.
MAX_BUFFER = 50_000


class AISStreamIngester(Ingester):
    """WebSocket-based AIS position firehose. Overrides the base poll
    model — fetch() runs the WS loop for LISTEN_SECONDS then returns
    everything buffered."""

    layer = "ships"          # IMPORTANT: same layer as ships.py so the
                             # writer treats them uniformly
    source = "AISStream.io (global firehose)"
    source_id = "aisstream"  # gates against infra/sources.yaml
    poll_interval_sec = 30.0
    # Websocket-style cycle: fetch() runs ~LISTEN_SECONDS (5 min) per
    # call. The default SLA formula (3× poll) would breach at 90s and
    # mark this perpetually 'degraded'. Override to 600s (~2× the
    # actual batch window with grace) so the SLA monitor reports an
    # honest signal instead of perpetual false-positive.
    sla_breach_threshold_sec = 600.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._buffered: List[Dict[str, Any]] = []

    async def fetch(self) -> List[Dict[str, Any]]:
        """Connect to AISStream, listen for LISTEN_SECONDS, return all
        buffered position reports."""
        if websockets is None:
            self.log.warning(
                "[aisstream] websockets lib not installed; cannot connect"
            )
            return []
        api_key = os.environ.get("AISSTREAM_API_KEY", "").strip()
        if not api_key:
            # No key → ingester is effectively disabled. Log once-per-cycle
            # at INFO so an operator can see why nothing is flowing.
            self.log.info(
                "[aisstream] AISSTREAM_API_KEY not set; skipping cycle"
            )
            return []

        self._buffered = []

        try:
            async with websockets.connect(
                WS_URL,
                user_agent_header=UA,
                ping_interval=30,
                ping_timeout=20,
            ) as ws:
                # Subscription message — sent immediately after connect.
                # 2026-05-09: subscribe to ShipStaticData too so we get
                # IMO + flag info for proper sanctions matching (the
                # ATLAS false positive was caused by PositionReport
                # not carrying IMO, forcing name-only matching). The
                # static-data messages are low-volume (vessels broadcast
                # them every 6 minutes per AIS spec) so the throughput
                # impact is negligible.
                sub = {
                    "APIKey": api_key,
                    "BoundingBoxes": [[[-90.0, -180.0], [90.0, 180.0]]],
                    "FiltersShipMMSI": [],
                    "FilterMessageTypes": [
                        "PositionReport",
                        "ShipStaticData",
                    ],
                }
                await ws.send(json.dumps(sub))
                self.log.info(
                    f"[aisstream] connected; listening {LISTEN_SECONDS}s "
                    f"(global bbox; PositionReport + ShipStaticData)"
                )
                end_time = asyncio.get_event_loop().time() + LISTEN_SECONDS

                while asyncio.get_event_loop().time() < end_time:
                    if len(self._buffered) >= MAX_BUFFER:
                        # Cap the per-cycle backlog. The next cycle
                        # picks up where this one left off.
                        break
                    try:
                        remaining = end_time - asyncio.get_event_loop().time()
                        if remaining <= 0:
                            break
                        msg = await asyncio.wait_for(
                            ws.recv(), timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        break
                    # websockets gives str OR bytes per frame opcode
                    if isinstance(msg, (bytes, bytearray)):
                        try:
                            msg = msg.decode("utf-8", errors="replace")
                        except Exception:  # noqa: BLE001
                            continue
                    self._handle_message(msg)
        except Exception as e:  # noqa: BLE001
            self.log.info(f"[aisstream] connection error: {e}")

        if self._buffered:
            self.log.info(
                f"[aisstream] cycle buffered {len(self._buffered)} positions"
            )
        return list(self._buffered)

    def _handle_message(self, raw: str) -> None:
        """Parse one AISStream message — PositionReport gives lat/lng/
        velocity, ShipStaticData gives IMO + dimension info. We buffer
        each kind into its own row shape; normalize() emits one
        GlassboxEvent per buffered row.
        """
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            return
        msg_type = evt.get("MessageType") or ""
        if msg_type == "PositionReport":
            self._handle_position(evt)
        elif msg_type == "ShipStaticData":
            self._handle_static(evt)

    def _handle_position(self, evt: Dict[str, Any]) -> None:
        meta = evt.get("MetaData") or {}
        body = (evt.get("Message") or {}).get("PositionReport") or {}
        mmsi = meta.get("MMSI") or body.get("UserID")
        if not mmsi:
            return
        lat = meta.get("latitude") or body.get("Latitude")
        lng = meta.get("longitude") or body.get("Longitude")
        if lat is None or lng is None:
            return
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            return
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
            return

        sog = body.get("Sog")
        velocity_ms: Optional[float] = None
        if isinstance(sog, (int, float)):
            try:
                velocity_ms = float(sog) * 0.514444
            except (TypeError, ValueError):
                pass

        heading = body.get("TrueHeading")
        if isinstance(heading, (int, float)) and heading != 511:
            try:
                heading_deg: Optional[float] = float(heading)
            except (TypeError, ValueError):
                heading_deg = None
        else:
            heading_deg = None

        self._buffered.append({
            "_kind":        "position",
            "mmsi":         int(mmsi),
            "name":         meta.get("ShipName") or None,
            "lat":          lat_f,
            "lng":          lng_f,
            "velocity_ms":  velocity_ms,
            "heading_deg":  heading_deg,
            "ts":           meta.get("time_utc"),
        })

    def _handle_static(self, evt: Dict[str, Any]) -> None:
        """Parse a ShipStaticData message. Provides IMO + ship type +
        callsign + destination — the metadata that's missing from
        PositionReport. Without lat/lng we can't write a fresh
        position event, but we CAN use the static data to enrich the
        entity row (the writer's UPSERT merges payload fields).

        Buffer it as a 'static' row; normalize() emits a 'state'-kind
        GlassboxEvent at the entity's last known position so the writer
        sees + merges the metadata onto the existing entity row."""
        meta = evt.get("MetaData") or {}
        body = (evt.get("Message") or {}).get("ShipStaticData") or {}
        mmsi = meta.get("MMSI") or body.get("UserID")
        if not mmsi:
            return
        # Only the last-broadcast lat/lng (from MetaData) is usable here;
        # ShipStaticData itself doesn't carry coords. Skip if the
        # MetaData is missing position too.
        lat = meta.get("latitude")
        lng = meta.get("longitude")
        if lat is None or lng is None:
            return
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            return
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
            return

        imo = body.get("ImoNumber") or body.get("Imo")
        # Validate IMO: AIS sometimes carries 0 or junk. Real IMO is
        # 7 digits.
        try:
            imo_int = int(imo) if imo else None
            if imo_int and (imo_int < 1_000_000 or imo_int > 9_999_999):
                imo_int = None
        except (TypeError, ValueError):
            imo_int = None

        self._buffered.append({
            "_kind":       "static",
            "mmsi":        int(mmsi),
            "name":        body.get("Name") or meta.get("ShipName") or None,
            "imo":         imo_int,
            "callsign":    body.get("CallSign") or None,
            "ship_type":   body.get("Type") or None,
            "destination": body.get("Destination") or None,
            "lat":         lat_f,
            "lng":         lng_f,
            "ts":          meta.get("time_utc"),
        })

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        """Convert buffered AISStream rows to GlassboxEvents shaped to
        match ships.py output so write_vessel_events handles both. Each
        row is either a 'position' (from PositionReport) or 'static'
        (from ShipStaticData); both produce a GlassboxEvent with the
        same layer + external_id so the writer's UPSERT merges payload
        fields onto the same vessel entity row."""
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []
        for r in raw_items:
            mmsi = r.get("mmsi")
            if not mmsi:
                continue
            kind = r.get("_kind", "position")
            payload: Dict[str, Any] = {
                "mmsi":         mmsi,
                "name":         r.get("name"),
                "_attribution": "Vessel positions: aisstream.io",
            }
            # ShipStaticData enriches the same entity row with IMO +
            # callsign + ship_type + destination. The writer's payload
            # whitelist preserves these, and they unlock IMO-exact
            # sanctions matching (the fix for the ATLAS false positive).
            if kind == "static":
                if r.get("imo") is not None:
                    payload["imo"] = r["imo"]
                if r.get("callsign"):
                    payload["callsign"] = r["callsign"]
                if r.get("ship_type") is not None:
                    payload["ship_type"] = r["ship_type"]
                if r.get("destination"):
                    payload["destination"] = r["destination"]
            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=str(mmsi),
                kind="position" if kind == "position" else "state",
                lat=float(r["lat"]),
                lng=float(r["lng"]),
                ts=now,        # use ingester clock; AISStream's time_utc
                               # is non-ISO and broadcaster-supplied
                severity=1,
                heading_deg=r.get("heading_deg"),
                velocity_ms=r.get("velocity_ms"),
                source=self.source,
                payload=payload,
                domain="maritime",
                geocode_quality="exact",
                decay_half_life_min=60,
            ))
        return out
