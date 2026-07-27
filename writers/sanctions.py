"""
Sanctions writer — P3-H Phase 3 extraction #24 (FINAL).

Fourth and final ENTITY+POSITION writer. Different from the other three
in that sanction entries are durable identity records, not point-in-time
positions — no current_geom, no position_track INSERT. Cross-domain SQL
joins surface live AIS/ADS-B-fed vessels matching SDN entries → red-flag
pin in the UI.

One writer: `write_sanction_entities` (note: lacks the `_events` suffix
that the other 23 writers carry — this is the only writer without it,
preserved from the original ingester contract). Generalized 2026-05-08
to handle multiple sanctioning authorities (OFAC, EU CFSP, UK OFSI).

NEW-row detection via `RETURNING (xmax = 0) AS is_new` — Postgres sets
xmax to non-zero on UPDATE, zero on fresh INSERT. Re-emits (every poll
cycle re-parses the full XML) update last_seen but don't count as new.

Imports from `writers._shared` for universal helpers including
`_sort_batch_for_upsert`; `db.acquire_write` for the write pool.
"""

from __future__ import annotations

import json
import logging
from typing import List

from ingesters.base import GlassboxEvent
from db import acquire_write
from writers._shared import _parse_ts, _sort_batch_for_upsert


_sanction_log = logging.getLogger("writers.sanction")


def _sanction_entity_properties(event: GlassboxEvent) -> dict:
    """Stable per-sanction-entry fields preserved on the entity row.

    Generalized 2026-05-08 to handle multiple sanctioning authorities (OFAC,
    EU CFSP, UK OFSI, …). The ingester sets payload.sanctioning_authority and
    payload.canonical_id_type; this helper preserves them through to the row.
    Backwards-compatible: a payload missing those keys is treated as OFAC.
    """
    p = event.payload or {}
    authority = p.get("sanctioning_authority") or "US Treasury OFAC"
    cid_type = p.get("canonical_id_type") or "ofac_sdn_id"
    out = {
        "sanction_external_id": event.external_id,
        # Legacy alias — older callers grep for ofac_sdn_external_id.
        "ofac_sdn_external_id": event.external_id if cid_type == "ofac_sdn_id" else None,
        "fcra_safe": False,  # CRITICAL — never use sanctions data for FCRA decisions
    }
    if out["ofac_sdn_external_id"] is None:
        del out["ofac_sdn_external_id"]
    for key in ("type", "display_name", "_attribution"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    # IMO + MMSI + aircraft serial — the precision keys for cross-domain
    # matching against live AIS / ADS-B feeds. Required for downstream
    # algorithms (sanctions_match) to do exact-match preference over the
    # false-positive-prone fuzzy name match.
    for key in ("imo", "mmsi", "aircraft_serial"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    # Phase 4c (2026-05-09): non-primary aliases (AKAs / romanizations /
    # tradename variants) extracted from OFAC SDN. The Splink ER pipeline
    # reads alt_names to expand the candidate match set.
    if isinstance(p.get("alt_names"), list) and p["alt_names"]:
        out["alt_names"] = list(p["alt_names"])
    # Authority-specific descriptive metadata (regime/program code, flag,
    # vessel type, list reference). Useful for the sanctions_match
    # algorithm + UI surfacing without exposing PII.
    for key in ("regime", "flag", "ship_type", "uk_ref", "ofac_program",
                "ofac_programs", "ofac_legal_basis_refs",
                "programme", "eu_ref"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    out["sanctioning_authority"] = authority
    return out


async def write_sanction_entities(events: List[GlassboxEvent]) -> int:
    """Persist sanctions-list entries to the entity table.

    Generalized 2026-05-08 to support multiple sanctioning authorities
    (OFAC, EU CFSP, UK OFSI, …). Each ingester sets payload.canonical_id_type
    (e.g. 'ofac_sdn_id', 'eu_cfsp_id', 'uk_ofsi_id'); the writer reads it.
    Defaults to 'ofac_sdn_id' so OFAC ingester behaviour is unchanged.

    Maps sanction entries (layer='sanctions', kind='index') to entity rows:
      payload.type='vessel'   → entity_type='sanctioned_vessel'
      payload.type='aircraft' → entity_type='sanctioned_aircraft'
      ev.external_id          → canonical_id (prefixed by ingester, e.g.
                                'ofac_sdn:vessel:...', 'uk_ofsi:vessel:...')
      payload.canonical_id_type → canonical_id_type

    Re-emits (every poll cycle the ingester re-parses the whole XML) are
    idempotent: ON CONFLICT (entity_type, canonical_id_type, canonical_id)
    updates last_seen but does NOT count as a new row. Detection via
    `RETURNING (xmax = 0) AS is_new` — Postgres sets xmax to non-zero on
    UPDATE, zero on fresh INSERT.

    Returns count of NEWLY inserted rows (always 0 after the first cycle
    per source).
    """
    if not events:
        return 0

    # P1-B: deterministic batch ordering across writers — see _sort_batch_for_upsert.
    # Defensive: sanctions runs from one source per pass today (OFAC then UK
    # then EU, sequentially), so cross-writer overlap is theoretical, but
    # within-batch ordering is free and the contract should be consistent
    # across every entity-table UPSERT writer.
    events = _sort_batch_for_upsert(events)

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "sanctions":
                        continue
                    if not ev.external_id:
                        continue

                    p = ev.payload or {}
                    sdn_type = p.get("type")
                    if sdn_type not in ("vessel", "aircraft"):
                        # Ingester filters before us, but be defensive.
                        continue

                    entity_type = "sanctioned_vessel" if sdn_type == "vessel" else "sanctioned_aircraft"
                    display_name = (p.get("display_name") or "").strip() or None
                    cid_type = p.get("canonical_id_type") or "ofac_sdn_id"
                    props_json = json.dumps(_sanction_entity_properties(ev))
                    ts = _parse_ts(ev.ts)

                    is_new = await conn.fetchval(
                        """
                        INSERT INTO entity
                            (entity_type, canonical_id_type, canonical_id,
                             display_name, properties, last_seen, updated_at)
                        VALUES
                            ($1, $2, $3, $4, $5::jsonb, $6, $6)
                        ON CONFLICT (entity_type, canonical_id_type, canonical_id)
                        DO UPDATE SET
                            display_name = COALESCE(EXCLUDED.display_name, entity.display_name),
                            properties   = entity.properties || EXCLUDED.properties,
                            last_seen    = GREATEST(entity.last_seen, EXCLUDED.last_seen),
                            updated_at   = EXCLUDED.updated_at
                        RETURNING (xmax = 0) AS is_new
                        """,
                        entity_type,
                        cid_type,
                        ev.external_id,
                        display_name,
                        props_json,
                        ts,
                    )
                    if is_new:
                        written += 1
    except Exception as e:
        _sanction_log.warning(
            f"write_sanction_entities failed after {written} entries: "
            f"{type(e).__name__}: {e}"
        )
        return written

    return written
