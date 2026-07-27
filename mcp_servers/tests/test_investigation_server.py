# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
glassbox-investigation-mcp tests. Mirror of the entities + events
test patterns, plus the investigation-specific cost=5 rate-limit
behavior on the LLM-bearing brief tool.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── list_tools ──────────────────────────────────────────────────────────


async def test_list_tools_returns_four_tools():
    from mcp_servers.investigation.server import list_tools
    tools = await list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "glassbox.investigation.brief",
        "glassbox.investigation.cross_domain",
        "glassbox.investigation.entity_resolution",
        "glassbox.investigation.match_sanctions",
    ]


async def test_descriptions_all_have_use_when_and_cost_hints():
    from mcp_servers.investigation.server import list_tools
    tools = await list_tools()
    for t in tools:
        d = t.description.lower()
        assert "use" in d
        assert ("cost" in d or "latency" in d)


async def test_brief_description_warns_about_5x_cost_multiplier():
    """The brief tool counts 5× toward the per-agent budget; the
    description must say so or the agent has no way to know."""
    from mcp_servers.investigation.server import _TOOL_BRIEF
    assert "5" in _TOOL_BRIEF.description.lower() \
        or "expensive" in _TOOL_BRIEF.description.lower()


async def test_match_sanctions_query_min_length_is_2():
    from mcp_servers.investigation.server import _TOOL_MATCH_SANCTIONS
    assert _TOOL_MATCH_SANCTIONS.inputSchema["properties"]["query"]["minLength"] == 2
    assert _TOOL_MATCH_SANCTIONS.inputSchema["properties"]["query"]["maxLength"] == 80


async def test_entity_resolution_min_confidence_bounded_zero_to_one():
    from mcp_servers.investigation.server import _TOOL_ENTITY_RESOLUTION
    p = _TOOL_ENTITY_RESOLUTION.inputSchema["properties"]["min_confidence"]
    assert p["minimum"] == 0.0
    assert p["maximum"] == 1.0


# ─── _summarize ──────────────────────────────────────────────────────────


def test_summarize_brief_records_entity_count_and_brief_preview():
    from mcp_servers.investigation.server import _summarize
    response = {
        "entities": [{"id": "1"}, {"id": "2"}],
        "meta": {"brief": "A 200-word brief about something interesting." * 5},
    }
    s = _summarize("glassbox.investigation.brief", response)
    assert s["entity_count"] == 2
    assert s["brief_length"] > 0
    assert s["brief_preview"] is not None
    assert len(s["brief_preview"]) <= 120


def test_summarize_brief_handles_missing_brief():
    from mcp_servers.investigation.server import _summarize
    response = {"entities": [], "meta": {}}
    s = _summarize("glassbox.investigation.brief", response)
    assert s["entity_count"] == 0
    assert s["brief_length"] == 0
    assert s["brief_preview"] is None


def test_summarize_match_sanctions_groups_by_authority():
    from mcp_servers.investigation.server import _summarize
    response = {
        "count": 3,
        "results": [
            {"canonical_id_type": "ofac_sdn_id"},
            {"canonical_id_type": "eu_cfsp_id"},
            {"canonical_id_type": "ofac_sdn_id"},
        ],
    }
    s = _summarize("glassbox.investigation.match_sanctions", response)
    assert s["result_count"] == 3
    assert s["by_authority"] == {"ofac_sdn_id": 2, "eu_cfsp_id": 1}


def test_summarize_entity_resolution_records_alias_count():
    from mcp_servers.investigation.server import _summarize
    response = {"alias_count": 4, "min_confidence": 0.7, "aliases": []}
    s = _summarize("glassbox.investigation.entity_resolution", response)
    assert s["alias_count"] == 4
    assert s["min_confidence"] == 0.7


# ─── call_tool dispatch ──────────────────────────────────────────────────


async def test_call_tool_unknown_raises():
    from mcp_servers.investigation.server import call_tool
    with pytest.raises(ValueError, match="unknown tool"):
        await call_tool("not.a.tool", {})


async def test_call_tool_brief_passes_brief_llm_true_to_client():
    from mcp_servers.investigation import server as srv
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    fake_client = MagicMock()
    fake_client.viewport = AsyncMock(
        return_value={"entities": [], "meta": {"brief": "hi"}})
    args = {
        "bbox":       {"west": 0, "south": 0, "east": 1, "north": 1},
        "time_range": {"start": "2026-05-01T00:00:00Z",
                       "end":   "2026-05-02T00:00:00Z"},
    }
    fresh = TokenBucketRateLimiter(capacity=100.0, refill_per_sec=10.0)
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", fresh):
        await srv.call_tool("glassbox.investigation.brief", args)
    fake_client.viewport.assert_awaited_once()
    kwargs = fake_client.viewport.await_args.kwargs
    assert kwargs["brief_llm"] is True


async def test_call_tool_match_sanctions_dispatches_with_query():
    """Use a fresh limiter so the test doesn't inherit drained state
    from earlier rate-limit tests in the same module."""
    from mcp_servers.investigation import server as srv
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    fake_client = MagicMock()
    fake_client.sanctions_search = AsyncMock(
        return_value={"count": 0, "results": []})
    fresh = TokenBucketRateLimiter(capacity=100.0, refill_per_sec=10.0)
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", fresh):
        await srv.call_tool(
            "glassbox.investigation.match_sanctions",
            {"query": "MV ATLAS", "limit": 10},
        )
    kwargs = fake_client.sanctions_search.await_args.kwargs
    assert kwargs["query"] == "MV ATLAS"
    assert kwargs["limit"] == 10


async def test_call_tool_entity_resolution_dispatches_with_uuid():
    from mcp_servers.investigation import server as srv
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    fake_client = MagicMock()
    fake_client.entity_aliases = AsyncMock(
        return_value={"alias_count": 0, "aliases": []})
    fresh = TokenBucketRateLimiter(capacity=100.0, refill_per_sec=10.0)
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", fresh):
        await srv.call_tool(
            "glassbox.investigation.entity_resolution",
            {"entity_id": "abc-uuid", "min_confidence": 0.85},
        )
    kwargs = fake_client.entity_aliases.await_args.kwargs
    assert kwargs["entity_id"] == "abc-uuid"
    assert kwargs["min_confidence"] == 0.85


# ─── glassbox.investigation.cross_domain ─────────────────────────────────


async def test_cross_domain_requires_entity_id():
    from mcp_servers.investigation.server import _TOOL_CROSS_DOMAIN
    assert "entity_id" in _TOOL_CROSS_DOMAIN.inputSchema["required"]


async def test_cross_domain_within_hours_bounded():
    """Schema enforces the same 1..2160 range as the underlying
    REST endpoint so the agent gets a fast reject on bad input."""
    from mcp_servers.investigation.server import _TOOL_CROSS_DOMAIN
    wh = _TOOL_CROSS_DOMAIN.inputSchema["properties"]["within_hours"]
    assert wh["minimum"] == 1
    assert wh["maximum"] == 2160
    assert wh["default"] == 168


def test_summarize_cross_domain_records_unique_partners_and_by_type():
    from mcp_servers.investigation.server import _summarize
    response = {
        "events": [
            {"event_type": "rendezvous_detected",
             "partners": [{"entity_id": "p1"}, {"entity_id": "p2"}]},
            {"event_type": "rendezvous_detected",
             "partners": [{"entity_id": "p1"}]},  # repeat partner
            {"event_type": "shadow_fleet_cluster",
             "partners": [{"entity_id": "p3"},
                          {"entity_id": "p4"},
                          {"entity_id": "p2"}]},  # overlap with first
        ],
    }
    s = _summarize("glassbox.investigation.cross_domain", response)
    assert s["result_count"] == 3
    assert s["by_type"] == {"rendezvous_detected": 2,
                            "shadow_fleet_cluster": 1}
    # Unique partner UUIDs across all events: p1, p2, p3, p4 = 4.
    assert s["unique_partner_count"] == 4


def test_summarize_cross_domain_handles_empty():
    from mcp_servers.investigation.server import _summarize
    s = _summarize("glassbox.investigation.cross_domain", {"events": []})
    assert s["result_count"] == 0
    assert s["unique_partner_count"] == 0
    assert s["by_type"] == {}


async def test_call_tool_cross_domain_dispatches_with_filters():
    from mcp_servers.investigation import server as srv
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    fake_client = MagicMock()
    fake_client.entity_cross_domain = AsyncMock(
        return_value={"events": [], "result_count": 0})
    fresh = TokenBucketRateLimiter(capacity=100.0, refill_per_sec=10.0)
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", fresh):
        await srv.call_tool(
            "glassbox.investigation.cross_domain",
            {
                "entity_id": "v-uuid",
                "within_hours": 72,
                "event_types": ["rendezvous_detected", "shadow_fleet_cluster"],
                "limit": 100,
            },
        )
    fake_client.entity_cross_domain.assert_awaited_once_with(
        entity_id="v-uuid",
        within_hours=72,
        event_types=["rendezvous_detected", "shadow_fleet_cluster"],
        limit=100,
    )


async def test_call_tool_cross_domain_omits_event_types_when_absent():
    from mcp_servers.investigation import server as srv
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    fake_client = MagicMock()
    fake_client.entity_cross_domain = AsyncMock(
        return_value={"events": [], "result_count": 0})
    fresh = TokenBucketRateLimiter(capacity=100.0, refill_per_sec=10.0)
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", fresh):
        await srv.call_tool(
            "glassbox.investigation.cross_domain", {"entity_id": "v-uuid"},
        )
    kwargs = fake_client.entity_cross_domain.await_args.kwargs
    assert kwargs["entity_id"] == "v-uuid"
    assert kwargs["event_types"] is None
    assert kwargs["within_hours"] == 168
    assert kwargs["limit"] == 50


async def test_cross_domain_costs_only_1_token():
    """cross_domain is 'normal', not 'expensive' — agents can drill
    through ~30 entities/min mapping a network from a single seed."""
    from mcp_servers.investigation import server as srv
    from mcp_servers.shared.ratelimit import (
        RateLimited, TokenBucketRateLimiter,
    )
    fake_client = MagicMock()
    fake_client.entity_cross_domain = AsyncMock(
        return_value={"events": [], "result_count": 0})
    rl = TokenBucketRateLimiter(capacity=5.0, refill_per_sec=0.0001)
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", rl):
        for _ in range(5):
            await srv.call_tool(
                "glassbox.investigation.cross_domain",
                {"entity_id": "v-uuid"},
            )
        with pytest.raises(RateLimited) as exc:
            await srv.call_tool(
                "glassbox.investigation.cross_domain",
                {"entity_id": "v-uuid"},
            )
        assert exc.value.cost == 1.0


# ─── Rate-limit gating with cost=5 on brief ──────────────────────────────


async def test_brief_costs_5x_and_drains_bucket_in_one_call():
    """Investigation server's capacity is 5; brief costs 5; so the
    second call should fail immediately."""
    from mcp_servers.investigation import server as srv
    from mcp_servers.shared.ratelimit import (
        RateLimited, TokenBucketRateLimiter,
    )
    fake_client = MagicMock()
    fake_client.viewport = AsyncMock(
        return_value={"entities": [], "meta": {"brief": "x"}})
    # Capacity exactly 5 → first brief call drains it; second fails
    rl = TokenBucketRateLimiter(capacity=5.0, refill_per_sec=0.0001)
    args = {
        "bbox":       {"west": 0, "south": 0, "east": 1, "north": 1},
        "time_range": {"start": "2026-05-01T00:00:00Z",
                       "end":   "2026-05-02T00:00:00Z"},
    }
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", rl):
        await srv.call_tool("glassbox.investigation.brief", args)
        with pytest.raises(RateLimited) as exc:
            await srv.call_tool("glassbox.investigation.brief", args)
        # The exception carries the cost — confirm it was 5
        assert exc.value.cost == 5.0


async def test_cheap_tools_cost_only_1_token():
    """Verify match_sanctions costs 1, not 5 — so an agent can do
    multiple sanctions lookups before tipping over."""
    from mcp_servers.investigation import server as srv
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    fake_client = MagicMock()
    fake_client.sanctions_search = AsyncMock(
        return_value={"count": 0, "results": []})
    # Capacity 5 → 5 cheap calls allowed
    rl = TokenBucketRateLimiter(capacity=5.0, refill_per_sec=0.0001)
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", rl):
        for _ in range(5):
            await srv.call_tool(
                "glassbox.investigation.match_sanctions",
                {"query": "test"},
            )
        # 6th hits the limit
        from mcp_servers.shared.ratelimit import RateLimited
        with pytest.raises(RateLimited) as exc:
            await srv.call_tool(
                "glassbox.investigation.match_sanctions",
                {"query": "test"},
            )
        assert exc.value.cost == 1.0
