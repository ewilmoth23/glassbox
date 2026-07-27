"""
NOAA NDBC Realtime Observation ingester — live-data upgrade for the
`noaa_buoys` static layer shipped 2026-05-27 NIGHT LATE.

Source: https://www.ndbc.noaa.gov/data/realtime2/<station_id>.txt
License: US gov public domain (NOAA NDBC, Title 17 USC § 105).

Per-station text format ("realtime2"):
    #YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP WTMP DEWP VIS PTDY TIDE
    #yr mo dy hr mn degT m/s m/s   m  sec sec degT hPa  degC degC degC nmi hPa   ft
    2026 05 28 00 10 310  5.0  7.0  2.5    8    6  150 1027.5 14.9 12.3 ...
    ...

NDBC marks missing fields as "MM" — the parser must handle them.
Each station's file holds the last ~30 days of 10-min observations;
we cap per-cycle ingestion to the top-N most-recent rows (defaults
to 10) so a burst of stale-file replay doesn't dump 14×4000 rows.

Polling: 30 min per cycle (NDBC observations cycle every 10-30 min
depending on station + connection; 30 min is a polite + adequate
default).

Multi-station handling: 14 stations × 1 file each = 14 HTTP calls per
cycle. Each file is ~10-50KB so total bandwidth is modest. We pull
them in parallel via asyncio.gather() with a tight per-station timeout.

The STATIONS list mirrors `data/noaa_buoys.geojson` so the layer's
data scope is consistent whether the route serves the static seed or
DB-derived observations.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── 14 curated stations (mirrors data/noaa_buoys.geojson) ────────────────


STATIONS: List[Dict[str, Any]] = [
    {"station_id": "46006", "name": "Cape Beale (Pacific NW)",     "lat": 46.844, "lng": -137.382, "category": "pacific_nw"},
    {"station_id": "46059", "name": "West California",             "lat": 38.094, "lng": -129.998, "category": "pacific"},
    {"station_id": "46086", "name": "San Clemente Basin",          "lat": 32.499, "lng": -118.052, "category": "pacific"},
    {"station_id": "42001", "name": "Mid Gulf of Mexico",          "lat": 25.897, "lng": -89.667,  "category": "gulf"},
    {"station_id": "42040", "name": "Mobile South",                "lat": 29.207, "lng": -88.205,  "category": "gulf"},
    {"station_id": "41001", "name": "East Hatteras",               "lat": 34.704, "lng": -72.617,  "category": "atlantic"},
    {"station_id": "41010", "name": "Canaveral East",              "lat": 28.878, "lng": -78.476,  "category": "atlantic"},
    {"station_id": "44025", "name": "Long Island NY",              "lat": 40.251, "lng": -73.164,  "category": "atlantic"},
    {"station_id": "46035", "name": "Bering Sea AK",               "lat": 57.022, "lng": -177.708, "category": "alaska"},
    {"station_id": "46073", "name": "Aleutian / Pribilof",         "lat": 55.000, "lng": -172.001, "category": "alaska"},
    {"station_id": "51001", "name": "NW Hawaii",                   "lat": 23.445, "lng": -162.050, "category": "hawaii"},
    {"station_id": "51002", "name": "SW Hawaii",                   "lat": 17.094, "lng": -157.842, "category": "hawaii"},
    {"station_id": "51101", "name": "Lihue NW",                    "lat": 24.359, "lng": -162.075, "category": "hawaii"},
    {"station_id": "46089", "name": "Tillamook OR",                "lat": 45.913, "lng": -125.788, "category": "pacific_nw"},
]

_STATIONS_BY_ID: Dict[str, Dict[str, Any]] = {s["station_id"]: s for s in STATIONS}

# Stable namespace for ndbc-observation UUIDs — distinct from
# `_EVENT_UUID_NAMESPACE` so collisions can't happen across layers.
_NDBC_UUID_NAMESPACE = uuid.UUID("d3f02c8e-7b1a-4c93-9f5d-1e2a3b4c5d6e")


# ─── Severity ────────────────────────────────────────────────────────────


def _severity_for_wave_height(wave_height_m: Optional[float]) -> int:
    """Map wave height to severity. Calm < 1m → 1; choppy 1-3m → 3;
    rough 3-6m → 6; storm ≥6m → 8+. Cyclone seas ≥10m → 10."""
    if wave_height_m is None:
        return 1
    if wave_height_m >= 10:
        return 10
    if wave_height_m >= 6:
        return 8
    if wave_height_m >= 4:
        return 6
    if wave_height_m >= 3:
        return 5
    if wave_height_m >= 2:
        return 3
    if wave_height_m >= 1:
        return 2
    return 1


# ─── Parser ──────────────────────────────────────────────────────────────


_FIELD_ORDER = (
    "wind_dir", "wind_speed_ms", "gust_speed_ms",
    "wave_height_m", "dom_period_sec", "avg_period_sec", "wave_dir",
    "pressure_hpa", "air_temp_c", "sea_temp_c", "dewpoint_c",
    "vis_nmi", "pressure_tendency_hpa", "tide_ft",
)

_INT_FIELDS = {"wind_dir", "dom_period_sec", "avg_period_sec", "wave_dir"}


def _parse_field(raw: str, as_int: bool) -> Optional[Any]:
    """NDBC marks missing as 'MM'. Return None for missing; coerce
    the rest to int or float."""
    s = (raw or "").strip()
    if not s or s == "MM":
        return None
    try:
        return int(float(s)) if as_int else float(s)
    except (TypeError, ValueError):
        return None


def _parse_realtime2_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse one observation line. Returns a dict with timestamp
    fields + parsed measurements, or None for comments / malformed."""
    if not line:
        return None
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    parts = s.split()
    if len(parts) < 5:
        return None
    try:
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
        hour = int(parts[3])
        minute = int(parts[4])
    except (TypeError, ValueError):
        return None
    out: Dict[str, Any] = {
        "year": year, "month": month, "day": day,
        "hour": hour, "minute": minute,
    }
    # Measurement fields start at parts[5]. Tolerate truncated lines.
    for idx, key in enumerate(_FIELD_ORDER):
        raw = parts[5 + idx] if (5 + idx) < len(parts) else None
        if raw is None:
            out[key] = None
            continue
        out[key] = _parse_field(raw, key in _INT_FIELDS)
    return out


def _build_event_id(station_id: str, observed_at_iso: str) -> uuid.UUID:
    """Deterministic UUID5 keyed by (station, observation timestamp)
    so repeated polls don't double-write the same observation."""
    return uuid.uuid5(
        _NDBC_UUID_NAMESPACE,
        f"ndbc:{station_id}:{observed_at_iso}",
    )


# ─── Ingester ─────────────────────────────────────────────────────────────


class NoaaNdbcIngester(Ingester):
    layer = "noaa_buoys"
    source = "NOAA NDBC realtime2"
    source_id = "noaa_ndbc"                # gates against infra/sources.yaml
    poll_interval_sec = 1800.0             # 30 min

    BASE_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station_id}.txt"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"
    PER_STATION_TIMEOUT_SEC = 10.0
    MAX_OBSERVATIONS_PER_STATION_PER_CYCLE = 10

    async def _fetch_one_station(
        self,
        session: aiohttp.ClientSession,
        station: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        url = self.BASE_URL.format(station_id=station["station_id"])
        try:
            async with session.get(url) as r:
                if r.status != 200:
                    return None
                text = await r.text()
        except Exception as e:
            self.log.info(
                f"[noaa_ndbc] station {station['station_id']} fetch failed: {e}"
            )
            return None
        return {"station_id": station["station_id"], "lines": text.splitlines()}

    async def fetch(self) -> List[Dict[str, Any]]:
        """Pull realtime2 text for each station in parallel. Smoke mode
        caps to the first 3 stations."""
        stations = STATIONS[:3] if self.smoke_mode else STATIONS
        timeout = aiohttp.ClientTimeout(total=self.PER_STATION_TIMEOUT_SEC)
        headers = {"User-Agent": self.UA, "Accept": "text/plain"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            results = await asyncio.gather(*[
                self._fetch_one_station(s, st) for st in stations
            ], return_exceptions=True)
        return [r for r in results if isinstance(r, dict)]

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            station_id = item.get("station_id")
            station = _STATIONS_BY_ID.get(station_id)
            if station is None:
                continue
            lines = item.get("lines") or []
            count_for_this_station = 0
            for line in lines:
                if count_for_this_station >= self.MAX_OBSERVATIONS_PER_STATION_PER_CYCLE:
                    break
                parsed = _parse_realtime2_line(line)
                if parsed is None:
                    continue
                try:
                    observed_at = datetime(
                        parsed["year"], parsed["month"], parsed["day"],
                        parsed["hour"], parsed["minute"],
                        tzinfo=timezone.utc,
                    )
                except (TypeError, ValueError):
                    continue
                observed_at_iso = observed_at.isoformat()
                external_id = f"ndbc:{station_id}:{observed_at_iso}"

                wave_height = parsed.get("wave_height_m")
                severity = _severity_for_wave_height(wave_height)

                payload = {
                    "station_id": station_id,
                    "station_name": station["name"],
                    "category": station["category"],
                    "observed_at": observed_at_iso,
                    "wind_dir_deg": parsed.get("wind_dir"),
                    "wind_speed_ms": parsed.get("wind_speed_ms"),
                    "gust_speed_ms": parsed.get("gust_speed_ms"),
                    "wave_height_m": wave_height,
                    "dom_period_sec": parsed.get("dom_period_sec"),
                    "pressure_hpa": parsed.get("pressure_hpa"),
                    "air_temp_c": parsed.get("air_temp_c"),
                    "sea_temp_c": parsed.get("sea_temp_c"),
                    "title": f"NDBC {station_id} observation ({observed_at_iso})",
                    "link": f"https://www.ndbc.noaa.gov/station_page.php?station={station_id}",
                    "_attribution": "NDBC observation: NOAA National Data Buoy Center",
                }
                out.append(GlassboxEvent(
                    layer=self.layer,
                    external_id=external_id,
                    kind="ndbc_observation",
                    lat=station["lat"],
                    lng=station["lng"],
                    ts=observed_at_iso,
                    severity=severity,
                    source=self.source,
                    payload=payload,
                    domain="geo",
                    geocode_quality="exact",
                    decay_half_life_min=240,   # 4h — observations supersede fast
                    market_tags=[],
                    severity_for_market=2 if severity >= 6 else 0,
                ))
                count_for_this_station += 1
        return out
