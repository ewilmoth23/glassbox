"""
GDELT V2 bulk-CSV writer — P3-H Phase 3 extraction #20.

One writer: `write_gdelt_bulk_events`. Persists post-prefilter
GDELT V2 events to the event hypertable. layer='news', event_type=
'gdelt_bulk', subtype = HANDOFF_02 CAMEO subcategory
('armed_conflict.airstrike', 'economic.sanctions', etc.). Uses
`_maybe_embed` on synthesized title+country+location.

Third writer on the shared 'news' layer (after news/gdelt_topical
and newsdata) — tag-discriminates by checking `cameo_subcategory` or
`cameo_code` present in payload, only those rows are bulk-sourced.

WHERE NOT EXISTS dedup (same pattern as news/newsdata for the same
re-emit reason).

Imports from `writers._shared` for universal helpers + text-embed;
`db.acquire_write` for the write pool. No cross-cluster imports.
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


_gdelt_bulk_log = logging.getLogger("writers.gdelt_bulk")


def _gdelt_bulk_event_properties(event: GlassboxEvent) -> dict:
    """Map GdeltBulkIngester.normalize() payload → event.properties.
    Carries the prefilter priority + cameo_code so downstream queries can
    rank by editorial signal and trace back to the originating CAMEO row."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in (
        "headline", "url", "country", "actor1", "actor2",
        "cameo_code", "cameo_subcategory", "goldstein", "flags",
        "prefilter_priority", "prefilter_rules_version",
        "duplicate_of",
    ):
        if key in p:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_gdelt_bulk_events(events: List[GlassboxEvent]) -> int:
    """Persist GDELT V2 bulk-CSV events (post-prefilter) to the `event`
    hypertable.

    layer='news' (from GdeltBulkIngester) → event_type='gdelt_bulk'.
    event_subtype carries the HANDOFF_02 CAMEO subcategory
    ('armed_conflict.airstrike', 'economic.sanctions', etc.) so the
    same query that fetches all news can also filter by Glassbox
    semantic taxonomy.

    Embeddings are computed from the synthesized title + country to
    feed the Phase 4a similarity index.

    Returns count of NEW rows inserted; re-running with the same
    GDELT GLOBALEVENTID returns 0 (deterministic UUID + WHERE-NOT-EXISTS
    pre-check, mirrors the news writer's pattern).
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "news":
                        continue
                    if not ev.external_id:
                        continue
                    p = ev.payload or {}
                    # The bulk writer should ONLY handle bulk-sourced
                    # events. The empire still has the GDELTTopical
                    # write_news_events writer; both ingesters happen
                    # to share layer='news' so we tag-discriminate here.
                    if (p.get("cameo_subcategory") is None
                            and p.get("cameo_code") is None):
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"gdelt_bulk:{ev.external_id}",
                    )
                    props_json = json.dumps(_gdelt_bulk_event_properties(ev))

                    # Synthesize a title — GDELT V2 Events doesn't carry
                    # article headlines (those live in Mentions / GKG).
                    # "<subcategory> @ <location>" reads tolerably in
                    # Mission Control without us having to download a
                    # second CSV per cycle.
                    location = (p.get("headline") or "").strip()
                    subcat = (p.get("cameo_subcategory") or "unknown.unknown")
                    title = f"{subcat} @ {location}" if location else subcat
                    title = title[:500] or None
                    description = p.get("country") or None
                    embedding_lit = _maybe_embed(title, description, location)

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min, embedding)
                        SELECT
                            $1::uuid, 'gdelt_bulk', $2, $3,
                            ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                            $6, $7, $8, $9::jsonb,
                            COALESCE($10, 'geo'), COALESCE($11, 720),
                            $12::vector
                        WHERE NOT EXISTS (
                            SELECT 1 FROM event
                            WHERE id = $1::uuid
                              AND event_time >= NOW() - INTERVAL '30 days'
                        )
                        """,
                        event_id,
                        subcat,
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
        _gdelt_bulk_log.warning(
            f"write_gdelt_bulk_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
