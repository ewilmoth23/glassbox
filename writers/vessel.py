"""
Vessel writer — P3-H Phase 3 extraction #22.

Second of the four ENTITY+POSITION writers. entity_type='vessel',
canonical_id_type='mmsi'. layer='ships' (from AISStream / Digitraffic /
BarentsWatch / DMA). This is the writer the P1-B deadlock evidence
came from — multiple AIS sources observing the same MMSI within the
same scan tick. `_sort_batch_for_upsert` guards against the
cross-writer deadlock.

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


_vessel_log = logging.getLogger("writers.vessel")


def _vessel_entity_properties(event: GlassboxEvent) -> dict:
    """Per-vessel stable fields. ship_type / dark-flag don't change per snapshot;
    cog and heading are per-position and live on position_track."""
    p = event.payload or {}
    out = {}
    for key in ("name", "ship_type", "dark", "imo", "flag_state", "callsign"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    # Always include the canonical_id-equivalent for round-trip queries
    if "mmsi" in p and p["mmsi"] is not None:
        out["mmsi"] = p["mmsi"]
    return out


def _vessel_position_properties(event: GlassboxEvent) -> dict:
    """Per-snapshot fields. cog (course-over-ground) and nav status change
    every position; ship_type doesn't."""
    p = event.payload or {}
    out = {
        "severity": event.severity,
    }
    for key in ("cog", "nav_status", "draught", "destination"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return out


async def write_vessel_events(events: List[GlassboxEvent]) -> int:
    """Dual-write a batch of vessel position events.

    Same shape as `write_aircraft_events` but writes entity_type='vessel'
    with canonical_id_type='mmsi'. Returns count of events successfully
    persisted.
    """
    if not events:
        return 0

    # P1-B: deterministic batch ordering across writers — see _sort_batch_for_upsert.
    # This is the writer the deadlock evidence came from (aisstream + digitraffic
    # + barentswatch + DMA can all see the same MMSI within the same tick).
    events = _sort_batch_for_upsert(events)

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "ships":
                        continue
                    if not ev.external_id:
                        continue

                    entity_props_json = json.dumps(_vessel_entity_properties(ev))
                    position_props_json = json.dumps(_vessel_position_properties(ev))
                    ts = _parse_ts(ev.ts)
                    # Some AIS sources emit numeric MMSI in the `name` field
                    # when the vessel hasn't reported a string name. Coerce to
                    # str so asyncpg accepts it for the TEXT column.
                    raw_name = (ev.payload or {}).get("name")
                    if raw_name is None or raw_name == "":
                        display_name = None
                    elif isinstance(raw_name, str):
                        display_name = raw_name.strip() or None
                    else:
                        display_name = str(raw_name).strip() or None

                    # UPSERT entity. See write_aircraft_events for the
                    # rationale on the GREATEST() / CASE guards on
                    # current_geom + current_position_time + last_seen —
                    # AIS ingester retries can deliver positions slightly
                    # out of order, and we never want a stale snapshot to
                    # overwrite a fresher one.
                    entity_id = await conn.fetchval(
                        """
                        INSERT INTO entity
                            (entity_type, canonical_id_type, canonical_id,
                             display_name, properties, last_seen, updated_at,
                             current_geom, current_position_time,
                             current_velocity_ms, current_heading_deg)
                        VALUES
                            ('vessel', 'mmsi', $1, $2, $3::jsonb, $4, $4,
                             ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography, $4,
                             $7, $8)
                        ON CONFLICT (entity_type, canonical_id_type, canonical_id)
                        DO UPDATE SET
                            display_name = COALESCE(EXCLUDED.display_name, entity.display_name),
                            properties   = entity.properties || EXCLUDED.properties,
                            last_seen    = GREATEST(entity.last_seen, EXCLUDED.last_seen),
                            updated_at   = EXCLUDED.updated_at,
                            current_geom =
                                CASE WHEN EXCLUDED.current_position_time > entity.current_position_time
                                     OR entity.current_position_time IS NULL
                                     THEN EXCLUDED.current_geom ELSE entity.current_geom END,
                            current_position_time =
                                GREATEST(entity.current_position_time, EXCLUDED.current_position_time),
                            current_velocity_ms =
                                CASE WHEN EXCLUDED.current_position_time > entity.current_position_time
                                     OR entity.current_position_time IS NULL
                                     THEN EXCLUDED.current_velocity_ms ELSE entity.current_velocity_ms END,
                            current_heading_deg =
                                CASE WHEN EXCLUDED.current_position_time > entity.current_position_time
                                     OR entity.current_position_time IS NULL
                                     THEN EXCLUDED.current_heading_deg ELSE entity.current_heading_deg END
                        RETURNING id
                        """,
                        ev.external_id,
                        display_name,
                        entity_props_json,
                        ts,
                        float(ev.lng),
                        float(ev.lat),
                        float(ev.velocity_ms) if ev.velocity_ms is not None else None,
                        float(ev.heading_deg) if ev.heading_deg is not None else None,
                    )

                    await conn.execute(
                        """
                        INSERT INTO position_track
                            (time, entity_id, geom, altitude_m, velocity_ms,
                             heading_deg, properties)
                        VALUES
                            ($1, $2,
                             ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                             NULL, $5, $6, $7::jsonb)
                        """,
                        ts,
                        entity_id,
                        float(ev.lng),
                        float(ev.lat),
                        ev.velocity_ms,
                        ev.heading_deg,
                        position_props_json,
                    )
                    written += 1
    except Exception as e:
        _vessel_log.warning(
            f"write_vessel_events failed after {written} events: {type(e).__name__}: {e}"
        )
        return written

    return written
