"""
Rendezvous detection — Phase 4 algorithm #6.

Strategic context: two distinct entities pulling close to each other (within
~500m–1km) and both moving slowly are doing one of several specific things:
  - Vessel-to-vessel: ship-to-ship oil/cargo transfer (sanctions evasion is
    the high-signal case — Russian/Iranian shadow fleet does STS to disguise
    origin), refugee handoff, smuggling
  - Aircraft-aircraft: formation flight, in-flight refueling tanker meets
    fighter package
  - Aircraft-vessel: aircraft hovering over ship — drug interdiction, SAR,
    military operations
  - Aircraft-anchored vessel: helicopter delivery, surveillance overflight

This is the cross-entity proximity scan tightened to a small radius + a
low-velocity filter so we catch deliberate convergence, not a fast pass-by.

The existing cross_entity_proximity_scan operates at 50km radius for general
"X near Y" findings. This algorithm is the high-signal sibling: same join
shape, but radius=1km + both entities at velocity < 3 m/s.

Idempotency:
  Each (entity_a_id, entity_b_id) pair flagged at most once per dedup window.
  Lex-ordered ID pair so (A, B) and (B, A) become the same pair.

Performance:
  Self-join on entity table. The denormalized entity.current_geom + GiST
  index `entity_current_geom_gist` make ST_DWithin a small bbox-bounded
  index seek per outer row. At v1.0 scale (~30K entities) this runs in
  single-digit seconds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.rendezvous")


DEFAULT_RADIUS_M           = 1000    # max distance for rendezvous
DEFAULT_MAX_VELOCITY_MS    = 3.0     # both entities must be at-or-below this
DEFAULT_MIN_VELOCITY_MS    = 0.5     # both must be MOVING (not parked/anchored)
DEFAULT_LOOKBACK_MIN       = 30      # both entities active within past N min
DEFAULT_DEDUP_WINDOW_HRS   = 24

# --- 2026-05-19 P0-C audit fixes (FP rate 76.7% on 30-sample audit) ---
#
# Root causes of FPs found in audit (see ALGORITHM_FP_AUDIT_rendezvous_2026_05_19.md):
#   1. Snapshot-only firing — algorithm fires the instant two entities are within
#      1km at low velocity. No requirement that proximity be SUSTAINED. A pair of
#      vessels passing in a fairway gets flagged the same as a real STS transfer.
#   2. Aircraft-aircraft pairs dominated by airport taxi traffic. Aircraft at
#      1–3 m/s are ON THE GROUND. 451K of 841K (54%) findings were airport-taxi.
#   3. Velocity filter caught airport-taxi AND port-anchorage-maneuvering — the
#      0.5–3 m/s band IS taxi speed AND harbor-maneuver speed.
#
# Fixes added below:
#   A. SUSTAINED-PROXIMITY check: pair must be within radius at >=2 position-track
#      samples spanning >=DEFAULT_MIN_DURATION_MIN. This eliminates the
#      snapshot-only artifact entirely.
#   B. NO-RECENT-HIGH-SPEED check: neither entity may have had a velocity > 50 m/s
#      in the +/-30min window. This kills the "aircraft taxiing right before
#      takeoff" FP class (a taxiing aircraft at 1-3 m/s that's then airborne at
#      250 m/s minutes later is a departure, not a rendezvous).
#   C. Aircraft-aircraft pairs gated by sustained-proximity check (same as
#      vessels). An airport taxiway has many co-located aircraft at any instant
#      but they don't sustain close proximity for 20+ minutes.
DEFAULT_MIN_DURATION_MIN   = 20      # pair must be close for >= 20 min
DEFAULT_MAX_RECENT_VEL_MS  = 50.0    # neither entity may have been recently fast
# ---------------------------------------------------------------------


RENDEZVOUS_SCAN_SQL = """
WITH active_movers AS (
    SELECT
        e.id, e.entity_type, e.canonical_id, e.display_name,
        e.current_geom, e.current_position_time,
        pt.velocity_ms,
        recent.max_recent_vel_ms
    FROM entity e
    LEFT JOIN LATERAL (
        SELECT velocity_ms
        FROM position_track
        WHERE entity_id = e.id
        ORDER BY time DESC
        LIMIT 1
    ) pt ON TRUE
    LEFT JOIN LATERAL (
        -- 2026-05-19 fix B: check if entity was recently moving fast (e.g. a
        -- taxiing aircraft about to take off, or one that just landed).
        SELECT MAX(velocity_ms) AS max_recent_vel_ms
        FROM position_track
        WHERE entity_id = e.id
          AND time >= NOW() - interval '30 minutes'
    ) recent ON TRUE
    WHERE e.entity_type IN ('vessel', 'aircraft')
      AND e.current_geom IS NOT NULL
      AND e.current_position_time >= $4::timestamptz
      AND pt.velocity_ms IS NOT NULL
      AND pt.velocity_ms <= $2::float
      AND pt.velocity_ms >= $7::float
      -- 2026-05-19 fix B: skip entities that were recently fast (departures /
      -- arrivals at airports, ferries accelerating in harbors).
      AND (recent.max_recent_vel_ms IS NULL OR recent.max_recent_vel_ms <= $8::float)
      AND ($6::text IS NULL OR e.canonical_id LIKE $6)
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'rendezvous_detected'                                    AS event_type,
    a.entity_type || '_' || b.entity_type                    AS event_subtype,
    NOW()                                                    AS event_time,
    a.current_geom                                           AS geom,
    -- Severity: tighter pair = higher concern. <250m = 9, <500m = 8,
    -- <1000m = 7. Vessel-vessel pairs at any range get +1 (sanctions risk).
    LEAST(10.0,
        CASE
            WHEN ST_Distance(a.current_geom, b.current_geom) < 250 THEN 9.0
            WHEN ST_Distance(a.current_geom, b.current_geom) < 500 THEN 8.0
            ELSE 7.0
        END
        + CASE WHEN a.entity_type = 'vessel' AND b.entity_type = 'vessel'
               THEN 1.0 ELSE 0.0 END
    )::real                                                  AS severity,
    'Rendezvous: ' ||
        COALESCE(a.display_name, a.entity_type || ' ' || a.canonical_id) ||
        ' near ' ||
        COALESCE(b.display_name, b.entity_type || ' ' || b.canonical_id) ||
        ' (' || ROUND(ST_Distance(a.current_geom, b.current_geom)::numeric, 0) || 'm)'
                                                             AS title,
    a.entity_type || ' ' || a.canonical_id ||
        ' and ' || b.entity_type || ' ' || b.canonical_id ||
        ' are within ' ||
        ROUND(ST_Distance(a.current_geom, b.current_geom)::numeric, 0) ||
        'm of each other; both at low velocity (a=' ||
        ROUND(a.velocity_ms::numeric, 1) || ' m/s, b=' ||
        ROUND(b.velocity_ms::numeric, 1) || ' m/s)'
                                                             AS description,
    jsonb_build_object(
        'algorithm',           $5::text,
        'pair_kind',           a.entity_type || '_' || b.entity_type,
        'a_canonical_id',      a.canonical_id,
        'a_display_name',      a.display_name,
        'a_velocity_ms',       a.velocity_ms,
        'b_canonical_id',      b.canonical_id,
        'b_display_name',      b.display_name,
        'b_velocity_ms',       b.velocity_ms,
        'distance_m',          ROUND(ST_Distance(a.current_geom, b.current_geom)::numeric, 0)::int,
        'entity_ids',          jsonb_build_array(a.id::text, b.id::text)
    )                                                        AS properties,
    'maritime'                                               AS domain,
    1440                                                     AS decay_half_life_min,
    a.id                                                     AS entity_id
FROM active_movers a
JOIN active_movers b ON a.id < b.id
 AND ST_DWithin(a.current_geom, b.current_geom, $1)
-- 2026-05-19 fix A: SUSTAINED-PROXIMITY check. A real rendezvous holds for
-- at least DEFAULT_MIN_DURATION_MIN minutes. A pair passing each other in a
-- fairway/taxiway is close for <5 min and gets filtered here.
-- We check position_track for at least 2 distinct timestamps where the pair
-- was within $1 meters, spanning >= $9 minutes.
WHERE EXISTS (
    WITH pair_track AS (
        SELECT pa.time AS ta, pa.geom AS ga, pb.geom AS gb
        FROM position_track pa, position_track pb
        WHERE pa.entity_id = a.id
          AND pb.entity_id = b.id
          AND pa.time >= NOW() - interval '90 minutes'
          AND pb.time >= NOW() - interval '90 minutes'
          AND ABS(EXTRACT(EPOCH FROM (pa.time - pb.time))) <= 60
    ),
    close_samples AS (
        SELECT ta FROM pair_track WHERE ST_DWithin(ga, gb, $1)
    )
    SELECT 1
    FROM close_samples
    HAVING COUNT(*) >= 2
       AND EXTRACT(EPOCH FROM (MAX(ta) - MIN(ta))) >= $9::float * 60.0
)
AND NOT EXISTS (
    -- 2026-05-21: containment form (@>) — engages event_props_gin in
    -- one index lookup instead of two post-filters. Semantically
    -- equivalent because `properties.entity_ids` is always a jsonb
    -- array. Mirrors the cross_domain rewrite in api_v1.py.
    SELECT 1 FROM event finding
    WHERE finding.event_type = 'rendezvous_detected'
      AND finding.properties->>'algorithm' = $5
      AND finding.properties @> jsonb_build_object(
              'entity_ids', jsonb_build_array(a.id::text, b.id::text))
      AND finding.event_time >= $3::timestamptz
)
"""


async def run_rendezvous_scan(
    *,
    radius_m: float = DEFAULT_RADIUS_M,
    max_velocity_ms: float = DEFAULT_MAX_VELOCITY_MS,
    min_velocity_ms: float = DEFAULT_MIN_VELOCITY_MS,
    lookback_min: int = DEFAULT_LOOKBACK_MIN,
    dedup_window_hours: int = DEFAULT_DEDUP_WINDOW_HRS,
    algorithm_tag: str = "rendezvous",
    entity_canonical_id_like: str | None = None,
    max_recent_velocity_ms: float = DEFAULT_MAX_RECENT_VEL_MS,
    min_duration_min: float = DEFAULT_MIN_DURATION_MIN,
) -> int:
    """Run one rendezvous scan. Returns count of new findings.

    Args:
        radius_m: max distance between two entities to count as a rendezvous.
            Default 1000m. Tighter than the 50km cross-entity proximity scan.
        max_velocity_ms: both entities' last reported velocity must be at or
            below this. Default 3 m/s — captures slow-moving / station-keeping
            convergences (STS transfers happen at near-idle).
        min_velocity_ms: both entities must be at LEAST this fast. Default
            0.5 m/s — filters out parked/anchored pairs (port noise that
            would otherwise dominate the result set).
        lookback_min: both entities must have current_position_time within
            this many minutes. Default 30 min.
        dedup_window_hours: same pair flagged at most once per this window.
            Default 24h.
        algorithm_tag: tagged into properties.algorithm for dedup.
        entity_canonical_id_like: optional LIKE pattern for tests.
        max_recent_velocity_ms: 2026-05-19 P0-C fix B — neither entity may
            have had a velocity > this in the past 30 min. Default 50 m/s.
            Filters out aircraft departures/arrivals where snapshot caught
            the taxi phase but the entity is actually a fast-mover.
        min_duration_min: 2026-05-19 P0-C fix A — pair must have been within
            radius_m for at least this many minutes (>=2 position-track samples
            spanning >= this duration). Default 20 min. Eliminates the
            snapshot-only fairway-crossing FPs.
    """
    now = datetime.now(timezone.utc)
    active_cutoff = now - timedelta(minutes=lookback_min)
    dedup_cutoff  = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            RENDEZVOUS_SCAN_SQL,
            radius_m,                  # $1
            max_velocity_ms,           # $2
            dedup_cutoff,              # $3
            active_cutoff,             # $4
            algorithm_tag,             # $5
            entity_canonical_id_like,  # $6
            min_velocity_ms,           # $7
            max_recent_velocity_ms,    # $8
            min_duration_min,          # $9
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"rendezvous scan: {count} pairs flagged "
            f"(radius<={radius_m}m, max-vel<={max_velocity_ms}m/s)"
        )
    return count
