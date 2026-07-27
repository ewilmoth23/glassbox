# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Glassbox MCP servers — first-class agent access to /api/v1/* (R2 / HANDOFF_04).

Each server in this package is a separate process (entities, events,
investigation per the spec) with its own launchd plist. They all share
the same Postgres audit log + the shared httpx REST client.

Lives in its own venv at ``21_GLASSBOX_AI/mcp_servers/.venv/`` (Python
3.11+) because the ``mcp`` SDK requires Python ≥ 3.10. The main
glassbox-server venv stays on 3.9 with its asyncpg/sentence-transformers/
splink/etc. dep tree intact.
"""
