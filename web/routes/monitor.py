"""`/monitor/*` — deck.gl/maplibre situational-awareness console.

Extracted from `glassbox_server.py` 2026-05-21 LATER as P3-H extraction
#3. Three routes:
  - `GET /monitor` — the dashboard page
  - `GET /monitor/monitor.js` — sibling JS bundle
  - `GET /monitor/countries.geojson` — Natural Earth 110m borders
    (~820 KB, public domain, fetched via
    `09_SETUP_GUIDES/scripts/glassbox/fetch_countries_geojson.sh`)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

_MONITOR_DIR = Path(__file__).resolve().parent.parent.parent / "monitor"


@router.get("/monitor", response_class=HTMLResponse)
async def serve_monitor_page() -> HTMLResponse:
    """The deck.gl/maplibre-based situational-awareness console (Phase 3
    preview). Inspired by the worldmonitor.app reference dashboard
    (AGPL-3.0); built fresh — no copied code. Wires to Glassbox's
    existing /api/v1/signals/today + /api/v1/viewport endpoints."""
    p = _MONITOR_DIR / "index.html"
    if not p.exists():
        return HTMLResponse(f"<p>Monitor page not found: {p}</p>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@router.get("/monitor/monitor.js", include_in_schema=False)
async def serve_monitor_js() -> Response:
    """The monitor page's JS, served as a sibling URL so the inline
    `<script src="/monitor/monitor.js">` tag resolves cleanly without
    embedding ~9KB of JS in the HTML body. Cache-Control short so
    edits show up on refresh during dev."""
    p = _MONITOR_DIR / "monitor.js"
    if not p.exists():
        return Response(
            "// monitor.js missing",
            status_code=404,
            media_type="application/javascript",
        )
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=10"},
    )


@router.get("/monitor/countries.geojson", include_in_schema=False)
async def serve_monitor_countries() -> Response:
    """Natural Earth 110m countries GeoJSON (~820KB, public domain).
    Fetched once via 09_SETUP_GUIDES/scripts/glassbox/fetch_countries_geojson.sh.
    Used by /monitor's country-intel highlight overlay. Long cache TTL
    since the borders don't change. 404 if the script hasn't been run —
    the JS handles that gracefully (overlay just doesn't render)."""
    p = _MONITOR_DIR / "countries.geojson"
    if not p.exists():
        return Response("{}", status_code=404, media_type="application/json")
    return Response(
        content=p.read_bytes(),
        media_type="application/geo+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )
