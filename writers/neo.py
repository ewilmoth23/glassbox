"""
NASA NEO close-approach writer — P3-H Phase 3 extraction #3.

One writer: `write_neo_events`. Each NEO close-approach is unique per
neo_id within the close-approach window — `external_id` alone yields the
UUID5. layer='neo_asteroids', event_type='neo_close_approach',
subtype = 'hazardous' or 'normal'. No geo (NEOs ride sentinel 0,0).

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


_neo_log = logging.getLogger("writers.neo")


def _neo_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in ("name", "hazardous", "diameter_m_avg", "miss_km",
                "miss_lunar", "rel_velocity_kmh", "orbiting_body",
                "_attribution"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_neo_events(events: List[GlassboxEvent]) -> int:
    """Persist NASA NEO close-approach events. layer='neo_asteroids'.
    event_type='neo_close_approach'; subtype = 'hazardous' or 'normal'.
    No geo (sentinel 0,0).
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "neo_asteroids":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"neo:{ev.external_id}",
                    )
                    p = ev.payload or {}
                    subtype = "hazardous" if p.get("hazardous") else "normal"
                    title = (
                        f"NEO {p.get('name') or ''} close approach".strip()
                        or None
                    )
                    miss_km = p.get("miss_km")
                    description = (
                        f"Miss distance: {miss_km:.0f} km"
                        if isinstance(miss_km, (int, float)) else None
                    )
                    props_json = json.dumps(_neo_event_properties(ev))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'neo_close_approach', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'space'), COALESCE($11, 4320))
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
        _neo_log.warning(
            f"write_neo_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
