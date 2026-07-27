"""
Shadow-fleet cluster detector — Phase 4 algorithm #12.

Strategic context: shadow-fleet operations rarely involve a single
vessel. STS (ship-to-ship) transfers, dark-cluster gatherings, and
sanctions-evasion fleets typically involve 3+ vessels in a small
radius. By detecting these clusters we surface the WHOLE OPERATION,
not just a single rendezvous pair.

Three signals already cover individual rendezvous (sanctions_rendezvous
algorithm). This algorithm goes a step further: detect when N+ ACTIVE
sanctioned-vessel-underway findings exist within R km, regardless of
pairwise relationships. That's a fleet, not a meeting.

Detection thresholds (defaults, overridable):
  - min_cluster_size = 3 vessels
  - radius_m         = 10_000 m (10 km)
  - lookback_hours   = 6 (recent enough to be real-time)

Implementation:
  - Pull recent sanctioned_vessel_underway events with their geom +
    entity_id + sanctioning_authority.
  - Self-join via ST_DWithin to find each vessel's nearby peers.
  - Group by anchor entity_id; emit one cluster event per anchor whose
    neighborhood meets min_cluster_size.
  - Idempotency: dedup on (sorted set of anchor + neighbor entity_ids,
    24h). A cluster's vessel set changing → fresh event.

Severity 10 — multi-vessel sanctioned cluster is one of the highest-
priority operational findings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.shadow_fleet_cluster")


SHADOW_FLEET_CLUSTER_SQL = """
-- Pull recent sanctioned-vessel-underway events into per-vessel
-- representatives (latest event per entity in the lookback window).
WITH recent_sanc AS (
    SELECT DISTINCT ON (entity_id)
        entity_id,
        geom,
        properties->>'sanctioning_authority' AS authority,
        properties->>'live_vessel_name'      AS name,
        properties->>'mmsi'                  AS mmsi,
        event_time
    FROM event
    WHERE event_type = 'sanctioned_vessel_underway'
      AND event_time >= $1::timestamptz
      AND geom IS NOT NULL
      AND entity_id IS NOT NULL
    ORDER BY entity_id, event_time DESC
),
-- Proper geometric clustering via DBSCAN on Web Mercator-projected
-- points. Returns the same cluster_id for every vessel within `eps`
-- meters of at least one other cluster member. This produces ONE row
-- per physical gathering regardless of vessel count — fixes the
-- pivot-explosion bug where 11 anchors with overlapping neighborhoods
-- each emit a near-identical cluster.
--
-- Mercator distorts distances at high latitudes (~2x at 60°), so the
-- 10km threshold becomes effectively 5-7km near poles. Maritime shadow
-- fleet activity concentrates in mid-latitudes (Strait of Malacca,
-- Persian Gulf, Black Sea, Mediterranean) where distortion is <1.4x.
-- Acceptable for this use-case.
dbscan_clusters AS (
    SELECT
        entity_id, geom, authority, name, mmsi,
        ST_ClusterDBSCAN(ST_Transform(geom::geometry, 3857), $2::float, $3::int)
            OVER ()  AS cluster_id
    FROM recent_sanc
),
clusters AS (
    SELECT
        cluster_id,
        -- Anchor = vessel with smallest entity_id in the cluster.
        -- Deterministic + idempotent across runs with the same set.
        (array_agg(entity_id ORDER BY entity_id::text))[1]   AS anchor_id,
        (array_agg(geom ORDER BY entity_id::text))[1]        AS anchor_geom,
        (array_agg(name ORDER BY entity_id::text))[1]        AS anchor_name,
        (array_agg(mmsi ORDER BY entity_id::text))[1]        AS anchor_mmsi,
        array_agg(DISTINCT entity_id::text ORDER BY entity_id::text)
            AS member_ids,
        array_agg(DISTINCT name ORDER BY name)
            FILTER (WHERE name IS NOT NULL)                  AS member_names,
        array_agg(DISTINCT authority ORDER BY authority)
            FILTER (WHERE authority IS NOT NULL)             AS authorities,
        COUNT(DISTINCT entity_id)                            AS cluster_size,
        -- Cluster diameter in meters (Mercator). ST_ClusterDBSCAN only
        -- guarantees density-reachability (each point within eps of *some*
        -- cluster member), NOT bounded diameter. Chains of overlapping
        -- 10 km balls along dense global shipping lanes produced false
        -- "clusters" of 100+ vessels spanning continents (verified
        -- 2026-05-19 audit, 81% production FP rate). Project to 3857
        -- (same projection DBSCAN uses) so the result is in meters.
        ST_MaxDistance(
            ST_Collect(ST_Transform(geom::geometry, 3857)),
            ST_Collect(ST_Transform(geom::geometry, 3857))
        )                                                    AS diameter_m
    FROM dbscan_clusters
    WHERE cluster_id IS NOT NULL  -- skip noise points
    GROUP BY cluster_id
    HAVING COUNT(DISTINCT entity_id) >= $3::int
       -- Diameter cap: cluster must fit within 3× eps (~30 km for the
       -- default 10 km radius). Rejects DBSCAN-chain artifacts where
       -- transitive density-reachability stitches unrelated vessels into
       -- the same cluster. See
       -- ALGORITHM_FP_AUDIT_shadow_fleet_cluster_2026_05_19.md §7.
       AND ST_MaxDistance(
               ST_Collect(ST_Transform(geom::geometry, 3857)),
               ST_Collect(ST_Transform(geom::geometry, 3857))
           ) <= $2::float * 3
),
canonical_clusters AS (
    -- One row per DBSCAN cluster (no pivot duplication possible now —
    -- but keeping the alias name so downstream column references
    -- stay stable). DISTINCT ON the member_ids set for an extra
    -- safety net in case ST_ClusterDBSCAN ever emits ambiguous groups.
    SELECT DISTINCT ON (member_ids)
        anchor_id, anchor_geom, anchor_name, anchor_mmsi,
        member_ids, member_names, authorities, cluster_size, diameter_m
    FROM clusters
    ORDER BY member_ids, anchor_id
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'shadow_fleet_cluster'                                  AS event_type,
    CASE
        WHEN c.cluster_size >= 6 THEN 'large_fleet'
        WHEN c.cluster_size >= 4 THEN 'fleet'
        ELSE 'cluster'
    END                                                     AS event_subtype,
    NOW()                                                   AS event_time,
    c.anchor_geom                                           AS geom,
    10.0::real                                              AS severity,
    'CRITICAL — Shadow-fleet cluster: ' || c.cluster_size ||
        ' sanctioned vessels within ' ||
        ROUND(($2::numeric / 1000.0), 1) || ' km' ||
        CASE WHEN array_length(c.authorities, 1) >= 2
             THEN ' [multi-jurisdictional]'
             ELSE '' END                                    AS title,
    'Detected ' || c.cluster_size || ' active sanctioned-vessel ' ||
        'broadcasts within ' || ROUND(($2::numeric / 1000.0), 1) ||
        ' km of ' || COALESCE(c.anchor_name, 'MMSI ' || c.anchor_mmsi) ||
        '. Members: ' ||
        array_to_string(
            (SELECT array_agg(n) FROM unnest(c.member_names) AS n WHERE n IS NOT NULL),
            ', '
        ) ||
        '. Authorities involved: ' ||
        array_to_string(c.authorities, ', ') ||
        '. Multi-vessel sanctioned-vessel clusters typically indicate ' ||
        'STS transfer operations, dark-cluster gatherings, or ' ||
        'coordinated shadow-fleet logistics.'                AS description,
    jsonb_build_object(
        'algorithm',          $4::text,
        'cluster_size',       c.cluster_size,
        'radius_m',           $2::numeric,
        'diameter_m',         ROUND(c.diameter_m::numeric, 1),
        'lookback_hours',     $5::int,
        'member_entity_ids',  c.member_ids,
        'member_names',       c.member_names,
        'authorities',        c.authorities,
        'authority_count',    array_length(c.authorities, 1),
        'fcra_safe',          false,
        'multi_jurisdictional', array_length(c.authorities, 1) >= 2
    )                                                       AS properties,
    'maritime'                                              AS domain,
    1440                                                    AS decay_half_life_min,
    c.anchor_id                                             AS entity_id
FROM canonical_clusters c
WHERE (
        $7::text IS NULL
        OR (c.anchor_mmsi LIKE $7)
      )
  AND NOT EXISTS (
      SELECT 1 FROM event prior
      WHERE prior.event_type = 'shadow_fleet_cluster'
        AND prior.properties->>'algorithm' = $4
        -- Same member set within dedup window = no re-emit.
        AND (prior.properties->'member_entity_ids')::jsonb
              = to_jsonb(c.member_ids)
        AND prior.event_time >= $6::timestamptz
  )
"""


async def run_shadow_fleet_cluster_scan(
    *,
    radius_m: float = 10_000.0,
    min_cluster_size: int = 3,
    lookback_hours: int = 6,
    dedup_window_hours: int = 24,
    algorithm_tag: str = "shadow_fleet_cluster",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Detect clusters of ≥ N active sanctioned vessels within R km.
    Returns count of new findings.

    Args:
        radius_m: cluster radius. Default 10 km — typical STS-transfer
            scale; tighter than the 50-km proximity threshold so this
            captures tactical fleets, not regional groupings.
        min_cluster_size: minimum vessel count to qualify. Default 3.
        lookback_hours: only consider sanctioned_vessel_underway events
            from within this window. Default 6h — matches the freshness
            of an "active" cluster.
        dedup_window_hours: same member set flagged at most once per this
            window. Default 24h.
        algorithm_tag: properties.algorithm sentinel for test isolation.
        entity_canonical_id_like: optional MMSI LIKE for tests.

    Returns:
        Count of `shadow_fleet_cluster` rows inserted.
    """
    now = datetime.now(timezone.utc)
    lookback_cutoff = now - timedelta(hours=lookback_hours)
    dedup_cutoff    = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            SHADOW_FLEET_CLUSTER_SQL,
            lookback_cutoff,              # $1
            radius_m,                     # $2
            min_cluster_size,             # $3
            algorithm_tag,                # $4
            lookback_hours,               # $5
            dedup_cutoff,                 # $6
            entity_canonical_id_like,     # $7
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"shadow-fleet cluster: {count} new clusters of ≥{min_cluster_size} "
            f"sanctioned vessels within {radius_m/1000:.0f} km"
        )
    return count
