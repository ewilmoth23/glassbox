# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
glassbox-events-mcp — MCP server exposing event search + trends over
Glassbox's /api/v1 surface.

Tool catalog (HANDOFF_04 server 2):
  glassbox.events.search              → /api/v1/events/similar?q=<text>
  glassbox.events.similar_to          → /api/v1/events/similar?id=<uuid>
  glassbox.events.timeseries          → /api/v1/alerts/timeseries
  glassbox.events.in_bbox             → /api/v1/viewport (events only, type-filtered)
  glassbox.events.algorithm_findings  → in_bbox prefilled with algorithm event types
  glassbox.events.detail              → /api/v1/event/{id} (single-event row)

Run as a daemon-managed launchd process via stdio transport — clients
(Claude Desktop, Cowork-mode Claude, future LangGraph agents) speak to
it through the standard MCP wire protocol.

Bootstrap (mirrors entities/server.py):
  21_GLASSBOX_AI/mcp_servers/.venv/bin/python -m mcp_servers.events.server

Add to a Claude Desktop config alongside the entities server entry —
see 21_GLASSBOX_AI/mcp_servers/README.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make sibling-package imports work whether invoked as `python -m
# mcp_servers.events.server` from inside 21_GLASSBOX_AI/, or directly
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


_log = logging.getLogger("glassbox-events-mcp")
SERVER_NAME = "events"

# Mirror of the rare_tier1_query whitelist in api_v1.viewport. These
# are the event_types produced by glassbox-server's algorithm scan
# loop (5-min cadence) — i.e. *derived* events as opposed to raw
# ingester output. Kept module-level so an agent can introspect the
# default scope; an explicit `types` arg on algorithm_findings
# overrides this list (e.g. an agent that only cares about dark-ship
# events). volcanic_alert is included because the multijurisdictional
# / shadow-fleet pipeline can flag it as collateral context, even
# though the bare ingester writes the event row.
ALGORITHM_EVENT_TYPES: List[str] = [
    "dark_vessel_detected",
    "loitering_detected",
    "rendezvous_detected",
    "military_aircraft_underway",
    "aircraft_in_sanctioned_airspace",
    "sanctioned_vessel_went_dark",
    "sanctioned_vessel_rendezvous",
    "sanctioned_vessel_multijurisdictional",
    "sanctioned_vessel_underway",
    "shadow_fleet_cluster",
    "sanctioned_port_arrival",
    "port_call",
    "port_arrival",
    "port_departure",
]

# Per HANDOFF_04: Events = 300 calls/min/agent. Same shape as the
# entities server (capacity=30 burst, refill=5/sec sustained).
_RATE_LIMITER = TokenBucketRateLimiter(capacity=30.0, refill_per_sec=5.0)

server: Server = Server("glassbox-events-mcp")
_client: Optional[GlassboxRestClient] = None


def _agent_id_from_env() -> Optional[str]:
    return os.environ.get("GLASSBOX_MCP_AGENT_ID") or None


# ─── Tool catalog ────────────────────────────────────────────────────────


_TOOL_SEARCH = Tool(
    name="glassbox.events.search",
    description=(
        "Semantic search over Glassbox event embeddings using a free-"
        "text query (e.g. 'earthquake near Tokyo last week'). Returns "
        "events ordered by cosine distance ascending (smallest = most "
        "similar). Use for natural-language 'find events about X' "
        "questions. Do NOT use to find events similar to a specific "
        "event — use similar_to with the event's UUID instead. Default "
        "window is the last 30 days. Latency 100-500ms (embedding "
        "computed on the fly). Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Free-text query to embed and match against event embeddings.",
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 100, "default": 20,
            },
            "within_days": {
                "type": "integer", "minimum": 1, "maximum": 365, "default": 30,
                "description": "Restrict to events with event_time within last N days.",
            },
        },
    },
)


_TOOL_SIMILAR_TO = Tool(
    name="glassbox.events.similar_to",
    description=(
        "Find events semantically similar to a given event by its UUID. "
        "Use after an agent has identified one event of interest and "
        "wants to drill into related ones (e.g. 'find more like this "
        "incident'). For free-text queries use search instead. The seed "
        "event is excluded from results. Default window is the last "
        "30 days. Latency 50-200ms (embedding looked up, not computed). "
        "Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "required": ["event_id"],
        "additionalProperties": False,
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Empire event UUID.",
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 100, "default": 20,
            },
            "within_days": {
                "type": "integer", "minimum": 1, "maximum": 365, "default": 30,
            },
        },
    },
)


_TOOL_TIMESERIES = Tool(
    name="glassbox.events.timeseries",
    description=(
        "Per-event-type counts bucketed over time (default last 24h, "
        "1-hour buckets). Use for 'is the rate of X rising or falling' "
        "questions and dashboard sparklines. Returns a flat structure: "
        "event_types list + bucket-time list + per-type count arrays. "
        "Do NOT use for individual event lookups — use search or "
        "similar_to. Latency 50-200ms. Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "hours": {
                "type": "integer", "minimum": 1, "maximum": 168, "default": 24,
                "description": "How many hours back from now to bucket.",
            },
            "bucket_minutes": {
                "type": "integer", "minimum": 5, "maximum": 720, "default": 60,
                "description": "Bucket size in minutes.",
            },
        },
    },
)


_TOOL_IN_BBOX = Tool(
    name="glassbox.events.in_bbox",
    description=(
        "List events inside a geographic bounding box and time window. "
        "Use for 'what is happening in <region> right now' questions "
        "(e.g. 'show me events in the Strait of Hormuz over the last "
        "two hours'). Optional event_types client-side filter narrows "
        "to specific kinds (e.g. ['noaa_alert', 'gdacs_alert', "
        "'dark_vessel_detected']). Returns events ordered by event_time "
        "descending. Do NOT use for unconstrained 'find events about X' "
        "queries — use search instead (it ranks by semantic similarity). "
        "Latency 100-500ms depending on bbox size. Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "required": ["west", "south", "east", "north", "time_from", "time_to"],
        "additionalProperties": False,
        "properties": {
            "west":  {"type": "number", "minimum": -180, "maximum": 180},
            "south": {"type": "number", "minimum":  -90, "maximum":  90},
            "east":  {"type": "number", "minimum": -180, "maximum": 180},
            "north": {"type": "number", "minimum":  -90, "maximum":  90},
            "time_from": {
                "type": "string",
                "description": "ISO-8601 UTC start time, e.g. 2026-05-10T08:00:00Z.",
            },
            "time_to": {
                "type": "string",
                "description": "ISO-8601 UTC end time. Must be >= time_from.",
            },
            "event_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional whitelist of event_type strings. "
                    "Common values: noaa_alert, gdacs_alert, "
                    "dark_vessel_detected, earthquake, sanctioned_*."
                ),
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 1000, "default": 100,
            },
        },
    },
)


_TOOL_ALGORITHM_FINDINGS = Tool(
    name="glassbox.events.algorithm_findings",
    description=(
        "List events derived by Glassbox's algorithm scan loop (the "
        "5-min cycle that runs proximity, dark-ship, sanctions-match, "
        "military-flights, loitering, rendezvous, port-call, etc.) — "
        "as opposed to raw ingester output. Use for 'what is the "
        "system actively flagging' questions and operational triage. "
        "Defaults to a 24h global-bbox window if no filters are given. "
        "Optional `types` narrows to specific algorithms (e.g. "
        "['dark_vessel_detected', 'shadow_fleet_cluster']); when "
        "absent, all algorithm event_types are included. For raw "
        "ingester events (news, AQI, NWS, etc.) use in_bbox instead. "
        "Latency 100-500ms. Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "west":  {"type": "number", "minimum": -180, "maximum": 180,
                      "default": -180},
            "south": {"type": "number", "minimum":  -90, "maximum":  90,
                      "default":  -90},
            "east":  {"type": "number", "minimum": -180, "maximum": 180,
                      "default":  180},
            "north": {"type": "number", "minimum":  -90, "maximum":  90,
                      "default":   90},
            "hours": {
                "type": "integer", "minimum": 1, "maximum": 168, "default": 24,
                "description": "Window size in hours back from now.",
            },
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of algorithm event_types. When "
                    "omitted, all algorithm types are included. Subset "
                    "must intersect the algorithm whitelist; types not "
                    "in the whitelist are silently dropped (use in_bbox "
                    "for arbitrary event_type filtering)."
                ),
            },
            "limit": {
                "type": "integer", "minimum": 1, "maximum": 1000, "default": 200,
            },
        },
    },
)


_TOOL_DETAIL = Tool(
    name="glassbox.events.detail",
    description=(
        "Fetch the full event row for a single event by UUID. Use "
        "after an agent has identified an event of interest from "
        "search / similar_to / in_bbox / algorithm_findings and "
        "wants the complete record (full description, properties "
        "JSON bag, severity_for_market, decay window, related "
        "entity_id, lat/lng). Mirror of entities.detail's role for "
        "the event table. Returns 404 when the UUID has no matching "
        "row. Latency 20-100ms. Cost: cheap."
    ),
    inputSchema={
        "type": "object",
        "required": ["event_id"],
        "additionalProperties": False,
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Empire event UUID.",
            },
        },
    },
)


@server.list_tools()
async def list_tools() -> List[Tool]:
    return [_TOOL_SEARCH, _TOOL_SIMILAR_TO, _TOOL_TIMESERIES,
            _TOOL_IN_BBOX, _TOOL_ALGORITHM_FINDINGS, _TOOL_DETAIL]


# ─── Tool dispatch ───────────────────────────────────────────────────────


async def _dispatch_search(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.events_search_by_text(  # type: ignore[union-attr]
        query=args["query"],
        limit=int(args.get("limit", 20)),
        within_days=int(args.get("within_days", 30)),
    )


async def _dispatch_similar_to(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.events_similar_to(  # type: ignore[union-attr]
        event_id=args["event_id"],
        limit=int(args.get("limit", 20)),
        within_days=int(args.get("within_days", 30)),
    )


async def _dispatch_timeseries(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.alerts_timeseries(  # type: ignore[union-attr]
        hours=int(args.get("hours", 24)),
        bucket_minutes=int(args.get("bucket_minutes", 60)),
    )


async def _dispatch_in_bbox(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.events_in_bbox(  # type: ignore[union-attr]
        west=float(args["west"]),
        south=float(args["south"]),
        east=float(args["east"]),
        north=float(args["north"]),
        time_from=str(args["time_from"]),
        time_to=str(args["time_to"]),
        event_types=args.get("event_types") or None,
        limit=int(args.get("limit", 100)),
    )


def _resolve_algorithm_types(requested: Optional[List[str]]) -> List[str]:
    """Intersect the agent-supplied types subset with the algorithm
    whitelist. Returns the full whitelist when ``requested`` is None
    or empty. Pure helper — no I/O."""
    if not requested:
        return list(ALGORITHM_EVENT_TYPES)
    allowed = set(ALGORITHM_EVENT_TYPES)
    return [t for t in requested if t in allowed]


async def _dispatch_algorithm_findings(args: Dict[str, Any]) -> Dict[str, Any]:
    types = _resolve_algorithm_types(args.get("types"))
    if not types:
        # Agent requested only types outside the whitelist — return
        # an empty-shape response rather than fall through to a
        # whitelist-default that they explicitly didn't ask for.
        return {
            "events": [], "filtered_count": 0, "total_count": 0,
            "bbox": None, "time_from": None, "time_to": None,
            "query_ms": None,
            "algorithm_types_resolved": [],
        }
    hours = int(args.get("hours", 24))
    now = datetime.now(timezone.utc)
    time_to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_from = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = await _client.events_in_bbox(  # type: ignore[union-attr]
        west=float(args.get("west", -180)),
        south=float(args.get("south", -90)),
        east=float(args.get("east", 180)),
        north=float(args.get("north", 90)),
        time_from=time_from,
        time_to=time_to,
        event_types=types,
        limit=int(args.get("limit", 200)),
    )
    out["algorithm_types_resolved"] = types
    return out


async def _dispatch_detail(args: Dict[str, Any]) -> Dict[str, Any]:
    return await _client.event_detail(  # type: ignore[union-attr]
        event_id=str(args["event_id"]),
    )


_DISPATCH = {
    "glassbox.events.search":              (_dispatch_search,             "cheap"),
    "glassbox.events.similar_to":          (_dispatch_similar_to,         "cheap"),
    "glassbox.events.timeseries":          (_dispatch_timeseries,         "cheap"),
    "glassbox.events.in_bbox":             (_dispatch_in_bbox,            "cheap"),
    "glassbox.events.algorithm_findings":  (_dispatch_algorithm_findings, "cheap"),
    "glassbox.events.detail":              (_dispatch_detail,             "cheap"),
}


def _summarize(tool_name: str, response: Dict[str, Any]) -> Dict[str, Any]:
    """Bound the audit-log row size — never persist the full response.
    Mirrors the entities server pattern."""
    if tool_name in ("glassbox.events.search", "glassbox.events.similar_to"):
        events = response.get("events") or response.get("results") or []
        # Track event-type histogram so per-agent reporting can spot
        # an agent that's monomaniacally focused on one type.
        by_type: Dict[str, int] = {}
        for ev in events:
            t = ev.get("event_type") or "?"
            by_type[t] = by_type.get(t, 0) + 1
        return {"result_count": len(events), "by_type": by_type}
    if tool_name == "glassbox.events.timeseries":
        types = response.get("event_types") or []
        buckets = response.get("buckets") or []
        return {"event_type_count": len(types),
                "bucket_count": len(buckets)}
    if tool_name in ("glassbox.events.in_bbox",
                     "glassbox.events.algorithm_findings"):
        events = response.get("events") or []
        by_type: Dict[str, int] = {}
        for ev in events:
            t = ev.get("event_type") or "?"
            by_type[t] = by_type.get(t, 0) + 1
        out = {"result_count": len(events),
               "filtered_from": response.get("total_count"),
               "by_type": by_type}
        if tool_name == "glassbox.events.algorithm_findings":
            out["algorithm_types"] = response.get("algorithm_types_resolved")
        return out
    if tool_name == "glassbox.events.detail":
        # Per-event audit row carries the type + severity so triage
        # patterns ("show me agents who only ever pull dark_vessel
        # rows") show up cleanly without persisting the full body.
        return {"event_type": response.get("event_type"),
                "severity":   response.get("severity"),
                "has_geom":   response.get("lat") is not None}
    return {}


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    if name not in _DISPATCH:
        raise ValueError(f"unknown tool: {name}")
    handler, cost_class = _DISPATCH[name]
    agent_id = _agent_id_from_env()

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
    _log.info("glassbox-events-mcp up — base_url=%s", _client.base_url)
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
