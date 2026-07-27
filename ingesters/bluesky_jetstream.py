"""
Bluesky Jetstream ingester — public ATProto firehose subscriber.

Source:  wss://jetstream2.us-east.bsky.network/subscribe
License: Bluesky operates Jetstream as a public, no-auth-required JSON firehose.
         Posts on Bluesky are licensed by their authors; ATProto's data model
         supports attribution back to the original DID.
Attribution: required to display the author DID/handle on any post we surface.
NO KEY required.

Jetstream is the JSON-formatted version of the ATProto firehose, easier to
consume than the raw repo firehose. Each event is one of:
  - app.bsky.feed.post   (a new post)
  - app.bsky.feed.like   (a like — usually filtered out)
  - app.bsky.feed.repost (a repost)
  - app.bsky.graph.follow (a follow — filtered)

For Glassbox v1.0 we ONLY emit:
  1. Posts containing OSINT-relevant keywords (war/protest/explosion/wildfire/etc.)
  2. Posts with explicit geocoordinates in their facets

Bluesky firehose is HIGH VOLUME (~100s of events/sec at peak). The ingester
samples + filters aggressively to avoid drowning the broadcaster. We emit
~10-50 events/min after filtering.

Reconnect strategy:
  - WebSocket disconnect → wait 5s → reconnect (capped at 60s exponential)
  - On unexpected message format → log + skip (never crash)

Service URL alternatives (Bluesky operates multiple regional jetstreams):
  - wss://jetstream1.us-east.bsky.network/subscribe (primary)
  - wss://jetstream2.us-east.bsky.network/subscribe (failover)
  - wss://jetstream2.us-west.bsky.network/subscribe (west coast)

Reference: https://github.com/bluesky-social/jetstream
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:
    aiohttp = None  # ingester will be a no-op if aiohttp unavailable

from .base import GlassboxEvent, Ingester


# ─── OSINT keyword filter ─────────────────────────────────────────────────

# Posts containing any of these terms (case-insensitive, word-boundary) get
# emitted as events. Tuned for newsworthy/global-event signal.
_OSINT_KEYWORDS = (
    "war", "explosion", "shooting", "fire", "wildfire", "earthquake",
    "tornado", "hurricane", "flooding", "evacuation", "missile",
    "protest", "riot", "outage", "blackout",
    "breaking", "alert",
)
_KEYWORD_RX = re.compile(
    r"\b(" + "|".join(_OSINT_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


# ─── Severity (post + match strength) ─────────────────────────────────────

_HIGH_SEVERITY_KEYWORDS = {"explosion", "shooting", "missile", "evacuation", "tornado", "hurricane"}


def _severity_for_post(text: str) -> int:
    matches = _KEYWORD_RX.findall(text or "")
    if not matches:
        return 1
    base = 4
    for m in matches:
        if m.lower() in _HIGH_SEVERITY_KEYWORDS:
            base = 6
            break
    return base


# ─── Ingester ─────────────────────────────────────────────────────────────


class BlueskyJetstreamIngester(Ingester):
    """WebSocket-based ingester. Overrides the base class's polling model
    because Jetstream is a continuous stream — fetch() is called once and
    runs forever within the cycle.

    The base class's run_forever() will respawn this if it ever throws."""

    layer = "social_bluesky"
    source = "Bluesky Jetstream (ATProto public firehose)"
    source_id = "bluesky_jetstream"     # gates against infra/sources.yaml
    # Long poll interval — fetch runs the WebSocket loop "forever" within
    # one cycle. If it returns (disconnect), we reconnect on next cycle.
    poll_interval_sec = 30.0
    # Websocket-style cycle: fetch() runs ~LISTEN_SECONDS (5 min) per
    # call. The default SLA formula (3× poll) would breach at 90s and
    # mark this perpetually 'degraded'. Override to 600s (~2× the
    # actual batch window with grace) so the SLA monitor reports an
    # honest signal instead of perpetual false-positive.
    sla_breach_threshold_sec = 600.0

    URL = "wss://jetstream2.us-east.bsky.network/subscribe"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    # Filter: only the post collection (skip likes/reposts/follows for v1.0)
    WANTED_COLLECTIONS = ("app.bsky.feed.post",)

    # How long to listen per cycle before returning (lets the base class
    # check kill switches + restart cleanly). 5 min is a good balance.
    LISTEN_SECONDS = 300

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._buffered_events: List[Dict[str, Any]] = []

    async def fetch(self) -> List[Dict[str, Any]]:
        """Connect to Jetstream, listen for LISTEN_SECONDS, return all
        filtered post events from that window."""
        if aiohttp is None:
            self.log.warning("[bluesky_jetstream] aiohttp not installed; cannot connect")
            return []

        self._buffered_events = []
        timeout = aiohttp.ClientTimeout(total=self.LISTEN_SECONDS + 30)
        headers = {"User-Agent": self.UA}

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
                # Subscribe with collection filter to reduce traffic
                params = "&".join(f"wantedCollections={c}" for c in self.WANTED_COLLECTIONS)
                ws_url = self.URL + "?" + params

                async with s.ws_connect(ws_url, heartbeat=30) as ws:
                    self.log.info(f"[bluesky_jetstream] connected; listening {self.LISTEN_SECONDS}s")
                    end_time = asyncio.get_event_loop().time() + self.LISTEN_SECONDS

                    async for msg in ws:
                        if asyncio.get_event_loop().time() >= end_time:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_message(msg.data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            self.log.info(f"[bluesky_jetstream] WS error: {ws.exception()}")
                            break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                            break

        except asyncio.TimeoutError:
            self.log.info("[bluesky_jetstream] listen window timed out (normal)")
        except Exception as e:
            self.log.info(f"[bluesky_jetstream] connection error: {e}")

        return list(self._buffered_events)

    def _handle_message(self, raw: str) -> None:
        """Parse one Jetstream event, filter, buffer if interesting."""
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Jetstream event shape:
        # { "did": "did:plc:...", "time_us": 1234567890,
        #   "kind": "commit", "commit": {...} }
        if evt.get("kind") != "commit":
            return

        commit = evt.get("commit") or {}
        if commit.get("collection") not in self.WANTED_COLLECTIONS:
            return
        if commit.get("operation") != "create":
            return

        record = commit.get("record") or {}
        text = record.get("text") or ""

        # Filter: must contain an OSINT keyword
        if not _KEYWORD_RX.search(text):
            return

        # Try to extract geo from facets (many posts have none)
        lat, lng = self._extract_geo_from_facets(record.get("facets") or [])
        if lat is None or lng is None:
            # No location → skip for v1.0 (we are a globe)
            return

        self._buffered_events.append({
            "did":      evt.get("did", ""),
            "time_us":  evt.get("time_us", 0),
            "rkey":     commit.get("rkey", ""),
            "text":     text,
            "lat":      lat,
            "lng":      lng,
            "lang":     (record.get("langs") or ["und"])[0] if record.get("langs") else "und",
        })

    def _extract_geo_from_facets(self, facets: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
        """Best-effort: scan facet features for a location reference.

        Bluesky doesn't have a standard geo facet yet (as of 2026). Some
        early-adopter clients use community.lexicon.location.geo with
        $type and lat/lng fields. We look for those.
        """
        for facet in facets:
            for feature in (facet.get("features") or []):
                ft = feature.get("$type", "") or feature.get("type", "")
                if "geo" in ft.lower():
                    lat = feature.get("latitude") or feature.get("lat")
                    lng = feature.get("longitude") or feature.get("lng") or feature.get("lon")
                    try:
                        if lat is not None and lng is not None:
                            return float(lat), float(lng)
                    except (TypeError, ValueError):
                        continue
        return None, None

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for r in raw_items:
            ext_id = f"bsky:{r.get('did')}:{r.get('rkey')}"
            text = r.get("text") or ""
            severity = _severity_for_post(text)

            mtags: List[str] = []
            sev_market = 0
            if severity >= 6:
                mtags.append("social:breaking_event")
                sev_market = 3

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=ext_id,
                kind="event",
                lat=float(r["lat"]),
                lng=float(r["lng"]),
                ts=now,
                severity=severity,
                source=self.source,
                payload={
                    "did":  r.get("did"),
                    "rkey": r.get("rkey"),
                    "text": text[:500],          # cap to avoid dumping novellas
                    "lang": r.get("lang"),
                    "_attribution": "Post via Bluesky / ATProto firehose",
                },
                domain="social",
                geocode_quality="user_provided",  # facet-based geo is user-asserted, not GPS-grade
                decay_half_life_min=120,         # social signals stale fast
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
