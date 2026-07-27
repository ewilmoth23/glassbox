"""
NASA NEO ingester — Near-Earth Objects (asteroids on close-approach trajectories).

Source: https://api.nasa.gov/neo/rest/v1/feed?start_date=...&end_date=...&api_key=...
License: US public domain (NASA), commercial use OK
KEY required: NASA_API_KEY env var (NOT the FIRMS-specific MAP_KEY).

NEOs don't have a lat/lng on Earth — they're in space. We project the
close-approach point onto the Earth's surface at the closest moment for
the pin, with altitude_m representing the actual miss distance in meters.

The frontend renders this as a "watch" pin with a tooltip showing:
  - Asteroid name + size
  - Close-approach datetime
  - Miss distance (km, lunar distances)
  - Estimated diameter
  - Hazardous (Y/N per NASA's PHO classification)

We use a 7-day forward window. Polled every 6 hours since the catalog
updates daily and close approaches don't change minute-to-minute.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


def _severity_for_neo(diameter_m: Optional[float], miss_km: Optional[float], hazardous: bool) -> int:
    """Map NEO size + miss distance + PHO flag to internal 0-10 severity."""
    base = 2  # NEOs are interesting but rarely Earth-threatening
    if hazardous:
        base = 6
    if diameter_m is not None and diameter_m >= 140:    # PHO threshold
        base = max(base, 5)
    if diameter_m is not None and diameter_m >= 1000:   # km-class
        base = max(base, 8)
    if miss_km is not None and miss_km < 384400:        # closer than the Moon
        base = min(10, base + 2)
    return base


# ─── Ingester ─────────────────────────────────────────────────────────────


class NasaNeoIngester(Ingester):
    layer = "neo_asteroids"
    source = "NASA NEO Web Service (api.nasa.gov)"
    source_id = "nasa_neo"               # gates against infra/sources.yaml
    poll_interval_sec = 21600.0          # 6h

    URL = "https://api.nasa.gov/neo/rest/v1/feed"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"
    LOOKAHEAD_DAYS = 7

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._key = (
            os.environ.get("NASA_API_KEY")
            or "GaolpeVcVeJbW5kayTBN6uvtaUA8yByO9gfYVgRI"   # registered key, see INFRASTRUCTURE.md
        )
        if not self._key:
            self.log.warning("[nasa_neo] NASA_API_KEY missing — ingester will return [].")

    async def fetch(self) -> List[Dict[str, Any]]:
        if not self._key:
            return []
        today = datetime.now(timezone.utc).date()
        end = today + timedelta(days=self.LOOKAHEAD_DAYS)
        params = {
            "start_date": today.isoformat(),
            "end_date":   end.isoformat(),
            "api_key":    self._key,
        }
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL, params=params) as r:
                r.raise_for_status()
                data = await r.json()

        # Response shape: {near_earth_objects: {YYYY-MM-DD: [{...neo...}, ...]}}
        out: List[Dict[str, Any]] = []
        for date_str, neos in (data.get("near_earth_objects") or {}).items():
            for neo in neos:
                neo["_query_date"] = date_str
                out.append(neo)
        return out

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for n in raw_items:
            neo_id = n.get("id") or n.get("neo_reference_id")
            if not neo_id:
                continue

            name = n.get("name", "Unknown NEO")
            hazardous = bool(n.get("is_potentially_hazardous_asteroid"))

            # Diameter — average of min/max estimates (in meters)
            diameter_m: Optional[float] = None
            est_diam = n.get("estimated_diameter") or {}
            meters = est_diam.get("meters") or {}
            min_m = meters.get("estimated_diameter_min")
            max_m = meters.get("estimated_diameter_max")
            if min_m is not None and max_m is not None:
                diameter_m = (float(min_m) + float(max_m)) / 2.0

            # Take the FIRST close-approach in the window
            cad = (n.get("close_approach_data") or [])
            if not cad:
                continue
            ca = cad[0]

            # Miss distance in km
            miss_km: Optional[float] = None
            md = ca.get("miss_distance") or {}
            if md.get("kilometers"):
                try:
                    miss_km = float(md["kilometers"])
                except (TypeError, ValueError):
                    pass

            # NASA NEO doesn't give surface lat/lng; we use sentinel (0,0)
            # and let the frontend render this as a watch-list panel rather
            # than a pin. severity remains meaningful for filtering.
            severity = _severity_for_neo(diameter_m, miss_km, hazardous)

            mtags: List[str] = []
            sev_market = 0
            if hazardous and miss_km is not None and miss_km < 384400:
                mtags.append("space:close_pass")
                sev_market = 3   # newsworthy but rarely market-moving

            # NASA NEO emits close_approach_date_full as 'YYYY-MMM-DD HH:MM'
            # (e.g. '2026-May-13 19:49') — not ISO. Convert to ISO before
            # passing to the writer; fall back to now() on parse failure.
            ca_full = ca.get("close_approach_date_full")
            ts_iso = now
            if isinstance(ca_full, str) and ca_full.strip():
                try:
                    ts_iso = datetime.strptime(
                        ca_full.strip(), "%Y-%b-%d %H:%M"
                    ).replace(tzinfo=timezone.utc).isoformat()
                except (ValueError, TypeError):
                    # Some NEO entries already use ISO; let the writer try.
                    ts_iso = ca_full

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"neo:{neo_id}",
                kind="watch",                  # not a pin — frontend renders as panel
                lat=0.0,
                lng=0.0,
                ts=ts_iso,
                severity=severity,
                altitude_m=(miss_km * 1000.0) if miss_km is not None else None,
                source=self.source,
                payload={
                    "name":           name,
                    "hazardous":      hazardous,
                    "diameter_m_avg": diameter_m,
                    "miss_km":        miss_km,
                    "miss_lunar":     (miss_km / 384400.0) if miss_km is not None else None,
                    "rel_velocity_kmh": _safe_float((ca.get("relative_velocity") or {}).get("kilometers_per_hour")),
                    "orbiting_body":  ca.get("orbiting_body"),
                    "_attribution":   "Asteroid data: NASA NEO Web Service (JPL)",
                },
                domain="space",
                geocode_quality="not_geo",
                decay_half_life_min=4320,      # 3 days — close-approach windows are days-long
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
