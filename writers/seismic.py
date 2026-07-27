"""
USGS seismic writer — P3-H Phase 3 extraction #11.

One writer: `write_seismic_events`. Persists USGS earthquake events to
the `event` hypertable. layer='earthquakes', event_type='usgs_quake'.
Cross-writer isolation with EMSC: EMSC events use the same layer but
prefix external_id with 'emsc:' — those belong to write_emsc_quake_events.

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
from writers._shared import _EVENT_UUID_NAMESPACE, _parse_ts, _with_confidence


_seismic_log = logging.getLogger("writers.seismic")


def _seismic_event_properties(event: GlassboxEvent) -> dict:
    """Pull the USGS-shape fields off the ingester payload, adding external_id
    so dedup queries can find rows by USGS id."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in ("mag", "place", "title", "depth_km", "url", "tsunami", "alert"):
        if key in p:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_seismic_events(events: List[GlassboxEvent]) -> int:
    """Persist USGS / EMSC earthquake events to the `event` hypertable.

    Returns count of NEW rows inserted (re-running with the same external_id
    returns 0 — ON CONFLICT DO NOTHING via deterministic UUID).
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "earthquakes":
                        continue
                    if not ev.external_id:
                        continue
                    # Cross-writer isolation: EMSC events use the same layer
                    # but prefix external_id with 'emsc:'. Those belong in
                    # write_emsc_quake_events, not here.
                    if ev.external_id.startswith("emsc:"):
                        continue

                    ts = _parse_ts(ev.ts)
                    # Deterministic id so re-running with same USGS id is a
                    # no-op (the event hypertable's PK is (id, event_time)).
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"usgs_quake:{ev.external_id}",
                    )
                    props_json = json.dumps(_seismic_event_properties(ev))

                    title = (ev.payload or {}).get("title") or f"M? - {(ev.payload or {}).get('place', '')}"
                    description = (ev.payload or {}).get("place")

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1, 'usgs_quake', NULL, $2,
                             ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                             $5, $6, $7, $8::jsonb,
                             COALESCE($9, 'geo'), COALESCE($10, 60))
                        ON CONFLICT (id, event_time) DO NOTHING
                        """,
                        event_id,
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
                    # asyncpg returns "INSERT 0 N"; N is 0 on conflict, 1 otherwise
                    try:
                        if int(result.split()[-1]) == 1:
                            written += 1
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        _seismic_log.warning(
            f"write_seismic_events failed after {written} events: {type(e).__name__}: {e}"
        )
        return written

    return written
