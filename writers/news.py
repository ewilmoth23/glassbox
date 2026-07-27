"""
GDELT topical news writer — P3-H Phase 3 extraction #19.

One writer: `write_news_events`. Persists GDELT topical (per-topic
keyword-matched) news events. layer='news', event_type='gdelt_topical',
subtype = matched topic ('terrorism', 'cyber_attack', etc.). Uses
`_maybe_embed` for title+country semantic vectors.

WHERE NOT EXISTS dedup: GDELTTopicalIngester sets ts=NOW() each cycle
(not the article's publish time), so PK (id, event_time) ON CONFLICT
would miss re-emits with different cycle timestamps. The pre-check
on `id` alone uses the leading PK column for efficiency.

Sibling of writers/newsdata.py (#17) and writers/gdelt_bulk.py (#20)
— all three feed the 'news' layer with different external_id prefixes.

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


_news_log = logging.getLogger("writers.news")


def _news_event_properties(event: GlassboxEvent) -> dict:
    """Map GDELTTopicalIngester.normalize() payload → event.properties.
    Includes external_id so dedup queries can find rows by GDELT id."""
    p = event.payload or {}
    out = {
        "external_id": event.external_id,
    }
    for key in (
        "topic", "topics_matched", "headline", "url", "country",
        "language", "domain_name", "social_image", "mentions",
    ):
        if key in p:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_news_events(events: List[GlassboxEvent]) -> int:
    """Persist GDELT topical news events to the `event` hypertable.

    layer='news' (from GDELTTopicalIngester) → event_type='gdelt_topical'.
    event_subtype carries the matched topic ('terrorism', 'cyber_attack',
    etc.) so the same query that fetches all news can also filter by topic.

    Embeddings (event.embedding VECTOR(384)) are NOT populated here —
    that's Phase 4 (sentence-transformers integration). The column accepts
    NULL so this writer leaves it for later.

    Returns count of NEW rows inserted (re-running with the same external_id
    returns 0 — deterministic UUID + ON CONFLICT DO NOTHING).
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

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"gdelt_topical:{ev.external_id}",
                    )
                    props_json = json.dumps(_news_event_properties(ev))

                    p = ev.payload or {}
                    title = (p.get("headline") or "")[:500] or None
                    description = p.get("country") or None
                    subtype = p.get("topic") or None
                    embedding_lit = _maybe_embed(title, description)

                    # Dedup approach for news: GDELTTopicalIngester sets
                    # ts=NOW() each cycle (not the article's publish time),
                    # so a recurring re-emit of the same article would be
                    # different (id, event_time) tuples — the PK ON CONFLICT
                    # would miss and rows would accumulate. Use a WHERE
                    # NOT EXISTS pre-check on `id` alone — efficient since
                    # the PK has id as its leading column. The window
                    # `event_time >= NOW() - 30 days` lets TimescaleDB skip
                    # ancient chunks for the existence check.
                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min, embedding)
                        SELECT
                            $1::uuid, 'gdelt_topical', $2, $3,
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
        _news_log.warning(
            f"write_news_events failed after {written} events: {type(e).__name__}: {e}"
        )
        return written

    return written
