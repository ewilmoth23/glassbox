"""Tier-1 alerts surface — `/alerts/*` (extraction #4 of P3-H Phase 2).

Two routes plus the shared poll helper they coordinate:

  GET /alerts/timeseries — per-event-type counts bucketed across a time
                            window. Powers the sparklines on each alert
                            tile.
  GET /alerts/stream     — Server-Sent Events feed of newly-inserted
                            tier-1 events. Server-side polled (algorithms
                            insert directly via SQL, not the in-process
                            broadcast hook).

Module-level constants + helper (all moved with this cluster):

  _TIER1_EVENT_TYPES_FOR_POLL  — 13-element tuple of event_types to surface
                                  on the /alerts surface. Consumed by
                                  /alerts/timeseries + _poll_new_tier1_events
                                  + health_metrics's /system-state (via
                                  re-export at api_v1 module bottom).
  _TIER1_EVENT_TYPES           — 14-element tuple (adds `volcanic_alert`)
                                  emitted in the SSE `hello` payload so
                                  subscribers can pre-style their alert
                                  tiles.
  _poll_new_tier1_events       — async SQL helper. Public-by-test:
                                  test_alerts_stream.py imports it from
                                  api_v1 (works via re-export shim).

`coerce_jsonb` is imported from `web/_jsonb.py` (lifted 2026-05-27 as
the P3-H Phase 2 #7 prep). `_parse_bbox` is still imported from api_v1
at module top — safe because the re-export shim that triggers this
module's load lives at the BOTTOM of api_v1.py, by which time api_v1's
`_parse_bbox` (line ~395) is already defined.

Mounted by `api_v1.build_router()` at BOTH `/api/v1/*` AND `/api/intel/*`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from db import fetch_read
from web._jsonb import coerce_jsonb as _coerce_jsonb

# `_parse_bbox` still lives in api_v1.py (consumed by inline core +
# signals handlers). Safe at module top — the api_v1 shim that loads
# this module sits at api_v1's BOTTOM, after _parse_bbox is defined.
from api_v1 import _parse_bbox


# ─── Tier-1 event-type tuples ───────────────────────────────────────────


_TIER1_EVENT_TYPES_FOR_POLL: Tuple[str, ...] = (
    # Highest tier — sanctioned vessel just docked at a strategic /
    # sanctions-watchlist port. Phase 4d-4 (2026-05-09).
    "sanctioned_port_arrival",
    "shadow_fleet_cluster",
    "sanctioned_vessel_multijurisdictional",
    "sanctioned_vessel_went_dark",
    "sanctioned_vessel_rendezvous",
    "aircraft_in_sanctioned_airspace",
    "sanctioned_vessel_underway",
    "dark_vessel_detected",
    "military_aircraft_underway",
    "rendezvous_detected",
    "loitering_detected",
    "swpc_alert",
    "gdacs_alert",
)


# Why a SECOND tuple: the SSE `hello` payload advertises the full client-
# styleable set (which the *_FOR_POLL tuple doesn't include — that one
# governs what the polling query actually surfaces). Specifically,
# volcanic_alert is rare enough that we don't want to slow down each
# poll by ANY-array-checking it, but clients still want to know it
# *might* arrive on the same channel.
_TIER1_EVENT_TYPES: Tuple[str, ...] = (
    "sanctioned_port_arrival",
    "shadow_fleet_cluster",
    "sanctioned_vessel_multijurisdictional",
    "sanctioned_vessel_went_dark",
    "sanctioned_vessel_rendezvous",
    "aircraft_in_sanctioned_airspace",
    "sanctioned_vessel_underway",
    "dark_vessel_detected",
    "military_aircraft_underway",
    "rendezvous_detected",
    "loitering_detected",
    "swpc_alert",
    "gdacs_alert",
    "volcanic_alert",
)


# ─── Helper for the SSE poll (test_alerts_stream.py imports this) ──────


async def _poll_new_tier1_events(
    since: datetime,
    bbox: Optional[Tuple[float, float, float, float]],
) -> List[Dict[str, Any]]:
    """Pull tier-1 events with event_time > since. Optionally bbox-filtered."""
    if bbox:
        west, south, east, north = bbox
        sql = """
            SELECT id, event_type, event_subtype, event_time, severity,
                   title, description, properties, entity_id,
                   ST_Y(geom::geometry) AS lat,
                   ST_X(geom::geometry) AS lng
            FROM event
            WHERE event_time > $1
              AND event_type = ANY($2::text[])
              AND geom IS NOT NULL
              AND ST_Intersects(geom::geometry,
                                ST_MakeEnvelope($3, $4, $5, $6, 4326))
            ORDER BY event_time ASC
            LIMIT 200
        """
        return await fetch_read(
            sql, since, list(_TIER1_EVENT_TYPES_FOR_POLL),
            west, south, east, north,
        )
    sql = """
        SELECT id, event_type, event_subtype, event_time, severity,
               title, description, properties, entity_id,
               ST_Y(geom::geometry) AS lat,
               ST_X(geom::geometry) AS lng
        FROM event
        WHERE event_time > $1
          AND event_type = ANY($2::text[])
        ORDER BY event_time ASC
        LIMIT 200
    """
    return await fetch_read(sql, since, list(_TIER1_EVENT_TYPES_FOR_POLL))


# ─── Routes ───────────────────────────────────────────────────────────


router = APIRouter()


@router.get("/alerts/timeseries")
async def alerts_timeseries(
    hours: int = Query(24, ge=1, le=168,
        description="How many hours back from now to bucket. Default 24."),
    bucket_minutes: int = Query(60, ge=5, le=720,
        description="Bucket size in minutes. Default 60 (hourly)."),
):
    """Per-event-type counts per time bucket over the last `hours` window.

    Returns a flat structure: {event_types: [...], buckets: [...iso...],
    counts: {event_type: [...n_per_bucket...]}}.

    Used by the dashboard to draw sparklines on each tier-1 alert tile,
    so operators can see whether the firing rate is rising or falling
    rather than just an absolute count.
    """
    # Single-source the window start so the SQL date_bin origin and the
    # Python bucket_axis match to the microsecond. Computing
    # datetime.now() twice produces a drift of a few hundred microseconds,
    # which is enough to make the dict-key comparison fail and return
    # all-zero counts.
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    bucket_delta = timedelta(minutes=bucket_minutes)

    rows = await fetch_read(
        """
        WITH bins AS (
            SELECT
                date_bin($1, event_time, $3::timestamptz) AS bucket,
                event_type,
                COUNT(*) AS n
            FROM event
            WHERE event_time >= $3::timestamptz
              AND event_type = ANY($2::text[])
            GROUP BY 1, 2
        )
        SELECT bucket, event_type, n FROM bins ORDER BY bucket
        """,
        bucket_delta,                          # asyncpg encodes timedelta → INTERVAL
        list(_TIER1_EVENT_TYPES_FOR_POLL),
        start,
    )
    # Build a dense bucket axis from start → start+hours in bucket_delta
    # steps so the front-end sparkline doesn't need to fill gaps.
    n_buckets = (hours * 60) // bucket_minutes
    bucket_axis: List[datetime] = [
        start + bucket_delta * i for i in range(n_buckets + 1)
    ]
    # Map (bucket, event_type) → count; default 0
    counts_map: Dict[tuple, int] = {}
    for r in rows:
        counts_map[(r["bucket"], r["event_type"])] = r["n"]
    # Build per-event-type dense series
    counts: Dict[str, List[int]] = {
        t: [counts_map.get((b, t), 0) for b in bucket_axis]
        for t in _TIER1_EVENT_TYPES_FOR_POLL
    }
    return {
        "hours": hours,
        "bucket_minutes": bucket_minutes,
        "buckets": [b.isoformat() for b in bucket_axis],
        "event_types": list(_TIER1_EVENT_TYPES_FOR_POLL),
        "counts": counts,
    }


# ─── Tier-1 alerts SSE stream ─────────────────────────────────────────
# Pushes newly-inserted tier-1 events (sanctioned-vessel-rendezvous,
# sanctioned-vessel-went-dark, dark-vessel-detected, military-aircraft,
# sanctioned-airspace, etc.) to subscribed clients. Server polls the
# event table every poll_sec and yields any rows newer than its
# connection-start watermark.
#
# Why server-side polling rather than push: algorithms write findings
# directly to the DB via SQL INSERT; they don't go through the in-
# process broadcast hook. Polling the DB is the simplest way to
# surface them without modifying every algorithm. With a 5-10s
# interval the staleness vs the running scan loop (~5min cycle) is
# negligible.


@router.get("/alerts/stream")
async def alerts_stream(
    request: Request,
    bbox: Optional[str] = Query(
        None,
        description="Optional bbox 'west,south,east,north'. If omitted, world.",
    ),
    poll_sec: float = Query(10.0, ge=1.0, le=60.0,
        description="DB poll interval. Default 10s."),
):
    bbox_t: Optional[Tuple[float, float, float, float]] = None
    if bbox:
        bbox_t = _parse_bbox(bbox)

    async def event_gen():
        # Watermark: only emit events with event_time > watermark on
        # each poll. Initialize to NOW so the first poll only catches
        # things that fire AFTER connection. (Initial state comes from
        # the client's separate /viewport call.)
        watermark = datetime.now(timezone.utc)
        yield {
            "event": "hello",
            "data": json.dumps({
                "ts": watermark.isoformat(),
                "tier1_event_types": list(_TIER1_EVENT_TYPES),
                "poll_sec": poll_sec,
                "bbox": list(bbox_t) if bbox_t else None,
            }),
        }
        try:
            while True:
                if await request.is_disconnected():
                    return
                rows = await _poll_new_tier1_events(watermark, bbox_t)
                if rows:
                    # Advance watermark past the newest row
                    watermark = max(r["event_time"] for r in rows)
                    for r in rows:
                        yield {
                            "event": "alert",
                            "data": json.dumps({
                                "id": str(r["id"]),
                                "event_type": r["event_type"],
                                "event_subtype": r["event_subtype"],
                                "event_time": r["event_time"].isoformat(),
                                "severity": float(r["severity"]) if r["severity"] is not None else None,
                                "title": r["title"],
                                "description": r["description"],
                                "properties": _coerce_jsonb(r["properties"]),
                                "entity_id": str(r["entity_id"]) if r["entity_id"] else None,
                                "lat": float(r["lat"]) if r["lat"] is not None else None,
                                "lng": float(r["lng"]) if r["lng"] is not None else None,
                            }),
                        }
                else:
                    # Heartbeat so proxies don't time out the connection
                    yield {
                        "event": "ping",
                        "data": json.dumps({
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }),
                    }
                await asyncio.sleep(poll_sec)
        except asyncio.CancelledError:
            return

    return EventSourceResponse(event_gen())
