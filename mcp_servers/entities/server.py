# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
glassbox-entities-mcp — MCP server exposing read-only entity queries
over Glassbox's /api/v1 surface.

Tool catalog (HANDOFF_04 server 1):
  glassbox.entities.viewport     → /api/v1/viewport
  glassbox.entities.detail       → /api/v1/entity/{id}
  glassbox.entities.detail_ftm   → /api/v1/entity/{id}?format=ftm
  glassbox.entities.aliases      → /api/v1/entities/{id}/aliases  (Splink ER edges)

Run as a daemon-managed launchd process via stdio transport — clients
(Claude Desktop, Cowork-mode Claude, future LangGraph agents) speak to
it through the standard MCP wire protocol.

Bootstrap:
  21_GLASSBOX_AI/mcp_servers/.venv/bin/python -m mcp_servers.entities.server

  Add to a Claude Desktop / MCP-Inspector config:
    {
      "mcpServers": {
        "glassbox-entities": {
          "command": "<empire>/21_GLASSBOX_AI/mcp_servers/.venv/bin/python",
          "args": ["-m", "mcp_servers.entities.server"]
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make sibling-package imports work whether invoked as `python -m
# mcp_servers.entities.server` from inside 21_GLASSBOX_AI/, or directly
# via the launchd plist's full path.
_HERE = Path(__file__).resolve()
_PKG_ROOT = _HERE.parent.parent.parent  # → 21_GLASSBOX_AI/
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import TextContent, Tool  # noqa: E402

from mcp_servers.shared import (  # noqa: E402
    AuditCall, GlassboxRestClient, RateLimited, TokenBucketRateLimiter,
    audit_pool_close, audit_pool_init,
)


_log = logging.getLogger("glassbox-entities-mcp")
SERVER_NAME = "entities"

# Per HANDOFF_04: Entities = 300 calls/min/agent. capacity=30 gives 10s
# of sustained burst before the 5/sec refill kicks in — enough headroom
# for an agent doing parallel viewport+detail fan-out, tight enough to
# stop a runaway loop fast.
_RATE_LIMITER = TokenBucketRateLimiter(capacity=30.0, refill_per_sec=5.0)

server: Server = Server("glassbox-entities-mcp")
_client: Optional[GlassboxRestClient] = None


def _agent_id_from_env() -> Optional[str]:
    """MCP clients should send agent identity via env-var when launching
    the server. Stays None for unauthenticated invocations (dev / test)."""
    return os.environ.get("GLASSBOX_MCP_AGENT_ID") or None


# ─── Tool catalog ────────────────────────────────────────────────────────


_TOOL_VIEWPORT = Tool(
    name="glassbox.entities.viewport",
    description=(
        "Get a snapshot of all aircraft, vessels, or satellites in a "
        "bounding box and time range. Use for 'what's in this area' "
        "questions. Do NOT use for events or news (use the events "
        "server). Returns up to 500 entities, ordered by recency. "
        "Latency 50-300ms. Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "required": ["bbox", "time_range"],
        "additionalProperties": False,
        "properties": {
            "bbox": {
                "type": "object",
                "required": ["west", "south", "east", "north"],
                "properties": {
                    "west":  {"type": "number", "minimum": -180, "maximum": 180},
                    "south": {"type": "number", "minimum":  -90, "maximum":  90},
                    "east":  {"type": "number", "minimum": -180, "maximum": 180},
                    "north": {"type": "number", "minimum":  -90, "maximum":  90},
                },
                "additionalProperties": False,
            },
            "time_range": {
                "type": "object",
                "required": ["start", "end"],
                "properties": {
                    "start": {"type": "string", "format": "date-time"},
                    "end":   {"type": "string", "format": "date-time"},
                },
                "additionalProperties": False,
            },
            "types": {
                "type": "array",
                "items": {"enum": ["aircraft", "vessel", "satellite"]},
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 500, "default": 100,
            },
        },
    },
)

_TOOL_DETAIL = Tool(
    name="glassbox.entities.detail",
    description=(
        "Get full detail for a single entity by UUID — identity, recent "
        "track (default 24h), and nearby events. Use after viewport to "
        "drill into one entity the agent flagged as interesting. For "
        "the OCCRP / OpenSanctions ecosystem JSON shape use detail_ftm "
        "instead. Latency 50-200ms. Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "required": ["entity_id"],
        "additionalProperties": False,
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "Empire UUID (not canonical_id)",
            },
            "track_window_hours": {
                "type": "integer", "minimum": 1, "maximum": 720, "default": 24,
            },
        },
    },
)

_TOOL_DETAIL_FTM = Tool(
    name="glassbox.entities.detail_ftm",
    description=(
        "Same entity as detail, but returned in FollowTheMoney JSON "
        "shape — id (= canonical_id, stable across the OCCRP ecosystem), "
        "schema (Vessel / Airplane), and properties keyed by FtM names. "
        "Use when an agent will hand the result to a downstream FtM-aware "
        "tool (yente, OpenAleph, Aleph, Zavod). Returns 415 for entity "
        "types without a defined FtM mapping (today: satellites). "
        "Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "required": ["entity_id"],
        "additionalProperties": False,
        "properties": {
            "entity_id": {"type": "string"},
        },
    },
)


_TOOL_ALIASES = Tool(
    name="glassbox.entities.aliases",
    description=(
        "Return Splink entity-resolution alias edges for a vessel — "
        "i.e. answer 'is this live AIS vessel actually a sanctioned "
        "vessel under a different identifier?'. Use when the agent has "
        "an entity UUID from viewport / detail and wants to check for "
        "fuzzy-matched sanctioned-list aliases (different MMSI/IMO, "
        "renamed hull, etc.). Each alias is a row from the "
        "entity_relation table where relation_type = 'splink_alias' "
        "and from_entity_id = the given UUID. Sorted by confidence "
        "DESC. Latency 30-150ms. Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "required": ["entity_id"],
        "additionalProperties": False,
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "Empire UUID (not canonical_id).",
            },
            "min_confidence": {
                "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.0,
                "description": (
                    "Confidence floor; aliases below this score are "
                    "dropped server-side. Use 0.7+ for high-precision "
                    "triage, 0.0 to see every candidate."
                ),
            },
        },
    },
)


@server.list_tools()
async def list_tools() -> List[Tool]:
    return [_TOOL_VIEWPORT, _TOOL_DETAIL, _TOOL_DETAIL_FTM, _TOOL_ALIASES]


# ─── Tool dispatch ───────────────────────────────────────────────────────


async def _dispatch_viewport(args: Dict[str, Any]) -> Dict[str, Any]:
    bbox = args["bbox"]
    tr = args["time_range"]
    return await _client.viewport(  # type: ignore[union-attr]
        west=bbox["west"], south=bbox["south"],
        east=bbox["east"], north=bbox["north"],
        start=tr["start"], end=tr["end"],
        types=args.get("types"),
        limit=int(args.get("limit", 100)),
    )


async def _dispatch_detail(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.entity_detail(args["entity_id"])  # type: ignore[union-attr]


async def _dispatch_detail_ftm(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.entity_ftm(args["entity_id"])  # type: ignore[union-attr]


async def _dispatch_aliases(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.entity_aliases(  # type: ignore[union-attr]
        entity_id=args["entity_id"],
        min_confidence=float(args.get("min_confidence", 0.0)),
    )


_DISPATCH = {
    "glassbox.entities.viewport":   (_dispatch_viewport,   "cheap"),
    "glassbox.entities.detail":     (_dispatch_detail,     "cheap"),
    "glassbox.entities.detail_ftm": (_dispatch_detail_ftm, "cheap"),
    "glassbox.entities.aliases":    (_dispatch_aliases,    "cheap"),
}


def _summarize(tool_name: str, response: Dict[str, Any]) -> Dict[str, Any]:
    """Bound the audit-log row size — never persist the full response,
    just metadata that's useful for ops + per-agent reporting."""
    if tool_name == "glassbox.entities.viewport":
        ents = response.get("entities") or []
        return {
            "result_count": len(ents),
            "types_seen":   sorted({e.get("entity_type") for e in ents
                                    if e.get("entity_type")}),
        }
    if tool_name == "glassbox.entities.detail":
        ent = (response or {}).get("entity") or {}
        return {
            "entity_type":         ent.get("entity_type"),
            "track_point_count":   len(response.get("track") or []),
            "related_event_count": len(response.get("related_events") or []),
        }
    if tool_name == "glassbox.entities.detail_ftm":
        return {
            "ftm_schema":   response.get("schema"),
            "property_count": len((response.get("properties") or {})),
        }
    if tool_name == "glassbox.entities.aliases":
        aliases = response.get("aliases") or []
        confs = [a.get("confidence") for a in aliases
                 if a.get("confidence") is not None]
        return {
            "alias_count":   response.get("alias_count", len(aliases)),
            "min_confidence": response.get("min_confidence"),
            "max_alias_confidence": max(confs) if confs else None,
        }
    return {}


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    if name not in _DISPATCH:
        raise ValueError(f"unknown tool: {name}")
    handler, cost_class = _DISPATCH[name]
    agent_id = _agent_id_from_env()

    # Rate-limit gate BEFORE the audit context — a 429 shouldn't burn
    # an audit row, and the agent should see a fast retry-after signal
    # rather than a slow "succeeded but the LLM blew the budget" path.
    decision = await _RATE_LIMITER.try_consume(agent_id, cost=1.0)
    if not decision.allowed:
        raise RateLimited(decision.retry_after_sec, agent_id, 1.0)

    async with AuditCall(
        server=SERVER_NAME, tool=name,
        agent_id=agent_id,
        payload=arguments, cost_class=cost_class,
    ) as ac:
        result = await handler(arguments)
        ac.set_summary(_summarize(name, result))
    return [TextContent(type="text", text=json.dumps(result, default=str))]


# ─── Process lifecycle ───────────────────────────────────────────────────


async def _run() -> None:
    global _client
    logging.basicConfig(
        level=os.environ.get("GLASSBOX_MCP_LOG_LEVEL", "INFO"),
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    )
    await audit_pool_init()
    _client = GlassboxRestClient()
    _log.info("glassbox-entities-mcp up — base_url=%s", _client.base_url)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream,
                             server.create_initialization_options())
    finally:
        if _client is not None:
            await _client.aclose()
        await audit_pool_close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
