"""
NewsData.io writer — P3-H Phase 3 extraction #17.

One writer: `write_newsdata_events`. Persists NewsData.io articles to
the event hypertable. layer='news' is shared with GDELT topical, so we
filter on external_id prefix 'newsdata:' to avoid colliding with
write_news_events. event_type='newsdata', subtype = first article
category. Uses `_maybe_embed` for title+description semantic vectors.

Dedup via WHERE NOT EXISTS (ts may differ across re-emit cycles even
for same article_id).

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


_newsdata_log = logging.getLogger("writers.newsdata")


def _newsdata_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in ("title", "description", "url", "language", "country",
                "categories", "source_name", "source_url", "image_url",
                "_attribution"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_newsdata_events(events: List[GlassboxEvent]) -> int:
    """Persist NewsData.io articles to the `event` hypertable.

    Filters on layer='news' AND external_id starting with 'newsdata:' so
    we don't collide with GDELT topical (handled by write_news_events).
    event_type='newsdata'; subtype = first article category.
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
                    if not ev.external_id or not ev.external_id.startswith("newsdata:"):
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"newsdata:{ev.external_id}",
                    )
                    p = ev.payload or {}
                    title = (p.get("title") or "")[:500] or None
                    description = (p.get("description") or "") or None
                    cats = p.get("categories") or []
                    subtype = (cats[0] if cats else None)
                    props_json = json.dumps(_newsdata_event_properties(ev))
                    embedding_lit = _maybe_embed(title, description)

                    # Same WHERE NOT EXISTS approach as gdelt_topical:
                    # ts may differ across re-emit cycles even for same
                    # article_id, so PK ON CONFLICT alone misses.
                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min, embedding)
                        SELECT
                            $1::uuid, 'newsdata', $2, $3,
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
        _newsdata_log.warning(
            f"write_newsdata_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
