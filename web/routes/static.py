"""Static-asset routes — favicons, the atlas.js controller, legacy JS
aliases, the OG image, robots.txt, sitemap.xml.

Extracted from `glassbox_server.py` 2026-05-21 EVE as P3-H extraction
#5 under Option A (paired with the `web/_assets.py` lift in commit
`fde3e57`). 10 routes:

  GET /favicon.svg          — Glassbox SVG favicon
  GET /favicon.ico          — 302 redirect to /favicon.svg
  GET /atlas.js             — landing-page JS controller
                              (sibling URL to / so inline <script src>
                               resolves cleanly; ETag + short CDN TTL)
  GET /command.js           — legacy alias of /atlas.js
  GET /track.js             — first-party analytics tracker
  GET /satellite.min.js     — self-hosted satellite.js (MIT) for
                              in-browser SGP4 propagation
  GET /satellites_worker.js — Web Worker that runs SGP4 off main thread
  GET /og-image.png         — server-rendered OG image (1200×630) with
                              live KPI stats; depends on og_image module
  GET /robots.txt           — crawl rules
  GET /sitemap.xml          — XML sitemap

The `/atlas.js` handler reads from `LANDING_ATLAS_JS_PATH`; the `/`
landing page handler (in `web/routes/pages.py`) reads from the SAME
path AND calls `atlas_hash()` to inject the content hash into the
script src URL for cache-busting. Both modules pull from
`web._assets` so they cannot drift.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from web._assets import (
    LANDING_ATLAS_JS_PATH,
    LANDING_DIR,
    LANDING_FAVICON_PATH,
)

router = APIRouter()


@router.get("/favicon.svg", include_in_schema=False)
async def serve_favicon_svg() -> Response:
    """Glassbox brand-mark favicon. SVG so it crisp-renders at every
    size, dark-mode-friendly, no separate ICO needed for modern browsers."""
    if not LANDING_FAVICON_PATH.exists():
        return Response("", status_code=404, media_type="image/svg+xml")
    return Response(
        content=LANDING_FAVICON_PATH.read_text(encoding="utf-8"),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.ico", include_in_schema=False)
async def serve_favicon_ico() -> RedirectResponse:
    """Compat: redirects to the SVG. Modern browsers prefer the
    rel='icon' SVG declared in the HTML head, but some bookmark
    managers + RSS readers still hit /favicon.ico directly."""
    return RedirectResponse("/favicon.svg", status_code=302)


@router.get("/atlas.js", include_in_schema=False)
async def serve_atlas_js() -> Response:
    """The landing-page cockpit's JS controller. Sibling URL to / so
    the inline `<script src='/atlas.js?h=…'>` in landing/index.html
    resolves cleanly.

    Cache-Control headers force CDN + browser to re-validate every
    visit. Cloudflare's default Browser Cache TTL was overriding our
    short TTL with 31 days; `no-cache, must-revalidate, max-age=0`
    survives that and ETag/304 keeps the round-trip cheap when content
    didn't change."""
    if not LANDING_ATLAS_JS_PATH.exists():
        return Response(
            "// atlas.js missing",
            status_code=404,
            media_type="application/javascript",
        )
    content = LANDING_ATLAS_JS_PATH.read_text(encoding="utf-8")
    etag = '"' + hashlib.md5(content.encode()).hexdigest()[:16] + '"'
    return Response(
        content=content,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, must-revalidate, max-age=0",
            "CDN-Cache-Control": "max-age=10",
            "Cloudflare-CDN-Cache-Control": "max-age=10",
            "ETag": etag,
        },
    )


@router.get("/command.js", include_in_schema=False)
async def serve_command_js() -> Response:
    """Legacy alias of /atlas.js — kept so any cached html that still
    references /command.js doesn't 404 during the cutover."""
    if not LANDING_ATLAS_JS_PATH.exists():
        return Response(
            "// atlas.js missing",
            status_code=404,
            media_type="application/javascript",
        )
    return Response(
        content=LANDING_ATLAS_JS_PATH.read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=10"},
    )


@router.get("/track.js", include_in_schema=False)
async def serve_track_js() -> Response:
    """First-party analytics tracker — ~700 bytes inline. Loaded
    `async` from every public page. POSTs pageview + custom events
    to /api/v1/analytics/event."""
    p = LANDING_DIR / "track.js"
    if not p.exists():
        return Response(
            "// track.js missing",
            status_code=404,
            media_type="application/javascript",
        )
    return Response(
        content=p.read_bytes(),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/satellite.min.js", include_in_schema=False)
async def serve_satellite_js() -> Response:
    """Self-hosted satellite.js (MIT) for in-browser SGP4 propagation.
    Saved at landing/satellite.min.js to avoid CDN dependency."""
    p = LANDING_DIR / "satellite.min.js"
    if not p.exists():
        return Response(
            "// satellite.min.js missing",
            status_code=404,
            media_type="application/javascript",
        )
    return Response(
        content=p.read_bytes(),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/satellites_worker.js", include_in_schema=False)
async def serve_satellites_worker() -> Response:
    """Web Worker that runs SGP4 propagation off the main thread.
    Without it, propagating ~100 sats every 30s burns ~50ms on the
    Cesium render thread and stutters the globe."""
    p = LANDING_DIR / "satellites_worker.js"
    if not p.exists():
        return Response(
            "// worker missing",
            status_code=404,
            media_type="application/javascript",
        )
    return Response(
        content=p.read_bytes(),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=10"},
    )


@router.get("/og-image.png", include_in_schema=False)
async def serve_og_image() -> Response:
    """Server-rendered Open Graph image (1200×630). Pulled into KPI
    cards using the live dashboard summary so social shares display
    current-state intel counts. Cached 30 min."""
    import httpx
    from og_image import get_og_png

    stats = None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(
                "http://127.0.0.1:8790/api/v1/dashboard/summary?window_hours=24"
            )
            if r.status_code == 200:
                d = r.json()
                hr = await client.get(
                    "http://127.0.0.1:8790/api/v1/health/full"
                )
                ing = hr.json().get("ingesters", {}) if hr.status_code == 200 else {}
                stats = {
                    "critical":        d.get("critical", 0),
                    "open_cases":      d.get("open_cases", 0),
                    "signals":         d.get("signals", 0),
                    "ingesters_ok":    ing.get("ok", 0),
                    "ingesters_total": ing.get("total", 0),
                }
    except Exception:
        pass
    png = await get_og_png(stats)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=900"},
    )


@router.get("/robots.txt", include_in_schema=False)
async def serve_robots() -> Response:
    """Allow indexing of public pages, disallow API + auth + admin paths."""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /signals\n"
        "Allow: /monitor\n"
        "Allow: /globe\n"
        "Allow: /network\n"
        "Allow: /status\n"
        "Allow: /docs\n"
        "Disallow: /api/\n"
        "Disallow: /admin\n"
        "Disallow: /console\n"
        "\n"
        "Sitemap: https://mewrcreate.com/sitemap.xml\n"
    )
    return Response(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sitemap.xml", include_in_schema=False)
async def serve_sitemap() -> Response:
    """Sitemap for Google/Bing — every public page Glassbox exposes.
    lastmod uses UTC date so search engines re-crawl when content shifts."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = "https://mewrcreate.com"
    pages = [
        ("/",          "1.0", "hourly"),
        ("/pricing",   "0.95","weekly"),
        ("/monitor",   "0.9", "hourly"),
        ("/globe",     "0.8", "hourly"),
        ("/network",   "0.7", "daily"),
        ("/signals",   "0.9", "hourly"),
        ("/status",    "0.5", "hourly"),
        ("/docs",      "0.5", "weekly"),
    ]
    xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path, prio, freq in pages:
        xml.append(
            f"  <url><loc>{base}{path}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority></url>"
        )
    xml.append("</urlset>")
    return Response(
        content="\n".join(xml),
        media_type="application/xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )
