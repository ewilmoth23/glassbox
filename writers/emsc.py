"""
EMSC SeismicPortal writer — P3-H Phase 3 extraction #12.

One writer: `write_emsc_quake_events`. Persists EMSC SeismicPortal
earthquake events. layer='earthquakes' (shared with USGS) + external_id
prefix 'emsc:' distinguishes EMSC events from USGS. event_type='emsc_quake'.

Pairs with writers/seismic.py — together they handle the dual-source
'earthquakes' layer. The cross-writer prefix filter is what keeps the
two from racing on the same external_id.

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


_emsc_log = logging.getLogger("writers.emsc")


def _emsc_event_properties(event: GlassboxEvent) -> dict:
    """Map EmscFdsnIngester.normalize() payload → event.properties."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in (
        "magnitude", "magnitude_type", "depth_km", "region", "agency",
    ):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_emsc_quake_events(events: List[GlassboxEvent]) -> int:
    """Persist EMSC SeismicPortal earthquake events to the `event` hypertable.

    layer='earthquakes' (shared with USGS) + external_id prefix 'emsc:'
    distinguishes these from USGS events. event_type='emsc_quake' keeps
    them queryable as a separate source even though both feed the same
    earthquake "layer" semantically.

    Same `ON CONFLICT (id, event_time)` dedup as USGS — EMSC provides
    stable `time` per event from the FDSN protocol.

    Returns count of NEW rows inserted.
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
                    # Only consume emsc-prefixed events; USGS goes to
                    # write_seismic_events.
                    if not ev.external_id.startswith("emsc:"):
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"emsc_quake:{ev.external_id}",
                    )
                    props_json = json.dumps(_emsc_event_properties(ev))

                    p = ev.payload or {}
                    mag = p.get("magnitude")
                    region = p.get("region") or "Unknown region"
                    depth = p.get("depth_km")
                    agency = p.get("agency") or "EMSC"

                    title = f"M{mag} earthquake — {region}" if mag is not None else f"Earthquake — {region}"
                    desc_parts = []
                    if depth is not None:
                        desc_parts.append(f"depth {depth}km")
                    if agency:
                        desc_parts.append(f"reported by {agency}")
                    description = "; ".join(desc_parts) if desc_parts else None

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1, 'emsc_quake', NULL, $2,
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
                    try:
                        if int(result.split()[-1]) == 1:
                            written += 1
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        _emsc_log.warning(
            f"write_emsc_quake_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
