"""
Satellite writer — P3-H Phase 3 extraction #23.

Third of the four ENTITY+POSITION writers. entity_type='satellite',
canonical_id_type='norad'. layer='satellites' (from CelesTrak +
server-side SGP4 propagation).

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


_satellite_log = logging.getLogger("writers.satellite")


def _satellite_entity_properties(event: GlassboxEvent) -> dict:
    """Per-satellite stable fields. NORAD id, name, classification group
    (stations / starlink / gps-ops / etc.) don't change per snapshot."""
    p = event.payload or {}
    out = {}
    for key in ("name", "group", "norad", "international_designator"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return out


def _satellite_position_properties(event: GlassboxEvent) -> dict:
    """Per-snapshot fields. None for satellites today — orbital geometry is
    recomputed each cycle and lives on position_track via altitude_m + velocity_ms."""
    return {
        "severity": event.severity,
    }


async def write_satellite_events(events: List[GlassboxEvent]) -> int:
    """Dual-write a batch of satellite position events.

    Same shape as `write_aircraft_events` / `write_vessel_events` but writes
    entity_type='satellite' with canonical_id_type='norad'. NORAD catalog
    numbers are globally unique per CelesTrak's registry.

    Includes the Phase 2.5 denormalized current_geom + current_position_time
    columns with GREATEST/CASE guards against out-of-order arrival.

    Returns count of events successfully persisted.
    """
    if not events:
        return 0

    # P1-B: deterministic batch ordering across writers — see _sort_batch_for_upsert.
    events = _sort_batch_for_upsert(events)

    written = 0
    try:
        async with acquire_write() as conn:
            async with conn.transaction():
                for ev in events:
                    if ev.layer != "satellites":
                        continue
                    if not ev.external_id:
                        continue

                    entity_props_json = json.dumps(_satellite_entity_properties(ev))
                    position_props_json = json.dumps(_satellite_position_properties(ev))
                    ts = _parse_ts(ev.ts)
                    raw_name = (ev.payload or {}).get("name")
                    if raw_name is None or raw_name == "":
                        display_name = None
                    elif isinstance(raw_name, str):
                        display_name = raw_name.strip() or None
                    else:
                        display_name = str(raw_name).strip() or None

                    entity_id = await conn.fetchval(
                        """
                        INSERT INTO entity
                            (entity_type, canonical_id_type, canonical_id,
                             display_name, properties, last_seen, updated_at,
                             current_geom, current_position_time,
                             current_velocity_ms, current_altitude_m)
                        VALUES
                            ('satellite', 'norad', $1, $2, $3::jsonb, $4, $4,
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
                            current_altitude_m =
                                CASE WHEN EXCLUDED.current_position_time > entity.current_position_time
                                     OR entity.current_position_time IS NULL
                                     THEN EXCLUDED.current_altitude_m ELSE entity.current_altitude_m END
                        RETURNING id
                        """,
                        ev.external_id,
                        display_name,
                        entity_props_json,
                        ts,
                        float(ev.lng),
                        float(ev.lat),
                        float(ev.velocity_ms) if ev.velocity_ms is not None else None,
                        float(ev.altitude_m) if ev.altitude_m is not None else None,
                    )

                    await conn.execute(
                        """
                        INSERT INTO position_track
                            (time, entity_id, geom, altitude_m, velocity_ms,
                             heading_deg, properties)
                        VALUES
                            ($1, $2,
                             ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                             $5, $6, $7, $8::jsonb)
                        """,
                        ts,
                        entity_id,
                        float(ev.lng),
                        float(ev.lat),
                        ev.altitude_m,
                        ev.velocity_ms,
                        ev.heading_deg,
                        position_props_json,
                    )
                    written += 1
    except Exception as e:
        _satellite_log.warning(
            f"write_satellite_events failed after {written} events: {type(e).__name__}: {e}"
        )
        return written

    return written
