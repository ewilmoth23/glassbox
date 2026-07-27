"""
METAR / Aviation Weather writer — P3-H Phase 3 extraction #2.

One writer: `write_metar_events`. Persists NOAA Aviation Weather Center
METAR observations. Each station re-emits readings every ~hourly cycle;
ts is included in UUID5 so history accumulates row-per-reading.
layer='metar', event_type='metar', subtype = flight_rules
('VFR'/'MVFR'/'IFR'/'LIFR').

Imports from `writers._shared` for the universal helpers; `db.acquire_write`
for the write pool. No cross-cluster imports.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import List

from ingesters.base import GlassboxEvent
from db import acquire_write
from writers._shared import _EVENT_UUID_NAMESPACE, _parse_ts, _with_confidence


_metar_log = logging.getLogger("writers.metar")


def _metar_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in ("icao", "flight_rules", "wind_dir", "wind_speed", "wind_gust",
                "visibility_sm", "ceiling_ft", "temp_c", "dewpoint_c",
                "altimeter_hpa", "raw_metar", "_attribution"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_metar_events(events: List[GlassboxEvent]) -> int:
    """Persist NOAA AWC METAR observations to the `event` hypertable.

    layer='metar'. event_type='metar'; subtype = flight_rules
    ('VFR'/'MVFR'/'IFR'/'LIFR'). UUID5 includes ts so each new reading
    persists as its own row (history accumulates).
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "metar":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"metar:{ev.external_id}:{ev.ts}",
                    )
                    p = ev.payload or {}
                    subtype = p.get("flight_rules") or None
                    title = f"METAR {p.get('icao') or ''}".strip() or None
                    description = p.get("raw_metar") or None
                    props_json = json.dumps(_metar_event_properties(ev))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'metar', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'geo'), COALESCE($11, 60))
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
        _metar_log.warning(
            f"write_metar_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
