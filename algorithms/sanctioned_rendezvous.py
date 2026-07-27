"""
Sanctioned-vessel rendezvous detection — Phase 4 algorithm #9 (combined signal).

This is the textbook ship-to-ship oil sanctions-evasion signature:

  Two vessels, BOTH on OFAC SDN list (or one is, the other recently turned
  off AIS), within 1km of each other, both at slow STS-transfer speed
  (0.5–3 m/s), in open water near a sanctions-relevant chokepoint.

The plain `rendezvous_detected` algorithm fires on ALL slow-moving close
pairs (over 600 in Baltic alone) — most legitimate maritime traffic,
mostly noise. Filtering to pairs where AT LEAST ONE side is sanctioned
turns it into a critical signal: 1-10 hits per scan, mostly real STS.

Signal escalation hierarchy:
  - Both sanctioned + close (<500m) → severity 10 (textbook STS evasion)
  - One sanctioned + close (<500m) → severity 9
  - One sanctioned + close (<1km)  → severity 8

Cross-references against the existing `entity_type='sanctioned_vessel'`
data via name/IMO match. Idempotent per pair per dedup window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.sanctioned_rendezvous")


SANCTIONED_RENDEZVOUS_SCAN_SQL = """
WITH live_movers AS (
    -- Slow-moving currently-active vessels.
    SELECT
        e.id, e.canonical_id, e.display_name,
        e.current_geom, e.current_position_time,
        e.properties,
        pt.velocity_ms
    FROM entity e
    LEFT JOIN LATERAL (
        SELECT velocity_ms FROM position_track
        WHERE entity_id = e.id ORDER BY time DESC LIMIT 1
    ) pt ON TRUE
    WHERE e.entity_type = 'vessel'
      AND e.current_geom IS NOT NULL
      AND e.current_position_time >= $4::timestamptz
      AND pt.velocity_ms IS NOT NULL
      AND pt.velocity_ms BETWEEN $7::float AND $2::float
      AND ($6::text IS NULL OR e.canonical_id LIKE $6)
),
sanction_lookup AS (
    -- For each live vessel, see if it matches OFAC SDN by IMO (precise)
    -- or fuzzy name (catches renamed shadow-fleet). Returns one row per
    -- live vessel with a flag.
    SELECT
        lm.id AS vessel_id,
        bool_or(
            -- IMO exact match (definitive)
            (lm.properties->>'imo' IS NOT NULL
             AND sv.properties->>'imo' IS NOT NULL
             AND lm.properties->>'imo' = sv.properties->>'imo')
            OR
            -- Name fuzzy fallback — ONLY when IMO comparison cannot be
            -- made. See sanctions_match.py for the rationale: prior version
            -- had no IMO-null guard at all, causing false positives where
            -- two unrelated vessels shared a common name.
            ((lm.properties->>'imo' IS NULL OR sv.properties->>'imo' IS NULL)
             AND lm.display_name IS NOT NULL AND sv.display_name IS NOT NULL
             AND length(lm.display_name) >= 4
             AND length(sv.display_name) >= 4
             AND upper(lm.display_name) % upper(sv.display_name)
             AND similarity(upper(lm.display_name), upper(sv.display_name)) >= 0.9)
        ) AS is_sanctioned,
        max(sv.canonical_id) AS sanctioned_canonical_id,
        max(sv.display_name) AS sanctioned_name
    FROM live_movers lm
    JOIN entity sv ON sv.entity_type = 'sanctioned_vessel'
    GROUP BY lm.id
),
pairs AS (
    -- All pairs of slow-moving vessels within radius_m, lex-ordered.
    SELECT
        a.id AS a_id, a.canonical_id AS a_mmsi, a.display_name AS a_name,
        a.current_geom AS a_geom, a.velocity_ms AS a_vel,
        a.properties AS a_props,
        b.id AS b_id, b.canonical_id AS b_mmsi, b.display_name AS b_name,
        b.velocity_ms AS b_vel, b.properties AS b_props,
        ST_Distance(a.current_geom, b.current_geom) AS distance_m,
        sla.is_sanctioned AS a_sanctioned,
        sla.sanctioned_canonical_id AS a_sanc_id,
        sla.sanctioned_name AS a_sanc_name,
        slb.is_sanctioned AS b_sanctioned,
        slb.sanctioned_canonical_id AS b_sanc_id,
        slb.sanctioned_name AS b_sanc_name
    FROM live_movers a
    JOIN live_movers b ON a.id < b.id
     AND ST_DWithin(a.current_geom, b.current_geom, $1)
    LEFT JOIN sanction_lookup sla ON sla.vessel_id = a.id
    LEFT JOIN sanction_lookup slb ON slb.vessel_id = b.id
    WHERE COALESCE(sla.is_sanctioned, false) OR COALESCE(slb.is_sanctioned, false)
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'sanctioned_vessel_rendezvous'                              AS event_type,
    CASE
        WHEN p.a_sanctioned AND p.b_sanctioned THEN 'both_sanctioned'
        ELSE 'one_sanctioned'
    END                                                          AS event_subtype,
    NOW()                                                        AS event_time,
    p.a_geom                                                     AS geom,
    -- Severity ladder:
    --   both sanctioned, <500m  → 10 (textbook STS)
    --   one  sanctioned, <500m  → 9
    --   one  sanctioned, <1km   → 8
    CASE
        WHEN p.a_sanctioned AND p.b_sanctioned AND p.distance_m < 500 THEN 10.0
        WHEN p.a_sanctioned AND p.b_sanctioned                          THEN 9.0
        WHEN p.distance_m < 500                                         THEN 9.0
        ELSE                                                                 8.0
    END::real                                                    AS severity,
    'CRITICAL — Sanctioned vessel rendezvous: ' ||
        COALESCE(p.a_name, 'MMSI ' || p.a_mmsi) || ' near ' ||
        COALESCE(p.b_name, 'MMSI ' || p.b_mmsi) ||
        ' (' || ROUND(p.distance_m::numeric, 0) || 'm)' ||
        CASE WHEN p.a_sanctioned AND p.b_sanctioned
             THEN ' [BOTH ON OFAC SDN]' ELSE ' [one on OFAC SDN]' END
                                                                 AS title,
    'Vessels ' || p.a_mmsi || ' and ' || p.b_mmsi ||
        ' are within ' || ROUND(p.distance_m::numeric, 0) ||
        'm of each other at low velocity. ' ||
        CASE WHEN p.a_sanctioned THEN
            'Vessel A (' || COALESCE(p.a_name, p.a_mmsi) || ') matches OFAC SDN: "' || p.a_sanc_name || '". '
        ELSE '' END ||
        CASE WHEN p.b_sanctioned THEN
            'Vessel B (' || COALESCE(p.b_name, p.b_mmsi) || ') matches OFAC SDN: "' || p.b_sanc_name || '". '
        ELSE '' END ||
        'Velocities: a=' || ROUND(p.a_vel::numeric, 1) ||
        ' m/s, b=' || ROUND(p.b_vel::numeric, 1) || ' m/s.'
                                                                 AS description,
    jsonb_build_object(
        'algorithm',           $5::text,
        'a_mmsi',              p.a_mmsi,
        'a_name',              p.a_name,
        'a_imo',               p.a_props->>'imo',
        'a_sanctioned',        p.a_sanctioned,
        'a_sanctioned_canonical_id', p.a_sanc_id,
        'a_sanctioned_name',   p.a_sanc_name,
        'b_mmsi',              p.b_mmsi,
        'b_name',              p.b_name,
        'b_imo',               p.b_props->>'imo',
        'b_sanctioned',        p.b_sanctioned,
        'b_sanctioned_canonical_id', p.b_sanc_id,
        'b_sanctioned_name',   p.b_sanc_name,
        'distance_m',          ROUND(p.distance_m::numeric, 0)::int,
        'a_velocity_ms',       p.a_vel,
        'b_velocity_ms',       p.b_vel,
        'entity_ids',          jsonb_build_array(p.a_id::text, p.b_id::text),
        'fcra_safe',           false,
        'sanctioning_authority', 'US Treasury OFAC'
    )                                                            AS properties,
    'maritime'                                                   AS domain,
    1440                                                         AS decay_half_life_min,
    p.a_id                                                       AS entity_id
FROM pairs p
WHERE NOT EXISTS (
    -- 2026-05-21: containment form (@>) — engages event_props_gin in
    -- one index lookup instead of two post-filters. Semantically
    -- equivalent because `properties.entity_ids` is always a jsonb
    -- array. Mirrors the cross_domain rewrite in api_v1.py.
    SELECT 1 FROM event finding
    WHERE finding.event_type = 'sanctioned_vessel_rendezvous'
      AND finding.properties->>'algorithm' = $5
      AND finding.properties @> jsonb_build_object(
              'entity_ids', jsonb_build_array(p.a_id::text, p.b_id::text))
      AND finding.event_time >= $3::timestamptz
)
"""


async def run_sanctioned_rendezvous_scan(
    *,
    radius_m: float = 1000.0,
    max_velocity_ms: float = 3.0,
    min_velocity_ms: float = 0.5,
    lookback_min: int = 30,
    # 7-day dedup (was 24h — caused 3.45x over-firing per audit
    # 2026-05-13). Same vessel pair in proximity is the same rendezvous
    # if it lasted hours-to-days; refire only after a week (vessels
    # must have separated and re-encountered).
    dedup_window_hours: int = 7 * 24,
    algorithm_tag: str = "sanctioned_rendezvous",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Run one sanctioned-rendezvous scan. Returns count of new findings.

    Same parameters as `rendezvous` but only emits pairs where AT LEAST
    one side matches an OFAC SDN sanctioned-vessel by name or IMO.
    """
    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(minutes=lookback_min)
    dedup_cutoff  = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            SANCTIONED_RENDEZVOUS_SCAN_SQL,
            radius_m,                  # $1
            max_velocity_ms,           # $2
            dedup_cutoff,              # $3
            active_cutoff,             # $4
            algorithm_tag,             # $5
            entity_canonical_id_like,  # $6
            min_velocity_ms,           # $7
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"sanctioned-rendezvous scan: {count} pairs flagged "
            f"(at least one side on OFAC SDN)"
        )
    return count
