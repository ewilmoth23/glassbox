"""
Cross-domain proximity finder — Phase 1.4 of the Glassbox V2 plan.

The first algorithm. Scans the entity table joined with the most-recent
position_track row, against the event table. For each entity-event pair
within `radius_m` meters and within `window_min` minutes of `now`, it writes
a `detected_proximity` row to the event table that records both ids.

Why this exists:
  The "killer query" for Glassbox is *not* "show me all aircraft in this bbox"
  — it's "show me aircraft NEAR something interesting." That something else
  comes from other domains: an earthquake, a news event, a port strike, a
  conflict incident. Proximity is the simplest cross-domain primitive and it
  generalizes: when Phase 2 lands ships/satellites/quakes/news, this same
  algorithm finds those pairs without code changes.

Phase 1 limitation:
  At Phase 1 ship time, only `aircraft` entities exist in the entity table
  (planes.py is the only ingester dual-writing). Until Phase 2 dual-writes
  earthquakes/news/etc. into the event table, this algorithm has nothing to
  match against in production. Tests use synthetic event rows; production
  output will be empty until Phase 2.

Idempotency:
  The query includes a `NOT EXISTS` clause that suppresses duplicate findings
  for the same (entity_id, event_id) pair within the same scan window. This
  means the algorithm can be run on a 5-minute schedule without flooding the
  event table with redundant rows.

Performance:
  Single SQL statement. Uses GiST indexes on `event.geom` and
  `position_track.geom`. The lateral join takes one position_track row per
  entity, which is what GiST indexes are optimized for. Expected runtime at
  v1.0 scale (~10K aircraft, ~100K events): well under 1s.
"""

from __future__ import annotations

import logging

from db import acquire_write


_log = logging.getLogger("algorithms.proximity")


PROXIMITY_SCAN_SQL = """
-- Phase 2.5 perf rewrite (2026-05-08 audit): the previous version did
-- DISTINCT ON over position_track, which forced a sort across 47k+
-- entities × N positions. At v1.0 scale this consistently timed out at
-- 60s. Cross-entity scan was already migrated to entity.current_geom
-- (single GiST index seek per row); applying the same pattern here.
--
-- entity.current_position_time + entity.current_geom are kept in sync
-- by the planes/ships/sats writer paths, indexed by
-- entity_type_current_time_idx + entity_current_geom_gist.
WITH latest_positions AS (
    SELECT
        e.id AS entity_id,
        e.entity_type,
        e.canonical_id,
        e.display_name,
        e.current_position_time AS position_time,
        e.current_geom          AS position_geom
    FROM entity e
    WHERE e.current_position_time >= NOW() - ($1 * INTERVAL '1 minute')
      AND e.current_geom IS NOT NULL
      -- 2026-05-09 fix: exclude satellites from entity↔event proximity.
      -- ST_Distance/ST_DWithin are 2D ground-track operations on the
      -- geography(Point,4326) column; entity.current_geom carries no
      -- altitude. A satellite at ~500 km altitude whose ground track
      -- crosses an earthquake / fire / sanctioned vessel was getting
      -- flagged as "proximate" even though they're separated by hundreds
      -- of km vertically. The fix: satellites don't trigger entity↔event
      -- proximity. If we ever want satellite-overhead-pass detection,
      -- that's a separate algorithm with proper orbital geometry.
      AND e.entity_type <> 'satellite'
      AND ($4::text IS NULL OR e.canonical_id LIKE $4)
),
candidate_pairs AS (
    SELECT
        lp.entity_id,
        lp.entity_type,
        lp.canonical_id,
        lp.display_name,
        lp.position_time,
        lp.position_geom,
        ev.id           AS event_id,
        ev.event_type   AS source_event_type,
        ev.event_time,
        ev.geom         AS event_geom,
        ev.severity,
        ev.title        AS source_event_title,
        ST_Distance(lp.position_geom, ev.geom) AS distance_m
    FROM latest_positions lp
    JOIN event ev
      -- Per-event freshness: use the ingester-supplied decay_half_life_min
      -- so slow-moving event types (NASA EONET volcanoes/storms with
      -- decay=720, news with decay=720) are still caught when their
      -- event_time is hours old. Falls back to the global $1 window_min
      -- when decay is NULL (defensive — schema default is 60 anyway).
      ON ev.event_time >= NOW() - (COALESCE(ev.decay_half_life_min, $1)::int * INTERVAL '1 minute')
     -- 2026-05-19 P0-C audit (algorithm #7): exclude ALL algorithm-derived
     -- event types, not just detected_proximity. The original design intent
     -- (see file header lines 9-15) was "aircraft NEAR something interesting
     -- — an earthquake, a news event, a port strike, a conflict incident."
     -- But the algorithm was also matching against rendezvous_detected,
     -- dark_vessel_detected, loitering_detected, port_*, sanctioned_*,
     -- military_aircraft_underway etc. — i.e. its own algorithmic findings.
     -- This produced extreme fanout in busy lanes: a single rendezvous_detected
     -- in the English Channel was matching 27,000 nearby vessels within 50km.
     -- Sample audit (30 random findings, last 7d): 16 FP / 0 TP / 14 AMB.
     -- 53% FP rate. Restrict matching to raw external events only —
     -- earthquakes (usgs/emsc), news (gdelt/newsdata), space weather, AQI,
     -- volcanoes, SEC filings, etc. Algorithmically-derived findings should
     -- not feed proximity; if "aircraft near a rendezvous" is operationally
     -- interesting, that belongs in a dedicated downstream algorithm.
     AND ev.event_type NOT IN (
       'detected_proximity',
       'rendezvous_detected',
       'dark_vessel_detected',
       'loitering_detected',
       'port_call',
       'port_arrival',
       'port_departure',
       'sanctioned_vessel_went_dark',
       'sanctioned_vessel_rendezvous',
       'sanctioned_vessel_underway',
       'sanctioned_port_arrival',
       'aircraft_in_sanctioned_airspace',
       'military_aircraft_underway',
       'shadow_fleet_cluster_detected',
       'sanctions_match',
       'sanctions_multijurisdictional_match'
     )
     AND ev.geom IS NOT NULL
     AND ST_DWithin(lp.position_geom, ev.geom, $2)
     -- When entity_canonical_id_like is set (test isolation mode), also
     -- restrict events to those whose subtype matches the same prefix —
     -- otherwise wider radii pick up production events near the test
     -- entity and break the test's exact-count assertions. Production
     -- callers pass NULL and skip both filters.
     AND ($4::text IS NULL OR ev.event_subtype LIKE '%' || trim('%' from $4) || '%')
    WHERE NOT EXISTS (
        -- 2026-05-21: containment form (@>) lets the planner use event_props_gin.
        -- Equivalent to `properties->'entity_ids' ? ... AND properties->'event_ids' ? ...`
        -- when both keys hold jsonb arrays (always true for this writer).
        -- Single combined @> check is one index lookup instead of two
        -- sequential post-filters. Pattern mirrors the cross_domain rewrite
        -- in api_v1.py (220s → 30ms with the same change).
        SELECT 1 FROM event finding
        WHERE finding.event_type = 'detected_proximity'
          AND finding.properties->>'algorithm' = $3
          AND finding.event_time >= NOW() - ($1 * INTERVAL '1 minute')
          AND finding.properties @> jsonb_build_object(
                  'entity_ids', jsonb_build_array(lp.entity_id::text),
                  'event_ids',  jsonb_build_array(ev.id::text))
    )
    -- 2026-05-08 audit: bound worst-case row count so the join terminates
    -- in O(seconds) even when the entity × event cartesian is huge (47k
    -- aircraft × 800k events at v1.0 scale). Real cycles produce <1,000
    -- pairs, so a 50k cap is generous; the next 5-min cycle picks up
    -- anything that didn't fit. Without this the query would hit the
    -- 120s pool timeout and produce zero findings indefinitely.
    LIMIT 50000
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min
)
SELECT
    'detected_proximity'                              AS event_type,
    cp.entity_type || '_' || cp.source_event_type     AS event_subtype,
    GREATEST(cp.position_time, cp.event_time)         AS event_time,
    cp.position_geom                                  AS geom,
    cp.severity,
    cp.entity_type || ' ' ||
        COALESCE(cp.display_name, cp.canonical_id) || ' near ' ||
        cp.source_event_title                         AS title,
    'Proximity finding: ' || cp.entity_type || ' ' ||
        cp.canonical_id || ' within ' ||
        ROUND(cp.distance_m::numeric, 0) || ' m of event ' ||
        COALESCE(cp.source_event_title, '<untitled>') AS description,
    jsonb_build_object(
        'algorithm',  $3::text,
        'radius_m',   $2::int,
        'window_min', $1::int,
        'distance_m', ROUND(cp.distance_m::numeric, 0)::int,
        'entity_ids', jsonb_build_array(cp.entity_id::text),
        'event_ids',  jsonb_build_array(cp.event_id::text)
    ) AS properties,
    'geo'                                             AS domain,
    60                                                AS decay_half_life_min
FROM candidate_pairs cp
"""


CROSS_ENTITY_SCAN_SQL = """
-- Cross-entity proximity: pairs of DIFFERENT entity_types in spatial+temporal proximity.
--
-- Phase 2.5 (2026-05-07): rewritten to use the denormalized
-- entity.current_geom + entity.current_position_time columns. The GiST
-- index `entity_current_geom_gist` makes the spatial self-join a single
-- bbox-bounded index seek per outer row — single-digit seconds at v1.0
-- scale (~10K aircraft × 18K vessels) versus 120s+ in the old LATERAL
-- DISTINCT-ON approach.
--
-- Lex-ordering on entity_type (a.entity_type < b.entity_type) ensures
-- (aircraft, vessel) emits exactly once, never (vessel, aircraft) too.
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min
)
SELECT
    'detected_proximity'                                          AS event_type,
    a.entity_type || '_' || b.entity_type                         AS event_subtype,
    GREATEST(a.current_position_time, b.current_position_time)    AS event_time,
    a.current_geom                                                AS geom,
    NULL::real                                                    AS severity,
    a.entity_type || ' ' || COALESCE(a.display_name, a.canonical_id) ||
        ' near ' || b.entity_type || ' ' || COALESCE(b.display_name, b.canonical_id)
                                                                  AS title,
    'Cross-entity proximity: ' || a.entity_type || ' ' || a.canonical_id ||
        ' within ' ||
        ROUND(ST_Distance(a.current_geom, b.current_geom)::numeric, 0) ||
        ' m of ' || b.entity_type || ' ' || b.canonical_id        AS description,
    jsonb_build_object(
        'algorithm',  $3::text,
        'pair_kind',  'entity_to_entity',
        'radius_m',   $2::int,
        'window_min', $1::int,
        'distance_m', ROUND(ST_Distance(a.current_geom, b.current_geom)::numeric, 0)::int,
        'entity_ids', jsonb_build_array(a.id::text, b.id::text),
        'entity_types', jsonb_build_array(a.entity_type, b.entity_type)
    )                                                             AS properties,
    'geo'                                                         AS domain,
    60                                                            AS decay_half_life_min
FROM entity a
JOIN entity b
  ON a.entity_type < b.entity_type
 AND a.current_geom IS NOT NULL
 AND b.current_geom IS NOT NULL
 AND a.current_position_time >= NOW() - ($1 * INTERVAL '1 minute')
 AND b.current_position_time >= NOW() - ($1 * INTERVAL '1 minute')
 AND ST_DWithin(a.current_geom, b.current_geom, $2)
 -- 2026-05-09 fix: exclude satellite from cross-entity proximity.
 -- See PROXIMITY_SCAN_SQL above for the full rationale. Without this,
 -- aircraft↔satellite was producing ~100k false positives per 24h
 -- because satellite ground-tracks at 500km altitude were ST_DWithin
 -- of every plane in the world. Satellites operate in a different
 -- vertical regime than surface entities; they belong in their own
 -- algorithm.
 AND a.entity_type <> 'satellite'
 AND b.entity_type <> 'satellite'
WHERE ($4::text IS NULL OR (a.canonical_id LIKE $4 AND b.canonical_id LIKE $4))
  AND NOT EXISTS (
    -- 2026-05-21: containment form (@>) — see proximity per-event dedup
    -- block above for rationale. Single @> with a 2-element array engages
    -- event_props_gin in one index lookup instead of two post-filters.
    SELECT 1 FROM event finding
    WHERE finding.event_type = 'detected_proximity'
      AND finding.properties->>'algorithm' = $3
      AND finding.event_time >= NOW() - ($1 * INTERVAL '1 minute')
      AND finding.properties @> jsonb_build_object(
              'entity_ids', jsonb_build_array(a.id::text, b.id::text))
  )
"""


async def run_cross_entity_proximity_scan(
    *,
    radius_m: int = 50_000,
    window_min: int = 60,
    algorithm_tag: str = "proximity_cross",
    entity_canonical_id_like: str | None = None,
    timeout_sec: float = 120.0,
) -> int:
    """Find pairs of DIFFERENT entity types within `radius_m` of each other.

    Phase 1.4 of the V2 plan called for "aircraft find vessels + events" —
    the entity↔event side is `run_proximity_scan` above; this function
    handles entity↔entity (aircraft↔vessel etc.). The two run independently
    so they can have different schedules / radii if needed.

    Lex-ordered pairing (entity_type < entity_type) means each conceptual
    pair appears once: (aircraft, vessel) emits but (vessel, aircraft) does
    not. Idempotent within window via NOT EXISTS check.

    Performance: Phase 2.5 rewrote this to spatial-self-join on the
    denormalized `entity.current_geom` column (GiST-indexed) instead of
    LATERAL'ing through position_track. Single-digit seconds at v1.0
    scale. `timeout_sec` retained for safety but rarely needed.

    Returns count of new findings inserted this run.
    """
    async with acquire_write() as conn:
        result = await conn.execute(
            CROSS_ENTITY_SCAN_SQL,
            window_min,
            radius_m,
            algorithm_tag,
            entity_canonical_id_like,
            timeout=timeout_sec,
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"cross-entity proximity scan: {count} findings "
            f"(radius={radius_m}m, window={window_min}min)"
        )
    return count


async def run_proximity_scan(
    *,
    radius_m: int = 50_000,
    window_min: int = 60,
    algorithm_tag: str = "proximity",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Run one proximity-detection pass. Returns count of findings inserted.

    Args:
        radius_m: max distance (meters) for an entity to be flagged as proximate
            to an event. Default 50_000 (50km) per the V2 plan.
        window_min: only consider entity positions and events that occurred in
            the past `window_min` minutes. Bounds the scan and provides the
            dedup window.
        algorithm_tag: written to `properties.algorithm` so different runs of
            the algorithm (e.g. tests vs production) don't dedup against each
            other. Production uses the default 'proximity'.
        entity_canonical_id_like: optional SQL LIKE pattern to restrict the
            scan to entities whose canonical_id matches. Tests use this to
            isolate from production data; production passes None (= all entities).

    Returns:
        Count of `detected_proximity` rows inserted on this run.
    """
    async with acquire_write() as conn:
        # `INSERT ... SELECT` returns row count via the asyncpg result tag.
        result = await conn.execute(
            PROXIMITY_SCAN_SQL,
            window_min,
            radius_m,
            algorithm_tag,
            entity_canonical_id_like,
        )
    # asyncpg returns commands like "INSERT 0 N"
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"proximity scan: {count} findings (radius={radius_m}m, window={window_min}min)"
        )
    return count
