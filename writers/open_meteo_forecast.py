"""
Open-Meteo Forecast writer — P2-B Phase 1 climate_forecast live-data
upgrade.

One writer: `write_open_meteo_forecast_events`. Persists daily
climate forecasts.

layer='climate_forecast', event_type='climate_forecast', subtype =
city name. No embedding (numeric fields only). Real geo (city
centroid) — geocode_quality='city'.

Imports from `writers._shared` for universal helpers; `db.acquire_write`
for the write pool. No cross-cluster imports.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import List

from ingesters.base import GlassboxEvent
from db import acquire_write
from writers._shared import (
    _EVENT_UUID_NAMESPACE,
    _parse_ts,
    _with_confidence,
)


_climate_log = logging.getLogger("writers.open_meteo_forecast")


_CLIMATE_PROPERTY_KEYS = (
    "city",
    "temp_max_c",
    "temp_min_c",
    "precipitation_mm",
    "forecast_date",
    "title",
    "_attribution",
)


def _climate_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in _CLIMATE_PROPERTY_KEYS:
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_open_meteo_forecast_events(events: List[GlassboxEvent]) -> int:
    """Persist Open-Meteo daily forecasts to the `event` hypertable.
    layer='climate_forecast'. event_type='climate_forecast'; subtype =
    city name. Idempotent per (city, forecast_date) via the
    deterministic UUID5 derivation."""
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "climate_forecast":
                        continue
                    if not ev.external_id:
                        continue

                    p = ev.payload or {}
                    ts = _parse_ts(ev.ts)
                    city = p.get("city") or "unknown"
                    forecast_date = p.get("forecast_date") or "unknown"
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"climate_forecast:{city}:{forecast_date}",
                    )
                    subtype = city[:120] if city else None
                    title = (p.get("title") or "")[:500] or None
                    # Description: human-readable summary of the day's
                    # high/low/precip — keeps the event row self-narrating
                    # without the consumer needing to parse properties.
                    t_max = p.get("temp_max_c")
                    t_min = p.get("temp_min_c")
                    precip = p.get("precipitation_mm")
                    description = (
                        f"High {t_max}°C · Low {t_min}°C · "
                        f"Precip {precip}mm"
                    ) if t_max is not None else None
                    props_json = json.dumps(_climate_event_properties(ev))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'climate_forecast', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'geo'), COALESCE($11, 720))
                        ON CONFLICT (id, event_time) DO NOTHING
                        """,
                        event_id,
                        subtype,
                        ts,
                        float(ev.lng),
                        float(ev.lat),
                        float(ev.severity) if ev.severity is not None else None,
                        title,
                        description,
                        props_json,
                        ev.domain,
                        ev.decay_half_life_min,
                    )
                    try:
                        if int(result.split()[-1]) == 1:
                            written += 1
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        _climate_log.warning(
            f"write_open_meteo_forecast_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
