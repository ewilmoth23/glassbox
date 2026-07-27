"""Command-Dashboard rollup — `/dashboard/summary` (extraction #6 of P3-H Phase 2).

Single-route module. Returns the stat-strip payload shown along the
bottom of the unified Command Dashboard cockpit:
  signals      — total findings in window
  critical     — critical-severity findings
  open_cases   — distinct entities with at least one critical finding
  sources      — distinct ingester sources active in the past hour
  geolocated   — findings with a non-null geom in window
  subscribers  — verified email subscribers (community trust signal)

Single batched CTE so the endpoint stays cheap (<150 ms warm) — no
per-row fanout.

Imports `SIGNALS_CATEGORY_ORDER` + `SIGNALS_CATEGORIES_BY_TYPE` from
`web/_signals_categories.py` (lifted in commit `30cb1c2`) — those are
also consumed by the still-inline signals routes via the api_v1
re-export alias. Dashboard takes the direct import because there's
no reason to inherit the underscore-prefix legacy in a fresh module.

Mounted by `api_v1.build_router()` at BOTH `/api/v1/*` AND `/api/intel/*`.
No re-export shim needed at api_v1 — no test imports anything from this
module.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query

from db import fetch_read
from web._signals_categories import (
    SIGNALS_CATEGORIES_BY_TYPE,
    SIGNALS_CATEGORY_ORDER,
)


router = APIRouter()


@router.get("/dashboard/summary")
async def dashboard_summary(
    window_hours: int = Query(24, ge=1, le=168),
):
    """One-shot stat-strip payload for the unified Command Dashboard.

    Returns the numbers shown along the bottom of the cockpit:
      signals       — total findings in window
      critical      — critical-severity findings
      open_cases    — distinct entities with at least one critical finding
      sources       — distinct ingester sources active in the past hour
      geolocated    — findings with a non-null geom in window
      subscribers   — verified email subscribers (community trust signal)
    """
    # Single batched query to keep the endpoint cheap (<150ms warm).
    crit_types = [
        cat["event_type"] for cat in SIGNALS_CATEGORY_ORDER
        if cat["severity"] == "critical"
    ]
    all_types = list(SIGNALS_CATEGORIES_BY_TYPE.keys())

    rows = await fetch_read(
        """
        WITH win AS (
            SELECT
              count(*) FILTER (WHERE event_type = ANY($2::text[]))           AS signals,
              count(*) FILTER (WHERE event_type = ANY($3::text[]))           AS critical,
              count(DISTINCT entity_id) FILTER (
                WHERE event_type = ANY($3::text[]) AND entity_id IS NOT NULL
              )                                                              AS open_cases,
              count(*) FILTER (
                WHERE event_type = ANY($2::text[]) AND geom IS NOT NULL
              )                                                              AS geolocated
            FROM event
            WHERE event_time >= NOW() - ($1::int || ' hours')::interval
        ),
        srcs AS (
            SELECT count(DISTINCT source_id) AS sources
            FROM event
            WHERE event_time >= NOW() - INTERVAL '1 hour'
              AND source_id IS NOT NULL
        ),
        subs AS (
            SELECT count(*) AS subscribers
            FROM signals_subscription
            WHERE verified = true AND unsubscribed_at IS NULL
        )
        SELECT win.signals, win.critical, win.open_cases, win.geolocated,
               srcs.sources, subs.subscribers
        FROM win, srcs, subs
        """,
        window_hours, all_types, crit_types,
    )
    r = rows[0] if rows else {}
    return {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "window_hours":  window_hours,
        "signals":       int(r.get("signals") or 0),
        "critical":      int(r.get("critical") or 0),
        "open_cases":    int(r.get("open_cases") or 0),
        "sources":       int(r.get("sources") or 0),
        "geolocated":    int(r.get("geolocated") or 0),
        "subscribers":   int(r.get("subscribers") or 0),
    }
