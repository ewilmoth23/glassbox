"""
NOAA / weather.gov ingester — free, no key, US-focused weather alerts.

Source: https://www.weather.gov/documentation/services-web-api
API:    https://api.weather.gov/alerts/active

Why this matters for predictions:
  - Outdoor sports totals (NFL/MLB/MLS): rain/wind moves O/U lines predictably
  - Storm prediction markets on Kalshi (hurricanes, freezes)
  - Snowfall events: ski/airline-disruption markets
  - Severe-weather alerts feed Sentinel briefs

What we pull
------------
1. /alerts/active                — every active US weather alert with bbox + severity
2. /points/{lat},{lng}/forecast  — 7-day forecast for a known venue (e.g. NFL stadium)
3. /stations/{id}/observations/latest — current obs for stadium-grade weather

This module ships the alerts pull (broad coverage). Per-stadium forecast
expansion is a follow-on once we have a venue list (NFL/MLB/MLS/EPL stadiums
mapped to lat/lng).

Auth: none. weather.gov is generous but asks for a User-Agent identifying
the application. They'll throttle anonymous abusers.

Author: 2026-04-27 — task #169
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import GlassboxEvent, Ingester


_NOAA_ALERTS_URL = "https://api.weather.gov/alerts/active"
_USER_AGENT = "MEWRGlassbox/2.0 (+https://fulcrumtechnologies.io; hello@fulcrumtechnologies.io)"


_SEVERITY_MAP = {
    "Extreme":  10,
    "Severe":   8,
    "Moderate": 5,
    "Minor":    3,
    "Unknown":  2,
}


class NOAAWeatherIngester(Ingester):
    """Pulls active US weather alerts (warnings, watches, advisories)."""

    layer = "weather_alerts"
    source = "weather.gov / NOAA NWS"
    poll_interval_sec = 5 * 60       # 5 min — alerts update in near-real-time

    async def fetch(self) -> List[Dict[str, Any]]:
        try:
            import aiohttp
        except ImportError:
            self.log.warning("[noaa] aiohttp missing")
            return []
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_NOAA_ALERTS_URL,
                                   headers={"User-Agent": _USER_AGENT,
                                            "Accept": "application/geo+json"}) as resp:
                if resp.status != 200:
                    self.log.info(f"[noaa] HTTP {resp.status}")
                    return []
                data = await resp.json(content_type=None)
                return data.get("features", []) or []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        for f in raw_items:
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            event_name = props.get("event") or "Weather Alert"
            severity_word = (props.get("severity") or "Unknown").strip()
            sev = _SEVERITY_MAP.get(severity_word, 2)
            external_id = props.get("id") or f.get("id") or hashlib.md5(
                (event_name + str(props.get("sent", ""))).encode()
            ).hexdigest()[:12]

            # Try to get a representative point — use polygon centroid as best-effort
            lat, lng = self._geom_centroid(geom)
            states = props.get("areaDesc") or ""

            ts = props.get("sent") or props.get("onset") or datetime.now(timezone.utc).isoformat()

            domain = "weather"
            severity_for_market = self._market_severity(event_name, severity_word)

            decay_min = self._decay_minutes(event_name)

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=str(external_id),
                kind="alert",
                lat=lat, lng=lng,
                ts=ts,
                severity=sev,
                source=self.source,
                payload={
                    "event_name": event_name,
                    "severity_word": severity_word,
                    "headline": props.get("headline", ""),
                    "description": (props.get("description") or "")[:600],
                    "areaDesc": states,
                    "expires": props.get("expires"),
                    "urgency": props.get("urgency"),
                    "certainty": props.get("certainty"),
                },
                domain=domain,
                geocode_quality="region",
                severity_for_market=severity_for_market,
                decay_half_life_min=decay_min,
                market_tags=self._market_tags(event_name, states),
            ))
        return out

    # ─── Helpers ────────────────────────────────────────────────────────

    def _geom_centroid(self, geom: Dict[str, Any]) -> tuple[float, float]:
        """Best-effort centroid from GeoJSON geometry. Returns (0, 0) on failure."""
        gtype = geom.get("type", "")
        coords = geom.get("coordinates")
        try:
            if gtype == "Point":
                return float(coords[1]), float(coords[0])
            if gtype == "Polygon":
                ring = coords[0]
                lats = [c[1] for c in ring]
                lngs = [c[0] for c in ring]
                return sum(lats) / len(lats), sum(lngs) / len(lngs)
            if gtype == "MultiPolygon":
                # take first polygon's first ring
                ring = coords[0][0]
                lats = [c[1] for c in ring]
                lngs = [c[0] for c in ring]
                return sum(lats) / len(lats), sum(lngs) / len(lngs)
        except Exception:
            pass
        return 0.0, 0.0

    def _market_severity(self, event_name: str, severity_word: str) -> int:
        """How much this should move related markets, 0-10."""
        e = event_name.lower()
        base = _SEVERITY_MAP.get(severity_word, 2)
        if "hurricane" in e or "tornado" in e:
            return min(10, base + 2)
        if "tropical storm" in e or "blizzard" in e or "ice storm" in e:
            return min(10, base + 1)
        if "winter weather" in e or "thunderstorm" in e:
            return base
        return max(0, base - 1)

    def _decay_minutes(self, event_name: str) -> int:
        e = event_name.lower()
        if "hurricane" in e or "tropical" in e:
            return 60 * 24       # 24h — slow-moving, sustained market impact
        if "winter storm" in e or "blizzard" in e:
            return 60 * 12
        if "tornado" in e or "flash flood" in e:
            return 60            # fast-moving
        return 180

    def _market_tags(self, event_name: str, area: str) -> List[str]:
        tags: List[str] = []
        e = event_name.lower()
        # Generic weather tag
        tags.append("weather:alert:" + e.replace(" ", "_"))
        # Hurricane / tropical storm: feed Kalshi tropical-storm markets
        if "hurricane" in e or "tropical" in e:
            tags.append("kalshi:hurricane")
        if "tornado" in e:
            tags.append("kalshi:tornado")
        # Crude state extraction from areaDesc — useful for state-level markets
        if area:
            for state in ("FL", "TX", "CA", "NY", "PA", "OH", "GA", "NC"):
                if f", {state}" in area or area.startswith(state + ","):
                    tags.append(f"weather:state:{state}")
                    break
        return tags
