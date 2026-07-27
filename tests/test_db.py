"""
Integration test for db.py — hits the real Postgres on the Mac Mini.

Run from MEWR root with:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_db.py -v

Requires Postgres + .env.glassbox set up per Phase 0.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetchrow, fetch, execute  # noqa: E402


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


async def test_pool_connects_and_returns_select_one():
    val = await fetchval("SELECT 1")
    assert val == 1


async def test_schema_migration_row_present():
    """Phase 0 init.sql inserts schema_migration version 001-init.
    If this fails, the database isn't initialized correctly."""
    row = await fetchrow("SELECT version FROM schema_migration ORDER BY id LIMIT 1")
    assert row is not None
    assert row["version"] == "001-init"


async def test_required_tables_exist():
    """Sanity check that the user-table set we'll write to in Phase 1.1 exists."""
    rows = await fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
        "AND tablename = ANY($1::text[]) ORDER BY tablename",
        ["entity", "position_track", "event", "source"],
    )
    names = sorted(r["tablename"] for r in rows)
    assert names == ["entity", "event", "position_track", "source"]


async def test_required_extensions_present():
    rows = await fetch(
        "SELECT extname FROM pg_extension WHERE extname = ANY($1::text[]) ORDER BY extname",
        ["postgis", "timescaledb", "vector", "btree_gist"],
    )
    names = sorted(r["extname"] for r in rows)
    assert names == ["btree_gist", "postgis", "timescaledb", "vector"]


async def test_transaction_rollback_works():
    """Make sure the contract for transactional writes is honored — needed by
    1.1 dual-write so a failed entity insert doesn't leave a stranded
    position_track row."""
    from db import acquire

    # Insert a sentinel row, but rollback. Verify it didn't persist.
    sentinel_type = "test_db_rollback_sentinel"
    try:
        async with acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO source (source_type, fetched_at) VALUES ($1, NOW())",
                    sentinel_type,
                )
                # Force rollback
                raise RuntimeError("intentional rollback")
    except RuntimeError:
        pass

    count = await fetchval(
        "SELECT count(*) FROM source WHERE source_type = $1", sentinel_type
    )
    assert count == 0, "rollback failed — row persisted"


async def test_helper_round_trip():
    """Test the full insert/select/cleanup path through the helpers."""
    sentinel = "test_db_helper_roundtrip"
    try:
        await execute(
            "INSERT INTO source (source_type, fetched_at) VALUES ($1, NOW())",
            sentinel,
        )
        row = await fetchrow(
            "SELECT source_type FROM source WHERE source_type = $1", sentinel
        )
        assert row is not None
        assert row["source_type"] == sentinel
    finally:
        await execute("DELETE FROM source WHERE source_type = $1", sentinel)


# ─── P1-A: two-pool split tests (added 2026-05-21) ────────────────────────


async def test_two_pools_both_initialize():
    """init_pool() (legacy) calls init_pools() under the hood — both
    api_pool and write_pool should exist after the autouse fixture runs."""
    from db import get_api_pool, get_write_pool
    api = get_api_pool()
    write = get_write_pool()
    assert api is not None
    assert write is not None
    # The two pools are independent objects, not aliases
    assert api is not write


async def test_pool_stats_reports_both_pools():
    """pool_stats() should return {'api': {...}, 'write': {...}} alongside
    the legacy flat keys (which mirror the write pool for backwards compat)."""
    from db import pool_stats
    s = pool_stats()
    assert s.get("initialized") is True
    assert "api" in s and "write" in s
    for label in ("api", "write"):
        sub = s[label]
        assert sub.get("size") is not None
        assert sub.get("min_size") is not None
        assert sub.get("max_size") is not None
        assert sub.get("free") is not None
        assert sub.get("in_use") is not None
    # Legacy flat keys exist and mirror the write pool
    assert s["size"] == s["write"]["size"]
    assert s["max_size"] == s["write"]["max_size"]


async def test_api_pool_has_stricter_statement_timeout():
    """The api_pool should be configured with a 10 s statement_timeout;
    the write_pool with 120 s. We verify via SHOW statement_timeout on
    a connection from each pool."""
    from db import acquire_read, acquire_write
    async with acquire_read() as conn:
        timeout_str = await conn.fetchval("SHOW statement_timeout")
    # Postgres formats this as e.g. "10s" or "10000ms" depending on settings
    # Normalize to milliseconds
    api_ms = _parse_pg_duration_ms(timeout_str)
    assert api_ms == 10_000, f"api_pool timeout was {timeout_str} ({api_ms} ms)"

    async with acquire_write() as conn:
        timeout_str = await conn.fetchval("SHOW statement_timeout")
    write_ms = _parse_pg_duration_ms(timeout_str)
    assert write_ms == 120_000, f"write_pool timeout was {timeout_str} ({write_ms} ms)"


async def test_api_pool_kills_slow_query_write_pool_does_not():
    """Functional verification that the 10 s api_pool actually enforces:
    a 15-second pg_sleep should time out on acquire_read but complete on
    acquire_write. This is the core regression test for the split — if
    this passes, viewport p95 under writer load is structurally protected.

    Uses pg_sleep(11) — just over the 10 s api_pool ceiling, well under
    the 120 s write_pool ceiling, and quick enough that the test suite
    doesn't drag (each side takes 10–11 s)."""
    import asyncpg
    from db import acquire_read, acquire_write

    # 1. acquire_read should raise QueryCanceledError (or TimeoutError if
    #    asyncpg's command_timeout fires first — both indicate enforcement).
    cancelled = False
    try:
        async with acquire_read() as conn:
            await conn.fetchval("SELECT pg_sleep(11)")
    except (asyncpg.QueryCanceledError, asyncio.TimeoutError):
        cancelled = True
    except Exception as e:
        # asyncpg may also raise InternalServerError wrapping the canceled
        # statement message. Accept anything that mentions "canceled" or
        # "timeout" as evidence the enforcement worked.
        msg = str(e).lower()
        if "cancel" in msg or "timeout" in msg or "statement_timeout" in msg:
            cancelled = True
        else:
            raise
    assert cancelled, (
        "api_pool did NOT enforce statement_timeout — a 11 s sleep "
        "completed against the 10 s read-pool limit. The split is broken.")

    # 2. acquire_write should complete the same query (its timeout is 120 s)
    async with acquire_write() as conn:
        # Use a slightly shorter sleep to keep the test under 25 s total
        val = await conn.fetchval("SELECT pg_sleep(11)")
    # pg_sleep returns void, asyncpg surfaces as None. Reaching here = success.
    assert val is None


async def test_legacy_acquire_routes_to_write_pool():
    """The pre-split `acquire()` context manager must continue to work
    and must route to the write pool (the safer default — legacy callers
    may include heavy seeders that can't tolerate the 10 s read timeout)."""
    from db import acquire, acquire_write
    async with acquire() as legacy_conn:
        legacy_timeout = await legacy_conn.fetchval("SHOW statement_timeout")
    async with acquire_write() as write_conn:
        write_timeout = await write_conn.fetchval("SHOW statement_timeout")
    assert legacy_timeout == write_timeout
    assert _parse_pg_duration_ms(legacy_timeout) == 120_000


async def test_legacy_helpers_still_work():
    """The pre-split module helpers (fetchval, fetch, execute) keep working
    after the split, routed to the write pool. This is the contract that
    the ~50 test files importing them rely on."""
    from db import fetchval as legacy_fetchval
    val = await legacy_fetchval("SELECT 42")
    assert val == 42


# ─── helpers ──────────────────────────────────────────────────────────────


def _parse_pg_duration_ms(raw: str) -> int:
    """Parse Postgres SHOW statement_timeout output to milliseconds.

    Examples seen in the wild:
        "10s"      → 10000
        "10000ms"  → 10000
        "2min"     → 120000
        "120s"     → 120000
        "0"        → 0  (disabled)
    """
    s = raw.strip().lower()
    if s in ("0", "off", ""):
        return 0
    if s.endswith("ms"):
        return int(s[:-2])
    if s.endswith("s"):
        return int(float(s[:-1]) * 1000)
    if s.endswith("min"):
        return int(float(s[:-3]) * 60_000)
    if s.endswith("h"):
        return int(float(s[:-1]) * 3_600_000)
    # Fallback: assume ms
    return int(s)


# Needed for asyncio.TimeoutError in test_api_pool_kills_slow_query_write_pool_does_not
import asyncio  # noqa: E402


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
