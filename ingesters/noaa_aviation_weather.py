"""
NOAA Aviation Weather Center ingester — global METAR + SIGMET.

Source: https://aviationweather.gov/api/data/metar (and /sigmet)
License: US public domain (NOAA — verified 2026-05-04)
Attribution: not legally required (US gov public domain) but rendered as good citizenship.
NO KEY required. UA + rate-limit respect MANDATORY.

METAR (Meteorological Aerodrome Report) is the de facto standard for
airport weather observations. We pull the global feed every 5 min for
~3,500 reporting stations.

What we surface:
  - Cloud ceiling
  - Visibility
  - Wind speed/gust
  - Temp / dewpoint
  - Significant flight rules (VFR / MVFR / IFR / LIFR)

SIGMETs (Significant Meteorological Information) are dispatched warnings
about hazardous weather (thunderstorms, turbulence, icing, volcanic ash).
We poll them every 5 min and severity-scale by hazard type.

Frontend pairs this with ourairports.py for airport pins:
  - METAR data populates the popup when user clicks an airport
  - SIGMETs render as polygon overlays
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


_FLIGHT_RULES_SEVERITY = {
    "VFR":    1,
    "MVFR":   3,
    "IFR":    5,
    "LIFR":   7,
}


def _parse_metar_severity(metar: Dict[str, Any]) -> int:
    fr = (metar.get("fltCat") or metar.get("fltcat") or "").upper()
    return _FLIGHT_RULES_SEVERITY.get(fr, 2)


# ─── Ingester ─────────────────────────────────────────────────────────────


class NoaaAviationWeatherIngester(Ingester):
    layer = "metar"
    source = "NOAA Aviation Weather Center (aviationweather.gov)"
    source_id = "noaa_aviation_weather"  # gates against infra/sources.yaml
    poll_interval_sec = 300.0            # 5 min — METAR cadence

    URL = "https://aviationweather.gov/api/data/metar"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    # 2026-05-04 fix: AWC requires either ids= or bbox= filter; pulling
    # without one returns 400 Bad Request. We use a curated list of TOP-30
    # major international airports — large enough for global coverage,
    # small enough that the URL fits comfortably (~250 chars vs ~700 with 80).
    # 2026-05-05: trimmed from 80 → 30 after AWC returned 502 Bad Gateway
    # on the longer URL. Future enhancement: viewport-driven per-region.
    MAJOR_AIRPORT_IDS = (
        # NA top hubs
        "KJFK,KLAX,KORD,KDFW,KATL,KDEN,KSFO,KSEA,KMIA,KBOS,"
        # Europe top hubs
        "EGLL,LFPG,EDDF,EHAM,EDDM,LEMD,LIRF,LSZH,EGKK,LFPO,"
        # Asia + Mid-East + Oceania top hubs
        "RJTT,RKSI,VHHH,WSSS,RJAA,OMDB,YSSY,VTBS,VABB,FAOR"
    )

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        # 2026-05-04 fix: ids= is REQUIRED. AWC's bare /metar returns 400.
        params = {
            "ids":    self.MAJOR_AIRPORT_IDS,
            "format": "json",
            "hours":  "1",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL, params=params) as r:
                r.raise_for_status()
                data = await r.json()

        # Response is a list of METAR dicts
        if isinstance(data, list):
            return data
        return []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for m in raw_items:
            icao = (m.get("icaoId") or m.get("icao_id") or "").strip()
            if not icao:
                continue
            try:
                lat = float(m.get("lat", 0) or 0)
                lng = float(m.get("lon", 0) or 0)
            except (TypeError, ValueError):
                continue
            if lat == 0 and lng == 0:
                continue

            severity = _parse_metar_severity(m)
            obs_time_raw = m.get("obsTime") or m.get("reportTime")
            # NOAA AWC sometimes returns obsTime as a Unix epoch
            # (10-digit integer or numeric string), and sometimes as
            # an ISO-8601 string. The downstream writer's _parse_ts only
            # handles ISO; convert epoch values here so it never sees
            # raw seconds.
            obs_time = now
            if obs_time_raw is not None:
                if isinstance(obs_time_raw, (int, float)):
                    obs_time = datetime.fromtimestamp(
                        float(obs_time_raw), tz=timezone.utc
                    ).isoformat()
                elif isinstance(obs_time_raw, str):
                    s = obs_time_raw.strip()
                    if s.isdigit() and len(s) >= 10:
                        try:
                            obs_time = datetime.fromtimestamp(
                                int(s), tz=timezone.utc
                            ).isoformat()
                        except (ValueError, OSError):
                            obs_time = now
                    else:
                        obs_time = s   # assume ISO; let writer parse

            mtags: List[str] = []
            sev_market = 0
            if severity >= 7:    # LIFR — flights getting cancelled
                mtags.append("aviation:lifr_conditions")
                sev_market = 4

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"metar:{icao}",
                kind="state",
                lat=lat,
                lng=lng,
                ts=str(obs_time),
                severity=severity,
                source=self.source,
                payload={
                    "icao":          icao,
                    "flight_rules":  m.get("fltCat") or m.get("fltcat"),
                    "wind_dir":      m.get("wdir"),
                    "wind_speed":    m.get("wspd"),
                    "wind_gust":     m.get("wgst"),
                    "visibility_sm": m.get("visib"),
                    "ceiling_ft":    m.get("ceil"),
                    "temp_c":        m.get("temp"),
                    "dewpoint_c":    m.get("dewp"),
                    "altimeter_hpa": m.get("altim"),
                    "raw_metar":     m.get("rawOb") or m.get("raw_text"),
                    "_attribution":  "Aviation weather: NOAA AWC (US public domain)",
                },
                domain="geo",
                geocode_quality="exact",
                decay_half_life_min=60,
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
