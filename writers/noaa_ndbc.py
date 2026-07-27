"""
NOAA NDBC Realtime Observation writer — P2-B Phase 1.5 live-data
upgrade for the noaa_buoys static layer.

One writer: `write_noaa_ndbc_events`. Persists per-station
observations (wave height, wind, sea-surface temp, pressure).

layer='noaa_buoys', event_type='ndbc_observation', subtype = station
id. Idempotent per (station_id, observation timestamp) via the
deterministic UUID5 derivation. No embedding (numeric fields only).
Real geo (station anchored location) — geocode_quality='exact'.

Imports from `writers._shared` for universal helpers; `db.acquire_write`
for the write pool. No cross-cluster imports. Uses the ingester
module's `_build_event_id` to derive the writer-side event id so the
two sides agree on dedup key derivation.
"""

from __future__ import annotations

import json
import logging
from typing import List

from ingesters.base import GlassboxEvent
from ingesters.noaa_ndbc import _build_event_id
from db import acquire_write
from writers._shared import _parse_ts, _with_confidence


_ndbc_log = logging.getLogger("writers.noaa_ndbc")


_NDBC_PROPERTY_KEYS = (
    "station_id",
    "station_name",
    "category",
    "observed_at",
    "wind_dir_deg",
    "wind_speed_ms",
    "gust_speed_ms",
    "wave_height_m",
    "dom_period_sec",
    "pressure_hpa",
    "air_temp_c",
    "sea_temp_c",
    "title",
    "link",
    "_attribution",
)


def _ndbc_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in _NDBC_PROPERTY_KEYS:
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_noaa_ndbc_events(events: List[GlassboxEvent]) -> int:
    """Persist NDBC realtime observations to the `event` hypertable.
    layer='noaa_buoys'. event_type='ndbc_observation'; subtype =
    station id. Idempotent per (station_id, observed_at) via UUID5."""
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "noaa_buoys":
                        continue
                    if not ev.external_id:
                        continue

                    p = ev.payload or {}
                    ts = _parse_ts(ev.ts)
                    station_id = p.get("station_id") or "unknown"
                    observed_at = p.get("observed_at") or ev.ts
                    event_id = _build_event_id(station_id, observed_at)
                    subtype = station_id[:32] if station_id else None
                    title = (p.get("title") or "")[:500] or None
                    # Description: human-readable measurement summary
                    wave_h = p.get("wave_height_m")
                    sea_t = p.get("sea_temp_c")
                    wind_s = p.get("wind_speed_ms")
                    desc_parts = []
                    if wave_h is not None:
                        desc_parts.append(f"Wave {wave_h}m")
                    if sea_t is not None:
                        desc_parts.append(f"SST {sea_t}°C")
                    if wind_s is not None:
                        desc_parts.append(f"Wind {wind_s}m/s")
                    description = " · ".join(desc_parts) if desc_parts else None
                    props_json = json.dumps(_ndbc_event_properties(ev))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'ndbc_observation', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'geo'), COALESCE($11, 240))
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
        _ndbc_log.warning(
            f"write_noaa_ndbc_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
