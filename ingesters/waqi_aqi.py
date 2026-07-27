"""
WAQI ingester — World Air Quality Index real-time AQI feed.

Source: https://api.waqi.info/v2/map/bounds
License: free with attribution; commercial use permitted with real key
Attribution: required ("Air Quality: World Air Quality Index Project (aqicn.org)")
KEY required: WAQI_API_TOKEN env var.

Replaces 4 sites in glassbox.html that hardcode `?token=demo` (rate-limited globally).
This backend version means the Mac Mini is the sole caller, the key is in env not JS,
and clients consume a clean SSE stream.

WAQI returns AQI on the standard EPA scale:
  0-50      Good             severity 1
  51-100    Moderate         severity 3
  101-150   Unhealthy SG     severity 5
  151-200   Unhealthy        severity 6
  201-300   Very Unhealthy   severity 8
  301-500+  Hazardous        severity 10

The /v2/map/bounds endpoint returns ALL stations in a bbox at once,
which is way more efficient than pulling each city's geo feed individually.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


def _severity_for_aqi(aqi: float) -> int:
    if aqi <= 50:    return 1
    if aqi <= 100:   return 3
    if aqi <= 150:   return 5
    if aqi <= 200:   return 6
    if aqi <= 300:   return 8
    return 10


# ─── Ingester ─────────────────────────────────────────────────────────────


class WaqiAqiIngester(Ingester):
    layer = "air_quality"
    source = "WAQI World Air Quality Index"
    source_id = "waqi_aqi"               # gates against infra/sources.yaml
    poll_interval_sec = 600.0            # 10 min — AQI changes slowly enough

    URL = "https://api.waqi.info/v2/map/bounds"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    # Single global query — WAQI handles -90,-180,90,180
    BBOX = "-90,-180,90,180"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token = (
            os.environ.get("WAQI_API_TOKEN")
            or "4eda9a3f2c1b91061f04c5a6419fc5f970371bc9"   # registered key, see INFRASTRUCTURE.md
        )
        if not self._token:
            self.log.warning(
                "[waqi_aqi] WAQI_API_TOKEN not set — ingester will return [] "
                "every cycle. Register at https://aqicn.org/data-platform/token/"
            )

    async def fetch(self) -> List[Dict[str, Any]]:
        if not self._token:
            return []
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        params = {
            "latlng":   self.BBOX,
            "networks": "all",
            "token":    self._token,
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL, params=params) as r:
                r.raise_for_status()
                data = await r.json()

        # WAQI returns {status: 'ok', data: [{lat, lon, uid, aqi, station: {name, time}}]}
        if data.get("status") != "ok":
            self.log.info(f"[waqi_aqi] WAQI returned non-ok status: {data.get('status')}")
            return []
        return data.get("data") or []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for r in raw_items:
            uid = r.get("uid")
            if uid is None:
                continue
            try:
                lat = float(r.get("lat", 0) or 0)
                lng = float(r.get("lon", 0) or 0)
            except (TypeError, ValueError):
                continue
            if lat == 0 and lng == 0:
                continue

            # AQI can be a string for stations with no data ("-")
            aqi_raw = r.get("aqi")
            if aqi_raw is None or aqi_raw == "-" or aqi_raw == "":
                continue
            try:
                aqi = float(aqi_raw)
            except (TypeError, ValueError):
                continue

            severity = _severity_for_aqi(aqi)
            station = r.get("station") or {}
            station_name = station.get("name", "Unknown station")
            station_time = station.get("time")

            mtags: List[str] = []
            sev_market = 0
            if aqi >= 200:
                mtags.append("environment:hazardous_air")
                sev_market = 4

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"waqi:{uid}",
                kind="state",
                lat=lat,
                lng=lng,
                ts=station_time or now,
                severity=severity,
                source=self.source,
                payload={
                    "aqi":           aqi,
                    "station_name":  station_name,
                    "station_time":  station_time,
                    "_attribution": "Air Quality: aqicn.org (WAQI Project)",
                },
                domain="env",
                geocode_quality="exact",         # station coords
                decay_half_life_min=60,          # AQI updates ~hourly per station
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
