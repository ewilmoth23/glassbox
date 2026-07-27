"""
Loitering detection — Phase 4 algorithm #5.

Strategic context: an entity that's still broadcasting but has stayed within
a small radius for hours is doing something specific:
  - Vessel anchored offshore (not at a port) for many hours = ship-to-ship
    transfer staging, smuggling rendezvous waiting, or weather-hold
  - Aircraft circling for >30min = surveillance, search & rescue, holding
    for landing diversion
  - Tanker idling outside a sanctioned port = sanctions evasion

This algorithm flags any vessel/aircraft whose recent position_track points
all fall within a small bounding circle (~1km radius) over a long span
(>4 hours), even though the entity is still actively broadcasting.

Geometry: we compute the centroid of recent positions, then the maximum
distance from that centroid to any position. If that max distance is below
the loitering radius threshold, the entity hasn't really moved — it's
loitering.

Filters (defensive against false positives at v1.0):
  - At least 5 position_track pings in the lookback window (filters
    stale entities that just had one stale ping)
  - Total span >= min_span_hours (filters short stops)
  - Entity is currently active (current_position_time within 1h)
  - Path-length ratio: total path length / centroid-radius > 2 (filters
    truly anchored vessels — those have ~0 path length; loiterers
    actually move within their box)

Idempotency:
  Each entity is flagged at most once per dedup window via NOT EXISTS.
  Re-emergence as loitering after the dedup window = new event.

Performance:
  Single statement; CTE collects positions per entity, then aggregates.
  GiST index on position_track.geom + btree(entity_id, time DESC) make
  this fast at v1.0 scale (~120K position_track rows in past 24h).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.loitering")


DEFAULT_LOOKBACK_HOURS    = 8       # window of position_track to evaluate
DEFAULT_MIN_SPAN_HOURS    = 4       # entity must have pings spanning >= this
DEFAULT_RADIUS_M          = 1000.0  # all pings within this radius of centroid
DEFAULT_MIN_PINGS         = 5       # at least this many pings
# 7-day dedup (was 24h — caused 1.67x over-firing per audit 2026-05-13).
# Loitering periods rarely exceed a week in practice; the prior 24h
# window let the same entity loitering in the same area emit fresh
# findings every day. Lookback (8h) << dedup, so an entity that left
# and came back within the dedup window is correctly skipped.
DEFAULT_DEDUP_WINDOW_HRS  = 7 * 24


LOITERING_SCAN_SQL = """
-- Bounding-box approach: compute the lat/lng span of each entity's
-- recent position_track rows in ONE pass. If both spans are below the
-- threshold, the entity hasn't really moved — it's loitering.
--
-- Why bounding box not centroid+max-distance: at v1.0 scale (~38K entities,
-- ~120K position_track rows in 8h), the centroid+per-row distance loop
-- timed out. Bounding-box is a single GROUP BY over min/max, fully indexed
-- on (entity_id, time DESC).
--
-- 1 degree lat ≈ 111 km. radius_m=1000 → lat_span_threshold ≈ 0.018 deg.
-- We approximate longitude using the entity's mean lat (cos correction).
WITH per_entity AS (
    SELECT
        e.id              AS entity_id,
        e.entity_type     AS entity_type,
        e.canonical_id    AS canonical_id,
        e.display_name    AS display_name,
        e.current_geom    AS center_geom,
        count(*)          AS pings,
        min(p.time)       AS first_ping,
        max(p.time)       AS last_ping,
        avg(p.velocity_ms) AS avg_velocity,
        max(ST_Y(p.geom::geometry)) - min(ST_Y(p.geom::geometry)) AS lat_span,
        max(ST_X(p.geom::geometry)) - min(ST_X(p.geom::geometry)) AS lng_span,
        avg(ST_Y(p.geom::geometry)) AS mean_lat
    FROM entity e
    JOIN position_track p ON p.entity_id = e.id
    WHERE e.entity_type IN ('vessel', 'aircraft')
      AND e.current_position_time IS NOT NULL
      AND e.current_position_time >= $5::timestamptz
      AND p.time >= $1::timestamptz
      AND ($7::text IS NULL OR e.canonical_id LIKE $7)
    GROUP BY e.id, e.entity_type, e.canonical_id, e.display_name, e.current_geom
    HAVING count(*) >= $3::int
       AND (max(p.time) - min(p.time)) >= make_interval(hours => $2::int)
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'loitering_detected'                                       AS event_type,
    pe.entity_type                                             AS event_subtype,
    NOW()                                                      AS event_time,
    pe.center_geom                                             AS geom,
    -- Severity: 4h span = 2, 8h = 4, 16h = 8, 24h+ = capped at 10
    LEAST(10.0, GREATEST(3.0,
        EXTRACT(EPOCH FROM (pe.last_ping - pe.first_ping)) / 3600.0 / 2.0
    ))::real                                                    AS severity,
    'Loitering detected: ' ||
        COALESCE(pe.display_name, pe.entity_type || ' ' || pe.canonical_id)
                                                               AS title,
    pe.entity_type || ' ' || pe.canonical_id ||
        ' has stayed within a small bbox (~' ||
        ROUND((GREATEST(pe.lat_span, pe.lng_span * cos(radians(pe.mean_lat))) * 111000)::numeric, 0) ||
        'm) for ' ||
        ROUND(EXTRACT(EPOCH FROM (pe.last_ping - pe.first_ping))::numeric / 3600.0, 1) ||
        'h (' || pe.pings || ' pings, avg velocity ' ||
        COALESCE(ROUND(pe.avg_velocity::numeric, 1)::text, '?') || ' m/s)'
                                                               AS description,
    jsonb_build_object(
        'algorithm',         $6::text,
        'canonical_id',      pe.canonical_id,
        'entity_type',       pe.entity_type,
        'pings',             pe.pings,
        'first_ping',        pe.first_ping,
        'last_ping',         pe.last_ping,
        'span_hours',        ROUND(EXTRACT(EPOCH FROM (pe.last_ping - pe.first_ping))::numeric / 3600.0, 2),
        'lat_span_deg',      ROUND(pe.lat_span::numeric, 5),
        'lng_span_deg',      ROUND(pe.lng_span::numeric, 5),
        'avg_velocity_ms',   pe.avg_velocity,
        'radius_threshold_m', $4::int
    )                                                          AS properties,
    CASE WHEN pe.entity_type = 'vessel' THEN 'maritime' ELSE 'aviation' END
                                                               AS domain,
    1440                                                       AS decay_half_life_min,
    pe.entity_id                                               AS entity_id
FROM per_entity pe
-- Convert radius_m threshold to lat-degrees (1 deg ≈ 111000 m).
-- For longitude: shrink by cos(mean_lat) to account for converging meridians.
WHERE pe.lat_span <= ($4::float / 111000.0)
  AND pe.lng_span * cos(radians(pe.mean_lat)) <= ($4::float / 111000.0)
  AND pe.avg_velocity > 0.2   -- pure-anchored vessels have ~0 avg vel
  -- FP audit 2026-05-19 (P0-C #5): reject "stale-pings" pattern where
  -- multiple position_track rows have IDENTICAL lat/lon (lat_span=lng_span=0)
  -- but report a non-trivial velocity_ms field. This is the AIS-receiver-shed
  -- signature: same payload redelivered N times, velocity from the stale ping
  -- exceeds the anchored-vessel filter (0.2 m/s). The algorithm intent (per
  -- docstring line 27) is "loiterers actually move within their box" — zero
  -- positional movement means we have no real evidence of motion at all.
  -- Withdrew 28,238 historical FPs (50% of corpus) matching this signature.
  AND NOT (pe.lat_span = 0 AND pe.lng_span = 0)
  AND NOT EXISTS (
      SELECT 1 FROM event finding
      WHERE finding.event_type = 'loitering_detected'
        AND finding.properties->>'algorithm' = $6
        AND finding.entity_id = pe.entity_id
        AND finding.event_time >= $8::timestamptz
  )
"""


async def run_loitering_scan(
    *,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    min_span_hours: int = DEFAULT_MIN_SPAN_HOURS,
    min_pings: int = DEFAULT_MIN_PINGS,
    radius_m: float = DEFAULT_RADIUS_M,
    dedup_window_hours: int = DEFAULT_DEDUP_WINDOW_HRS,
    algorithm_tag: str = "loitering",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Run one loitering scan. Returns count of new findings.

    Args:
        lookback_hours: how far back in position_track to look. Default 8h.
        min_span_hours: entity must have pings spanning at least this long.
            Default 4h. Filters out brief stops.
        min_pings: minimum number of position_track rows over the window.
            Default 5. Filters out entities with stale single pings.
        radius_m: all pings must fall within this radius of the entity's
            recent-position centroid. Default 1000m (1km).
        dedup_window_hours: a given entity is flagged at most once per
            this window. Default 24h.
        algorithm_tag: tagged into properties.algorithm for dedup.
        entity_canonical_id_like: optional LIKE pattern for tests.
    """
    now = datetime.now(timezone.utc)
    lookback_cutoff   = now - timedelta(hours=lookback_hours)
    active_cutoff     = now - timedelta(hours=1)   # entity must have current ping in past 1h
    dedup_cutoff      = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            LOITERING_SCAN_SQL,
            lookback_cutoff,             # $1
            min_span_hours,              # $2
            min_pings,                   # $3
            radius_m,                    # $4
            active_cutoff,               # $5
            algorithm_tag,               # $6
            entity_canonical_id_like,    # $7
            dedup_cutoff,                # $8
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"loitering scan: {count} entities flagged "
            f"(lookback={lookback_hours}h, radius<={radius_m}m, span>={min_span_hours}h)"
        )
    return count
