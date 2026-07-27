"""
Test isolation for the Glassbox suite.

Before this file existed, every test ran against the live `glassbox` database
— the same DB the production daemon is writing to 24/7. That caused 202
fixture-setup errors and 41 dual-write failures in the full-suite run on
2026-05-19 (see backlog P0-F.1) because `init_pool()` raced the daemon for
asyncpg connections and the slowest teardown took 107s deleting test rows
from a 100M+-row hypertable.

This conftest points the suite at `glassbox_test` — a separate database on
the same Postgres instance with the same schema (init.sql + migrations
002/004/005/006). The DB is created/maintained by the operator:

    psql -U ewilmoth -d postgres -c "CREATE DATABASE glassbox_test OWNER glassbox;"
    psql -U ewilmoth -d glassbox_test -c "CREATE EXTENSION IF NOT EXISTS postgis; \
        CREATE EXTENSION IF NOT EXISTS vector;"
    psql -U glassbox -d glassbox_test -f infra/postgres/init.sql
    psql -U glassbox -d glassbox_test -f infra/postgres/migrations/002_entity_current_position.sql
    psql -U glassbox -d glassbox_test -f infra/postgres/migrations/004_mcp_audit_log.sql
    psql -U glassbox -d glassbox_test -f infra/postgres/migrations/005_signals_subscription.sql
    psql -U glassbox -d glassbox_test -f infra/postgres/migrations/006_entity_motion_denormalize.sql

To bypass test isolation (e.g. running a smoke test against the real
glassbox DB intentionally), set GLASSBOX_TEST_USE_LIVE=1 in the environment.

Why two paths below:
  db.py's _build_dsn() checks GLASSBOX_DB_URL FIRST. If set, it returns the
  URL as-is — discrete keys are ignored. .env.glassbox sets GLASSBOX_DB_URL,
  so just overriding GLASSBOX_DB_NAME is insufficient (the URL still points
  at the production DB). We must either:
    (a) rewrite the dbname inside the URL, or
    (b) clear GLASSBOX_DB_URL and set discrete keys including password.
  We do (a) because the URL might encode driver-specific options we don't
  want to drop.

When this conftest runs, .env.glassbox may or may not be loaded yet (db.py
loads it at module import). To be safe we load it ourselves so we can read
the URL and mutate it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv


_EMPIRE_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _EMPIRE_ROOT / ".env.glassbox"
_TEST_DB_NAME = "glassbox_test"


def _swap_dbname(url: str, new_name: str) -> str:
    """Replace the database name in a postgresql:// URL.

    Matches the last `/<dbname>` segment of the path (right before `?` or
    end-of-string). Avoids touching any earlier slashes (e.g. in a Unix-
    socket host).
    """
    return re.sub(r"/[^/?]+(\?|$)", f"/{new_name}\\1", url, count=1)


def _route_to_test_db() -> None:
    # Pull .env.glassbox in so GLASSBOX_DB_URL is populated. load_dotenv
    # does NOT override existing env vars by default, so subsequent
    # assignments below win even if db.py later calls load_dotenv too.
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE)

    url = os.environ.get("GLASSBOX_DB_URL")
    if url:
        os.environ["GLASSBOX_DB_URL"] = _swap_dbname(url, _TEST_DB_NAME)
    # Also set the discrete-key name in case anything reads it directly.
    os.environ["GLASSBOX_DB_NAME"] = _TEST_DB_NAME


# Must happen at module load — conftest.py is pytest's earliest hook, and
# tests will `import db` shortly after.
if not os.environ.get("GLASSBOX_TEST_USE_LIVE"):
    _route_to_test_db()
