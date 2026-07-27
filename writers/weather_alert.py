"""
NOAA NWS weather-alert writer — P3-H Phase 3 extraction #13.

One writer: `write_weather_alert_events`. Persists NOAA NWS weather
alerts to the event hypertable. layer='weather_alerts',
event_type='noaa_alert', subtype = alert kind ('Tornado Warning',
'Flash Flood Warning', 'Hurricane Warning', etc.).

Dedup uses WHERE NOT EXISTS (vs ON CONFLICT) — NOAA's CAP `sent`
timestamp is usually stable across re-emits but the ingester's
`or now` fallback means we can't rely on (id, event_time) PK
uniqueness. The pre-check on `id` alone uses the leading column of
the PK index.

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


_weather_log = logging.getLogger("writers.weather")


def _weather_alert_event_properties(event: GlassboxEvent) -> dict:
    """Map NoaaNwsIngester.normalize() payload → event.properties.
    Includes external_id so dedup queries can find rows by NWS alert id."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in (
        "event", "headline", "severity_raw", "urgency", "certainty",
        "area_desc", "sender_name", "effective", "expires", "instruction",
    ):
        if key in p:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_weather_alert_events(events: List[GlassboxEvent]) -> int:
    """Persist NOAA NWS weather alerts to the `event` hypertable.

    layer='weather_alerts' (from NoaaNwsIngester) → event_type='noaa_alert'.
    event_subtype carries the alert kind ('Tornado Warning', 'Flash Flood
    Warning', 'Hurricane Warning', etc.) so queries can filter.

    Dedup uses WHERE NOT EXISTS (same as `write_news_events`). NOAA's CAP
    `sent` timestamp is usually stable across re-emits of the same alert,
    but the ingester's `or now` fallback means we can't rely on
    (id, event_time) PK uniqueness. The pre-check on `id` alone is robust
    and uses the leading column of the PK index.

    Returns count of NEW rows inserted.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "weather_alerts":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"noaa_alert:{ev.external_id}",
                    )
                    props_json = json.dumps(_weather_alert_event_properties(ev))

                    p = ev.payload or {}
                    title = (p.get("headline") or "")[:500] or None
                    description = p.get("area_desc") or None
                    subtype = p.get("event") or None

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        SELECT
                            $1::uuid, 'noaa_alert', $2, $3,
                            ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                            $6, $7, $8, $9::jsonb,
                            COALESCE($10, 'geo'), COALESCE($11, 30)
                        WHERE NOT EXISTS (
                            SELECT 1 FROM event
                            WHERE id = $1::uuid
                              AND event_time >= NOW() - INTERVAL '30 days'
                        )
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
        _weather_log.warning(
            f"write_weather_alert_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
