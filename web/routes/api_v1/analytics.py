"""First-party analytics — `/analytics/*` (extraction #5 of P3-H Phase 2).

Two routes:

  POST /analytics/event   — pageview + custom event capture. No cookies
                            (no GDPR banner needed); IP is salted+hashed
                            with a daily-rotating key before storage,
                            making cross-day correlation impossible.
  GET  /analytics/summary — operator dashboard aggregate (pageviews,
                            uniques, top paths, conversions, referrers,
                            countries). Admin-secret gated.

Both wrap the `request_rate_limit` decorator (lifted to
`web/_rate_limit.py` in commit `fcc4d10`). The /event endpoint
permits 120/minute per IP to absorb client-side burst loops on
high-traffic pages; /summary is tighter at 30/minute because it's an
operator-only surface that should never need to be polled hard.

Mounted by `api_v1.build_router()` at BOTH `/api/v1/*` AND `/api/intel/*`.

No re-export shim needed at api_v1 — none of the symbols defined here
are imported by tests. The `_RATE_BUCKETS` re-export added in
commit `fcc4d10` (the lift) is what keeps
`test_signals_subscribe_endpoint.py` working; this extraction inherits
that shim unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from db import execute_write, fetch_read
from web._rate_limit import request_rate_limit


router = APIRouter()


@router.post("/analytics/event")
@request_rate_limit(max_per_window=120, window_sec=60, scope="analytics")
async def analytics_event(request: Request):
    """First-party analytics — pageview + custom event capture.
    No cookies (no GDPR banner needed). IP is hashed before storage.
    Body: { event_type, path, source?, referrer?, meta? }
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    event_type = (body.get("event_type") or "pageview")[:64]
    path       = (body.get("path") or "/")[:512]
    source     = (body.get("source") or "")[:128]
    referrer   = (body.get("referrer") or "")[:512]
    meta       = body.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    ip = (request.headers.get("cf-connecting-ip")
          or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "unknown"))
    # Hash IP with a daily-rotating salt — gives us "unique visitor"
    # counting without storing PII. Day-rotating means cross-day
    # matching is impossible (which is the GDPR-friendly tradeoff).
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ip_hash = hashlib.sha256(f"{ip}:{day}:glassbox".encode()).hexdigest()[:16]
    ua = request.headers.get("user-agent", "")[:300]
    country = request.headers.get("cf-ipcountry", "")[:8]

    try:
        await execute_write(
            "INSERT INTO analytics_event "
            "(event_type, path, source, referrer, meta, ip_hash, user_agent, country) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)",
            event_type, path, source, referrer,
            json.dumps(meta), ip_hash, ua, country,
        )
    except Exception:
        # Table missing → swallow (first request lazily creates it below).
        # We don't want analytics to break the pageview itself.
        pass
    return {"ok": True}


@router.get("/analytics/summary")
@request_rate_limit(max_per_window=30, window_sec=60, scope="analytics_summary")
async def analytics_summary(request: Request,
                            window_hours: int = Query(24, ge=1, le=720)):
    """Aggregate analytics for the operator dashboard.
    Admin-secret gated (mirrors /admin/analytics).
    Counts: pageviews, unique visitors, top paths, conversions
    (waitlist signups), top sources."""
    # Inline admin gate (avoid cross-import with glassbox_server).
    expected = os.environ.get("GLASSBOX_ADMIN_SECRET") or ""
    if not expected or not (
        (request.cookies.get("glassbox_admin") or "") == expected or
        (request.headers.get("X-Admin-Secret") or "") == expected or
        (request.query_params.get("admin_secret") or "") == expected
    ):
        raise HTTPException(status_code=401, detail="admin auth required")
    rows_total = await fetch_read(
        "SELECT COUNT(*) AS c FROM analytics_event "
        "WHERE ts > now() - ($1 || ' hours')::interval",
        str(window_hours),
    )
    rows_unique = await fetch_read(
        "SELECT COUNT(DISTINCT ip_hash) AS c FROM analytics_event "
        "WHERE ts > now() - ($1 || ' hours')::interval",
        str(window_hours),
    )
    rows_paths = await fetch_read(
        "SELECT path, COUNT(*) AS hits, COUNT(DISTINCT ip_hash) AS uniques "
        "FROM analytics_event "
        "WHERE event_type='pageview' AND ts > now() - ($1 || ' hours')::interval "
        "GROUP BY path ORDER BY hits DESC LIMIT 20",
        str(window_hours),
    )
    rows_conv = await fetch_read(
        "SELECT source, COUNT(*) AS conversions FROM analytics_event "
        "WHERE event_type='waitlist_signup' "
        "AND ts > now() - ($1 || ' hours')::interval "
        "GROUP BY source ORDER BY conversions DESC LIMIT 10",
        str(window_hours),
    )
    rows_referrer = await fetch_read(
        "SELECT referrer, COUNT(*) AS hits FROM analytics_event "
        "WHERE event_type='pageview' "
        "AND referrer NOT LIKE 'https://mewrcreate.com%' "
        "AND referrer NOT LIKE 'https://www.mewrcreate.com%' "
        "AND referrer != '' "
        "AND ts > now() - ($1 || ' hours')::interval "
        "GROUP BY referrer ORDER BY hits DESC LIMIT 10",
        str(window_hours),
    )
    rows_country = await fetch_read(
        "SELECT country, COUNT(DISTINCT ip_hash) AS uniques "
        "FROM analytics_event "
        "WHERE country != '' AND ts > now() - ($1 || ' hours')::interval "
        "GROUP BY country ORDER BY uniques DESC LIMIT 15",
        str(window_hours),
    )
    return {
        "window_hours": window_hours,
        "total_events": rows_total[0]["c"] if rows_total else 0,
        "unique_visitors": rows_unique[0]["c"] if rows_unique else 0,
        "top_paths": [dict(r) for r in rows_paths],
        "conversions_by_source": [dict(r) for r in rows_conv],
        "top_external_referrers": [dict(r) for r in rows_referrer],
        "top_countries": [dict(r) for r in rows_country],
    }
