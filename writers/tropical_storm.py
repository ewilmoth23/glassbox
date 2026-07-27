"""
NOAA NHC tropical-cyclone writer — P3-H Phase 3 extraction #15.

One writer: `write_tropical_storm_events`. Persists NHC tropical-cyclone
advisories. layer='tropical_storms', event_type='tropical_storm',
subtype = class_label (tropical_depression / tropical_storm / hurricane /
post_tropical / etc.). Per-advisory event_id (ts in UUID5 input) so the
intensity/position timeline accumulates row-per-advisory.

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


_nhc_log = logging.getLogger("writers.nhc")


def _nhc_event_properties(event: GlassboxEvent) -> dict:
    """Map NhcStormsIngester payload → event.properties."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in (
        "storm_id", "name", "classification", "class_label",
        "wind_kt", "pressure_mb", "movement_dir", "movement_kt",
        "forecastTrack", "forecastCone", "publicAdvisory", "_attribution",
    ):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_tropical_storm_events(events: List[GlassboxEvent]) -> int:
    """Persist NHC tropical-cyclone advisories to the `event` hypertable.

    Filters on layer='tropical_storms'. Each storm advisory is keyed by
    (storm_id, lastUpdate) — we use the deterministic UUID so re-running
    with the same advisory is idempotent. New advisories (different
    lastUpdate) for the same storm produce new rows so the timeline of
    intensity/position changes is preserved.

    Returns count of NEW rows inserted.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "tropical_storms":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    # Per-advisory event_id: each NHC advisory at a different
                    # ts is its own row in the timeline.
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"tropical_storm:{ev.external_id}:{ev.ts}",
                    )
                    props_json = json.dumps(_nhc_event_properties(ev))

                    p = ev.payload or {}
                    subtype = p.get("class_label") or "tropical_cyclone"
                    title = (
                        f"{subtype.replace('_', ' ').title()} "
                        f"{p.get('name') or p.get('storm_id') or ''}"
                    )[:500]

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, severity_for_market,
                             title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'tropical_storm', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9, $10::jsonb,
                             COALESCE($11, 'atmospheric'), COALESCE($12, 720))
                        ON CONFLICT (id, event_time) DO NOTHING
                        """,
                        event_id,
                        subtype,
                        ts,
                        float(ev.lng),
                        float(ev.lat),
                        float(ev.severity) if ev.severity is not None else None,
                        float(ev.severity_for_market) if ev.severity_for_market is not None else None,
                        title,
                        f"NHC advisory for {p.get('name') or 'storm'} — "
                            f"{p.get('class_label', 'storm').replace('_', ' ')}, "
                            f"{p.get('wind_kt') or '?'} kt, "
                            f"{p.get('pressure_mb') or '?'} mb"[:1000],
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
        _nhc_log.warning(
            f"write_tropical_storm_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
