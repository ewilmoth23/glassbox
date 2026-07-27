"""
NOAA NHC tropical cyclone ingester.

Source: https://www.nhc.noaa.gov/CurrentStorms.json
License: US Government public domain (NOAA NHC, 17 USC §105). Commercial-OK.
NO API key required.

Strategic context: hurricane forecasts go public via NHC advisories 3-5 days
before media coverage of "Hurricane X makes landfall". The CurrentStorms.json
firehose surfaces:
  - Active named storms (TS, HU)
  - Pre-named tropical depressions (TD)
  - Storm classification + intensity (wind speed in knots)
  - Position + movement vector + forecast track (cone of uncertainty)

Pre-season (Atlantic: June 1, East Pacific: May 15) the endpoint returns
{"activeStorms": []}. The ingester silently emits zero events. As soon as
NHC names a system, it appears here in real time.

Coverage: Atlantic + Eastern Pacific basins (NHC's mandate). Western
Pacific typhoons are tracked by JTWC — separate future ingester.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .base import GlassboxEvent, Ingester


# Lat/lng come as e.g. "21.1N" / "94.4W" — convert to signed float.
_COORD_RE = re.compile(r"^(?P<value>-?\d+(?:\.\d+)?)(?P<hemi>[NSEW])?$")


def _parse_coord(raw: Any) -> Optional[float]:
    """Convert NHC coord strings ('21.1N', '-94.4W', '21.1') to signed float.
    Returns None if input is missing/unparseable."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    m = _COORD_RE.match(s)
    if not m:
        try:
            return float(s)
        except (ValueError, TypeError):
            return None
    val = float(m.group("value"))
    hemi = m.group("hemi") or ""
    if hemi in ("S", "W"):
        val = -abs(val)
    elif hemi in ("N", "E"):
        val = abs(val)
    return val


# Saffir–Simpson + sub-hurricane severity mapping (based on max-wind kt).
# Used as fallback when the response provides intensity but no classification,
# or when classification is missing.
def _severity_from_wind_kt(wind_kt: Optional[float]) -> int:
    if wind_kt is None:
        return 5
    if wind_kt < 35:
        return 3   # tropical depression
    if wind_kt < 64:
        return 5   # tropical storm
    if wind_kt < 83:
        return 7   # cat 1 hurricane
    if wind_kt < 96:
        return 8   # cat 2
    if wind_kt < 113:
        return 9   # cat 3 major
    if wind_kt < 137:
        return 10  # cat 4 major
    return 10      # cat 5 — already maxed


# Classification → readable label mapping
_CLASSIFICATION_LABELS = {
    "TD":  "tropical_depression",
    "TS":  "tropical_storm",
    "HU":  "hurricane",
    "MH":  "major_hurricane",
    "STD": "subtropical_depression",
    "STS": "subtropical_storm",
    "PT":  "post_tropical",
    "DB":  "disturbance",
    "WV":  "tropical_wave",
    "EX":  "extratropical",
    "PTC": "potential_tropical_cyclone",
}


# ─── Ingester ─────────────────────────────────────────────────────────────


class NhcStormsIngester(Ingester):
    layer = "tropical_storms"
    source = "NOAA National Hurricane Center"
    source_id = "noaa_nhc"
    poll_interval_sec = 300.0   # 5 min — NHC advisories every 6h, intermediate at 3h

    URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL) as r:
                r.raise_for_status()
                data = await r.json()
        if not isinstance(data, dict):
            self.log.warning(
                f"[noaa_nhc] expected dict, got {type(data).__name__}"
            )
            return []
        storms = data.get("activeStorms") or []
        if not isinstance(storms, list):
            self.log.warning(
                f"[noaa_nhc] activeStorms not a list ({type(storms).__name__})"
            )
            return []
        return storms

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []

        for s in raw_items:
            storm_id = (s.get("id") or s.get("stormId") or "").strip()
            if not storm_id:
                continue

            lat = _parse_coord(s.get("latitude") or s.get("latitudeNumeric"))
            lng = _parse_coord(s.get("longitude") or s.get("longitudeNumeric"))
            if lat is None or lng is None:
                continue

            classification = (s.get("classification") or "").strip().upper()
            class_label = _CLASSIFICATION_LABELS.get(classification, classification.lower() or "unknown")

            # intensity = max sustained wind in knots; pressure in millibars
            try:
                wind_kt: Optional[float] = float(s.get("intensity")) if s.get("intensity") not in (None, "") else None
            except (TypeError, ValueError):
                wind_kt = None
            try:
                pressure_mb: Optional[float] = float(s.get("pressure")) if s.get("pressure") not in (None, "") else None
            except (TypeError, ValueError):
                pressure_mb = None

            severity = _severity_from_wind_kt(wind_kt)

            name = (s.get("name") or "").strip().title()
            display = name or storm_id

            ts = s.get("lastUpdate") or s.get("issuedTime") or datetime.now(timezone.utc).isoformat()

            try:
                movement_dir = float(s.get("movementDir")) if s.get("movementDir") not in (None, "") else None
            except (TypeError, ValueError):
                movement_dir = None
            try:
                movement_kt = float(s.get("movementSpeed")) if s.get("movementSpeed") not in (None, "") else None
            except (TypeError, ValueError):
                movement_kt = None

            payload: Dict[str, Any] = {
                "storm_id":        storm_id,
                "name":            name,
                "classification":  classification,
                "class_label":     class_label,
                "wind_kt":         wind_kt,
                "pressure_mb":     pressure_mb,
                "movement_dir":    movement_dir,
                "movement_kt":     movement_kt,
                "_attribution":    "NOAA National Hurricane Center",
            }
            # Pass-through forecast track if present (used by frontends to draw
            # the cone). We don't unpack — too schema-dependent.
            for key in ("forecastTrack", "forecastCone", "publicAdvisory"):
                if key in s:
                    payload[key] = s[key]

            title = (
                f"{class_label.replace('_', ' ').title()} {display}"
                + (f" — {int(wind_kt)} kt" if wind_kt is not None else "")
            )

            # Market tags: Kalshi/Polymarket carry hurricane-landfall markets.
            mtags = []
            if classification in ("HU", "MH") or (wind_kt and wind_kt >= 64):
                mtags.append("weather:hurricane")
            elif classification in ("TS", "STS") or (wind_kt and wind_kt >= 39):
                mtags.append("weather:tropical_storm")

            sev_market = 0
            if severity >= 9:
                sev_market = 9
            elif severity >= 7:
                sev_market = 6
            elif severity >= 5:
                sev_market = 3

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"nhc:{storm_id}",
                kind="tropical_storm",
                lat=float(lat),
                lng=float(lng),
                ts=ts,
                severity=severity,
                source=self.source,
                payload=payload,
                domain="atmospheric",
                geocode_quality="point",
                # Tropical cyclones decay slowly — 12h between advisories.
                # Use 720min (12h) as the freshness window so the proximity
                # scan keeps picking them up between advisories.
                decay_half_life_min=720,
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
