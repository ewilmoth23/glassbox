# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
glassbox-entities-mcp tests.

Two layers:
  * server-shape unit tests: list_tools, call_tool dispatch + arg
    validation, audit summary shape — all without network or DB.
  * audit unit tests: AuditCall context manager records elapsed +
    success + summary; failure path captures the error message.

Live MCP-protocol round-trip (stdio framing) is left for a follow-up
integration commit — those tests need a live Glassbox API + DB to be
meaningful and aren't worth running on every commit.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── list_tools ──────────────────────────────────────────────────────────


async def test_list_tools_returns_four_tools():
    from mcp_servers.entities.server import list_tools
    tools = await list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "glassbox.entities.aliases",
        "glassbox.entities.detail",
        "glassbox.entities.detail_ftm",
        "glassbox.entities.viewport",
    ]


async def test_each_tool_description_has_use_when_and_cost_hints():
    """HANDOFF_04: every tool description must (1) state what it does,
    (2) say when to use it vs. similar tools, (3) note costs."""
    from mcp_servers.entities.server import list_tools
    tools = await list_tools()
    for t in tools:
        desc = t.description.lower()
        assert "use" in desc, f"{t.name}: missing 'use ...' guidance"
        assert ("cost" in desc or "latency" in desc), \
            f"{t.name}: missing cost / latency hint"


async def test_viewport_tool_input_schema_rejects_bad_bbox():
    """The MCP transport will validate against inputSchema before the
    tool even runs. We assert the schema declares the right bounds."""
    from mcp_servers.entities.server import _TOOL_VIEWPORT
    schema = _TOOL_VIEWPORT.inputSchema
    bbox_props = schema["properties"]["bbox"]["properties"]
    assert bbox_props["west"]["minimum"]  == -180
    assert bbox_props["west"]["maximum"]  ==  180
    assert bbox_props["north"]["maximum"] ==   90
    # types enum is locked down
    types = schema["properties"]["types"]["items"]["enum"]
    assert set(types) == {"aircraft", "vessel", "satellite"}


async def test_detail_tool_requires_entity_id():
    from mcp_servers.entities.server import _TOOL_DETAIL
    schema = _TOOL_DETAIL.inputSchema
    assert "entity_id" in schema["required"]


# ─── _summarize ──────────────────────────────────────────────────────────


def test_summarize_viewport_counts_entities_and_types():
    from mcp_servers.entities.server import _summarize
    response = {"entities": [
        {"entity_type": "aircraft", "id": "1"},
        {"entity_type": "vessel",   "id": "2"},
        {"entity_type": "vessel",   "id": "3"},
    ]}
    s = _summarize("glassbox.entities.viewport", response)
    assert s["result_count"] == 3
    assert s["types_seen"] == ["aircraft", "vessel"]


def test_summarize_detail_counts_track_and_related():
    from mcp_servers.entities.server import _summarize
    response = {
        "entity": {"entity_type": "vessel"},
        "track": [{"t": 1}, {"t": 2}],
        "related_events": [{"e": 1}],
    }
    s = _summarize("glassbox.entities.detail", response)
    assert s["entity_type"]         == "vessel"
    assert s["track_point_count"]   == 2
    assert s["related_event_count"] == 1


def test_summarize_detail_ftm_counts_props():
    from mcp_servers.entities.server import _summarize
    response = {"id": "x", "schema": "Vessel",
                "properties": {"name": ["X"], "imoNumber": ["1"]}}
    s = _summarize("glassbox.entities.detail_ftm", response)
    assert s["ftm_schema"]     == "Vessel"
    assert s["property_count"] == 2


def test_summarize_unknown_tool_returns_empty_dict():
    from mcp_servers.entities.server import _summarize
    assert _summarize("nope", {"anything": 1}) == {}


def test_summarize_aliases_records_count_and_max_confidence():
    from mcp_servers.entities.server import _summarize
    response = {
        "entity_id": "abc-123",
        "min_confidence": 0.5,
        "alias_count": 3,
        "aliases": [
            {"to_entity_id": "x", "confidence": 0.92},
            {"to_entity_id": "y", "confidence": 0.71},
            {"to_entity_id": "z", "confidence": 0.55},
        ],
    }
    s = _summarize("glassbox.entities.aliases", response)
    assert s["alias_count"] == 3
    assert s["min_confidence"] == 0.5
    assert s["max_alias_confidence"] == 0.92


def test_summarize_aliases_handles_empty_list():
    from mcp_servers.entities.server import _summarize
    s = _summarize("glassbox.entities.aliases",
                   {"entity_id": "x", "min_confidence": 0.0,
                    "alias_count": 0, "aliases": []})
    assert s["alias_count"] == 0
    assert s["max_alias_confidence"] is None


# ─── call_tool dispatch ──────────────────────────────────────────────────


async def test_call_tool_unknown_raises():
    from mcp_servers.entities.server import call_tool
    with pytest.raises(ValueError, match="unknown tool"):
        await call_tool("not.a.tool", {})


async def test_call_tool_dispatches_viewport_to_client():
    """Patch the module-level client to return a known dict, then
    confirm the handler runs end-to-end (client call + audit + JSON
    response)."""
    from mcp_servers.entities import server as srv
    fake_response = {"entities": [{"entity_type": "vessel", "id": "v1"}]}
    fake_client = MagicMock()
    fake_client.viewport = AsyncMock(return_value=fake_response)
    with patch.object(srv, "_client", fake_client):
        out = await srv.call_tool(
            "glassbox.entities.viewport",
            {
                "bbox": {"west": 0, "south": 0, "east": 10, "north": 10},
                "time_range": {"start": "2026-05-01T00:00:00Z",
                               "end":   "2026-05-02T00:00:00Z"},
                "types": ["vessel"],
                "limit": 50,
            },
        )
    assert len(out) == 1
    body = json.loads(out[0].text)
    assert body == fake_response
    fake_client.viewport.assert_awaited_once()
    kwargs = fake_client.viewport.await_args.kwargs
    assert kwargs["west"] == 0 and kwargs["east"] == 10
    assert kwargs["types"] == ["vessel"]
    assert kwargs["limit"] == 50


async def test_call_tool_dispatches_detail_ftm_to_client():
    from mcp_servers.entities import server as srv
    fake_response = {"id": "x:1", "schema": "Vessel",
                     "properties": {"name": ["X"]}}
    fake_client = MagicMock()
    fake_client.entity_ftm = AsyncMock(return_value=fake_response)
    with patch.object(srv, "_client", fake_client):
        out = await srv.call_tool(
            "glassbox.entities.detail_ftm", {"entity_id": "x:1"},
        )
    assert json.loads(out[0].text) == fake_response
    fake_client.entity_ftm.assert_awaited_once_with("x:1")


async def test_aliases_tool_input_schema_requires_entity_id():
    from mcp_servers.entities.server import _TOOL_ALIASES
    assert "entity_id" in _TOOL_ALIASES.inputSchema["required"]
    # min_confidence has a [0, 1] bound so the LLM can't accidentally
    # pass a percent (47) and get nothing back.
    mc = _TOOL_ALIASES.inputSchema["properties"]["min_confidence"]
    assert mc["minimum"] == 0.0
    assert mc["maximum"] == 1.0


async def test_call_tool_dispatches_aliases_to_client():
    from mcp_servers.entities import server as srv
    fake_response = {
        "entity_id": "v-uuid", "min_confidence": 0.7, "alias_count": 1,
        "aliases": [{"to_entity_id": "sanctioned-uuid", "confidence": 0.88}],
    }
    fake_client = MagicMock()
    fake_client.entity_aliases = AsyncMock(return_value=fake_response)
    with patch.object(srv, "_client", fake_client):
        out = await srv.call_tool(
            "glassbox.entities.aliases",
            {"entity_id": "v-uuid", "min_confidence": 0.7},
        )
    assert json.loads(out[0].text) == fake_response
    fake_client.entity_aliases.assert_awaited_once_with(
        entity_id="v-uuid", min_confidence=0.7,
    )


async def test_call_tool_aliases_defaults_min_confidence_to_zero():
    """When agent doesn't supply min_confidence, the dispatcher must
    pass 0.0 — show every candidate, agent narrows by their own logic."""
    from mcp_servers.entities import server as srv
    fake_client = MagicMock()
    fake_client.entity_aliases = AsyncMock(
        return_value={"alias_count": 0, "aliases": []})
    with patch.object(srv, "_client", fake_client):
        await srv.call_tool(
            "glassbox.entities.aliases", {"entity_id": "v-uuid"},
        )
    kwargs = fake_client.entity_aliases.await_args.kwargs
    assert kwargs["min_confidence"] == 0.0


# ─── AuditCall (no DB required — pool stays None, falls into
#     "warning + skip" branch) ────────────────────────────────────────────


async def test_audit_call_swallows_no_pool_state():
    """If audit_pool_init wasn't called, AuditCall must NOT raise.
    The block runs to completion; only the audit row is missing."""
    from mcp_servers.shared.audit import AuditCall
    # _pool defaults to None at module import — perfect for this test.
    async with AuditCall(server="entities", tool="dummy",
                         payload={"x": 1}) as ac:
        ac.set_summary({"ok": True})


async def test_audit_call_does_not_suppress_caller_exception():
    """An exception inside the AuditCall block must propagate."""
    from mcp_servers.shared.audit import AuditCall
    with pytest.raises(RuntimeError, match="boom"):
        async with AuditCall(server="entities", tool="dummy",
                             payload={}) as ac:
            ac.set_summary({"failing": True})
            raise RuntimeError("boom")


# ─── TokenBucketRateLimiter unit tests ───────────────────────────────────


async def test_rate_limiter_allows_calls_within_capacity():
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    rl = TokenBucketRateLimiter(capacity=5.0, refill_per_sec=1.0)
    for _ in range(5):
        d = await rl.try_consume("agent_a")
        assert d.allowed
    # 6th in the same instant is denied
    d = await rl.try_consume("agent_a")
    assert not d.allowed
    assert d.retry_after_sec > 0


async def test_rate_limiter_per_agent_isolated():
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    rl = TokenBucketRateLimiter(capacity=2.0, refill_per_sec=1.0)
    # Drain agent_a
    assert (await rl.try_consume("agent_a")).allowed
    assert (await rl.try_consume("agent_a")).allowed
    assert not (await rl.try_consume("agent_a")).allowed
    # agent_b still has full bucket
    assert (await rl.try_consume("agent_b")).allowed


async def test_rate_limiter_refills_over_time():
    """Patch the limiter's monotonic clock to advance manually so
    refill math is deterministic without any sleeps."""
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    rl = TokenBucketRateLimiter(capacity=2.0, refill_per_sec=1.0)
    fake_t = [1000.0]
    rl._now_fn = lambda: fake_t[0]
    assert (await rl.try_consume("a")).allowed
    assert (await rl.try_consume("a")).allowed
    assert not (await rl.try_consume("a")).allowed
    # Advance 3 sec — refills 3 tokens but capped at capacity=2
    fake_t[0] += 3.0
    assert (await rl.try_consume("a")).allowed
    assert (await rl.try_consume("a")).allowed
    assert not (await rl.try_consume("a")).allowed


async def test_rate_limiter_cost_supports_multi_token_calls():
    """Investigation server's LLM-bearing tools count 5×."""
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    rl = TokenBucketRateLimiter(capacity=10.0, refill_per_sec=1.0)
    assert (await rl.try_consume("a", cost=4.0)).allowed
    assert (await rl.try_consume("a", cost=5.0)).allowed
    # 1 token left; cost=5 fails
    d = await rl.try_consume("a", cost=5.0)
    assert not d.allowed
    assert d.retry_after_sec == pytest.approx(4.0, rel=0.05)  # need 4 more


async def test_rate_limiter_anonymous_agent_has_dedicated_bucket():
    """agent_id=None doesn't bypass — gets its own shared bucket."""
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    rl = TokenBucketRateLimiter(capacity=1.0, refill_per_sec=0.1)
    assert (await rl.try_consume(None)).allowed
    assert not (await rl.try_consume(None)).allowed
    # Named agent unaffected
    assert (await rl.try_consume("agent_x")).allowed


async def test_rate_limiter_invalid_construction_raises():
    from mcp_servers.shared.ratelimit import TokenBucketRateLimiter
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=0, refill_per_sec=1.0)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity=10, refill_per_sec=0)


# ─── call_tool rate-limit integration ────────────────────────────────────


async def test_call_tool_raises_rate_limited_on_overflow():
    from mcp_servers.entities import server as srv
    from mcp_servers.shared.ratelimit import (
        RateLimited, TokenBucketRateLimiter,
    )
    fake_response = {"entities": []}
    fake_client = MagicMock()
    fake_client.viewport = AsyncMock(return_value=fake_response)
    # Replace the module-level limiter with a 1-token bucket so we can
    # tip it over within the test in 2 calls.
    tiny = TokenBucketRateLimiter(capacity=1.0, refill_per_sec=0.001)
    args = {
        "bbox":       {"west": 0, "south": 0, "east": 1, "north": 1},
        "time_range": {"start": "2026-05-01T00:00:00Z",
                       "end":   "2026-05-02T00:00:00Z"},
    }
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", tiny):
        # 1st call — bucket has 1 token, succeeds
        await srv.call_tool("glassbox.entities.viewport", args)
        # 2nd call — bucket is empty, raises
        with pytest.raises(RateLimited):
            await srv.call_tool("glassbox.entities.viewport", args)


async def test_call_tool_rate_limit_does_not_burn_audit_or_call_client():
    """When the limiter denies, the dispatcher must NOT call the REST
    client (no upstream load) AND must NOT enter the AuditCall block
    (no row written for the rejected call)."""
    from mcp_servers.entities import server as srv
    from mcp_servers.shared.ratelimit import (
        RateLimited, TokenBucketRateLimiter,
    )
    fake_client = MagicMock()
    fake_client.viewport = AsyncMock(
        return_value={"entities": []},
        side_effect=AssertionError("client must NOT be called when "
                                   "rate-limited"),
    )
    drained = TokenBucketRateLimiter(capacity=0.0001, refill_per_sec=0.0001)
    # Drain immediately (single sub-1-token capacity → first try fails)
    args = {
        "bbox":       {"west": 0, "south": 0, "east": 1, "north": 1},
        "time_range": {"start": "2026-05-01T00:00:00Z",
                       "end":   "2026-05-02T00:00:00Z"},
    }
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", drained):
        with pytest.raises(RateLimited):
            await srv.call_tool("glassbox.entities.viewport", args)
    # The AssertionError side_effect would have fired if .viewport ran.
    fake_client.viewport.assert_not_called()
