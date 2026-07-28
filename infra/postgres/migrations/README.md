# Glassbox Postgres migrations

Tracked migrations live in this directory. Each migration is a single `.sql` file with the form `NNN_descriptor.sql` (zero-padded sequence + short descriptive slug). The runner is at `infra/postgres/run_migrations.py`; tracked state lives in the `schema_migration` table (created automatically by the runner if missing).

## Migration sequence

| # | File | Purpose | Shipped |
|---|---|---|---|
| 001 | (none) | Reserved — never written | — |
| **002** | `002_entity_current_position.sql` | Phase 2.5 perf fix: denormalize `current_geom` + `current_position_time` onto `entity` so the cross-entity proximity scan can use the GiST index on `entity.current_geom` directly. Removed the LATERAL DISTINCT-ON pattern that wouldn't index-push. | 2026-05-08 |
| **003** | *(skipped — see below)* | Reserved in `21_GLASSBOX_AI/docs/GLASSBOX_V2_MIGRATION_PLAN.md:449-456` for **MobilityDB + `vessel_trajectory` (`tgeogpoint`)**. Never built. The trajectory queries it would have enabled (e.g. "vessels traveled >20kn for >3h within polygon Z") shipped as direct PostGIS + `position_track` queries inside `algorithms/port_call.py`, `loitering.py`, and `rendezvous.py` instead. | — |
| **004** | `004_mcp_audit_log.sql` | Audit table for the glassbox MCP servers (`mcp_audit_log`) — every MCP tool call gets a row with caller, tool, params hash, status, latency. | 2026-05-10 |
| **005** | `005_signals_subscription.sql` | Email subscription tables for the `/signals` daily-digest pipeline: `signals_subscription` + `signals_unsubscribe_token` + indexes. | 2026-05-11 |
| **006** | `006_entity_motion_denormalize.sql` | Denormalize `current_velocity_ms` + `current_heading_deg` + `current_altitude_m` onto `entity` (companion to 002). Shipped as ad-hoc `ALTER TABLE` on 2026-05-13 (commit `4439383`); tracked migration backfilled 2026-05-18 (P0-E close-out). | 2026-05-13 (column) / 2026-05-18 (tracked) |

## Why the 003 gap is intentional (not lost)

The number was reserved in the V2 migration plan, the work was scoped, and then the team decided MobilityDB wasn't worth the operational cost. The numbering wasn't reused because:

1. **Git history continuity.** Renumbering would require rewriting old commits that reference "migration 003" in commit messages or PRs.
2. **Team continuity.** Operators reading old docs (notably `21_GLASSBOX_AI/docs/GLASSBOX_V2_MIGRATION_PLAN.md`) will see "Migration 003" referenced. Keeping the gap means the doc still points to a real (absent) place rather than to a confused 003-that-isn't-what-was-promised.
3. **No downside.** Postgres doesn't care about gaps in our application-level sequence. The schema_migration table stores text version slugs, not a numeric sequence.

If a future migration needs a number, it should be **007**, not 003.

## Sequence rules going forward

- New migration numbers are monotonic. Don't fill gaps.
- Slug format: `NNN_short_descriptor.sql` where descriptor is `snake_case` and conveys the intent.
- Every migration must include a tail block that inserts its own row into `schema_migration` with `ON CONFLICT DO NOTHING`. See `006_entity_motion_denormalize.sql` for the canonical template.
- The runner derives the version slug from the filename as `NNN-descriptor` (note: hyphen, not underscore) — this matches what `schema_migration` already holds in the live DB. See `infra/postgres/run_migrations.py::_discover_migrations`.

## Running migrations

```bash
cd "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC"
source .env.glassbox
python3 infra/postgres/run_migrations.py
```

The runner is idempotent — it only applies migrations whose version slug is not already in `schema_migration`. As of 2026-05-19, both `glassbox` and `glassbox_test` databases report `UP TO DATE — no migrations to apply`.

For a fresh clone bootstrap, the order is: create the `glassbox` role + database → `psql -f infra/postgres/init.sql` (extensions + base tables) → `python3 infra/postgres/run_migrations.py` (002 + 004 + 005 + 006 in that order).
