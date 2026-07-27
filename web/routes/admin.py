"""Admin + pricing + entity-profile + public-status pages.

Extracted from `glassbox_server.py` 2026-05-22 as P3-H extraction #7.
Four routes that don't fit any other extracted concern, grouped here
because they're all small static-html-file handlers with single-route
local infrastructure:

  GET /admin/analytics  — operator-only analytics dashboard. Secret-gated
                          via cookie OR `X-Admin-Secret` header OR
                          `?admin_secret=…` query (one-shot bookmark
                          that sets the cookie). noindex,nofollow.
  GET /pricing          — Free/Pro/Team/Enterprise tiers page
  GET /entity/{id}      — per-entity profile (JS reads UUID from path,
                          fetches /api/v1/entity/{id}, cross_domain,
                          aliases in parallel)
  GET /status           — public-facing status page (Phase 6.3); polls
                          /api/v1/health/full every 30s

`_check_admin_auth` is module-private here because it's used by only
this one handler. If a future extraction adds another admin-gated
route, lift it to `web/_auth.py` following the Option A pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

# `web/routes/admin.py` → parent.parent.parent is `21_GLASSBOX_AI/`.
_PKG_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_DIR = _PKG_ROOT / "admin"
_PRICING_DIR = _PKG_ROOT / "pricing"
_ENTITY_PAGE_PATH = _PKG_ROOT / "entity" / "index.html"
_STATUS_PATH = _PKG_ROOT / "status" / "index.html"


def _check_admin_auth(request: Request) -> bool:
    """Gate for /admin/* and underlying analytics endpoints. Accepts:
       - Cookie: glassbox_admin=<secret>
       - Header: X-Admin-Secret: <secret>
       - Query : ?admin_secret=<secret>  (one-shot bookmark, sets cookie)
       Fail-closed if env not set."""
    expected = os.environ.get("GLASSBOX_ADMIN_SECRET") or ""
    if not expected:
        return False
    return (
        (request.cookies.get("glassbox_admin") or "") == expected or
        (request.headers.get("X-Admin-Secret") or "") == expected or
        (request.query_params.get("admin_secret") or "") == expected
    )


@router.get("/admin/analytics", response_class=HTMLResponse, include_in_schema=False)
async def serve_admin_analytics(request: Request):
    """Operator-only analytics dashboard. noindex,nofollow + secret-gated.
    First visit: append `?admin_secret=…` to the URL — sets a cookie and
    subsequent visits work without the query string."""
    if not _check_admin_auth(request):
        return JSONResponse(
            {"error": "unauthorized",
             "hint": "?admin_secret=<GLASSBOX_ADMIN_SECRET>"},
            401,
        )
    p = _ADMIN_DIR / "analytics.html"
    if not p.exists():
        return HTMLResponse("<p>Admin page missing.</p>", status_code=404)
    resp = HTMLResponse(p.read_text(encoding="utf-8"))
    # If they came via ?admin_secret query, set a 30-day cookie.
    qs_secret = request.query_params.get("admin_secret") or ""
    if qs_secret and qs_secret == (os.environ.get("GLASSBOX_ADMIN_SECRET") or ""):
        resp.set_cookie(
            "glassbox_admin", qs_secret, max_age=2592000,
            httponly=True, secure=True, samesite="lax",
        )
    return resp


@router.get("/pricing", response_class=HTMLResponse, include_in_schema=False)
async def serve_pricing_page() -> HTMLResponse:
    """Pricing page — Free/Pro/Team/Enterprise tiers. Pro and Team CTAs
    funnel to a waitlist via the existing /api/v1/signals/subscribe
    endpoint (source=pricing-{tier}). Once Stripe Payment Links exist
    for the Glassbox SaaS tiers, swap the modal `fetch` → those URLs."""
    p = _PRICING_DIR / "index.html"
    if not p.exists():
        return HTMLResponse("<p>Pricing page missing.</p>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@router.get("/entity/{entity_id}", response_class=HTMLResponse)
async def serve_entity_profile_page(entity_id: str) -> HTMLResponse:
    """Public-facing entity profile. Single static page; the JS reads
    the UUID out of window.location.pathname and fetches three
    backend endpoints in parallel: /api/v1/entity/{id} for profile +
    track, /api/v1/entities/{id}/cross_domain for multi-entity
    findings, /api/v1/entities/{id}/aliases for Splink ER edges.

    Path-shape validation is done client-side; an invalid UUID just
    surfaces the backend's 400 in the page's error banner. Saves a
    DB round-trip on the page load itself.
    """
    if not _ENTITY_PAGE_PATH.exists():
        return HTMLResponse(
            f"<p>Entity page not found: {_ENTITY_PAGE_PATH}</p>",
            status_code=404,
        )
    return HTMLResponse(_ENTITY_PAGE_PATH.read_text(encoding="utf-8"))


@router.get("/status", response_class=HTMLResponse)
async def serve_public_status_page() -> HTMLResponse:
    """Public-facing status page (Phase 6.3). Polls /api/v1/health/full
    every 30s and renders a clean Atlassian-style overview — overall
    banner (green / yellow / red), per-source ingester health grouped
    by layer, DB latency, SLA-breach count. Lives next to the daemon
    code so it ships alongside the API it depends on."""
    if not _STATUS_PATH.exists():
        return HTMLResponse(
            f"<p>Status page not found: {_STATUS_PATH}</p>",
            status_code=404,
        )
    return HTMLResponse(_STATUS_PATH.read_text(encoding="utf-8"))
