# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Per-call audit log for MCP tool invocations. Writes one row to
``mcp_audit_log`` per ``async with AuditCall(...)`` block.

One asyncpg pool per process (lazily initialized on first use). Bring
down with ``await audit_pool_close()`` at process shutdown — non-fatal
if not called (asyncpg handles close-on-exit).

Failures inside the audit path NEVER raise into the caller — the worst
audit outcome is "no row written"; the worst MCP outcome must NEVER be
"tool failed because audit was unhappy."
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import asyncpg


_log = logging.getLogger(__name__)
_pool: Optional[asyncpg.Pool] = None


def _load_db_env() -> Dict[str, str]:
    """Hydrate GLASSBOX_DB_* env vars from .env.glassbox if present.
    Mirrors the main glassbox-server's bootstrap pattern."""
    env_path = Path(os.environ.get("GLASSBOX_DB_ENV_FILE",
                                   ".env.glassbox"))
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v.strip().strip("'\""))
    required = ("GLASSBOX_DB_HOST", "GLASSBOX_DB_PORT", "GLASSBOX_DB_NAME",
                "GLASSBOX_DB_USER", "GLASSBOX_DB_PASSWORD")
    return {k: os.environ.get(k, "") for k in required}


async def audit_pool_init() -> None:
    """Idempotent pool init. Safe to call multiple times."""
    global _pool
    if _pool is not None:
        return
    cfg = _load_db_env()
    _pool = await asyncpg.create_pool(
        host=cfg["GLASSBOX_DB_HOST"],
        port=int(cfg["GLASSBOX_DB_PORT"] or 5432),
        database=cfg["GLASSBOX_DB_NAME"],
        user=cfg["GLASSBOX_DB_USER"],
        password=cfg["GLASSBOX_DB_PASSWORD"],
        min_size=1,
        max_size=4,
        # Audit writes are write-mostly + tiny; small pool is plenty.
    )


async def audit_pool_close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


class AuditCall:
    """Async context manager wrapping one MCP tool invocation.

    Usage:
        async with AuditCall(server="entities", tool="viewport",
                             agent_id=ctx.agent_id, payload=args,
                             cost_class="cheap") as ac:
            result = await client.viewport(**args)
            ac.set_summary({"result_count": len(result.get("entities", []))})
            return result

    The block tracks elapsed ms automatically. On exception, ``success``
    is False + the exception's repr is recorded. The exception still
    propagates so the caller can return an MCP error to the agent.
    """

    def __init__(
        self,
        *,
        server: str,
        tool: str,
        agent_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        cost_class: str = "cheap",
    ) -> None:
        self.server = server
        self.tool = tool
        self.agent_id = agent_id
        self.payload = payload or {}
        self.cost_class = cost_class
        self._t0 = 0.0
        self._summary: Dict[str, Any] = {}

    def set_summary(self, summary: Dict[str, Any]) -> None:
        """Record a non-PII summary of the response (e.g. result_count,
        entity_types). Stored under the row's response_summary column."""
        self._summary = summary

    async def __aenter__(self) -> "AuditCall":
        self._t0 = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        elapsed_ms = int((time.perf_counter() - self._t0) * 1000)
        success = exc is None
        error_message = None if success else f"{type(exc).__name__}: {exc}"
        try:
            if _pool is None:
                # Audit pool wasn't initialized — log loud once and move on.
                _log.warning(
                    "audit_pool_init() was never called; tool %s.%s ran "
                    "uninstrumented", self.server, self.tool,
                )
            else:
                async with _pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO mcp_audit_log
                            (server_name, tool_name, agent_id,
                             request_payload, response_summary,
                             latency_ms, cost_class, success, error_message)
                        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7, $8, $9)
                        """,
                        self.server, self.tool, self.agent_id,
                        json.dumps(self.payload),
                        json.dumps(self._summary),
                        elapsed_ms,
                        self.cost_class,
                        success,
                        error_message,
                    )
        except Exception as audit_err:  # noqa: BLE001
            # Defensive: audit failures must never break the caller's
            # response path.
            _log.warning("audit write failed for %s.%s: %s",
                         self.server, self.tool, audit_err)
        return False  # never suppress the original exception
