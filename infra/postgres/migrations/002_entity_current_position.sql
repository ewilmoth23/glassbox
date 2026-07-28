-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 002 — denormalize current_position onto entity
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Purpose:
--   Phase 2.5 perf fix. The cross-entity proximity scan
--   (`run_cross_entity_proximity_scan` in
--   `21_GLASSBOX_AI/algorithms/proximity.py`) joined `entity` to
--   `position_track` via a `DISTINCT ON (entity_id)` inside a LATERAL
--   subquery to find each entity's most recent position. PostGIS GiST
--   indexes on `position_track.geom` cannot be pushed through the
--   DISTINCT-ON pattern, which made the cross-entity scan time out at
--   v1.0 scale (~10K aircraft × 18K vessels, >120s).
--
--   Storing the latest position directly on `entity` lets the cross-entity
--   scan be a single GiST self-join on `entity.current_geom`. Single-digit
--   seconds at v1.0 scale.
--
--   `position_track` is unchanged — it remains the immutable history.
--   Only the most-recent snapshot is denormalized; queries that need
--   history still use `position_track`.
--
-- Idempotency:
--   ADD COLUMN ... IF NOT EXISTS + CREATE INDEX IF NOT EXISTS make this
--   safe to re-run. The schema_migration insert at the bottom is also
--   guarded by ON CONFLICT.
--
-- Run as bootstrap user (column ownership matches existing entity table):
--   psql -h 127.0.0.1 -d glassbox -U ewilmoth \
--        -f infra/postgres/migrations/002_entity_current_position.sql
--
-- Reverse (if you must — none of the new columns have NOT NULL constraints
-- so dropping is safe):
--   ALTER TABLE entity DROP COLUMN current_geom, DROP COLUMN current_position_time;
--   DROP INDEX entity_current_geom_gist;
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE entity
    ADD COLUMN IF NOT EXISTS current_geom GEOGRAPHY(POINT, 4326),
    ADD COLUMN IF NOT EXISTS current_position_time TIMESTAMPTZ;

-- GiST spatial index — the whole point of this migration. ST_DWithin and
-- ST_Intersects against current_geom now use this index for sub-millisecond
-- bbox pre-filtering.
CREATE INDEX IF NOT EXISTS entity_current_geom_gist
    ON entity USING gist (current_geom);

-- Composite index for queries that filter by entity_type AND time-window
-- on current_position_time (the cross-entity scan does this).
CREATE INDEX IF NOT EXISTS entity_type_current_time_idx
    ON entity (entity_type, current_position_time DESC)
    WHERE current_position_time IS NOT NULL;

-- ─── Backfill — populate from position_track ─────────────────────────────
-- One-shot. Subsequent ingester writes maintain the columns via
-- writers.write_aircraft_events / writers.write_vessel_events (see
-- `21_GLASSBOX_AI/writers.py`).
--
-- For each entity, set current_geom + current_position_time to the most
-- recent (by time) position_track row. Skips entities with no positions.

UPDATE entity e
SET current_geom = lp.geom,
    current_position_time = lp.time
FROM (
    SELECT DISTINCT ON (entity_id)
        entity_id, geom, time
    FROM position_track
    ORDER BY entity_id, time DESC
) lp
WHERE e.id = lp.entity_id
  AND (e.current_position_time IS NULL OR lp.time > e.current_position_time);

-- ─── schema_migration row ────────────────────────────────────────────────

INSERT INTO schema_migration (version, description, applied_by)
VALUES (
    '002-entity-current-position',
    'Denormalize current_geom + current_position_time onto entity for fast cross-entity proximity. Adds GiST index + composite type/time index. Backfills from position_track.',
    CURRENT_USER
)
ON CONFLICT (version) DO NOTHING;
