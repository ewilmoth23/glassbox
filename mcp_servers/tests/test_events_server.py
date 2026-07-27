# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
glassbox-events-mcp tests. Mirror of test_entities_server.py shape:
list_tools, dispatcher behavior, _summarize, rate-limit gating.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── list_tools ──────────────────────────────────────────────────────────


async def test_list_tools_returns_six_tools():
    from mcp_servers.events.server import list_tools
    tools = await list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "glassbox.events.algorithm_findings",
        "glassbox.events.detail",
        "glassbox.events.in_bbox",
        "glassbox.events.search",
        "glassbox.events.similar_to",
        "glassbox.events.timeseries",
    ]


async def test_each_tool_description_has_use_when_and_cost_hints():
    from mcp_servers.events.server import list_tools
    tools = await list_tools()
    for t in tools:
        desc = t.description.lower()
        assert "use" in desc, f"{t.name}: missing 'use ...' guidance"
        assert ("cost" in desc or "latency" in desc), \
            f"{t.name}: missing cost / latency hint"


async def test_search_requires_non_empty_query():
    """Schema enforces minLength=1 on query."""
    from mcp_servers.events.server import _TOOL_SEARCH
    schema = _TOOL_SEARCH.inputSchema
    assert "query" in schema["required"]
    assert schema["properties"]["query"]["minLength"] == 1


async def test_similar_to_requires_event_id():
    from mcp_servers.events.server import _TOOL_SIMILAR_TO
    assert "event_id" in _TOOL_SIMILAR_TO.inputSchema["required"]


async def test_timeseries_no_required_fields_uses_defaults():
    """timeseries is callable with no args — both have defaults (24h
    window, 60-min buckets). Required list must be empty/missing."""
    from mcp_servers.events.server import _TOOL_TIMESERIES
    schema = _TOOL_TIMESERIES.inputSchema
    # Either the field is missing or it's an empty list — both are valid
    assert not schema.get("required")


# ─── _summarize ──────────────────────────────────────────────────────────


def test_summarize_search_produces_by_type_histogram():
    from mcp_servers.events.server import _summarize
    response = {"events": [
        {"event_type": "armed_conflict", "id": "1"},
        {"event_type": "armed_conflict", "id": "2"},
        {"event_type": "natural_disaster", "id": "3"},
    ]}
    s = _summarize("glassbox.events.search", response)
    assert s["result_count"] == 3
    assert s["by_type"] == {"armed_conflict": 2, "natural_disaster": 1}


def test_summarize_search_handles_results_key_alias():
    """Endpoint may return 'results' instead of 'events' depending on
    its shape; both supported."""
    from mcp_servers.events.server import _summarize
    response = {"results": [{"event_type": "x"}]}
    s = _summarize("glassbox.events.search", response)
    assert s["result_count"] == 1


def test_summarize_similar_to_uses_same_shape_as_search():
    from mcp_servers.events.server import _summarize
    response = {"events": [{"event_type": "armed_conflict"}]}
    s = _summarize("glassbox.events.similar_to", response)
    assert s["result_count"] == 1
    assert s["by_type"] == {"armed_conflict": 1}


def test_summarize_timeseries_counts_types_and_buckets():
    from mcp_servers.events.server import _summarize
    response = {
        "event_types": ["a", "b", "c"],
        "buckets": ["t0", "t1", "t2", "t3"],
        "counts": {"a": [0, 1, 2, 3], "b": [1, 0, 1, 0], "c": [0]*4},
    }
    s = _summarize("glassbox.events.timeseries", response)
    assert s["event_type_count"] == 3
    assert s["bucket_count"] == 4


def test_summarize_unknown_tool_returns_empty_dict():
    from mcp_servers.events.server import _summarize
    assert _summarize("nope", {}) == {}


# ─── call_tool dispatch ──────────────────────────────────────────────────


async def test_call_tool_unknown_raises():
    from mcp_servers.events.server import call_tool
    with pytest.raises(ValueError, match="unknown tool"):
        await call_tool("not.a.tool", {})


async def test_call_tool_search_dispatches_with_query():
    from mcp_servers.events import server as srv
    fake_response = {"events": [{"event_type": "armed_conflict", "id": "x"}]}
    fake_client = MagicMock()
    fake_client.events_search_by_text = AsyncMock(return_value=fake_response)
    with patch.object(srv, "_client", fake_client):
        out = await srv.call_tool(
            "glassbox.events.search",
            {"query": "earthquake near Tokyo", "limit": 5, "within_days": 7},
        )
    body = json.loads(out[0].text)
    assert body == fake_response
    fake_client.events_search_by_text.assert_awaited_once_with(
        query="earthquake near Tokyo", limit=5, within_days=7,
    )


async def test_call_tool_similar_to_dispatches_with_event_id():
    from mcp_servers.events import server as srv
    fake_response = {"events": [{"id": "neighbor-1"}]}
    fake_client = MagicMock()
    fake_client.events_similar_to = AsyncMock(return_value=fake_response)
    with patch.object(srv, "_client", fake_client):
        await srv.call_tool(
            "glassbox.events.similar_to", {"event_id": "abc-123"},
        )
    fake_client.events_similar_to.assert_awaited_once()
    kwargs = fake_client.events_similar_to.await_args.kwargs
    assert kwargs["event_id"] == "abc-123"
    # Defaults applied when not specified
    assert kwargs["limit"] == 20
    assert kwargs["within_days"] == 30


async def test_call_tool_timeseries_dispatches_with_defaults():
    from mcp_servers.events import server as srv
    fake_response = {"event_types": [], "buckets": [], "counts": {}}
    fake_client = MagicMock()
    fake_client.alerts_timeseries = AsyncMock(return_value=fake_response)
    with patch.object(srv, "_client", fake_client):
        # No args → uses both defaults (24h, 60min)
        await srv.call_tool("glassbox.events.timeseries", {})
    kwargs = fake_client.alerts_timeseries.await_args.kwargs
    assert kwargs["hours"] == 24
    assert kwargs["bucket_minutes"] == 60


# ─── glassbox.events.in_bbox ─────────────────────────────────────────────


async def test_in_bbox_requires_bbox_and_time_window():
    from mcp_servers.events.server import _TOOL_IN_BBOX
    required = set(_TOOL_IN_BBOX.inputSchema["required"])
    assert {"west", "south", "east", "north", "time_from", "time_to"} <= required


def test_summarize_in_bbox_carries_total_and_filtered_count():
    """The audit summary must capture both the post-filter result count
    and the pre-filter total so an investigator can see how aggressive
    the type-filter was."""
    from mcp_servers.events.server import _summarize
    response = {
        "events": [
            {"event_type": "noaa_alert"},
            {"event_type": "gdacs_alert"},
            {"event_type": "noaa_alert"},
        ],
        "filtered_count": 3,
        "total_count": 17,
    }
    s = _summarize("glassbox.events.in_bbox", response)
    assert s["result_count"] == 3
    assert s["filtered_from"] == 17
    assert s["by_type"] == {"noaa_alert": 2, "gdacs_alert": 1}


async def test_call_tool_in_bbox_dispatches_with_full_args():
    from mcp_servers.events import server as srv
    fake_response = {
        "events": [{"event_type": "noaa_alert", "id": "a"}],
        "filtered_count": 1, "total_count": 5,
    }
    fake_client = MagicMock()
    fake_client.events_in_bbox = AsyncMock(return_value=fake_response)
    args = {
        "west": -75.0, "south": 25.0, "east": -65.0, "north": 35.0,
        "time_from": "2026-05-10T08:00:00Z",
        "time_to":   "2026-05-10T09:00:00Z",
        "event_types": ["noaa_alert", "gdacs_alert"],
        "limit": 200,
    }
    with patch.object(srv, "_client", fake_client):
        out = await srv.call_tool("glassbox.events.in_bbox", args)
    body = json.loads(out[0].text)
    assert body == fake_response
    fake_client.events_in_bbox.assert_awaited_once_with(
        west=-75.0, south=25.0, east=-65.0, north=35.0,
        time_from="2026-05-10T08:00:00Z",
        time_to="2026-05-10T09:00:00Z",
        event_types=["noaa_alert", "gdacs_alert"],
        limit=200,
    )


async def test_call_tool_in_bbox_omits_event_types_when_absent():
    """No event_types in args → client gets None (not [])."""
    from mcp_servers.events import server as srv
    fake_client = MagicMock()
    fake_client.events_in_bbox = AsyncMock(return_value={"events": []})
    args = {
        "west": -180, "south": -90, "east": 180, "north": 90,
        "time_from": "2026-05-10T00:00:00Z",
        "time_to":   "2026-05-10T01:00:00Z",
    }
    with patch.object(srv, "_client", fake_client):
        await srv.call_tool("glassbox.events.in_bbox", args)
    kwargs = fake_client.events_in_bbox.await_args.kwargs
    assert kwargs["event_types"] is None
    # limit default = 100
    assert kwargs["limit"] == 100


# ─── Client.events_in_bbox post-filter + response normalization ──────────


async def test_client_events_in_bbox_applies_event_type_filter_clientside():
    """The viewport endpoint's `types=` param filters entities not events,
    so the client filters by event_type after the response. Must drop
    events whose type is not in the whitelist + report both counts."""
    from mcp_servers.shared.client import GlassboxRestClient

    raw_response = {
        "meta": {
            "bbox": "0,0,10,10",
            "time_from": "2026-05-10T08:00:00Z",
            "time_to":   "2026-05-10T09:00:00Z",
            "query_ms":  42.7,
        },
        "entities": [
            # Entities are returned by the endpoint but must be dropped
            # in the client output (in_bbox is events-only).
            {"id": "should-not-appear-in-result"},
        ],
        "events": [
            {"event_type": "noaa_alert",      "id": "keep1"},
            {"event_type": "gdelt_bulk",      "id": "drop1"},
            {"event_type": "noaa_alert",      "id": "keep2"},
            {"event_type": "earthquake",      "id": "drop2"},
        ],
    }

    client = GlassboxRestClient()
    try:
        with patch.object(client, "get",
                          AsyncMock(return_value=raw_response)) as mock_get:
            out = await client.events_in_bbox(
                west=0, south=0, east=10, north=10,
                time_from="2026-05-10T08:00:00Z",
                time_to="2026-05-10T09:00:00Z",
                event_types=["noaa_alert"],
                limit=100,
            )
        # The HTTP call uses types="" so the endpoint suppresses entity
        # rows server-side (we still strip them client-side as defense
        # in depth).
        mock_get.assert_awaited_once()
        path, params = mock_get.await_args.args[0], mock_get.await_args.args[1]
        assert path == "/api/v1/viewport"
        assert params["bbox"] == "0,0,10,10"
        # See the docstring on events_in_bbox — `infrastructure` is the
        # 0-row entity type chosen to suppress the LATERAL track join
        # that aircraft (the api_v1 default) would trigger.
        assert params["types"] == "infrastructure"
        assert params["time_from"] == "2026-05-10T08:00:00Z"
        assert params["limit"] == 100

        # Client-side filter: 2 of 4 events survive.
        assert out["filtered_count"] == 2
        assert out["total_count"] == 4
        ids = sorted(e["id"] for e in out["events"])
        assert ids == ["keep1", "keep2"]
        # No entities surface in the output.
        assert "entities" not in out
        # Meta passthrough.
        assert out["query_ms"] == 42.7
    finally:
        await client.aclose()


async def test_client_events_in_bbox_no_filter_returns_all_events():
    """event_types=None → no client-side filter; total == filtered."""
    from mcp_servers.shared.client import GlassboxRestClient

    raw_response = {
        "meta": {"bbox": "0,0,1,1", "time_from": "t0", "time_to": "t1",
                 "query_ms": 5.0},
        "events": [{"event_type": "x", "id": "1"},
                   {"event_type": "y", "id": "2"}],
    }
    client = GlassboxRestClient()
    try:
        with patch.object(client, "get",
                          AsyncMock(return_value=raw_response)):
            out = await client.events_in_bbox(
                west=0, south=0, east=1, north=1,
                time_from="t0", time_to="t1",
                event_types=None,
            )
        assert out["filtered_count"] == 2
        assert out["total_count"] == 2
    finally:
        await client.aclose()


# ─── glassbox.events.algorithm_findings ──────────────────────────────────


def test_resolve_algorithm_types_returns_full_whitelist_when_none():
    from mcp_servers.events.server import (
        ALGORITHM_EVENT_TYPES, _resolve_algorithm_types,
    )
    assert _resolve_algorithm_types(None) == list(ALGORITHM_EVENT_TYPES)
    assert _resolve_algorithm_types([]) == list(ALGORITHM_EVENT_TYPES)


def test_resolve_algorithm_types_intersects_with_whitelist():
    """Out-of-whitelist types are silently dropped — agents who want
    them should use in_bbox instead."""
    from mcp_servers.events.server import _resolve_algorithm_types
    requested = ["dark_vessel_detected", "noaa_alert", "shadow_fleet_cluster"]
    result = _resolve_algorithm_types(requested)
    assert "dark_vessel_detected" in result
    assert "shadow_fleet_cluster" in result
    assert "noaa_alert" not in result
    assert len(result) == 2


def test_resolve_algorithm_types_empty_when_no_intersection():
    """When the agent's whitelist has no overlap with algorithm types,
    return empty list — caller short-circuits to an empty response."""
    from mcp_servers.events.server import _resolve_algorithm_types
    assert _resolve_algorithm_types(["noaa_alert", "aqi_reading"]) == []


def test_summarize_algorithm_findings_includes_resolved_types():
    """Summary must include `algorithm_types` so the audit row records
    which whitelist subset the agent actually queried with."""
    from mcp_servers.events.server import _summarize
    response = {
        "events": [{"event_type": "dark_vessel_detected", "id": "x"}],
        "filtered_count": 1, "total_count": 50,
        "algorithm_types_resolved": ["dark_vessel_detected"],
    }
    s = _summarize("glassbox.events.algorithm_findings", response)
    assert s["result_count"] == 1
    assert s["filtered_from"] == 50
    assert s["algorithm_types"] == ["dark_vessel_detected"]


async def test_call_tool_algorithm_findings_uses_full_whitelist_by_default():
    """No bbox + no types + no hours → defaults: global bbox, 24h
    window, full algorithm whitelist."""
    from mcp_servers.events import server as srv

    fake_response = {
        "events": [], "filtered_count": 0, "total_count": 0,
        "meta": {},
    }
    fake_client = MagicMock()
    fake_client.events_in_bbox = AsyncMock(return_value=fake_response)
    with patch.object(srv, "_client", fake_client):
        out = await srv.call_tool("glassbox.events.algorithm_findings", {})
    body = json.loads(out[0].text)
    # Resolved types echoed in response so agents see what got queried.
    assert body["algorithm_types_resolved"] == list(srv.ALGORITHM_EVENT_TYPES)
    fake_client.events_in_bbox.assert_awaited_once()
    kwargs = fake_client.events_in_bbox.await_args.kwargs
    assert kwargs["west"] == -180.0
    assert kwargs["east"] == 180.0
    assert kwargs["event_types"] == list(srv.ALGORITHM_EVENT_TYPES)
    assert kwargs["limit"] == 200
    # time_from / time_to are computed as ISO-8601 UTC strings.
    assert kwargs["time_from"].endswith("Z")
    assert kwargs["time_to"].endswith("Z")


async def test_call_tool_algorithm_findings_respects_types_subset():
    """An explicit `types` arg narrows the whitelist."""
    from mcp_servers.events import server as srv

    fake_client = MagicMock()
    fake_client.events_in_bbox = AsyncMock(
        return_value={"events": [], "filtered_count": 0, "total_count": 0})
    with patch.object(srv, "_client", fake_client):
        await srv.call_tool(
            "glassbox.events.algorithm_findings",
            {"types": ["dark_vessel_detected", "shadow_fleet_cluster"]},
        )
    kwargs = fake_client.events_in_bbox.await_args.kwargs
    assert kwargs["event_types"] == ["dark_vessel_detected",
                                     "shadow_fleet_cluster"]


async def test_call_tool_algorithm_findings_short_circuits_on_empty_intersection():
    """Agent requested only types outside the whitelist → don't fall
    through to the full default whitelist; return an empty shape."""
    from mcp_servers.events import server as srv

    fake_client = MagicMock()
    fake_client.events_in_bbox = AsyncMock(
        return_value={"events": [], "filtered_count": 0,
                      "total_count": 0})
    with patch.object(srv, "_client", fake_client):
        out = await srv.call_tool(
            "glassbox.events.algorithm_findings",
            {"types": ["noaa_alert", "aqi_reading"]},
        )
    body = json.loads(out[0].text)
    assert body["events"] == []
    assert body["algorithm_types_resolved"] == []
    fake_client.events_in_bbox.assert_not_called()


async def test_detail_requires_event_id():
    from mcp_servers.events.server import _TOOL_DETAIL
    assert "event_id" in _TOOL_DETAIL.inputSchema["required"]


def test_summarize_detail_carries_type_severity_geom_marker():
    """The audit summary should record the event_type + severity (so
    triage patterns surface) and a boolean for whether the event had
    a geom (vs. an aspatial row)."""
    from mcp_servers.events.server import _summarize
    response = {
        "id": "abc-123",
        "event_type": "dark_vessel_detected",
        "event_subtype": None,
        "severity": 4.0,
        "title": "x",
        "lat": 12.34,
        "lng": 56.78,
        "properties": {"_huge_blob": "x" * 5000},  # must NOT leak
    }
    s = _summarize("glassbox.events.detail", response)
    assert s == {"event_type": "dark_vessel_detected",
                 "severity":   4.0,
                 "has_geom":   True}


def test_summarize_detail_marks_aspatial_event():
    """An event with no geom (lat=None) → has_geom=False."""
    from mcp_servers.events.server import _summarize
    response = {"event_type": "sec_filing", "severity": 2.0,
                "lat": None, "lng": None}
    s = _summarize("glassbox.events.detail", response)
    assert s["has_geom"] is False


async def test_call_tool_detail_dispatches_with_event_id():
    from mcp_servers.events import server as srv
    fake_response = {
        "id": "11111111-1111-4111-8111-111111111111",
        "event_type": "rendezvous_detected",
        "severity": 3.5,
        "lat": 25.7,
        "lng": -80.1,
    }
    fake_client = MagicMock()
    fake_client.event_detail = AsyncMock(return_value=fake_response)
    with patch.object(srv, "_client", fake_client):
        out = await srv.call_tool(
            "glassbox.events.detail",
            {"event_id": "11111111-1111-4111-8111-111111111111"},
        )
    body = json.loads(out[0].text)
    assert body["event_type"] == "rendezvous_detected"
    fake_client.event_detail.assert_awaited_once_with(
        event_id="11111111-1111-4111-8111-111111111111",
    )


async def test_call_tool_algorithm_findings_respects_hours_window():
    """`hours=72` should produce a time_from ~72h before time_to."""
    from datetime import datetime
    from mcp_servers.events import server as srv

    fake_client = MagicMock()
    fake_client.events_in_bbox = AsyncMock(
        return_value={"events": [], "filtered_count": 0, "total_count": 0})
    with patch.object(srv, "_client", fake_client):
        await srv.call_tool("glassbox.events.algorithm_findings",
                            {"hours": 72})
    kwargs = fake_client.events_in_bbox.await_args.kwargs
    tf = datetime.fromisoformat(kwargs["time_from"].replace("Z", "+00:00"))
    tt = datetime.fromisoformat(kwargs["time_to"].replace("Z", "+00:00"))
    delta_h = (tt - tf).total_seconds() / 3600
    # Allow ±2s slop for clock advancement between the two now() calls.
    assert 71.999 <= delta_h <= 72.001


# ─── Rate-limit gating (mirrors entities-server tests) ───────────────────


async def test_call_tool_raises_rate_limited_on_overflow():
    from mcp_servers.events import server as srv
    from mcp_servers.shared.ratelimit import (
        RateLimited, TokenBucketRateLimiter,
    )
    fake_client = MagicMock()
    fake_client.events_search_by_text = AsyncMock(
        return_value={"events": []})
    tiny = TokenBucketRateLimiter(capacity=1.0, refill_per_sec=0.001)
    args = {"query": "x"}
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", tiny):
        await srv.call_tool("glassbox.events.search", args)
        with pytest.raises(RateLimited):
            await srv.call_tool("glassbox.events.search", args)


async def test_call_tool_rate_limit_does_not_call_client():
    """When the limiter denies, the dispatcher must NOT touch the REST
    client and must NOT enter the AuditCall block."""
    from mcp_servers.events import server as srv
    from mcp_servers.shared.ratelimit import (
        RateLimited, TokenBucketRateLimiter,
    )
    fake_client = MagicMock()
    fake_client.events_search_by_text = AsyncMock(
        return_value={"events": []},
        side_effect=AssertionError("client must NOT be called when "
                                   "rate-limited"),
    )
    drained = TokenBucketRateLimiter(capacity=0.0001, refill_per_sec=0.0001)
    with patch.object(srv, "_client", fake_client), \
         patch.object(srv, "_RATE_LIMITER", drained):
        with pytest.raises(RateLimited):
            await srv.call_tool("glassbox.events.search", {"query": "x"})
    fake_client.events_search_by_text.assert_not_called()
