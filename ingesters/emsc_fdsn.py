"""
EMSC FDSN Event ingester — European seismic events catalog.

Source: https://www.seismicportal.eu/fdsnws/event/1/query
License: **CC BY 4.0** (verified 2026-05-04 at seismicportal.eu/fdsn-wsevent.html)
DOI:     10.17616/R3N93X
Attribution required: YES — render in UI footer.

IMPORTANT: Only the FDSN endpoint (/fdsnws/event/1/) is CC BY 4.0.
The general EMSC website material is COPYRIGHT (non-commercial only).
This ingester ONLY hits the FDSN endpoint. Do not refactor it to use
seismicportal.eu/index.html or any general endpoint.

Why have this AND USGS?
  - USGS coverage is best in the Americas + Pacific
  - EMSC coverage is best in Europe + Mediterranean + North Africa + Middle East
  - Cross-validation reduces false-positives and catches one-source dropouts
  - Both are CC-licensed (USGS public domain, EMSC CC BY 4.0)

We do NOT dedup against USGS at the ingester level — both are emitted as
separate events with `source` field clearly distinguishing them. The
correlator (intelligence_loop / cognition pipeline) handles cross-source
dedup if needed.

Rate limits: max 20,000 events per request; reasonable polling expected.
We use 10 min (600s) which is more than enough for catalog freshness.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Severity mapping (magnitude → 0-10) ──────────────────────────────────

def _severity_for_magnitude(mag: float) -> int:
    """Map seismic magnitude (Richter/MW) to internal 0-10 severity scale.

    Reference: USGS magnitude classes
      M < 2.5   — micro (1-2)
      M 2.5-4   — minor (3)
      M 4-5     — light (4-5)
      M 5-6     — moderate (6)
      M 6-7     — strong (7)
      M 7-8     — major (8-9)
      M >= 8    — great (10)
    """
    if mag < 2.5:
        return 1
    if mag < 4.0:
        return 3
    if mag < 5.0:
        return 5
    if mag < 6.0:
        return 6
    if mag < 7.0:
        return 7
    if mag < 8.0:
        return 9
    return 10


# ─── Ingester ─────────────────────────────────────────────────────────────


class EmscFdsnIngester(Ingester):
    layer = "earthquakes"        # same logical layer as USGS — distinguished by source
    source = "EMSC SeismicPortal FDSN (CC BY 4.0, DOI 10.17616/R3N93X)"
    source_id = "emsc_fdsn_event"
    poll_interval_sec = 600.0    # 10 min

    URL = "https://www.seismicportal.eu/fdsnws/event/1/query"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    # Filter — only events ≥ M2.5, last 24h. Smaller events would drown the globe.
    MIN_MAGNITUDE = 2.5
    LOOKBACK_HOURS = 24

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        start = datetime.now(timezone.utc) - timedelta(hours=self.LOOKBACK_HOURS)
        params = {
            "format":       "json",
            "start":        start.strftime("%Y-%m-%dT%H:%M:%S"),
            "minmag":       str(self.MIN_MAGNITUDE),
            "limit":        "1000",
            "orderby":      "time",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL, params=params) as r:
                # FDSN returns 204 (No Content) when no events match — treat as []
                if r.status == 204:
                    return []
                r.raise_for_status()
                data = await r.json()

        # FDSN JSON wraps events in GeoJSON FeatureCollection
        return data.get("features") or []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for f in raw_items:
            props = f.get("properties") or {}
            geom = f.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) < 2:
                continue

            lng = float(coords[0])
            lat = float(coords[1])
            depth_km = float(coords[2]) if len(coords) > 2 and coords[2] is not None else None

            ext_id = (
                props.get("source_id")
                or props.get("unid")
                or f.get("id")
                or ""
            )
            if not ext_id:
                continue

            mag = float(props.get("mag", 0) or 0)
            severity = _severity_for_magnitude(mag)

            # Loop market tags — large quakes affect insurance, agriculture, energy markets
            mtags: List[str] = []
            sev_market = 0
            if mag >= 7.0:
                mtags.append("geology:major_quake")
                sev_market = 7
            elif mag >= 6.0:
                mtags.append("geology:strong_quake")
                sev_market = 4

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"emsc:{ext_id}",     # prefix to keep distinct from USGS
                kind="event",
                lat=lat,
                lng=lng,
                ts=props.get("time") or now,
                severity=severity,
                altitude_m=(-depth_km * 1000) if depth_km is not None else None,
                source=self.source,
                payload={
                    "magnitude":     mag,
                    "magnitude_type": props.get("magtype"),
                    "depth_km":      depth_km,
                    "region":        props.get("flynn_region") or props.get("region"),
                    "agency":        props.get("auth"),
                    "_attribution": "Earthquakes: EMSC/CSEM (CC BY 4.0) — DOI 10.17616/R3N93X",
                },
                domain="geo",
                geocode_quality="exact",         # seismic events are GPS-grade
                decay_half_life_min=60,          # quake events stop being fresh after ~1h
                market_tags=mtags,
                severity_for_market=sev_market,
            ))

        return out
