"""
Air-quality (WAQI) writer — P3-H Phase 3 first per-cluster extraction.

One writer: `write_aqi_events`. Stations re-emit AQI every cycle when
value changes, so each ts produces a distinct UUID5 → history
accumulates row-per-reading. layer='air_quality', event_type='aqi_reading',
subtype = severity bucket name ('good' / 'moderate' / 'unhealthy_sensitive'
/ 'unhealthy' / 'very_unhealthy' / 'hazardous').

Imports from `writers._shared` (universal helpers) and `db.acquire_write`
(write pool). No cross-cluster imports — this module is self-contained.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import List

from ingesters.base import GlassboxEvent
from db import acquire_write
from writers._shared import _EVENT_UUID_NAMESPACE, _parse_ts, _with_confidence


_aqi_log = logging.getLogger("writers.aqi")


def _aqi_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in ("aqi", "station_name", "station_time", "_attribution"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


def _aqi_severity_bucket(aqi: float | None) -> str | None:
    if aqi is None:
        return None
    if aqi <= 50:
        return "good"
    if aqi <= 100:
        return "moderate"
    if aqi <= 150:
        return "unhealthy_sensitive"
    if aqi <= 200:
        return "unhealthy"
    if aqi <= 300:
        return "very_unhealthy"
    return "hazardous"


async def write_aqi_events(events: List[GlassboxEvent]) -> int:
    """Persist WAQI air-quality readings to the `event` hypertable.

    layer='air_quality'. event_type='aqi_reading'; subtype = AQI bucket
    name. UUID5 includes ts so each reading persists as its own row.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "air_quality":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"aqi_reading:{ev.external_id}:{ev.ts}",
                    )
                    p = ev.payload or {}
                    aqi = p.get("aqi")
                    try:
                        aqi_f = float(aqi) if aqi is not None else None
                    except (TypeError, ValueError):
                        aqi_f = None
                    subtype = _aqi_severity_bucket(aqi_f)
                    title = (
                        f"AQI {int(aqi_f)} at {p.get('station_name') or 'station'}"
                        if aqi_f is not None else None
                    )
                    props_json = json.dumps(_aqi_event_properties(ev))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'aqi_reading', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'env'), COALESCE($11, 60))
                        ON CONFLICT (id, event_time) DO NOTHING
                        """,
                        event_id,
                        subtype,
                        ts,
                        float(ev.lng),
                        float(ev.lat),
                        float(ev.severity) if ev.severity is not None else None,
                        title,
                        p.get("station_name") or None,
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
        _aqi_log.warning(
            f"write_aqi_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
