# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
glassbox-investigation-mcp — MCP server for higher-stakes / LLM-bearing
tools. The third and last of the HANDOFF_04 server triple.

Tool catalog:
  glassbox.investigation.brief             → /api/v1/viewport?brief_llm=true (LLM-bearing)
  glassbox.investigation.match_sanctions   → /api/v1/sanctions/search (3-authority)
  glassbox.investigation.entity_resolution → /api/v1/entities/{id}/aliases (Splink ER)
  glassbox.investigation.cross_domain      → /api/v1/entities/{id}/cross_domain (multi-entity findings)

Per HANDOFF_04: investigation server gets 30 calls/min/agent (vs 300
for entities/events), and LLM-bearing tools count 5× toward the budget
(so 6 brief calls/min, OR 30 cheap-tool calls). Implementation:
TokenBucketRateLimiter(capacity=5, refill=0.5) with cost=5 on the
brief tool.

Spec'd but NOT in this slice (defer until infrastructure exists):
  nl_query       — needs LangGraph + a query-planner; that's M10
  search_documents — needs OpenAleph (M8)
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
# mcp_servers.investigation.server` from inside 21_GLASSBOX_AI/, or
# directly via launchd plist's full path.
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


_log = logging.getLogger("glassbox-investigation-mcp")
SERVER_NAME = "investigation"

# Per HANDOFF_04: investigation = 30 calls/min/agent (vs 300 for the
# cheap servers). LLM-bearing tools count 5× → cost=5 means at most
# ~6 brief calls/min/agent. capacity=5 + refill=0.5/sec achieves
# 30/min sustained at cost=1.
_RATE_LIMITER = TokenBucketRateLimiter(capacity=5.0, refill_per_sec=0.5)

# Higher per-call timeout — the brief tool round-trips through Ollama
# (~10-15s cold, <50ms cached per the viewport endpoint's docstring).
_INVESTIGATION_TIMEOUT_SEC = 60.0

server: Server = Server("glassbox-investigation-mcp")
_client: Optional[GlassboxRestClient] = None


def _agent_id_from_env() -> Optional[str]:
    return os.environ.get("GLASSBOX_MCP_AGENT_ID") or None


# ─── Tool catalog ────────────────────────────────────────────────────────


_TOOL_BRIEF = Tool(
    name="glassbox.investigation.brief",
    description=(
        "Generate a 200-word LLM-augmented intelligence brief for "
        "everything in a bounding box and time range. Use ONCE the "
        "agent has identified a region of interest and wants narrative "
        "context (not raw entities). Do NOT use as a first-pass query "
        "— use entities.viewport instead, then drill in with brief if "
        "the agent decides the area is interesting. Returns: viewport "
        "snapshot + meta.brief (the LLM-generated prose). Latency: "
        "10-15s cold (Ollama load), <2s cached (5-min cache). "
        "**Cost: expensive. Counts 5× against the 30/min budget.**"
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
                "type": "integer", "minimum": 1, "maximum": 5000, "default": 1000,
            },
        },
    },
)


_TOOL_MATCH_SANCTIONS = Tool(
    name="glassbox.investigation.match_sanctions",
    description=(
        "Search the consolidated 3-authority (OFAC + EU CFSP + UK OFSI) "
        "sanctions index for a name, IMO, or MMSI. Use to verify whether "
        "a specific vessel / aircraft / company is sanctioned and under "
        "which regime. Three matching paths: exact IMO (digits, 6+ "
        "chars), trigram fuzzy match on name (similarity ≥ 0.4), name "
        "substring fallback. Returns ranked results with "
        "canonical_id_type so the agent can identify which authority. "
        "Latency: 50-200ms. Cost: normal."
    ),
    inputSchema={
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string", "minLength": 2, "maxLength": 80,
                "description": "Vessel/aircraft name, IMO digits, or MMSI.",
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 500, "default": 50,
            },
        },
    },
)


_TOOL_ENTITY_RESOLUTION = Tool(
    name="glassbox.investigation.entity_resolution",
    description=(
        "Look up Splink ER alias edges for a live entity — answers 'is "
        "this vessel actually a sanctioned one under a different "
        "identifier?'. Returns linked sanctioned-vessel records with "
        "match probability scores, sorted by confidence descending. "
        "Use after match_sanctions returns no direct hit but the agent "
        "suspects an alias. Latency: 50-150ms. Cost: normal."
    ),
    inputSchema={
        "type": "object",
        "required": ["entity_id"],
        "additionalProperties": False,
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "Empire UUID for the live (non-sanctioned) entity.",
            },
            "min_confidence": {
                "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.0,
                "description": "Drop alias candidates below this Splink probability.",
            },
        },
    },
)


_TOOL_CROSS_DOMAIN = Tool(
    name="glassbox.investigation.cross_domain",
    description=(
        "Multi-entity findings — algorithm-derived events where the "
        "given entity appears alongside one or more partners "
        "(rendezvous_detected, sanctioned_vessel_rendezvous, "
        "shadow_fleet_cluster, sanctioned_vessel_multijurisdictional, "
        "etc.). Use to answer 'what other entities is this vessel/"
        "aircraft entangled with?' and to map a network from a single "
        "starting node. Each event row carries a `partners` array with "
        "the other participating entities resolved (display_name + "
        "canonical_id + entity_type) so an agent doesn't need a "
        "round-trip to entities.detail. Default 7-day window, 50-result "
        "cap. Latency 50-300ms. Cost: normal."
    ),
    inputSchema={
        "type": "object",
        "required": ["entity_id"],
        "additionalProperties": False,
        "properties": {
            "entity_id": {
                "type": "string",
                "description": "Empire UUID for the entity at the centre of the search.",
            },
            "within_hours": {
                "type": "integer", "minimum": 1, "maximum": 2160, "default": 168,
                "description": "Window size in hours back from now. Default 168 = 7 days.",
            },
            "event_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional whitelist (e.g. ['rendezvous_detected', "
                    "'shadow_fleet_cluster']). When omitted, all multi-"
                    "entity event_types are surfaced."
                ),
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 500, "default": 50,
            },
        },
    },
)


@server.list_tools()
async def list_tools() -> List[Tool]:
    return [_TOOL_BRIEF, _TOOL_MATCH_SANCTIONS,
            _TOOL_ENTITY_RESOLUTION, _TOOL_CROSS_DOMAIN]


# ─── Tool dispatch ───────────────────────────────────────────────────────


async def _dispatch_brief(args: Dict[str, Any]) -> Dict[str, Any]:
    bbox = args["bbox"]
    tr = args["time_range"]
    return await _client.viewport(  # type: ignore[union-attr]
        west=bbox["west"], south=bbox["south"],
        east=bbox["east"], north=bbox["north"],
        start=tr["start"], end=tr["end"],
        types=args.get("types"),
        limit=int(args.get("limit", 1000)),
        brief_llm=True,
    )


async def _dispatch_match_sanctions(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.sanctions_search(  # type: ignore[union-attr]
        query=args["query"],
        limit=int(args.get("limit", 50)),
    )


async def _dispatch_entity_resolution(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.entity_aliases(  # type: ignore[union-attr]
        entity_id=args["entity_id"],
        min_confidence=float(args.get("min_confidence", 0.0)),
    )


async def _dispatch_cross_domain(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.entity_cross_domain(  # type: ignore[union-attr]
        entity_id=args["entity_id"],
        within_hours=int(args.get("within_hours", 168)),
        event_types=args.get("event_types") or None,
        limit=int(args.get("limit", 50)),
    )


# (handler, cost_class, rate_limit_cost) per tool. brief = expensive,
# counts 5× toward the per-agent budget.
_DISPATCH = {
    "glassbox.investigation.brief":             (_dispatch_brief,             "expensive", 5.0),
    "glassbox.investigation.match_sanctions":   (_dispatch_match_sanctions,   "normal",    1.0),
    "glassbox.investigation.entity_resolution": (_dispatch_entity_resolution, "normal",    1.0),
    "glassbox.investigation.cross_domain":      (_dispatch_cross_domain,      "normal",    1.0),
}


def _summarize(tool_name: str, response: Dict[str, Any]) -> Dict[str, Any]:
    if tool_name == "glassbox.investigation.brief":
        meta = response.get("meta") or {}
        brief = meta.get("brief") or ""
        ents = response.get("entities") or []
        return {
            "entity_count": len(ents),
            "brief_length": len(brief),
            "brief_preview": brief[:120] if brief else None,
        }
    if tool_name == "glassbox.investigation.match_sanctions":
        results = response.get("results") or []
        # Track which authorities matched — useful for an agent
        # that's specifically interested in EU vs OFAC coverage.
        by_authority: Dict[str, int] = {}
        for r in results:
            t = r.get("canonical_id_type") or "?"
            by_authority[t] = by_authority.get(t, 0) + 1
        return {
            "result_count": int(response.get("count") or len(results)),
            "by_authority": by_authority,
        }
    if tool_name == "glassbox.investigation.entity_resolution":
        return {
            "alias_count":    int(response.get("alias_count") or 0),
            "min_confidence": response.get("min_confidence"),
        }
    if tool_name == "glassbox.investigation.cross_domain":
        events = response.get("events") or []
        # Per-event_type histogram + a unique-partners count so an
        # operator scanning the audit log can spot agents that
        # repeatedly map the same entity's network (vs. agents
        # who use it as a one-shot drill-down).
        by_type: Dict[str, int] = {}
        unique_partners: set = set()
        for ev in events:
            t = ev.get("event_type") or "?"
            by_type[t] = by_type.get(t, 0) + 1
            for p in ev.get("partners") or []:
                pid = p.get("entity_id")
                if pid:
                    unique_partners.add(pid)
        return {
            "result_count": len(events),
            "by_type": by_type,
            "unique_partner_count": len(unique_partners),
        }
    return {}


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    if name not in _DISPATCH:
        raise ValueError(f"unknown tool: {name}")
    handler, cost_class, rate_cost = _DISPATCH[name]
    agent_id = _agent_id_from_env()

    decision = await _RATE_LIMITER.try_consume(agent_id, cost=rate_cost)
    if not decision.allowed:
        raise RateLimited(decision.retry_after_sec, agent_id, rate_cost)

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
    # Investigation server uses a longer timeout because the brief tool
    # waits on Ollama (~10-15s cold).
    _client = GlassboxRestClient(timeout_sec=_INVESTIGATION_TIMEOUT_SEC)
    _log.info("glassbox-investigation-mcp up — base_url=%s timeout=%ss",
              _client.base_url, _INVESTIGATION_TIMEOUT_SEC)
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
