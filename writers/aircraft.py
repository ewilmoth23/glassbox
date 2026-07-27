"""
Aircraft writer — P3-H Phase 3 extraction #21.

First of the four ENTITY+POSITION writers (aircraft, vessel, satellite,
sanction_entities). These share a different shape from the 20 event-table
writers extracted so far:

  - UPSERT into `entity` on (entity_type, canonical_id_type, canonical_id)
  - INSERT into `position_track` for the snapshot
  - Use `_sort_batch_for_upsert` to prevent cross-writer ON-CONFLICT
    deadlocks (P1-B, 2026-05-20)

One writer: `write_aircraft_events`. entity_type='aircraft',
canonical_id_type='icao24'. layer='planes' (from OpenSky/adsb.lol/ADS-B
Exchange). Per-position track + per-entity stable callsign/military/
emergency/origin_country.

Imports from `writers._shared` for universal helpers including
`_sort_batch_for_upsert`; `db.acquire_write` for the write pool.
No cross-cluster imports.
"""

from __future__ import annotations

import json
import logging
from typing import List

from ingesters.base import GlassboxEvent
from db import acquire_write
from writers._shared import _parse_ts, _sort_batch_for_upsert


_log = logging.getLogger("writers.aircraft")


def _aircraft_entity_properties(event: GlassboxEvent) -> dict:
    """Extract the subset of payload that's stable per-entity (not per-position)."""
    p = event.payload or {}
    out = {}
    for key in ("callsign", "military", "emergency", "origin_country"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return out


def _aircraft_position_properties(event: GlassboxEvent) -> dict:
    """Extract the per-snapshot fields that belong on position_track."""
    p = event.payload or {}
    out = {
        "severity": event.severity,
    }
    for key in ("squawk", "on_ground", "time_position"):
        if key in p and p[key] is not None:
            out[key] = p[key]
    return out


async def write_aircraft_events(events: List[GlassboxEvent]) -> int:
    """Dual-write a batch of aircraft events.

    Returns count of events successfully persisted. On error returns 0 (the
    SSE broadcast already happened before this is called, so dropping the
    durable archive for one cycle is recoverable).
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
                    if ev.layer != "planes":
                        # Defensive — caller confused. Skip silently rather
                        # than corrupt the entity table with mistyped rows.
                        continue
                    if not ev.external_id:
                        continue

                    entity_props_json = json.dumps(_aircraft_entity_properties(ev))
                    position_props_json = json.dumps(_aircraft_position_properties(ev))
                    ts = _parse_ts(ev.ts)
                    display_name = (ev.payload or {}).get("callsign") or None
                    if display_name:
                        display_name = display_name.strip() or None

                    # UPSERT entity, get back the entity_id.
                    # On conflict:
                    #   - merge properties (flag changes mid-flight)
                    #   - advance last_seen / current_geom / current_position_time
                    #     ONLY when the new ts is newer (defends against out-of-order
                    #     position arrival from rate-limited ingester retries)
                    entity_id = await conn.fetchval(
                        """
                        INSERT INTO entity
                            (entity_type, canonical_id_type, canonical_id,
                             display_name, properties, last_seen, updated_at,
                             current_geom, current_position_time,
                             current_velocity_ms, current_heading_deg, current_altitude_m)
                        VALUES
                            ('aircraft', 'icao24', $1, $2, $3::jsonb, $4, $4,
                             ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography, $4,
                             $7, $8, $9)
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
                                     THEN EXCLUDED.current_heading_deg ELSE entity.current_heading_deg END,
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
                        float(ev.heading_deg) if ev.heading_deg is not None else None,
                        float(ev.altitude_m) if ev.altitude_m is not None else None,
                    )

                    # INSERT position_track snapshot. PostGIS expects ST_MakePoint(lng, lat).
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
        _log.warning(f"write_aircraft_events failed after {written} events: {type(e).__name__}: {e}")
        return written

    return written
