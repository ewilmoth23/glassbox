"""`/globe/*` — 3D globe view with conflict-spike extrusions.

Extracted from `glassbox_server.py` 2026-05-21 EVE as P3-H extraction
#4. Two routes:
  - `GET /globe` — globe.gl + three.js page (both MIT-licensed)
  - `GET /globe/globe.js` — sibling JS bundle

Sister to `/monitor`: same data, different rendering. SSE-driven live
spike pulses.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

_GLOBE_DIR = Path(__file__).resolve().parent.parent.parent / "globe"


@router.get("/globe", response_class=HTMLResponse)
async def serve_globe_page() -> HTMLResponse:
    """3D globe view with conflict-spike extrusions per grid cell.
    Sister to /monitor: same data, different rendering. globe.gl + three.js,
    both MIT-licensed. SSE-driven live spike pulses."""
    p = _GLOBE_DIR / "index.html"
    if not p.exists():
        return HTMLResponse(f"<p>Globe page not found: {p}</p>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@router.get("/globe/globe.js", include_in_schema=False)
async def serve_globe_js() -> Response:
    """The globe page's JS, served as a sibling URL so the inline
    `<script src="/globe/globe.js">` tag resolves cleanly."""
    p = _GLOBE_DIR / "globe.js"
    if not p.exists():
        return Response(
            "// globe.js missing",
            status_code=404,
            media_type="application/javascript",
        )
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=10"},
    )
