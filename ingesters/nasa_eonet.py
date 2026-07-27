"""
NASA EONET ingester — Earth Observatory Natural Event Tracker.

Source: https://eonet.gsfc.nasa.gov/api/v3/events?status=open
License: US public domain (NASA — verified 2026-05-04)
Attribution: optional but polite. We render "NASA EONET" in the UI footer.
NO API KEY required (different from api.nasa.gov which needs a key).

EONET aggregates active natural events from authoritative agencies:
  - Wildfires (InciWeb, MODIS)
  - Severe storms (NHC, JTWC)
  - Volcanoes (Smithsonian GVP)
  - Sea + lake ice (NSIDC)
  - Floods (Dartmouth Flood Observatory)
  - Drought, dust + haze, manmade events

Each event has:
  - id, title, description
  - categories[] (one of ~12 EONET category codes)
  - geometry[]: list of timestamped points (or polygons for storms)
  - sources[] (the upstream feed this came from)

We use the LAST (most recent) geometry point for the pin.

Event closure:
  - status=open returns active events only
  - Once an event closes (e.g. wildfire contained), it leaves the open set
  - Our dedup naturally handles this — when EONET drops it, we stop emitting

Rate limit: no published cap; reasonable polling. We use 30 min (1800s).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .base import GlassboxEvent, Ingester


# EONET category code → severity boost
# (events from a CATEGORY get a base severity; severe storms higher than dust)
_CATEGORY_SEVERITY = {
    "wildfires":      7,
    "severeStorms":   8,
    "volcanoes":      8,
    "earthquakes":    7,    # EONET also re-broadcasts USGS quakes
    "floods":         7,
    "drought":        4,
    "dustHaze":       3,
    "landslides":     6,
    "manmade":        5,
    "seaLakeIce":     4,
    "snow":           4,
    "tempExtremes":   5,
    "waterColor":     3,
}


# Category → market tag (Loop integration)
_CATEGORY_MARKET = {
    "wildfires":    "weather:wildfire",
    "severeStorms": "weather:storm",
    "volcanoes":    "geology:eruption",
    "floods":       "weather:flood",
    "drought":      "weather:drought",
}


def _last_geometry_point(event: Dict[str, Any]) -> Optional[Tuple[float, float, str]]:
    """Return (lat, lng, ts) for the most recent geometry, or None."""
    geoms = event.get("geometry") or []
    if not geoms:
        return None
    last = geoms[-1]
    coords = last.get("coordinates")
    ts = last.get("date")
    if not coords:
        return None
    # EONET point is [lng, lat]; polygon is [[ [lng,lat], ... ]] etc.
    # For polygon, take first vertex.
    if isinstance(coords[0], (int, float)):
        lng, lat = coords[0], coords[1]
    elif isinstance(coords[0], list):
        # nested
        first = coords[0]
        while isinstance(first[0], list):
            first = first[0]
        lng, lat = first[0], first[1]
    else:
        return None
    return (float(lat), float(lng), ts or "")


# ─── Ingester ─────────────────────────────────────────────────────────────


class NasaEonetIngester(Ingester):
    layer = "natural_events"
    source = "NASA EONET (Earth Observatory Natural Event Tracker)"
    source_id = "nasa_eonet"             # gates against infra/sources.yaml
    poll_interval_sec = 1800.0           # 30 min — natural events change slowly

    URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=30)
        # 2026-05-05 fix: EONET ignores the format=json param when called via
        # aiohttp (content negotiation returns RSS regardless of Accept header
        # OR format param). Workaround: tell aiohttp to ignore the response
        # content-type header via r.json(content_type=None) — that lets us
        # parse the body as JSON regardless of what EONET's server claims.
        # In practice the body IS valid JSON — the mimetype label is just wrong.
        headers = {
            "User-Agent": self.UA,
            "Accept": "application/json, */*;q=0.9",
        }
        params = {
            "status": "open",
            "days":   "30",
            "limit":  "200",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL, params=params) as r:
                r.raise_for_status()
                # content_type=None bypasses aiohttp's content-type sanity check
                data = await r.json(content_type=None)
        return data.get("events") or []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for ev in raw_items:
            ext_id = ev.get("id") or ""
            if not ext_id:
                continue

            point = _last_geometry_point(ev)
            if point is None:
                continue
            lat, lng, ts = point

            # Pick the highest-severity matching category. EONET events can
            # belong to multiple categories.
            cats = ev.get("categories") or []
            cat_codes = [c.get("id") for c in cats if c.get("id")]
            severity = max(
                (_CATEGORY_SEVERITY.get(c, 4) for c in cat_codes),
                default=4,
            )

            # Loop market tags
            mtags: List[str] = []
            for c in cat_codes:
                tag = _CATEGORY_MARKET.get(c)
                if tag and tag not in mtags:
                    mtags.append(tag)

            # severity_for_market: large-magnitude events move markets
            sev_market = 0
            if severity >= 8:
                sev_market = 7
            elif severity >= 7:
                sev_market = 5

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=str(ext_id),
                kind="event",
                lat=lat,
                lng=lng,
                ts=ts or now,
                severity=severity,
                source=self.source,
                payload={
                    "title":       ev.get("title"),
                    "description": ev.get("description"),
                    "categories":  cat_codes,
                    "sources":     [s.get("id") for s in (ev.get("sources") or [])],
                    "link":        ev.get("link"),
                    "_attribution": "NASA EONET",
                },
                domain="geo",
                geocode_quality="exact",     # EONET geocodes from authoritative sources
                decay_half_life_min=720,     # 12h — natural events persist for hours-days
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
