"""
CISA KEV writer — P2-A Phase 1 MVP (cyber-attack data layers).

One writer: `write_cisa_kev_events`. Persists CISA-published
known-exploited-vulnerability disclosures.

layer='cyber_kev', event_type='kev_disclosure', subtype = vendorProject
(cluster by software publisher: 'Microsoft', 'Apache', 'Cisco', ...).
Uses `_maybe_embed` on vulnerability_name + short_description for
semantic vector retrieval ("CVEs similar to this one"). Sentinel coords
(non-geo) — KEV entries aren't geographically positioned.

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


_kev_log = logging.getLogger("writers.cisa_kev")


# Properties whitelisted onto the row's `properties` jsonb. Everything
# downstream consumers need surfaced for filter/sort/render lives here.
_KEV_PROPERTY_KEYS = (
    "cve_id",
    "vendor_project",
    "product",
    "vulnerability_name",
    "short_description",
    "required_action",
    "date_added",
    "due_date",
    "known_ransomware_campaign_use",
    "notes",
    "cwes",
    "title",
    "link",
    "_attribution",
)


def _kev_event_properties(event: GlassboxEvent) -> dict:
    p = event.payload or {}
    out = {"external_id": event.external_id}
    for key in _KEV_PROPERTY_KEYS:
        if key in p and p[key] is not None:
            out[key] = p[key]
    return _with_confidence(out, event.layer)


async def write_cisa_kev_events(events: List[GlassboxEvent]) -> int:
    """Persist CISA KEV disclosures to the `event` hypertable.

    layer='cyber_kev'. event_type='kev_disclosure'; subtype = vendor
    (Microsoft, Apache, Cisco, ...). No geo (sentinel 0,0).
    Idempotent per cve_id via the deterministic UUID5 derivation.
    """
    if not events:
        return 0

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "cyber_kev":
                        continue
                    if not ev.external_id:
                        continue

                    p = ev.payload or {}
                    ts = _parse_ts(ev.ts)
                    cve_id = p.get("cve_id") or ev.external_id
                    event_id = uuid.uuid5(
                        _EVENT_UUID_NAMESPACE,
                        f"kev_disclosure:{cve_id}",
                    )
                    subtype = (p.get("vendor_project") or "")[:120] or None
                    title = (p.get("title") or p.get("vulnerability_name") or "")[:500] or None
                    description = (p.get("short_description") or "")[:1000] or None
                    props_json = json.dumps(_kev_event_properties(ev))
                    # Embed the human-readable text — vulnerability name + short
                    # description carry the semantic signal. Vendor + product
                    # are also useful but live in payload (semi-structured).
                    embedding_lit = _maybe_embed(title, description, p.get("vendor_project"))

                    result = await conn.execute(
                        """
                        INSERT INTO event
                            (id, event_type, event_subtype, event_time,
                             geom, severity, title, description, properties,
                             domain, decay_half_life_min, embedding)
                        VALUES
                            ($1::uuid, 'kev_disclosure', $2, $3,
                             ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
                             $6, $7, $8, $9::jsonb,
                             COALESCE($10, 'cyber'), COALESCE($11, 43200),
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
        _kev_log.warning(
            f"write_cisa_kev_events failed after {written} events: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
