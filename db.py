"""
Glassbox Postgres connection management.

Two async connection pools for the whole server process. Pools are created
at server startup (call `await init_pools()`) and closed at shutdown.

  - **api_pool**   — short statement_timeout (10s). Used by API route handlers
                     for viewport, entity detail, signals, health, etc. A
                     misbehaving SQL or runaway scan fails fast instead of
                     hogging a connection while the ingesters back up.
  - **write_pool** — long statement_timeout (120s). Used by writers
                     (`writers.py`), algorithm emitters (`algorithms/*.py`),
                     and any heavy maintenance query. Long-running UPSERTs +
                     entity↔event spatial scans need the headroom.

The split was added 2026-05-21 as P1-A in `GLASSBOX_BACKEND_BACKLOG.md`. Before
the split, a single pool of 30 connections meant that a sustained writer load
could starve API consumers; viewport p95 occasionally hit 40s. With the split,
the API has a dedicated 10-connection budget that cannot be exhausted by
writers no matter how loaded they are.

Credentials come from `.env.glassbox` at empire root (gitignored). The five
required keys are GLASSBOX_DB_{HOST,PORT,NAME,USER,PASSWORD} or the combined
GLASSBOX_DB_URL — the URL takes precedence if both are set.

Usage:

    from db import init_pools, close_pools, acquire_read, acquire_write
    from db import fetch_read, fetchrow_read, execute_write

    await init_pools()
    rows = await fetch_read("SELECT * FROM entity WHERE entity_type = $1", "aircraft")
    async with acquire_write() as conn:
        async with conn.transaction():
            await conn.execute("INSERT ...")
    await close_pools()

Backwards compatibility — the original single-pool helpers still work:

    from db import init_pool, close_pool, acquire, fetch, fetchrow, execute

These delegate to the **write pool** (the safer default — long-timeout queries
don't fail unexpectedly when invoked from legacy callers). New code should
use the explicit `_read` / `_write` variants.

The pools are process-wide. Tests that want isolation should use a transactional
fixture that begins a transaction and rolls back at teardown.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional

import asyncpg
from dotenv import load_dotenv


# ─── Load .env.glassbox from empire root ──────────────────────────────────

_EMPIRE_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _EMPIRE_ROOT / ".env.glassbox"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


def _build_dsn() -> str:
    """Build a DSN from env vars. URL takes precedence over discrete keys."""
    url = os.environ.get("GLASSBOX_DB_URL")
    if url:
        return url
    host = os.environ.get("GLASSBOX_DB_HOST", "127.0.0.1")
    port = os.environ.get("GLASSBOX_DB_PORT", "5432")
    name = os.environ.get("GLASSBOX_DB_NAME", "glassbox")
    user = os.environ.get("GLASSBOX_DB_USER", "glassbox")
    password = os.environ.get("GLASSBOX_DB_PASSWORD")
    if not password:
        raise RuntimeError(
            "Glassbox Postgres credentials missing. Expected GLASSBOX_DB_URL or "
            "GLASSBOX_DB_PASSWORD (+ HOST/PORT/NAME/USER) in .env.glassbox at "
            f"{_ENV_FILE}."
        )
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


# ─── Pool singletons ──────────────────────────────────────────────────────

_api_pool: Optional[asyncpg.Pool] = None
_write_pool: Optional[asyncpg.Pool] = None


# Tuning defaults. Postgres default max_connections is 100, so 10 + 20 = 30
# total leaves plenty of headroom for psql / pg_cron / replication. The
# pre-split shared pool was also 30, so memory footprint is unchanged.
_API_MIN, _API_MAX = 4, 10
_API_STATEMENT_TIMEOUT_MS = 10_000      # 10 s — read queries should be fast
_API_COMMAND_TIMEOUT_S = 10.0

_WRITE_MIN, _WRITE_MAX = 4, 20
_WRITE_STATEMENT_TIMEOUT_MS = 120_000   # 120 s — matches pre-split value
_WRITE_COMMAND_TIMEOUT_S = 120.0


async def init_pools(
    *,
    api_min_size: int = _API_MIN, api_max_size: int = _API_MAX,
    write_min_size: int = _WRITE_MIN, write_max_size: int = _WRITE_MAX,
) -> None:
    """Create both pools. Idempotent — re-calling with an already-initialized
    pool is a no-op. Use init_pools() at server boot, before any handler runs.

    Per 2026-05-21 P1-A: the API pool gets a strict 10 s statement_timeout so
    a runaway SELECT can't pin a connection beyond the cap; the write pool
    keeps the legacy 120 s timeout for heavy ingester/algorithm SQL.
    """
    global _api_pool, _write_pool
    dsn = _build_dsn()

    if _api_pool is None:
        _api_pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=api_min_size,
            max_size=api_max_size,
            command_timeout=_API_COMMAND_TIMEOUT_S,
            server_settings={
                "statement_timeout":          str(_API_STATEMENT_TIMEOUT_MS),
                "application_name":           "glassbox-server-api",
            },
        )

    if _write_pool is None:
        _write_pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=write_min_size,
            max_size=write_max_size,
            command_timeout=_WRITE_COMMAND_TIMEOUT_S,
            server_settings={
                "statement_timeout":          str(_WRITE_STATEMENT_TIMEOUT_MS),
                "application_name":           "glassbox-server-write",
            },
        )


async def close_pools() -> None:
    global _api_pool, _write_pool
    if _api_pool is not None:
        await _api_pool.close()
        _api_pool = None
    if _write_pool is not None:
        await _write_pool.close()
        _write_pool = None


def get_api_pool() -> asyncpg.Pool:
    if _api_pool is None:
        raise RuntimeError("API pool not initialized — call await init_pools() first.")
    return _api_pool


def get_write_pool() -> asyncpg.Pool:
    if _write_pool is None:
        raise RuntimeError("Write pool not initialized — call await init_pools() first.")
    return _write_pool


def pool_stats() -> dict:
    """Snapshot both pools for /api/v1/health/full. Returns:
        {"initialized": True,
         "api":   {"size":N, "min_size":N, "max_size":N, "free":N, "in_use":N},
         "write": {"size":N, "min_size":N, "max_size":N, "free":N, "in_use":N}}
    or {"initialized": False} if init_pools hasn't been called yet.

    Legacy shape for backwards-compat: the top-level keys "size", "min_size",
    "max_size", "free", "in_use" mirror the WRITE pool (where the pre-split
    code's `_pool` reference now points), so existing /health/full consumers
    keep working without code changes.
    """
    if _api_pool is None and _write_pool is None:
        return {"initialized": False}

    out: dict = {"initialized": True}
    for label, pool in (("api", _api_pool), ("write", _write_pool)):
        if pool is None:
            continue
        try:
            size = pool.get_size()
            free = pool.get_idle_size()
            out[label] = {
                "size":     int(size),
                "min_size": int(pool.get_min_size()),
                "max_size": int(pool.get_max_size()),
                "free":     int(free),
                "in_use":   int(size) - int(free),
            }
        except Exception as e:  # noqa: BLE001
            out[label] = {"error": f"{type(e).__name__}: {e}"}

    # Backwards-compat: flatten the write pool stats onto the top level so
    # /api/v1/health/full's old shape (.size, .free, .in_use) keeps working.
    if "write" in out and isinstance(out["write"], dict) and "error" not in out["write"]:
        for k in ("size", "min_size", "max_size", "free", "in_use"):
            out[k] = out["write"][k]

    return out


# ─── Context managers ─────────────────────────────────────────────────────

@asynccontextmanager
async def acquire_read() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection from the API pool (10 s statement_timeout).

    Use this for any read-side handler — viewport, entity detail, signals,
    sanctions queries, health probes. The strict timeout protects the API
    from a misbehaving SQL pinning a connection.
    """
    pool = get_api_pool()
    async with pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def acquire_write() -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection from the write pool (120 s statement_timeout).

    Use this for writers (`writers.py`), algorithm emitters (INSERTs), and
    any heavy maintenance SQL that legitimately needs the long timeout.
    """
    pool = get_write_pool()
    async with pool.acquire() as conn:
        yield conn


# ─── Convenience query helpers — read pool ────────────────────────────────

async def fetch_read(query: str, *args: Any) -> List[asyncpg.Record]:
    async with acquire_read() as conn:
        return await conn.fetch(query, *args)


async def fetchrow_read(query: str, *args: Any) -> Optional[asyncpg.Record]:
    async with acquire_read() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval_read(query: str, *args: Any) -> Any:
    async with acquire_read() as conn:
        return await conn.fetchval(query, *args)


async def execute_read(query: str, *args: Any) -> str:
    """Read-pool execute (rare — for SET LOCAL, SELECT side-effects, etc.)."""
    async with acquire_read() as conn:
        return await conn.execute(query, *args)


# ─── Convenience query helpers — write pool ───────────────────────────────

async def fetch_write(query: str, *args: Any) -> List[asyncpg.Record]:
    async with acquire_write() as conn:
        return await conn.fetch(query, *args)


async def fetchrow_write(query: str, *args: Any) -> Optional[asyncpg.Record]:
    async with acquire_write() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval_write(query: str, *args: Any) -> Any:
    async with acquire_write() as conn:
        return await conn.fetchval(query, *args)


async def execute_write(query: str, *args: Any) -> str:
    async with acquire_write() as conn:
        return await conn.execute(query, *args)


# ─── Backwards-compatible legacy helpers ──────────────────────────────────
#
# Pre-2026-05-21 callers used `init_pool` / `close_pool` / `acquire` and the
# unsuffixed `fetch` / `fetchrow` / `fetchval` / `execute`. These continue to
# work and route to the WRITE pool — the safer default, because legacy code
# may include heavy seeders, test fixtures, or maintenance scripts that
# can't tolerate the strict 10 s read-pool timeout.
#
# New code should use the explicit `_read` / `_write` variants. Once every
# call site has been migrated, these aliases can be removed.

async def init_pool(min_size: int = 4, max_size: int = 30) -> None:
    """Legacy entry point — initializes both pools. The min_size/max_size
    args are honored for the WRITE pool (where pre-split callers' load
    landed); the API pool uses its own defaults."""
    await init_pools(write_min_size=min_size, write_max_size=max_size)


async def close_pool() -> None:
    """Legacy entry point — closes both pools."""
    await close_pools()


def get_pool() -> asyncpg.Pool:
    """Legacy accessor — returns the WRITE pool. Use get_api_pool() /
    get_write_pool() for explicit routing in new code."""
    return get_write_pool()


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    """Legacy context manager — acquires from the WRITE pool."""
    async with acquire_write() as conn:
        yield conn


async def fetch(query: str, *args: Any) -> List[asyncpg.Record]:
    return await fetch_write(query, *args)


async def fetchrow(query: str, *args: Any) -> Optional[asyncpg.Record]:
    return await fetchrow_write(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    return await fetchval_write(query, *args)


async def execute(query: str, *args: Any) -> str:
    return await execute_write(query, *args)
