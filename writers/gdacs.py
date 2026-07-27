"""
GDACS (Global Disaster Alert and Coordination System) writer — P3-H
Phase 3 extraction #6.

One writer: `write_gdacs_events`. Persists GDACS RSS alerts to the event
hypertable. layer='gdacs', event_type='gdacs_alert', subtype = GDACS
event_type ('EQ'/'TC'/'FL'/'VO'/...). UUID5 includes episode_id so
episode updates (same event_id, new episode_id) emit distinct rows.

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


_gdacs_log = logging.getLogger("writers.gdacs")


def _gdacs_event_properties(event: GlassboxEvent) -> dict:
    """Map GdacsIngester payload → event.properties."""
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in (
        "gdacs_event_id", "gdacs_episode_id", "gdacs_event_type",
        "alert_level", "alert_score", "country", "iso3",
        "from_date", "to_date", "title", "description",
        "raw_severity_value", "raw_severity_unit",
        "affected_population_value", "affected_population_unit",
        "_attribution",
    ):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_gdacs_events(events: List[GlassboxEvent]) -> int:
    """Persist GDACS alerts to the `event` hypertable.

    External_id format: 'gdacs:{event_type}:{event_id}'. Episode updates
    (same event_id, new episode_id) get distinct UUIDs because the hash
    includes episode_id. Re-runs of the same (event_id, episode_id) at
    same ts are dedup'd via ON CONFLICT DO NOTHING.

    Returns count of NEW rows.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "gdacs":
                        continue
                    if not ev.external_id:
                        continue

                    p = ev.payload or {}
                    ts = _parse_ts(ev.ts)
                    # Include episode_id so episode updates emit new rows.
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"gdacs:{ev.external_id}:{p.get('gdacs_episode_id') or ''}",
                    )
                    props_json = json.dumps(_gdacs_event_properties(ev))

                    subtype = p.get("gdacs_event_type") or None
                    title = (p.get("title") or "")[:500] or None
                    description = (p.get("description") or "")[:1000] or None

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'gdacs_alert', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'geo'), COALESCE($11, 1440))
                        ON CONFLICT (id, event_time) DO NOTHING
                        """,
                        event_id, subtype, ts,
                        float(ev.lng), float(ev.lat),
                        float(ev.severity) if ev.severity is not None else None,
                        title, description, props_json,
                        ev.domain, ev.decay_half_life_min,
                    )
                    try:
                        if int(result.split()[-1]) == 1:
                            written += 1
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        _gdacs_log.warning(
            f"write_gdacs_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
