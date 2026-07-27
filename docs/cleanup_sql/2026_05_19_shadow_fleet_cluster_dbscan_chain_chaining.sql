-- Shadow-fleet cluster FP cleanup — DBSCAN density-reachability chain bug
-- Audit doc: 21_GLASSBOX_AI/docs/ALGORITHM_FP_AUDIT_shadow_fleet_cluster_2026_05_19.md
-- Date: 2026-05-19
--
-- ROOT CAUSE
-- ----------
-- `21_GLASSBOX_AI/algorithms/shadow_fleet_cluster.py` lines 74-80:
--     ST_ClusterDBSCAN(ST_Transform(geom::geometry, 3857), eps=10_000m, minpts=3)
-- DBSCAN's density-reachability chain property means:
--   if A is within eps of B, and B within eps of C, ..., and Y within eps of Z,
--   then A and Z are placed in the same cluster *even though* they may be
--   thousands of km apart.
-- Empirically: clusters with cluster_size >= 10 span 100+ degrees of lat/lon
-- (verified for evt 04c76094 — 130 vessels spanning Atlantic→East Asia,
--  lon_span=152°, lat_span=94°). Title still claims "within 10.0 km".
--
-- FP CLASS
-- --------
-- Any cluster whose member entity positions span > 1° longitude OR latitude
-- (~111 km — 11× the claimed 10 km radius) is geometrically inconsistent
-- with the algorithm's marketing claim. This dominates large clusters
-- because sanctioned vessels are scattered along global shipping lanes.
--
-- SAFETY
-- ------
-- Audit-preserving UPDATE only. Sets withdrawn=true + withdrawal_reason.
-- Wrap in BEGIN/ROLLBACK first to verify counts.
--
-- EXPECTED COUNTS (live DB, 2026-05-19)
-- -------------------------------------
--   Total non-withdrawn shadow_fleet_cluster: 2,748
--   With cluster_size >= 10 (almost certainly FPs by chain bug): 2,079
--   With cluster_size 3-9 (need member-pos verification): 669
--
-- This script flags as FP only the clusters that fail an explicit
-- per-cluster geometric span check (> 1° lat or lon). Some small (sz=3-5)
-- clusters with global spread will also be caught.

BEGIN;

WITH per_evt AS (
    SELECT e.id, e.properties->'member_entity_ids' AS mids
    FROM event e
    WHERE e.event_type = 'shadow_fleet_cluster'
      AND (e.properties->>'withdrawn') IS NULL
),
exploded AS (
    SELECT p.id AS evt_id, (jsonb_array_elements_text(p.mids))::uuid AS m_id
    FROM per_evt p
),
spans AS (
    SELECT ex.evt_id,
           (MAX(ST_X(ent.current_geom::geometry)) - MIN(ST_X(ent.current_geom::geometry))) AS lon_span,
           (MAX(ST_Y(ent.current_geom::geometry)) - MIN(ST_Y(ent.current_geom::geometry))) AS lat_span,
           COUNT(*) AS pos_found
    FROM exploded ex JOIN entity ent ON ent.id = ex.m_id
    WHERE ent.current_geom IS NOT NULL
    GROUP BY ex.evt_id
),
fp_ids AS (
    SELECT evt_id FROM spans
    WHERE lon_span > 1.0 OR lat_span > 1.0   -- > 111 km, 11x claimed 10 km radius
)
UPDATE event SET properties = properties || jsonb_build_object(
    'withdrawn', true,
    'withdrawal_reason', 'dbscan_density_reachability_chain_bug',
    'withdrawal_audit', 'ALGORITHM_FP_AUDIT_shadow_fleet_cluster_2026_05_19',
    'withdrawn_at', now()::text
)
WHERE id IN (SELECT evt_id FROM fp_ids);
-- NB: `RETURNING id;` was REMOVED on 2026-05-19 after a prior run hung
-- psql for 19+ minutes trying to print 2,225 UUIDs (~180 KB) to a slow
-- terminal renderer. The row count is reported by psql via the standard
-- `UPDATE N` line, which is sufficient for verification.

-- Interim sanity-check (before COMMIT): expected withdrawn_now = 2,225, still_active = 523.
SELECT
  COUNT(*) FILTER (WHERE (properties->>'withdrawn')::bool = true) AS withdrawn_now,
  COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS still_active
FROM event
WHERE event_type = 'shadow_fleet_cluster';

COMMIT; -- flipped from ROLLBACK on 2026-05-19 after dry-run verified 2,225 == expected

-- After commit, sanity check:
-- SELECT
--   COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS still_active,
--   COUNT(*) FILTER (WHERE (properties->>'withdrawn')::boolean = true) AS withdrawn,
--   COUNT(*) AS total
-- FROM event WHERE event_type = 'shadow_fleet_cluster';
