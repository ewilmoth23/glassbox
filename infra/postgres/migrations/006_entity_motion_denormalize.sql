-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 006 — denormalize motion kinematics (velocity / heading / altitude) onto entity
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Purpose:
--   Originally added ad-hoc against the live DB on 2026-05-13 (commit
--   `4439383`) without a tracked migration file. This file backfills the
--   migration so a fresh DB rebuild — or restoring a pre-2026-05-13 backup —
--   matches production schema, and the AISStream / planes / satellite
--   writers (which UPSERT these columns) do not break.
--
--   Companion to migration 002 (current_geom + current_position_time). 002
--   denormalized position; 006 denormalizes motion. The viewport query in
--   `21_GLASSBOX_AI/api_v1.py::query_viewport` reads all five columns from
--   `entity` and avoids any join against the 100M+-row `position_track`
--   hypertable on the hot path.
--
-- Writer semantics (see `21_GLASSBOX_AI/writers.py`):
--   - aircraft writes all three columns
--   - vessel writes velocity_ms + heading_deg (altitude_m N/A)
--   - satellite writes velocity_ms + altitude_m (heading N/A in orbit)
--   On UPSERT, columns only overwrite when
--   EXCLUDED.current_position_time > entity.current_position_time
--   (same out-of-order guard as current_geom).
--
-- Idempotency:
--   ADD COLUMN ... IF NOT EXISTS makes this safe to re-run. Running against
--   the live DB where the columns already exist is a no-op; the runner
--   still records the migration in `schema_migration`.
--
-- No indexes:
--   These columns are always read alongside `current_geom` /
--   `current_position_time`, which are already indexed (002). No queries
--   filter on velocity_ms / heading_deg / altitude_m directly.
--
-- Run as bootstrap user (column ownership matches existing entity table):
--   psql -h 127.0.0.1 -d glassbox -U ewilmoth \
--        -f infra/postgres/migrations/006_entity_motion_denormalize.sql
--   OR
--   python3 infra/postgres/run_migrations.py
--
-- Reverse (if you must — no NOT NULL constraints, drop is safe):
--   ALTER TABLE entity
--       DROP COLUMN current_velocity_ms,
--       DROP COLUMN current_heading_deg,
--       DROP COLUMN current_altitude_m;
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE entity
    ADD COLUMN IF NOT EXISTS current_velocity_ms real,
    ADD COLUMN IF NOT EXISTS current_heading_deg real,
    ADD COLUMN IF NOT EXISTS current_altitude_m  real;

-- ─── schema_migration row ────────────────────────────────────────────────
-- Belt-and-suspenders: the migration runner also inserts on success, but
-- this lets a hand-applied psql run track the migration too.

INSERT INTO schema_migration (version, description, applied_by)
VALUES (
    '006-entity-motion-denormalize',
    'Backfill tracked migration for the velocity_ms / heading_deg / altitude_m columns added ad-hoc against the live DB on 2026-05-13 (commit 4439383). Idempotent — columns already exist on prod.',
    CURRENT_USER
)
ON CONFLICT (version) DO NOTHING;
