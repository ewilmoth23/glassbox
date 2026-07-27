"""Landing + shell page routes — public-facing HTML surfaces.

Extracted from `glassbox_server.py` 2026-05-21 EVE as P3-H extraction
#6 under Option A (third commit in the trilogy after `fde3e57` lifted
shared infra and `84e03c3` extracted the static-asset routes).
7 routes:

  GET /          — landing page with cache-busting atlas.js injection,
                   DASHBOARD_PATH fallback if landing/index.html missing
  GET /web       — legacy glassbox-web.html dashboard (no-globe)
  GET /glassbox  — production glassbox.html 3D-globe page (~2 MB)
  GET /markets   — glassbox-markets.html prediction-markets feed
  GET /pro       — glassbox-pro.html Pro tier sales/landing
  GET /console   — operator console (localhost-only by intent)
  GET /demo      — Phase 1.5 demo for /api/v1/viewport

The `/` handler is the second consumer of the `atlas_hash()` shared
helper (alongside `/atlas.js` in `web/routes/static.py`). Both pull
from `web._assets` so the URL hash and the served file content cannot
drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from web._assets import (
    DASHBOARD_PATH,
    LANDING_PATH,
    atlas_hash,
    serve_static_html,
)

router = APIRouter()

# `web/routes/pages.py` → parent.parent.parent is `21_GLASSBOX_AI/`.
_PKG_ROOT = Path(__file__).resolve().parent.parent.parent
_CONSOLE_PATH = _PKG_ROOT / "console" / "index.html"
_DEMO_PATH = _PKG_ROOT / "demo" / "index.html"


@router.get("/", response_class=HTMLResponse)
async def root_landing() -> HTMLResponse:
    """Public-facing landing page — clean introduction + live ticker
    of latest critical findings + 'pick your channel' overview of all
    six consumer surfaces (signals / status / RSS / JSON Feed / CSV /
    embed widget / entity profile).

    Injects a content-hash query param onto the atlas.js script tag
    (replacing any hard-coded ?v=...) so the URL changes whenever
    atlas.js does. Solves the Cloudflare-overrides-Cache-Control
    problem at its real layer: by making the URL the cache key.

    The legacy 115KB glassbox-web.html dashboard is unchanged and
    still served at /web (see serve_web_dashboard)."""
    if LANDING_PATH.exists():
        html = LANDING_PATH.read_text(encoding="utf-8")
        # Replace any `/atlas.js?v=…` script src with `/atlas.js?h={hash}`.
        # The pattern is anchored on the literal `/atlas.js?` prefix and
        # ends at the next quote so it works regardless of which `?v=` we
        # last committed. If no ?v= is present, fall back to plain /atlas.js.
        h = atlas_hash()
        html = re.sub(
            r'/atlas\.js\?[^"\']*',
            f'/atlas.js?h={h}',
            html,
        )
        # No-cache on the root HTML so the browser always fetches it fresh
        # and sees the latest `?h=…` value. The HTML is tiny (~52KB) so
        # paying that cost on every load is fine. Cloudflare's Browser
        # Cache TTL respects `no-cache` (unlike `max-age=0`) for the root,
        # since the latter would otherwise be overridden by their default.
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-cache, must-revalidate",
                "CDN-Cache-Control": "no-cache",
                "Cloudflare-CDN-Cache-Control": "no-cache",
            },
        )
    # Fallback: if the new landing is missing for any reason, fall
    # through to the legacy dashboard rather than 404 the root.
    if DASHBOARD_PATH.exists():
        return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(
        "<h1>Glassbox Server :8790</h1>"
        "<p>No landing or dashboard file found.</p>"
        "<p>Try <a href='/signals'>/signals</a> or "
        "<a href='/api/health'>/api/health</a>.</p>",
        status_code=200,
    )


@router.get("/web", response_class=HTMLResponse)
async def serve_web_dashboard() -> HTMLResponse:
    """Serve glassbox-web.html (no-globe dashboard) from same-origin so
    SSE + fetch work without the file:// quirks."""
    if not DASHBOARD_PATH.exists():
        return HTMLResponse(
            f"<p>Not found: {DASHBOARD_PATH}</p>", status_code=404,
        )
    return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))


@router.get("/glassbox", response_class=HTMLResponse)
async def serve_glassbox_3d() -> HTMLResponse:
    """The big production glassbox.html — 3D-globe view. ~2 MB so first
    paint is slow on cold loads, but subsequent navigation is cached."""
    return serve_static_html("glassbox.html")


@router.get("/markets", response_class=HTMLResponse)
async def serve_glassbox_markets() -> HTMLResponse:
    """glassbox-markets.html — severity ≥ 5 events filtered for
    prediction-markets traders. Subscribes to the same /api/glassbox/stream
    SSE as /web."""
    return serve_static_html("glassbox-markets.html")


@router.get("/pro", response_class=HTMLResponse)
async def serve_glassbox_pro() -> HTMLResponse:
    """glassbox-pro.html — Pro tier sales/landing page. Light backend
    integration (watchlist + email capture only)."""
    return serve_static_html("glassbox-pro.html")


@router.get("/console", response_class=HTMLResponse)
async def serve_operator_console() -> HTMLResponse:
    """Operator console — surfaces the new endpoints (/event/{id},
    /metrics/prefilter, /entities/{id}/cross_domain) in a single
    operator-grade page. Localhost-only by intent (binds 127.0.0.1).
    Same-origin so the JS can fetch /api/v1/* without CORS.

    Lives at 21_GLASSBOX_AI/console/index.html. NOT in
    05_WEBSITE_AND_LANDING/ because it's checked in to git (no
    embedded secrets) and the operator dashboard genuinely belongs
    next to the daemon code rather than the public marketing site.
    """
    if not _CONSOLE_PATH.exists():
        return HTMLResponse(
            "<p>console/index.html not found</p>", status_code=404,
        )
    return HTMLResponse(_CONSOLE_PATH.read_text(encoding="utf-8"))


@router.get("/demo", response_class=HTMLResponse)
async def serve_v1_demo() -> HTMLResponse:
    """Same-origin demo for /api/v1/viewport — exercises the killer query
    end-to-end including the deterministic + LLM brief layers.

    Phase 1.5/Phase 3-preview: minimal demo page that exercises
    /api/v1/viewport with a Leaflet map + brief panel. Lives at
    21_GLASSBOX_AI/demo/index.html so it ships alongside the backend
    and isn't part of the 05_WEBSITE_AND_LANDING production deploy."""
    if not _DEMO_PATH.exists():
        return HTMLResponse(
            f"<p>Demo file not found: {_DEMO_PATH}</p>", status_code=404,
        )
    return HTMLResponse(_DEMO_PATH.read_text(encoding="utf-8"))
