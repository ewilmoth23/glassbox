"""
NOAA SWPC space-weather writer — P3-H Phase 3 extraction #14.

One writer: `write_space_weather_events`. Persists NOAA SWPC alerts.
layer='space_weather' AND kind='swpc_alert' (defensive: DONKI shares
the layer but uses kind='donki_*' and is handled by writers/donki.py).
event_type='swpc_alert', subtype = SWPC kind (geomagnetic_kindex etc.).

Re-issues of the same product_id get distinct issue_datetime → distinct
deterministic UUID → ON CONFLICT DO NOTHING is safe.

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


_swpc_log = logging.getLogger("writers.swpc")


def _swpc_event_properties(event: GlassboxEvent) -> dict:
    """Map NoaaSwpcIngester payload → event.properties."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in (
        "product_id", "kind", "alert_kind", "level", "headline",
        "message", "_attribution",
    ):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_space_weather_events(events: List[GlassboxEvent]) -> int:
    """Persist NOAA SWPC alerts to the `event` hypertable.

    Filters on layer='space_weather' AND kind='swpc_alert' so we never
    accidentally write NASA DONKI events (which share the layer but use
    kind='donki_*' — and aren't dual-write-wired in v1.0).

    Dedup: SWPC re-issues the same product_id when conditions persist.
    Each issuance has a distinct issue_datetime, which we include in the
    external_id, so the deterministic UUID differs per re-issue. Use
    ON CONFLICT DO NOTHING on (id, event_time) PK.

    Returns count of NEW rows inserted.
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
                    if ev.kind != "swpc_alert":
                        # Defensive: don't catch DONKI (kind='donki_*').
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"swpc_alert:{ev.external_id}",
                    )
                    props_json = json.dumps(_swpc_event_properties(ev))

                    p = ev.payload or {}
                    title = (p.get("headline") or "")[:500] or None
                    subtype = p.get("kind") or None  # geomagnetic_kindex etc.

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'swpc_alert', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'atmospheric'), COALESCE($11, 720))
                        ON CONFLICT (id, event_time) DO NOTHING
                        """,
                        event_id,
                        subtype,
                        ts,
                        float(ev.lng),
                        float(ev.lat),
                        float(ev.severity) if ev.severity is not None else None,
                        title,
                        p.get("message") or None,
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
        _swpc_log.warning(
            f"write_space_weather_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
