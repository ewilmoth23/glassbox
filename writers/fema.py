"""
FEMA OpenFEMA Disaster Declarations writer — P3-H Phase 3 extraction #8.

One writer: `write_fema_events`. Persists FEMA disaster declarations.
layer='fema_declarations', event_type='fema_declaration', subtype =
declaration_type ('dr'/'em'/'fm', lowercased). UUID5 stable on
external_id (which encodes femaDeclarationString) so re-emits dedupe to 0.

NOTE: like sec_filing, write_fema_events does NOT invoke `_with_confidence`
in the writer body — confidence coverage gap from before refactor scope.
(`_LAYER_TO_PLATFORM` maps 'fema_declarations' → 'manual', but
_fema_event_properties never calls _with_confidence.) Pre-existing
behavior preserved.

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


_fema_log = logging.getLogger("writers.openfema")


def _fema_event_properties(event: GlassboxEvent) -> dict:
    """Stable property whitelist for FEMA disaster declarations."""
    p = event.payload or {}
    out = {
        "_attribution": "Disaster declarations: FEMA",
    }
    for key in ("disaster_number", "fema_declaration", "state",
                "declaration_type", "incident_type", "declaration_title",
                "declaration_date", "incident_begin_date", "incident_end_date",
                "designated_area_count",
                "ih_program", "ia_program", "pa_program", "hm_program",
                "fema_region"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_fema_events(events: List[GlassboxEvent]) -> int:
    """Persist FEMA disaster declarations to the `event` hypertable.

    Filters layer='fema_declarations'. UUID5 stable on external_id
    (which encodes femaDeclarationString) so re-emits dedupe to 0.
    A declaration's state can change (e.g., area count rising as more
    counties join); subsequent emits with the same fema_id update no
    rows but keep the original event_time fixed.

    Returns count of NEW rows inserted.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "fema_declarations":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"openfema:{ev.external_id}",
                    )
                    props_json = json.dumps(_fema_event_properties(ev))
                    p = ev.payload or {}
                    incident = (p.get("incident_type") or "Disaster")
                    title = (
                        f"{incident} — "
                        f"{p.get('declaration_title') or p.get('fema_declaration') or '?'}"
                        + (f" ({p.get('state')})" if p.get("state") else "")
                    )[:500]
                    description = (
                        f"FEMA {p.get('declaration_type') or '?'} declaration "
                        f"{p.get('fema_declaration') or '?'} — "
                        f"{p.get('designated_area_count', 0)} designated area"
                        f"{'s' if (p.get('designated_area_count') or 0) != 1 else ''}; "
                        f"programs: "
                        f"{'IH ' if p.get('ih_program') else ''}"
                        f"{'IA ' if p.get('ia_program') else ''}"
                        f"{'PA ' if p.get('pa_program') else ''}"
                        f"{'HM ' if p.get('hm_program') else ''}"
                    ).strip()[:1000]

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, severity_for_market,
                             title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'fema_declaration', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9, $10::jsonb,
                             COALESCE($11, 'atmospheric'), COALESCE($12, 10080))
                        ON CONFLICT (id, event_time) DO NOTHING
                        """,
                        event_id,
                        (p.get("declaration_type") or "DR").lower(),
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
        _fema_log.warning(
            f"write_fema_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
