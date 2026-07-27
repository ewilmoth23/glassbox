"""
Spamhaus DROP/EDROP writer — P2-A Phase 1 MVP (cyber-attack data layers).

One writer: `write_spamhaus_drop_events`. Persists hijacked / criminal
IP block list entries.

layer='cyber_spamhaus_drop', event_type='spamhaus_block_entry',
subtype = list name ('DROP' / 'EDROP'). No embedding (block-list
entries have no rich text — CIDR + SBL ID + list name carry the
useful signal). Sentinel coords (non-geo).

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
from writers._shared import (
    _EVENT_UUID_NAMESPACE,
    _parse_ts,
    _with_confidence,
)


_spam_log = logging.getLogger("writers.spamhaus_drop")


_SPAMHAUS_PROPERTY_KEYS = (
    "cidr",
    "sbl_id",
    "list_name",
    "title",
    "link",
    "_attribution",
)


def _spamhaus_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in _SPAMHAUS_PROPERTY_KEYS:
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_spamhaus_drop_events(events: List[GlassboxEvent]) -> int:
    """Persist Spamhaus DROP/EDROP entries to the `event` hypertable.

    layer='cyber_spamhaus_drop'. event_type='spamhaus_block_entry';
    subtype = list name (DROP / EDROP). No geo (sentinel 0,0).
    Idempotent per SBL id via the deterministic UUID5 derivation.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "cyber_spamhaus_drop":
                        continue
                    if not ev.external_id:
                        continue

                    p = ev.payload or {}
                    ts = _parse_ts(ev.ts)
                    sbl_id = p.get("sbl_id") or ev.external_id
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"spamhaus_block_entry:{sbl_id}",
                    )
                    subtype = (p.get("list_name") or "")[:32] or None
                    title = (p.get("title") or "")[:500] or None
                    description = (p.get("cidr") or "")[:120] or None
                    props_json = json.dumps(_spamhaus_event_properties(ev))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'spamhaus_block_entry', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'cyber'), COALESCE($11, 43200))
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
        _spam_log.warning(
            f"write_spamhaus_drop_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
