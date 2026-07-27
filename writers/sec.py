"""
SEC EDGAR filings writer — P3-H Phase 3 extraction #5.

One writer: `write_sec_filing_events`. First extraction that uses
`_maybe_embed` (sentence-transformers text embedding for the title) —
proves the EMBED helper path through the lift in commit `8e554a8`.
Each EDGAR filing has a unique atom-feed id; `external_id` alone yields
the UUID5. layer='securities_filings', event_type='sec_filing',
subtype = form code (8-K, 10-Q, 10-K, S-1, etc.). No geo (sentinel 0,0).

Imports from `writers._shared` for universal helpers + the text-embed
helper; `db.acquire_write` for the write pool. No cross-cluster imports.
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
    _maybe_embed,
    _parse_ts,
    _with_confidence,
)


_sec_log = logging.getLogger("writers.sec")


def _sec_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in ("form", "title", "link", "_attribution"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_sec_filing_events(events: List[GlassboxEvent]) -> int:
    """Persist SEC EDGAR filings to the `event` hypertable.

    layer='securities_filings'. event_type='sec_filing'; subtype = form code
    (8-K, 10-Q, 10-K, S-1, etc.). No geo (sentinel 0,0).
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "securities_filings":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"sec_filing:{ev.external_id}",
                    )
                    p = ev.payload or {}
                    subtype = p.get("form") or None
                    title = (p.get("title") or "")[:500] or None
                    description = p.get("link") or None
                    props_json = json.dumps(_sec_event_properties(ev))
                    # Embed title only — description is the EDGAR link URL
                    # which doesn't carry useful semantic signal.
                    embedding_lit = _maybe_embed(title)

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min, embedding)
                        VALUES
                            ($1::uuid, 'sec_filing', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'entity'), COALESCE($11, 720),
                             $12::vector)
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
                        embedding_lit,
                    )
                    try:
                        if int(result.split()[-1]) == 1:
                            written += 1
                    except (ValueError, IndexError):
                        pass
    except Exception as e:
        _sec_log.warning(
            f"write_sec_filing_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
