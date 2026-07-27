"""
USGS Volcano Hazards Program ingester.

Source: https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes
License: US Government public domain (USGS, 17 USC §105). Commercial-OK.
NO API key required.

Strategic context: USGS VHP issues alert-level changes for the 68
volcanoes the US monitors (Cascades, Alaska/Aleutians, Yellowstone,
Hawaii, CNMI). Alert-level transitions (NORMAL → ADVISORY → WATCH →
WARNING; aviation color codes GREEN → YELLOW → ORANGE → RED) precede
mainstream eruption coverage by 4–24h.

What we ingest: only currently-elevated volcanoes (anything not
NORMAL/GREEN). The endpoint already filters to that set, so the row
count is small (typically 5–25 globally at any given time).

Why we don't ship the full monitored list: 60+ at NORMAL is noise;
the elevated subset is the actionable signal.

Coordinates: USGS VHP doesn't expose coordinates in any public API.
We hardcode the 30+ most-likely-elevated volcanoes (Aleutians,
Cascades, Yellowstone, Hawaii) — verified from each volcano's USGS
landing page. Volcanoes outside this list emit at sentinel coords
(0, 0) and the dashboard filters those off the map; brief + alerts
still surface the name + level.

Refresh cadence: USGS issues alerts intra-day during episodes;
we poll every 15 min — small payload, low load on USGS.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Coordinates lookup for high-traffic monitored volcanoes ──────────────
# Sourced from each volcano's USGS landing page. Format: vnum → (lat, lng).
# Volcanoes not in this map emit at sentinel (0, 0). Adding more is just a
# matter of looking up the USGS page; structure is stable.
_VOLCANO_COORDS: Dict[str, Tuple[float, float]] = {
    # Alaska / Aleutians (most-active US volcanoes — ~25/year of elevations)
    "311120": (52.0763, -176.1297),  # Great Sitkin
    "311360": (54.7554, -163.9711),  # Shishaldin
    "311100": (52.4222, -174.1542),  # Kanaga
    "311240": (52.8222, -169.9444),  # Bogoslof
    "311080": (53.4310, -168.1310),  # Akun
    "311170": (51.9300, -177.1700),  # Cleveland
    "311180": (52.3169, -174.7658),  # Korovin
    "311300": (54.5180, -164.6491),  # Pavlof
    "311310": (54.5500, -164.4400),  # Pavlof Sister
    "311320": (54.1340, -165.9860),  # Akutan
    "311340": (54.7510, -163.9700),  # Westdahl
    "311380": (54.0560, -166.4690),  # Fisher
    "311230": (52.0760, -176.1300),  # Tanaga
    "311260": (52.3300, -171.2500),  # Carlisle
    "311280": (52.5780, -174.5240),  # Atka
    "311140": (52.1810, -175.5080),  # Adagdak
    "311220": (51.9400, -177.5400),  # Gareloi
    "311250": (52.4170, -174.1300),  # Kasatochi
    "312030": (60.4853, -152.7438),  # Redoubt
    "312040": (61.2989, -152.2539),  # Spurr
    "312100": (60.0214, -152.1357),  # Iliamna
    "312170": (58.3678, -155.4036),  # Augustine
    "312180": (58.1950, -155.2530),  # Katmai
    "312190": (58.2640, -155.1650),  # Trident

    # Cascades (Pacific NW — Mount St Helens etc.)
    "321010": (40.4920, -121.5080),  # Lassen Peak
    "321020": (41.4090, -122.1940),  # Shasta
    "322010": (44.4607, -121.7710),  # South Sister (Three Sisters)
    "322060": (45.3780, -121.6960),  # Mount Hood
    "322070": (46.2009, -122.1880),  # Mount St Helens
    "322090": (46.8528, -121.7603),  # Mount Rainier
    "322100": (48.7762, -121.8131),  # Mount Baker
    "322110": (48.1130, -121.1130),  # Glacier Peak
    "322120": (44.6717, -121.7976),  # Newberry
    "322130": (43.7220, -121.2290),  # Crater Lake / Mt Mazama

    # Hawaii
    "332010": (19.4210, -155.2870),  # Kilauea
    "332020": (19.4750, -155.6080),  # Mauna Loa
    "332030": (19.8200, -155.4600),  # Hualalai
    "332040": (20.7080, -156.2530),  # Haleakala

    # Yellowstone + intermountain west
    "325010": (44.4297, -110.5885),  # Yellowstone
    "323020": (43.4270, -120.7530),  # Diamond Craters
    "327010": (43.0700, -113.5500),  # Craters of the Moon

    # Marianas (CNMI — administered by HVO)
    "284141": (20.4500, 144.8000),   # Anatahan
    "284130": (16.8800, 145.6700),   # Pagan
    "284120": (15.0140, 145.6440),   # Asuncion
}


def _coords_for_vnum(vnum: Optional[str]) -> Tuple[float, float]:
    """Return (lat, lng) for a vnum if known; otherwise sentinel (0, 0)."""
    if not vnum:
        return (0.0, 0.0)
    return _VOLCANO_COORDS.get(str(vnum), (0.0, 0.0))


# Alert-level → severity ladder. NORMAL never appears in the elevated feed
# (filtered server-side) but we keep it for defensive completeness.
_ALERT_SEVERITY = {
    "NORMAL":   0,
    "ADVISORY": 5,
    "WATCH":    7,
    "WARNING":  10,
}

# Color-code → numeric severity boost for aviation alerts.
_COLOR_BOOST = {
    "GREEN":  0,
    "YELLOW": 1,
    "ORANGE": 2,
    "RED":    3,
}


def _severity(alert_level: Optional[str], color_code: Optional[str]) -> int:
    """Compose severity from alert_level + color_code. Returns 1..10."""
    base = _ALERT_SEVERITY.get((alert_level or "").upper(), 5)
    boost = _COLOR_BOOST.get((color_code or "").upper(), 0)
    return max(1, min(10, base + boost))


# ─── Ingester ─────────────────────────────────────────────────────────────


class UsgsVolcanoIngester(Ingester):
    layer = "volcanic_activity"
    source = "USGS Volcano Hazards Program"
    source_id = "usgs_volcano"
    poll_interval_sec = 900.0   # 15 min — alerts during episodes update
                                #          intra-day; small payload

    URL = (
        "https://volcanoes.usgs.gov/hans-public/api/volcano/"
        "getElevatedVolcanoes"
    )
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL) as r:
                r.raise_for_status()
                data = await r.json()
        if not isinstance(data, list):
            self.log.warning(
                f"[usgs_volcano] expected list, got {type(data).__name__}"
            )
            return []
        return data

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        sentinel_count = 0   # diagnostic — how many volcanoes lack coords?

        for item in raw_items:
            volcano_name = (item.get("volcano_name") or "").strip()
            vnum = (item.get("vnum") or "").strip()
            if not volcano_name or not vnum:
                continue

            alert_level = (item.get("alert_level") or "").strip().upper()
            color_code  = (item.get("color_code")  or "").strip().upper()
            obs_full = (item.get("obs_fullname") or "").strip()
            obs_abbr = (item.get("obs_abbr")    or "").strip()
            notice_id = (item.get("notice_identifier") or "").strip()
            notice_url = (item.get("notice_url") or "").strip()
            sent_utc = (item.get("sent_utc") or "").strip()

            severity = _severity(alert_level, color_code)
            lat, lng = _coords_for_vnum(vnum)
            if lat == 0.0 and lng == 0.0:
                sentinel_count += 1
                geocode_quality = "needs_match"
            else:
                geocode_quality = "point"

            ts = sent_utc or datetime.now(timezone.utc).isoformat()

            payload: Dict[str, Any] = {
                "volcano_name":      volcano_name,
                "vnum":              vnum,
                "alert_level":       alert_level or None,
                "color_code":        color_code or None,
                "observatory":       obs_full or None,
                "observatory_abbr":  obs_abbr or None,
                "notice_identifier": notice_id or None,
                "notice_url":        notice_url or None,
                "_attribution":      "Volcanic activity: USGS VHP",
            }

            title = (
                f"{volcano_name} — {alert_level or '?'}/{color_code or '?'}"
                + (f" ({obs_abbr})" if obs_abbr else "")
            )

            # Market tags: volcanic activity rarely has clean markets but
            # major hazards (Yellowstone, Hawaii) carry insurance + travel
            # exposure. Tag conservatively.
            mtags: List[str] = []
            if severity >= 8:
                mtags.append("hazard:volcano")

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"usgs_volcano:{vnum}:{notice_id or ts}",
                kind="volcanic_alert",
                lat=float(lat),
                lng=float(lng),
                ts=ts,
                severity=severity,
                source=self.source,
                payload=payload,
                domain="atmospheric",
                geocode_quality=geocode_quality,
                # Volcanic alerts are valid until the next notice — typically
                # hours to weeks. 24h half-life keeps the ranking signal
                # decaying while leaving older alerts in the catalog.
                decay_half_life_min=1440,
                market_tags=mtags,
                severity_for_market=max(0, severity - 4) if mtags else 0,
            ))

        if sentinel_count:
            self.log.info(
                f"[usgs_volcano] {sentinel_count} volcanoes emitted at sentinel "
                f"(0,0) — vnum not in _VOLCANO_COORDS; brief still surfaces them"
            )
        return out
