"""`/network/*` — entity-relationship graph console.

Extracted from `glassbox_server.py` 2026-05-21 as the first concern of
backlog item P3-H (god-file split). Smallest meaningful slice: page
handler + sibling JS, no shared mutable state, recently touched in P2-D
(click-to-expand) so well-understood.

Wires to existing `/api/v1/signals/today`, `/api/v1/entity/{id}`, and
`/api/v1/entities/{id}/cross_domain` — no new backend endpoints.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

_NETWORK_DIR = Path(__file__).resolve().parent.parent.parent / "network"


@router.get("/network", response_class=HTMLResponse)
async def serve_network_page() -> HTMLResponse:
    """Entity-relationship graph console — Splink alias clusters,
    cross-domain partner edges, sanctioned-vessel rendezvous, shadow-
    fleet pairs. Same dark cyberpunk shell as /monitor but the center
    pane is a vis-network force-directed graph instead of a map."""
    p = _NETWORK_DIR / "index.html"
    if not p.exists():
        return HTMLResponse(f"<p>Network page not found: {p}</p>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@router.get("/network/network.js", include_in_schema=False)
async def serve_network_js() -> Response:
    """The network page's JS, served as a sibling URL so the inline
    `<script src="/network/network.js">` tag resolves cleanly. Same
    pattern as /monitor/monitor.js — small Cache-Control so edits
    show up on refresh during dev."""
    p = _NETWORK_DIR / "network.js"
    if not p.exists():
        return Response(
            "// network.js missing",
            status_code=404,
            media_type="application/javascript",
        )
    return Response(
        content=p.read_text(encoding="utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=10"},
    )
