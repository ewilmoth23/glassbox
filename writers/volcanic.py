"""
USGS Volcano Hazards Program writer — P3-H Phase 3 extraction #7.

One writer: `write_volcanic_events`. Persists USGS VHP elevated-volcano
alerts. layer='volcanic_activity', event_type='volcanic_alert',
subtype = alert level (lowercased: 'normal'/'advisory'/'watch'/'warning').
Per-notice idempotency: same notice_identifier within the same volcano
produces a stable event UUID so re-runs are no-ops; alert-level changes
produce new rows (timeline preserved).

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


_volcano_log = logging.getLogger("writers.usgs_volcano")


def _volcano_event_properties(event: GlassboxEvent) -> dict:
    """Stable property whitelist for volcanic alerts."""
    p = event.payload or {}
    out = {
        "_attribution": "Volcanic activity: USGS VHP",
    }
    for key in ("volcano_name", "vnum", "alert_level", "color_code",
                "observatory", "observatory_abbr",
                "notice_identifier", "notice_url"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_volcanic_events(events: List[GlassboxEvent]) -> int:
    """Persist USGS VHP elevated-volcano alerts to the `event` hypertable.

    Filters on layer='volcanic_activity'. Per-notice idempotency: same
    notice_identifier within the same volcano produces a stable event UUID
    so re-runs are no-ops. Different notices for the same volcano (alert
    level changing) produce new rows so the timeline is preserved.

    Returns count of NEW rows inserted.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "volcanic_activity":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    # external_id is already keyed by (vnum, notice_id, ts) in
                    # the ingester so a stable UUID5 is sufficient.
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"usgs_volcano:{ev.external_id}",
                    )
                    props_json = json.dumps(_volcano_event_properties(ev))
                    p = ev.payload or {}
                    color = (p.get("color_code") or "?").upper()
                    level = (p.get("alert_level") or "?").upper()
                    title = (
                        f"{p.get('volcano_name') or 'Volcano'} — "
                        f"{level}/{color}"
                    )[:500]
                    description = (
                        f"USGS {p.get('observatory_abbr') or 'VHP'} alert "
                        f"for {p.get('volcano_name') or 'volcano'}: "
                        f"alert level {level}, aviation color {color}. "
                        f"Source: {p.get('notice_url') or 'USGS VHP'}"
                    )[:1000]

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, severity_for_market,
                             title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'volcanic_alert', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9, $10::jsonb,
                             COALESCE($11, 'atmospheric'), COALESCE($12, 1440))
                        ON CONFLICT (id, event_time) DO NOTHING
                        """,
                        event_id,
                        level.lower(),
                        ts,
                        float(ev.lng),
                        float(ev.lat),
                        float(ev.severity) if ev.severity is not None else None,
                        float(ev.severity_for_market) if ev.severity_for_market is not None else None,
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
        _volcano_log.warning(
            f"write_volcanic_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
