"""
Open-Meteo Forecast ingester — daily climate / weather forecasts for
15 hand-curated major world cities. Live-data upgrade for the
`climate_forecast` static layer shipped 2026-05-27 NIGHT LATE.

Source: https://api.open-meteo.com/v1/forecast
License: CC-BY 4.0 — Open-Meteo permits commercial use with
attribution per https://open-meteo.com/en/license.

Polling cadence: 6h (Open-Meteo refreshes its forecast every 3-6h
depending on the model; 6h is a polite default that matches the
slowest-refresh layer in their ensemble).

Multi-city batching: Open-Meteo accepts comma-separated lat/lng
arrays in one request and returns a JSON array (one element per
city in request order). We pull all 15 cities in a single API call —
much cheaper than 15 sequential requests + still within Open-Meteo's
generous free-tier limits.

Output:
    layer = "climate_forecast"
    event_type = "climate_forecast"
    subtype = city name
    severity = scaled by extreme-temp threshold (heat wave / cold snap)

The CITIES list mirrors the static seed at
`data/climate_forecast.geojson` so the layer's content is consistent
whether the route serves the static seed or DB-derived data. Adding
a city requires updating BOTH this list AND the static seed file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── 15 curated cities (mirrors data/climate_forecast.geojson) ────────────

CITIES: List[Dict[str, Any]] = [
    {"name": "New York",      "lat": 40.7,  "lng": -73.9},
    {"name": "London",        "lat": 51.5,  "lng": -0.1},
    {"name": "Tokyo",         "lat": 35.7,  "lng": 139.7},
    {"name": "Delhi",         "lat": 28.6,  "lng": 77.2},
    {"name": "Rio de Janeiro","lat": -22.9, "lng": -43.2},
    {"name": "Sydney",        "lat": -33.9, "lng": 151.2},
    {"name": "Moscow",        "lat": 55.8,  "lng": 37.6},
    {"name": "Mexico City",   "lat": 19.4,  "lng": -99.1},
    {"name": "Cairo",         "lat": 30.0,  "lng": 31.2},
    {"name": "Paris",         "lat": 48.9,  "lng": 2.3},
    {"name": "Beijing",       "lat": 39.9,  "lng": 116.4},
    {"name": "Chicago",       "lat": 41.9,  "lng": -87.6},
    {"name": "Johannesburg",  "lat": -26.2, "lng": 28.0},
    {"name": "Singapore",     "lat": 1.3,   "lng": 103.8},
    {"name": "Buenos Aires",  "lat": -34.6, "lng": -58.4},
]


# ─── Severity helper ─────────────────────────────────────────────────────


def _severity_for_temp(temp_max_c: Optional[float]) -> int:
    """Scale severity by extreme-temperature threshold. Heat waves
    (≥40°C) and extreme cold (≤-20°C) both alarm at the top of the
    scale; temperate days are ambient. None temps default low."""
    if temp_max_c is None:
        return 1
    if temp_max_c >= 45:
        return 9
    if temp_max_c >= 40:
        return 8
    if temp_max_c >= 35:
        return 6
    if temp_max_c >= 30:
        return 4
    if temp_max_c <= -25:
        return 9
    if temp_max_c <= -20:
        return 8
    if temp_max_c <= -10:
        return 5
    if temp_max_c <= 0:
        return 3
    return 2


# ─── Ingester ─────────────────────────────────────────────────────────────


class OpenMeteoForecastIngester(Ingester):
    layer = "climate_forecast"
    source = "Open-Meteo Forecast (CC-BY 4.0)"
    source_id = "open_meteo_forecast"     # gates against infra/sources.yaml
    poll_interval_sec = 21600.0           # 6h — Open-Meteo's forecast cadence

    BASE_URL = "https://api.open-meteo.com/v1/forecast"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    async def fetch(self) -> List[Dict[str, Any]]:
        """Pull all 15 cities in one multi-coordinate request.
        Returns the JSON array Open-Meteo emits, one element per city
        in CITIES order. Smoke mode caps to the first 3 cities to
        keep smoke runs fast."""
        cities = CITIES[:3] if self.smoke_mode else CITIES
        lats = ",".join(str(c["lat"]) for c in cities)
        lngs = ",".join(str(c["lng"]) for c in cities)
        params = {
            "latitude": lats,
            "longitude": lngs,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": "auto",
            "forecast_days": "1",
        }
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.BASE_URL, params=params) as r:
                r.raise_for_status()
                data = await r.json()
        # Open-Meteo returns either a single dict (one city) or a list
        # (multi-city). Normalize to list so normalize() handles both.
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return data
        return []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        cities = CITIES[:3] if self.smoke_mode else CITIES
        for idx, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            daily = item.get("daily")
            if not isinstance(daily, dict):
                continue
            time_list = daily.get("time") or []
            tmax_list = daily.get("temperature_2m_max") or []
            tmin_list = daily.get("temperature_2m_min") or []
            precip_list = daily.get("precipitation_sum") or []
            if not (time_list and tmax_list):
                continue

            # Match back to the CITIES entry by request-order index.
            # Open-Meteo guarantees response-array order matches request
            # lat/lng order, but the response's reported lat/lng may
            # differ slightly (snaps to the nearest grid cell).
            city = cities[idx] if idx < len(cities) else None
            if city is None:
                continue

            forecast_date = time_list[0]
            t_max = tmax_list[0] if tmax_list else None
            t_min = tmin_list[0] if tmin_list else None
            precip = precip_list[0] if precip_list else None

            severity = _severity_for_temp(t_max)

            # Use REQUEST lat/lng (the seed's canonical coords), not the
            # response's grid-snapped lat/lng. Keeps city positions stable
            # across polls if Open-Meteo changes grid resolution.
            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"om-forecast:{city['name']}:{forecast_date}",
                kind="climate_forecast",
                lat=city["lat"],
                lng=city["lng"],
                ts=f"{forecast_date}T00:00:00+00:00",
                severity=severity,
                source=self.source,
                payload={
                    "city": city["name"],
                    "temp_max_c": t_max,
                    "temp_min_c": t_min,
                    "precipitation_mm": precip,
                    "forecast_date": forecast_date,
                    "title": f"Climate forecast — {city['name']} ({forecast_date})",
                    "_attribution": "Climate / weather forecast: Open-Meteo",
                },
                domain="geo",
                geocode_quality="city",
                decay_half_life_min=720,        # 12h — forecast supersedes fast
                market_tags=[],
                severity_for_market=0,
            ))
        return out
