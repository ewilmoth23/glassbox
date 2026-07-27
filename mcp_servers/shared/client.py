# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Async REST client for the Glassbox /api/v1 surface.

The MCP servers wrap REST instead of GraphQL (per RESEARCH_INTEGRATION_
PROPOSAL §8: "Adapt for REST not GraphQL"). One ``GlassboxRestClient``
per process; httpx connection pool handles keep-alive + retries.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx


_DEFAULT_BASE_URL = "http://127.0.0.1:8790"
# 30s default — comfortably above the slowest cheap endpoint we wrap
# (events/similar with q=<text> embeds the query via
# sentence-transformers, which can take 1-5s warm and ~10s cold).
# Investigation-server (LLM-bearing) tools will likely override.
_DEFAULT_TIMEOUT_SEC = 30.0


class GlassboxRestClient:
    """Thin async client. Methods return parsed JSON dicts; errors raise.

    Usage:
        client = GlassboxRestClient()
        try:
            ...
        finally:
            await client.aclose()
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._base_url = (base_url
                          or os.environ.get("GLASSBOX_API_URL", _DEFAULT_BASE_URL)
                          ).rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            headers={"User-Agent":
                     "glassbox-mcp/1.0 (mewrcreate.com/glassbox)"},
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self._base_url}{path}"
        r = await self._client.get(url, params=params)
        r.raise_for_status()
        return r.json()

    # ─── REST surface — one method per /api/v1 endpoint we expose ─────

    async def viewport(
        self,
        *,
        west: float, south: float, east: float, north: float,
        start: str, end: str,
        types: Optional[list] = None,
        limit: int = 100,
        brief: bool = False,
        brief_llm: bool = False,
    ) -> Dict[str, Any]:
        """GET /api/v1/viewport — entities within a bbox + time range.

        The endpoint takes bbox as a comma-string and the time range as
        ``time_from`` + ``time_to`` (NOT ``start``/``end``). Caller-side
        we keep the named-arg API for clarity.
        """
        params: Dict[str, Any] = {
            "bbox":      f"{west},{south},{east},{north}",
            "time_from": start,
            "time_to":   end,
            "limit":     limit,
        }
        if types:
            params["types"] = ",".join(types)
        if brief_llm:
            params["brief_llm"] = "true"
        elif brief:
            params["brief"] = "true"
        return await self.get("/api/v1/viewport", params)

    async def entity_detail(self, entity_id: str) -> Dict[str, Any]:
        """GET /api/v1/entity/{id} — full detail with track + related events."""
        return await self.get(f"/api/v1/entity/{entity_id}")

    async def entity_ftm(self, entity_id: str) -> Dict[str, Any]:
        """GET /api/v1/entity/{id}?format=ftm — FollowTheMoney shape."""
        return await self.get(f"/api/v1/entity/{entity_id}",
                              params={"format": "ftm"})

    # ─── Events surface ───────────────────────────────────────────────

    async def events_search_by_text(
        self,
        *,
        query: str,
        limit: int = 20,
        within_days: int = 30,
    ) -> Dict[str, Any]:
        """GET /api/v1/events/similar?q=<text> — semantic similarity
        search over event.embedding (HNSW cosine). Returns events
        ordered by cosine distance ascending."""
        return await self.get("/api/v1/events/similar", params={
            "q": query, "limit": limit, "within_days": within_days,
        })

    async def events_similar_to(
        self,
        *,
        event_id: str,
        limit: int = 20,
        within_days: int = 30,
    ) -> Dict[str, Any]:
        """GET /api/v1/events/similar?id=<uuid> — neighbors of an
        existing event by embedding similarity."""
        return await self.get("/api/v1/events/similar", params={
            "id": event_id, "limit": limit, "within_days": within_days,
        })

    async def alerts_timeseries(
        self,
        *,
        hours: int = 24,
        bucket_minutes: int = 60,
    ) -> Dict[str, Any]:
        """GET /api/v1/alerts/timeseries — per-event-type counts in
        time buckets. Powers trend / sparkline questions."""
        return await self.get("/api/v1/alerts/timeseries", params={
            "hours": hours, "bucket_minutes": bucket_minutes,
        })

    async def event_detail(self, event_id: str) -> Dict[str, Any]:
        """GET /api/v1/event/{event_id} — single-event detail row.

        Mirror of entity_detail's role for the event table — agents
        drilling from a search/similar/in_bbox result into the full
        event row. Returns the full record minus the embedding column
        (binary blob; use /events/similar?id=<uuid> to use it
        instead).
        """
        return await self.get(f"/api/v1/event/{event_id}")

    async def events_in_bbox(
        self,
        *,
        west: float, south: float, east: float, north: float,
        time_from: str, time_to: str,
        event_types: Optional[list] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /api/v1/viewport — events inside a bbox + time window.

        Wraps viewport with ``types=`` (empty entity-type filter so the
        server still returns events) and applies an optional client-side
        ``event_type`` filter on the response. Returns a flat shape:
        ``{events: [...], filtered_count, total_count, bbox, time_from,
        time_to}``. The viewport's entity rows are dropped — agents
        wanting entities should use the entities-MCP viewport tool.

        Note on the filter location: the underlying ``/api/v1/viewport``
        endpoint's ``types`` parameter constrains *entity* types, not
        event types. The event filter is therefore applied here in the
        client. For very busy bboxes a future iteration can push this
        down to the server with a new query parameter.

        Why ``types=infrastructure`` and not empty: the api_v1
        ``_parse_types("")`` defaults the empty value to
        ``["aircraft"]``, which kicks the LATERAL position-track join.
        On a global bbox that's ~120ms of avoidable work since we throw
        the entity rows away anyway. ``infrastructure`` is allowed by
        the validator and currently has no rows in the entity table —
        the entity query returns instantly. Revisit if/when we start
        populating infrastructure entities.
        """
        params: Dict[str, Any] = {
            "bbox":      f"{west},{south},{east},{north}",
            "time_from": time_from,
            "time_to":   time_to,
            "types":     "infrastructure",
            "limit":     limit,
        }
        raw = await self.get("/api/v1/viewport", params)
        events = raw.get("events") or []
        total = len(events)
        if event_types:
            allowed = set(event_types)
            events = [e for e in events if e.get("event_type") in allowed]
        return {
            "events":         events,
            "filtered_count": len(events),
            "total_count":    total,
            "bbox":           raw.get("meta", {}).get("bbox"),
            "time_from":      raw.get("meta", {}).get("time_from"),
            "time_to":        raw.get("meta", {}).get("time_to"),
            "query_ms":       raw.get("meta", {}).get("query_ms"),
        }

    # ─── Investigation surface ────────────────────────────────────────

    async def sanctions_search(
        self,
        *,
        query: str,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /api/v1/sanctions/search — across OFAC + EU + UK.
        Three matching paths (IMO exact, name fuzzy via trigram,
        name substring fallback). q is min 2 / max 80 chars."""
        return await self.get("/api/v1/sanctions/search", params={
            "q": query, "limit": limit,
        })

    async def entity_aliases(
        self,
        *,
        entity_id: str,
        min_confidence: float = 0.0,
    ) -> Dict[str, Any]:
        """GET /api/v1/entities/{id}/aliases — Splink ER alias edges
        for a vessel. Answers 'is this live vessel actually a
        sanctioned one under a different identifier?'"""
        return await self.get(
            f"/api/v1/entities/{entity_id}/aliases",
            params={"min_confidence": min_confidence},
        )

    async def entity_cross_domain(
        self,
        *,
        entity_id: str,
        within_hours: int = 168,
        event_types: Optional[list] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """GET /api/v1/entities/{id}/cross_domain — algorithm-derived
        events where the entity appears alongside one or more partners
        (rendezvous, proximity, sanctioned_vessel_*, shadow_fleet_cluster,
        etc.). Each event row carries a `partners` array with the
        OTHER entities resolved (display_name + canonical_id +
        entity_type)."""
        params: Dict[str, Any] = {
            "within_hours": within_hours,
            "limit":        limit,
        }
        if event_types:
            params["event_types"] = ",".join(event_types)
        return await self.get(
            f"/api/v1/entities/{entity_id}/cross_domain", params,
        )
