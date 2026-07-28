#!/usr/bin/env python3
"""
Glassbox Postgres migration runner.

Applies init.sql + any future migrations under infra/postgres/migrations/
idempotently, tracking what's been applied in the schema_migration table.

USAGE:
    # Apply all pending migrations
    python3 infra/postgres/run_migrations.py

    # Just print what WOULD run, don't execute
    python3 infra/postgres/run_migrations.py --dry-run

    # Show current schema state + applied migrations
    python3 infra/postgres/run_migrations.py --status

ENV VARS (with sensible defaults):
    PG_HOST     127.0.0.1
    PG_PORT     5432
    PG_DB       glassbox
    PG_USER     glassbox
    PG_PASSWORD <required if not in pgpass>

EXIT CODES:
    0 — success (or no-op if up to date)
    1 — connection failed / permission denied
    2 — migration applied with errors (rolled back)

CONVENTIONS for future migrations:
    File naming: NNN_short_description.sql (e.g. 002_add_event_severity_index.sql)
    NNN is a zero-padded 3-digit version. Init.sql is always version "001".
    Migrations are applied in NUMERICAL order, never lexical (003 > 002 > 001 > 011 = WRONG).
    Each file is wrapped in a single transaction. If any statement fails, the
    whole file rolls back and the migration is NOT marked applied.

REVERSIBILITY:
    For breaking changes, supply a paired down-migration as NNN_descriptor.down.sql.
    Run with --rollback NNN to apply the .down.sql AND remove the schema_migration row.
    Most schema additions don't need a down file — leave the row in place.

This script depends on:
    pip3 install --break-system-packages psycopg[binary]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import psycopg
except ImportError:
    print("FATAL: psycopg3 not installed. Run:")
    print("    pip3 install --break-system-packages 'psycopg[binary]'")
    sys.exit(1)


# ─── Config ───────────────────────────────────────────────────────────────


SCRIPT_DIR = Path(__file__).resolve().parent
INIT_SQL = SCRIPT_DIR / "init.sql"
MIGRATIONS_DIR = SCRIPT_DIR / "migrations"

DEFAULT_PG = {
    "host":     os.environ.get("PG_HOST",     "127.0.0.1"),
    "port":     os.environ.get("PG_PORT",     "5432"),
    "dbname":   os.environ.get("PG_DB",       "glassbox"),
    "user":     os.environ.get("PG_USER",     "glassbox"),
    "password": os.environ.get("PG_PASSWORD", ""),
}


# ─── Migration discovery ──────────────────────────────────────────────────


def _filename_to_version(path: Path) -> str:
    """Derive the canonical version slug from a migration filename.

    `init.sql` → "001-init"
    `002_entity_current_position.sql` → "002-entity-current-position"

    This matches the format the SQL files themselves use in their tail
    `INSERT INTO schema_migration` statements and the rows already present
    in the live DB. Earlier versions of this runner used the bare
    `NNN` prefix, which silently inserted phantom rows alongside the
    descriptor-suffix rows the SQL files wrote.
    """
    if path.name == "init.sql":
        return "001-init"
    # NNN_some_descriptor.sql → NNN-some-descriptor
    stem = path.stem  # strips .sql
    return stem.replace("_", "-")


def _discover_migrations() -> List[Tuple[str, Path]]:
    """Return [(version, path), ...] sorted by numeric prefix.

    Always includes init.sql as version "001-init". Then anything under
    migrations/ matching ^(\\d{3})_.*\\.sql$ (excluding *.down.sql).
    """
    out: List[Tuple[str, Path]] = []
    if INIT_SQL.exists():
        out.append((_filename_to_version(INIT_SQL), INIT_SQL))

    if MIGRATIONS_DIR.is_dir():
        rx = re.compile(r"^(\d{3})_[A-Za-z0-9_]+\.sql$")
        for path in sorted(MIGRATIONS_DIR.iterdir()):
            if path.name.endswith(".down.sql"):
                continue
            m = rx.match(path.name)
            if not m:
                continue
            # Skip if this is a stray duplicate of init.sql's slot
            if m.group(1) == "001" and INIT_SQL.exists():
                continue
            out.append((_filename_to_version(path), path))

    # Sort by the NNN numeric prefix of the version slug
    out.sort(key=lambda t: int(t[0].split("-", 1)[0]))
    return out


# ─── DB ops ───────────────────────────────────────────────────────────────


def _connect():
    if not DEFAULT_PG["password"]:
        # pgpass might still work; let psycopg try
        pass
    try:
        conn = psycopg.connect(**{k: v for k, v in DEFAULT_PG.items() if v != ""})
        conn.autocommit = False
        return conn
    except Exception as e:
        print(f"FATAL: connection failed — {e}")
        sys.exit(1)


def _ensure_schema_migration_table(conn) -> None:
    """Create schema_migration table if it doesn't exist.

    Schema matches what init.sql creates, so re-running on an init.sql-
    populated DB is a no-op. Mostly a safety net — init.sql also creates
    this table and runs before any other migration.
    """
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_migration (
                id SERIAL PRIMARY KEY,
                version TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                applied_by TEXT NOT NULL DEFAULT current_user
            );
        """)
        conn.commit()


def _list_applied(conn) -> List[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migration ORDER BY version;")
        return [r[0] for r in cur.fetchall()]


def _apply_one(conn, version: str, path: Path, dry_run: bool) -> bool:
    """Apply a single migration file. Returns True on success."""
    sql = path.read_text(encoding="utf-8")
    description = _extract_description(sql)
    print(f"  [{'DRY-RUN' if dry_run else 'APPLY  '}] {version}: {path.name}  — {description[:60]}")

    if dry_run:
        return True

    with conn.cursor() as cur:
        try:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migration (version, description) VALUES (%s, %s) ON CONFLICT (version) DO NOTHING;",
                (version, description),
            )
            conn.commit()
            print(f"           ✓ committed.")
            return True
        except Exception as e:
            conn.rollback()
            print(f"           ✗ FAILED — rolled back. Error: {e}")
            return False


def _extract_description(sql: str) -> str:
    """Pull the first non-empty comment line from SQL as the description."""
    for line in sql.splitlines():
        line = line.strip()
        if line.startswith("--") and len(line) > 2:
            return line.lstrip("- ").strip()
        if line and not line.startswith("--"):
            break
    return "(no description)"


# ─── Subcommands ──────────────────────────────────────────────────────────


def cmd_status(conn) -> int:
    print("Current Glassbox schema state:")
    print(f"  DB:   {DEFAULT_PG['dbname']} @ {DEFAULT_PG['host']}:{DEFAULT_PG['port']}")
    print(f"  User: {DEFAULT_PG['user']}")
    print()

    _ensure_schema_migration_table(conn)
    applied = set(_list_applied(conn))
    discovered = _discover_migrations()

    print(f"Discovered: {len(discovered)} migration files")
    print(f"Applied:    {len(applied)} migration(s)")
    print()
    print(f"{'version':<10} {'status':<10} file")
    print("-" * 70)
    for v, path in discovered:
        st = "applied" if v in applied else "pending"
        print(f"{v:<10} {st:<10} {path.name}")
    print()
    pending = [v for v, _ in discovered if v not in applied]
    if pending:
        print(f"PENDING: {len(pending)} migration(s) — run without --status to apply.")
    else:
        print("UP TO DATE — no migrations to apply.")
    return 0


def cmd_apply(conn, dry_run: bool) -> int:
    _ensure_schema_migration_table(conn)
    applied = set(_list_applied(conn))
    discovered = _discover_migrations()

    pending = [(v, p) for v, p in discovered if v not in applied]
    if not pending:
        print("Nothing to do — schema is up to date.")
        return 0

    print(f"Applying {len(pending)} pending migration(s)...")
    if dry_run:
        print("(DRY-RUN mode — no changes will be made.)")
    print()

    for v, path in pending:
        ok = _apply_one(conn, v, path, dry_run=dry_run)
        if not ok:
            print()
            print("HALT — migration failed. Fix the SQL and re-run.")
            return 2
    print()
    print(f"Done. Applied {len(pending)} migration(s).")
    return 0


# ─── CLI ──────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Glassbox Postgres migration runner")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would run; don't execute.")
    ap.add_argument("--status", action="store_true",
                    help="Show applied + pending migrations and exit.")
    args = ap.parse_args()

    conn = _connect()
    try:
        if args.status:
            return cmd_status(conn)
        return cmd_apply(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
