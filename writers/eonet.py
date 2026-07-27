"""
NASA EONET natural-event writer — P3-H Phase 3 extraction #10.

One writer: `write_natural_event_events`. Persists NASA EONET tracked
natural events (volcanoes, wildfires, severe storms, drought, dust/haze,
sea/lake ice, snow, floods, manmade, water color, temp extremes).
layer='natural_events', event_type='nasa_eonet', subtype = FIRST category
code (full list in properties.categories — EONET events can be multi-cat).

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


_eonet_log = logging.getLogger("writers.eonet")


def _eonet_event_properties(event: GlassboxEvent) -> dict:
    """Map NasaEonetIngester.normalize() payload → event.properties."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in ("title", "description", "categories", "sources", "link"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_natural_event_events(events: List[GlassboxEvent]) -> int:
    """Persist NASA EONET natural events (volcanoes, wildfires, severe storms,
    drought, dust/haze, sea/lake ice, snow, floods, manmade, water color, temp
    extremes) to the `event` hypertable.

    layer='natural_events' (from NasaEonetIngester) → event_type='nasa_eonet'.
    event_subtype carries the FIRST category code; the full list is in
    properties.categories. EONET events can belong to multiple categories
    (e.g., a wildfire may also be in the 'drought' category).

    ON CONFLICT (id, event_time) DO NOTHING — both deterministic uuid5 and
    event_time (parsed from EONET's geometry.date) are stable per event.

    Returns count of NEW rows inserted.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "natural_events":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"nasa_eonet:{ev.external_id}",
                    )
                    props_json = json.dumps(_eonet_event_properties(ev))

                    p = ev.payload or {}
                    title = (p.get("title") or "")[:500] or None
                    description = (p.get("description") or "")[:1000] or None
                    cats = p.get("categories") or []
                    subtype = cats[0] if cats else None

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1, 'nasa_eonet', $2, $3,
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
        _eonet_log.warning(
            f"write_natural_event_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
