"""
Sanctioned-vessel-port-arrival detector — Phase 4d-4.

Compound algorithm: emits a tier-1 alert when a vessel that's currently
matched to an OFAC SDN entry (via sanctions_match) is detected arriving
at one of the ~103 reference ports (via port_arrival or port_call).

Operationally this is the killer maritime-sanctions alert. Either of
the upstream events alone is interesting; together they're the signal
analysts ACT on — "sanctioned vessel SIBERIA just arrived at Bandar
Abbas" is the kind of finding that ends up in newsroom headlines.

Algorithm:
  For each port_arrival or port_call event in the last `window_min`:
    Look up sanctioned_vessel_underway events for the SAME entity_id
    in the last `sanction_lookback_min`. If at least one match exists:
      Emit `sanctioned_port_arrival` event combining the two.

  The compound event carries BOTH the sanctioning regime + the port
  metadata so consumers don't need to do any joins to display it.

Severity:
  Strategic port  + sanctioned vessel = 10 (max)  ← Bandar Abbas / Sevastopol / etc.
  Commercial port + sanctioned vessel =  8       ← LA / Singapore / etc.

Idempotency:
  Same (vessel, port) pair fires at most once per `dedup_hours`. A
  vessel that lingers at a sanctioned-relevant port emits this alert
  once per arrival, not per scan cycle.
"""

from __future__ import annotations

import logging
from db import acquire_write


_log = logging.getLogger("algorithms.sanctioned_port_arrival")


SANCTIONED_PORT_ARRIVAL_SQL = """
-- Pair recent port_arrival/port_call events with recent
-- sanctioned_vessel_underway events for the same entity. Inserts a
-- compound event_type='sanctioned_port_arrival' so downstream
-- consumers see the joined finding without their own join.
WITH recent_arrivals AS (
    SELECT
        pa.id              AS arrival_event_id,
        pa.entity_id,
        pa.event_time      AS arrival_time,
        pa.geom            AS port_geom,
        pa.event_type      AS arrival_kind,    -- 'port_arrival' | 'port_call'
        pa.properties      AS arrival_props
    FROM event pa
    WHERE pa.event_type IN ('port_arrival', 'port_call')
      AND pa.event_time >= NOW() - ($1::int * INTERVAL '1 minute')
      AND pa.entity_id IS NOT NULL
),
sanc_matches AS (
    SELECT DISTINCT ON (sv.entity_id)
        sv.entity_id,
        sv.event_time      AS match_time,
        sv.properties      AS sanc_props
    FROM event sv
    WHERE sv.event_type = 'sanctioned_vessel_underway'
      AND sv.event_time >= NOW() - ($2::int * INTERVAL '1 minute')
      AND sv.entity_id IS NOT NULL
    ORDER BY sv.entity_id, sv.event_time DESC
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, entity_id, domain, decay_half_life_min
)
SELECT
    'sanctioned_port_arrival'                                  AS event_type,
    -- subtype: '<regime>:<port_country>' e.g. 'IRAN:IR' /
    -- 'RUSSIA:VE' (Russian-sanctioned vessel at Venezuelan port)
    COALESCE(sm.sanc_props->>'match_regime', 'UNKNOWN') ||
        ':' || COALESCE(ra.arrival_props->>'port_country', '??')
                                                               AS event_subtype,
    ra.arrival_time                                            AS event_time,
    ra.port_geom                                               AS geom,
    -- Strategic port + sanctioned vessel = 10 (max). Commercial = 8.
    CASE
        WHEN ra.arrival_props->>'port_kind' = 'strategic' THEN 10.0
        ELSE 8.0
    END::real                                                  AS severity,
    'CRITICAL — Sanctioned vessel ' ||
        COALESCE(sm.sanc_props->>'live_vessel_name',
                 sm.sanc_props->>'mmsi') ||
        ' at ' || (ra.arrival_props->>'port_name')
                                                               AS title,
    'OFAC ' || COALESCE(sm.sanc_props->>'match_regime', 'sanctioned') ||
        '-listed vessel ' ||
        COALESCE(sm.sanc_props->>'live_vessel_name', 'unknown') ||
        ' (MMSI ' || COALESCE(sm.sanc_props->>'mmsi', '?') || ') ' ||
        CASE WHEN ra.arrival_kind = 'port_arrival'
             THEN 'arrived at '
             ELSE 'observed at '
        END ||
        (ra.arrival_props->>'port_name') || ' (' ||
        COALESCE(ra.arrival_props->>'port_country', '??') || ')'
                                                               AS description,
    jsonb_build_object(
        'algorithm',          $3::text,
        'arrival_event_id',   ra.arrival_event_id::text,
        'arrival_kind',       ra.arrival_kind,
        'vessel_id',          ra.entity_id::text,
        'vessel_name',        sm.sanc_props->>'live_vessel_name',
        'mmsi',               sm.sanc_props->>'mmsi',
        'sanctioning_authority', sm.sanc_props->>'sanctioning_authority',
        'match_regime',       sm.sanc_props->>'match_regime',
        'match_program',      sm.sanc_props->>'match_program',
        'match_kind',         sm.sanc_props->>'match_kind',
        'sanctioned_imo',     sm.sanc_props->>'sanctioned_imo',
        'live_imo',           sm.sanc_props->>'live_imo',
        'port_id',            ra.arrival_props->>'port_id',
        'port_name',          ra.arrival_props->>'port_name',
        'port_country',       ra.arrival_props->>'port_country',
        'port_kind',          ra.arrival_props->>'port_kind',
        'fcra_safe',          false
    )                                                          AS properties,
    ra.entity_id                                               AS entity_id,
    'maritime'                                                 AS domain,
    1440                                                       AS decay_half_life_min
FROM recent_arrivals ra
JOIN sanc_matches sm USING (entity_id)
WHERE NOT EXISTS (
    -- Idempotent within dedup_hours per (vessel, port) pair
    SELECT 1 FROM event prior
    WHERE prior.event_type = 'sanctioned_port_arrival'
      AND prior.entity_id  = ra.entity_id
      AND prior.properties->>'port_id' = ra.arrival_props->>'port_id'
      AND prior.event_time >= NOW() - ($4::int * INTERVAL '1 hour')
)
"""


async def run_sanctioned_port_arrival_scan(
    *,
    arrival_window_min: int = 60,
    sanction_lookback_min: int = 24 * 60,
    dedup_hours: int = 24,
    algorithm_tag: str = "sanctioned_port_arrival_v1",
) -> int:
    """Run one compound-detection pass. Returns count of new tier-1
    alerts inserted.

    Args:
        arrival_window_min: how far back to look for port_arrival /
            port_call events. Default 60 min — fresh signal only.
        sanction_lookback_min: how far back to look for the matching
            sanctioned_vessel_underway event. Default 24h — sanctions
            findings persist that long via their own decay window.
        dedup_hours: don't re-emit (vessel, port) within this window.
            Default 24h — one alert per arrival, not per scan.
        algorithm_tag: for test isolation. Production uses
            'sanctioned_port_arrival_v1'.
    """
    async with acquire_write() as conn:
        result = await conn.execute(
            SANCTIONED_PORT_ARRIVAL_SQL,
            arrival_window_min,
            sanction_lookback_min,
            algorithm_tag,
            dedup_hours,
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"sanctioned_port_arrival scan: {count} compound tier-1 alerts "
            f"(arrival_window={arrival_window_min}min, "
            f"sanction_lookback={sanction_lookback_min}min)"
        )
    return count
