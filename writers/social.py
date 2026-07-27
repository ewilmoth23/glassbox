"""
Bluesky Jetstream social writer — P3-H Phase 3 extraction #18.

One writer: `write_social_events`. Persists OSINT-keyword posts from
Bluesky Jetstream firehose. layer='social_bluesky',
event_type='bluesky_post', subtype = first matched OSINT keyword.
Posts are unique per (did, rkey) so external_id alone yields the UUID5.

Has two single-cluster privates that stay HERE (no other writer
references them):
  - `_OSINT_HIGH_SEVERITY` — set of high-severity keywords (not used
    by other clusters; was confirmed module-private in inventory grep)
  - `_bluesky_subtype(text)` — first-keyword-matched-in-text helper

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


_bluesky_log = logging.getLogger("writers.bluesky")


_OSINT_HIGH_SEVERITY = {"explosion", "shooting", "missile", "evacuation",
                        "tornado", "hurricane"}


def _bluesky_subtype(text: str) -> str | None:
    """First OSINT keyword that matched; for filtering by topic."""
    import re as _re
    if not text:
        return None
    for kw in ("explosion", "shooting", "missile", "evacuation", "tornado",
               "hurricane", "wildfire", "earthquake", "flooding", "war",
               "protest", "riot", "outage", "blackout", "breaking", "alert",
               "fire"):
        if _re.search(rf"\b{kw}\b", text, _re.IGNORECASE):
            return kw
    return None


def _bluesky_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in ("did", "rkey", "text", "lang", "_attribution"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_social_events(events: List[GlassboxEvent]) -> int:
    """Persist Bluesky Jetstream posts to the `event` hypertable.

    layer='social_bluesky' → event_type='bluesky_post'. Each post is unique
    per (did, rkey) so external_id alone yields a deterministic UUID5.
    Idempotent across cycles via ON CONFLICT DO NOTHING.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "social_bluesky":
                        continue
                    if not ev.external_id:
                        continue

                    ts = _parse_ts(ev.ts)
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"bluesky_post:{ev.external_id}",
                    )
                    p = ev.payload or {}
                    text = p.get("text") or ""
                    subtype = _bluesky_subtype(text)
                    title = (text[:200]) or None
                    description = p.get("did") or None
                    props_json = json.dumps(_bluesky_event_properties(ev))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min)
                        VALUES
                            ($1::uuid, 'bluesky_post', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'social'), COALESCE($11, 120))
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
        _bluesky_log.warning(
            f"write_social_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
