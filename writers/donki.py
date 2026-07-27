"""
NASA DONKI space-weather writer — P3-H Phase 3 extraction #4.

One writer: `write_donki_events`. Persists FLR/CME/GST events.
layer='space_weather' AND kind='watch' (defensive — SWPC shares the
layer but uses kind='swpc_alert'; the kind-filter prevents collision
with write_space_weather_events). event_type='donki', subtype =
sub-event-type ('FLR'/'CME'/'GST'). No geo (sentinel 0,0).

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


_donki_log = logging.getLogger("writers.donki")


def _donki_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in ("event_type", "title", "link", "class", "speed_km_s",
                "kp_max", "source_location", "_attribution"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_donki_events(events: List[GlassboxEvent]) -> int:
    """Persist NASA DONKI events (FLR/CME/GST) to the `event` hypertable.

    layer='space_weather' AND kind='watch' (defensive, to skip SWPC's
    kind='swpc_alert'). event_type='donki'; subtype = sub-event-type
    (FLR/CME/GST). No geo coords — sentinel (0,0).
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "space_weather":
                        continue
                    if ev.kind != "watch":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"donki:{ev.external_id}",
                    )
                    p = ev.payload or {}
                    subtype = p.get("event_type") or None
                    title = p.get("title") or None
                    description = p.get("source_location") or None
                    props_json = json.dumps(_donki_event_properties(ev))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'donki', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'space'), COALESCE($11, 720))
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
        _donki_log.warning(
            f"write_donki_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
