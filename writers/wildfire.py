"""
NASA FIRMS wildfire active-fire writer — P3-H Phase 3 extraction #9.

One writer: `write_wildfire_events`. Persists NASA FIRMS active-fire
detections. layer='wildfires', event_type='nasa_firms', subtype = FIRMS
dataset name (VIIRS_SNPP_NRT, MODIS_NRT, LANDSAT_NRT, etc.) so queries
can filter by sensor. UUID5 stable per fire-pixel observation.

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


_wildfire_log = logging.getLogger("writers.wildfire")


def _wildfire_event_properties(event: GlassboxEvent) -> dict:
    """Map NasaFirmsIngester.normalize() payload → event.properties."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in (
        "dataset", "brightness_k", "confidence", "frp_mw",
        "satellite", "instrument", "daynight",
    ):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_wildfire_events(events: List[GlassboxEvent]) -> int:
    """Persist NASA FIRMS active-fire detections to the `event` hypertable.

    layer='wildfires' (from NasaFirmsIngester) → event_type='nasa_firms'.
    event_subtype carries the FIRMS dataset name (VIIRS_SNPP_NRT, MODIS_NRT,
    LANDSAT_NRT, VIIRS_NOAA20_NRT, etc.) so queries can filter by sensor.

    Uses ON CONFLICT (id, event_time) DO NOTHING — both id (deterministic
    uuid5) and event_time (parsed from acq_date+acq_time) are stable per
    fire-pixel observation, so the standard hypertable PK conflict is the
    correct dedup mechanism (matches `write_seismic_events`).

    Returns count of NEW rows inserted.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "wildfires":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"nasa_firms:{ev.external_id}",
                    )
                    props_json = json.dumps(_wildfire_event_properties(ev))

                    p = ev.payload or {}
                    dataset = p.get("dataset") or "FIRMS"
                    brightness = p.get("brightness_k")
                    confidence = p.get("confidence")
                    frp = p.get("frp_mw")
                    title = f"Active fire detection ({dataset})"
                    desc_parts = []
                    if brightness is not None:
                        desc_parts.append(f"brightness {brightness}K")
                    if confidence:
                        desc_parts.append(f"confidence {confidence}")
                    if frp is not None:
                        desc_parts.append(f"FRP {frp} MW")
                    description = ", ".join(desc_parts) if desc_parts else None
                    subtype = dataset

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1, 'nasa_firms', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'geo'), COALESCE($11, 120))
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
        _wildfire_log.warning(
            f"write_wildfire_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
