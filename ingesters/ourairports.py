"""
OurAirports ingester — global airport catalog.

Source: https://davidmegginson.github.io/ourairports-data/airports.csv
License: CC0 (public domain — verified at https://ourairports.com/data/)
Attribution: not required (CC0) but rendered as good citizenship.
NO KEY required.

OurAirports is the world's most complete open airport dataset:
  - ~80,000 airports, heliports, balloonports, seaplane bases
  - Coords (lat/lng/elevation_ft)
  - ICAO + IATA + GPS codes
  - Type (large_airport / medium_airport / small_airport / etc.)
  - Country + region
  - Scheduled service (Y/N)

For Glassbox v1.0 we ingest ONLY large_airport + medium_airport with
scheduled_service=yes — that's ~3,500 airports globally, manageable for
the globe view at any zoom level.

The catalog updates daily. Polling every 24h is plenty.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Dict, List

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Severity (airports are ambient infrastructure — low constant severity) ─

_TYPE_SEVERITY = {
    "large_airport":   2,
    "medium_airport":  1,
    "small_airport":   0,    # filtered out below
}


# ─── Ingester ─────────────────────────────────────────────────────────────


class OurAirportsIngester(Ingester):
    layer = "airports"
    source = "OurAirports (CC0)"
    source_id = "ourairports"             # gates against infra/sources.yaml
    poll_interval_sec = 86400.0           # 24h — catalog changes slowly

    URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    # Filter: include only these airport types, with scheduled service
    KEEP_TYPES = {"large_airport", "medium_airport"}

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=120)   # CSV is ~12 MB
        headers = {"User-Agent": self.UA, "Accept": "text/csv"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL) as r:
                r.raise_for_status()
                text = await r.text()

        # CSV with header. Smoke mode caps at 100 records (production: ~3,300).
        reader = csv.DictReader(io.StringIO(text))
        rows: List[Dict[str, Any]] = []
        smoke_cap = 100 if self.smoke_mode else None
        for row in reader:
            atype = (row.get("type") or "").strip()
            if atype not in self.KEEP_TYPES:
                continue
            # Only airports with scheduled service — filters tiny regional fields
            if (row.get("scheduled_service") or "").strip().lower() != "yes":
                continue
            rows.append(row)
            if smoke_cap is not None and len(rows) >= smoke_cap:
                break
        return rows

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for r in raw_items:
            ident = (r.get("ident") or "").strip()
            if not ident:
                continue
            try:
                lat = float(r.get("latitude_deg", "") or 0)
                lng = float(r.get("longitude_deg", "") or 0)
            except (TypeError, ValueError):
                continue
            if lat == 0 and lng == 0:
                continue

            atype = r.get("type") or ""
            severity = _TYPE_SEVERITY.get(atype, 0)
            elev_ft_raw = r.get("elevation_ft")
            try:
                elev_m = float(elev_ft_raw) * 0.3048 if elev_ft_raw else None
            except (TypeError, ValueError):
                elev_m = None

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"airport:{ident}",
                kind="state",
                lat=lat,
                lng=lng,
                ts=now,
                severity=severity,
                altitude_m=elev_m,
                source=self.source,
                payload={
                    "icao":          ident,
                    "iata":          (r.get("iata_code") or "").strip(),
                    "gps":           (r.get("gps_code") or "").strip(),
                    "name":          (r.get("name") or "").strip(),
                    "type":          atype,
                    "country":       (r.get("iso_country") or "").strip(),
                    "region":        (r.get("iso_region") or "").strip(),
                    "municipality":  (r.get("municipality") or "").strip(),
                    "_attribution": "Airports: OurAirports (CC0)",
                },
                domain="geo",
                geocode_quality="exact",
                decay_half_life_min=43200,    # airports don't move; very long half-life
                market_tags=[],
                severity_for_market=0,
            ))

        return out
