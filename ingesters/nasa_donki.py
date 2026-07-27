"""
NASA DONKI ingester — Space Weather Database Of Notifications, Knowledge, Information.

Source: https://api.nasa.gov/DONKI/{event_type}?startDate=...&api_key=...
License: US public domain (NASA), commercial use OK
KEY required: NASA_API_KEY env var.

DONKI event types covered for v1.0:
  - FLR  — Solar flares
  - CME  — Coronal mass ejections
  - GST  — Geomagnetic storms

We pull a 7-day backward window. Each event is space-based (no Earth lat/lng)
so we use sentinel (0,0) and let the frontend render as a panel widget like
nasa_neo. The `payload` carries the actual data + classification (X/M/C class
for flares, Kp index for storms).

Solar flare classification:
  X = strongest
  M = medium
  C = small
  B/A = ambient

Geomagnetic storm Kp index:
  Kp 4 = active           severity 3
  Kp 5 = minor storm      severity 5
  Kp 6 = moderate storm   severity 6
  Kp 7 = strong storm     severity 7
  Kp 8 = severe storm     severity 8
  Kp 9 = extreme storm    severity 10

Polled hourly — these events rarely fire on shorter intervals.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Severity helpers ─────────────────────────────────────────────────────

_FLARE_CLASS_SEVERITY = {
    "X": 9,    # severe (radio blackouts on sunlit side, RF interference)
    "M": 6,    # moderate (limited blackouts in polar regions)
    "C": 3,    # minor
    "B": 1,    # ambient
    "A": 1,    # ambient
}


def _severity_for_flare(flare_class: Optional[str]) -> int:
    if not flare_class:
        return 2
    cls_letter = flare_class[0].upper()
    return _FLARE_CLASS_SEVERITY.get(cls_letter, 2)


def _severity_for_storm(kp_index: Optional[float]) -> int:
    if kp_index is None:
        return 3
    if kp_index >= 9:  return 10
    if kp_index >= 8:  return 8
    if kp_index >= 7:  return 7
    if kp_index >= 6:  return 6
    if kp_index >= 5:  return 5
    if kp_index >= 4:  return 3
    return 1


# ─── Ingester ─────────────────────────────────────────────────────────────


class NasaDonkiIngester(Ingester):
    layer = "space_weather"
    source = "NASA DONKI Space Weather (api.nasa.gov)"
    source_id = "nasa_donki"             # gates against infra/sources.yaml
    poll_interval_sec = 3600.0           # 1h

    BASE_URL = "https://api.nasa.gov/DONKI"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"
    LOOKBACK_DAYS = 7

    EVENT_TYPES = ("FLR", "CME", "GST")   # solar flares, CMEs, geomagnetic storms

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._key = (
            os.environ.get("NASA_API_KEY")
            or "GaolpeVcVeJbW5kayTBN6uvtaUA8yByO9gfYVgRI"   # registered key
        )
        if not self._key:
            self.log.warning("[nasa_donki] NASA_API_KEY missing — ingester will return [].")

    async def fetch(self) -> List[Dict[str, Any]]:
        if not self._key:
            return []
        results: List[Dict[str, Any]] = []
        start = (datetime.now(timezone.utc) - timedelta(days=self.LOOKBACK_DAYS)).date()
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA}

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            for ev_type in self.EVENT_TYPES:
                url = f"{self.BASE_URL}/{ev_type}"
                params = {"startDate": start.isoformat(), "api_key": self._key}
                try:
                    async with s.get(url, params=params) as r:
                        r.raise_for_status()
                        data = await r.json()
                except Exception as e:
                    self.log.info(f"[nasa_donki] {ev_type} fetch failed: {e}")
                    continue
                # data is a list of event dicts
                if isinstance(data, list):
                    for item in data:
                        item["_event_type"] = ev_type
                        results.append(item)
        return results

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for ev in raw_items:
            ev_type = ev.get("_event_type") or ""
            if ev_type == "FLR":
                ext = ev.get("flrID")
                cls = ev.get("classType")
                severity = _severity_for_flare(cls)
                title = f"Solar flare {cls}" if cls else "Solar flare"
                ts = ev.get("peakTime") or ev.get("beginTime") or now
                payload_extra = {"class": cls, "source_location": ev.get("sourceLocation")}
                mtag = "space:solar_flare" if severity >= 6 else None

            elif ev_type == "CME":
                ext = ev.get("activityID")
                title = "Coronal mass ejection"
                # CME analyses[] has speed and Earth-impact info
                analyses = ev.get("cmeAnalyses") or []
                speed = None
                if analyses:
                    speed = analyses[0].get("speed")
                severity = 5 if speed and speed > 1000 else 3
                ts = ev.get("startTime") or now
                payload_extra = {"speed_km_s": speed}
                mtag = "space:cme" if severity >= 5 else None

            elif ev_type == "GST":
                ext = ev.get("gstID")
                kpidx = (ev.get("allKpIndex") or [{}])
                kp_max = max((float(k.get("kpIndex", 0)) for k in kpidx if k.get("kpIndex") is not None), default=None)
                severity = _severity_for_storm(kp_max)
                title = f"Geomagnetic storm Kp{kp_max}" if kp_max else "Geomagnetic storm"
                ts = ev.get("startTime") or now
                payload_extra = {"kp_max": kp_max}
                mtag = "space:geomag_storm" if severity >= 6 else None

            else:
                continue

            if not ext:
                continue

            mtags: List[str] = [mtag] if mtag else []
            sev_market = 4 if severity >= 7 else 0

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"donki:{ev_type}:{ext}",
                kind="watch",
                lat=0.0,
                lng=0.0,
                ts=ts,
                severity=severity,
                source=self.source,
                payload={
                    "event_type":   ev_type,
                    "title":        title,
                    "link":         ev.get("link"),
                    "_attribution": "Space weather: NASA DONKI",
                    **payload_extra,
                },
                domain="space",
                geocode_quality="not_geo",
                decay_half_life_min=720,       # 12h
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
