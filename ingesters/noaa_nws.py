"""
NOAA NWS ingester — active US weather alerts (warnings/watches/advisories).

Replaces every browser-side Open-Meteo call for US weather. Open-Meteo is
CC-BY-NC 4.0 (non-commercial); NOAA NWS is US public domain (commercial OK).

Source: https://api.weather.gov/alerts/active
License: US public domain (verified 2026-05-04 at weather.gov/disclaimer)
Attribution required: NO (US gov public domain) — but UA + rate-limit respect MANDATORY.
Trademark: cannot present in a way implying NWS endorsement of MEWR/Glassbox.

Each alert has:
  - geometry (polygon/multipolygon when geocoded; we centroid for the pin)
  - severity: Extreme | Severe | Moderate | Minor | Unknown
  - urgency:  Immediate | Expected | Future | Past | Unknown
  - certainty: Observed | Likely | Possible | Unlikely | Unknown
  - event:   "Tornado Warning" | "Flood Watch" | "Winter Storm Advisory" | etc.

We map (severity, urgency) → 0-10 internal severity scale for the globe.

Rate limits: NWS has no published cap but expects polite usage. We poll
every 5 minutes (300s). The endpoint is cached at the CDN ~30s, so faster
polling adds load without freshness gain.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Severity mapping ─────────────────────────────────────────────────────

# NWS severity → 0-10 base (before urgency boost)
_SEVERITY_MAP = {
    "Extreme":  9,
    "Severe":   7,
    "Moderate": 5,
    "Minor":    3,
    "Unknown":  2,
}

# Urgency boost (+0 to +1)
_URGENCY_BOOST = {
    "Immediate": 1,
    "Expected":  0,
    "Future":   -1,
    "Past":     -2,
    "Unknown":   0,
}


def _severity_for_alert(props: Dict[str, Any]) -> int:
    sev = _SEVERITY_MAP.get(props.get("severity", "Unknown"), 2)
    boost = _URGENCY_BOOST.get(props.get("urgency", "Unknown"), 0)
    out = sev + boost
    if out < 0:
        return 0
    if out > 10:
        return 10
    return out


def _centroid(geometry: Optional[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    """Return (lat, lng) centroid for the alert polygon, or None.

    NWS returns GeoJSON Polygon or MultiPolygon. We compute a simple mean
    over all coordinate pairs (good enough for a pin; not for analysis).
    """
    if not geometry:
        return None
    geo_type = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return None

    pts: List[Tuple[float, float]] = []  # (lng, lat) — GeoJSON order

    def _walk(c: Any) -> None:
        # A coordinate pair is [lng, lat] (numbers). Anything else is a list to recurse.
        if (
            isinstance(c, (list, tuple))
            and len(c) >= 2
            and isinstance(c[0], (int, float))
            and isinstance(c[1], (int, float))
        ):
            pts.append((float(c[0]), float(c[1])))
            return
        if isinstance(c, (list, tuple)):
            for item in c:
                _walk(item)

    _walk(coords)
    if not pts:
        return None
    avg_lng = sum(p[0] for p in pts) / len(pts)
    avg_lat = sum(p[1] for p in pts) / len(pts)
    return (avg_lat, avg_lng)


# ─── Ingester ─────────────────────────────────────────────────────────────


class NoaaNwsIngester(Ingester):
    layer = "weather_alerts"
    source = "NOAA National Weather Service (api.weather.gov)"
    source_id = "noaa_nws"               # gates against infra/sources.yaml
    poll_interval_sec = 300.0            # 5 min — NWS CDN cache ~30s; faster adds no value

    URL = "https://api.weather.gov/alerts/active"

    # NWS REQUIRES a User-Agent that identifies your app + a contact email.
    # See https://www.weather.gov/documentation/services-web-api#/default/get_alerts
    # If we ever start getting 403s, this is the first thing to check.
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA, "Accept": "application/geo+json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL) as r:
                r.raise_for_status()
                data = await r.json()

        # GeoJSON FeatureCollection. Each feature has properties + geometry.
        features = data.get("features") or []
        # Only return alerts that have geometry (we need a pin location).
        return [f for f in features if f.get("geometry")]

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for f in raw_items:
            props = f.get("properties") or {}
            geom = f.get("geometry")
            cent = _centroid(geom)
            if not cent:
                continue
            lat, lng = cent

            ext_id = props.get("id") or f.get("id") or ""
            if not ext_id:
                continue

            severity = _severity_for_alert(props)
            event_kind = props.get("event") or "Weather Alert"

            # Market tags (Loop): hurricane / tornado / flood / winter storm
            # are all meaningful market signals on Kalshi.
            mtags: List[str] = []
            ev_lower = event_kind.lower()
            if "hurricane" in ev_lower or "tropical" in ev_lower:
                mtags.append("weather:hurricane")
            elif "tornado" in ev_lower:
                mtags.append("weather:tornado")
            elif "flood" in ev_lower:
                mtags.append("weather:flood")
            elif "winter" in ev_lower or "snow" in ev_lower or "ice" in ev_lower:
                mtags.append("weather:winter_storm")
            elif "wildfire" in ev_lower or "fire" in ev_lower:
                mtags.append("weather:wildfire")

            # severity_for_market: Extreme alerts move markets; Minor doesn't.
            sev_market = 0
            if severity >= 8:
                sev_market = 8
            elif severity >= 6:
                sev_market = 5

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=str(ext_id),
                kind="alert",
                lat=float(lat),
                lng=float(lng),
                ts=props.get("sent") or now,
                severity=severity,
                source=self.source,
                payload={
                    "event": event_kind,
                    "headline": props.get("headline"),
                    "severity_raw": props.get("severity"),
                    "urgency": props.get("urgency"),
                    "certainty": props.get("certainty"),
                    "area_desc": props.get("areaDesc"),
                    "sender_name": props.get("senderName"),
                    "effective": props.get("effective"),
                    "expires": props.get("expires"),
                    "instruction": props.get("instruction"),
                    "_attribution": "NOAA National Weather Service (US public domain)",
                },
                # Loop classification
                domain="geo",
                geocode_quality="polygon_centroid",   # not exact GPS
                decay_half_life_min=30,               # alerts can stand for hours
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
