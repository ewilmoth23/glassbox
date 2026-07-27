"""`/signals/*` — public dashboard, iframe widget, and feed aliases.

Extracted from `glassbox_server.py` 2026-05-21 LATER as P3-H extraction
#2. Four routes:
  - `GET /signals/embed` — iframe-friendly slim widget for partner sites
  - `GET /signals` — public "today's signals" dashboard
  - `GET /signals.rss` — root-level alias → `/api/v1/signals.rss`
  - `GET /signals.json` — root-level alias → `/api/v1/signals.json`

The `.rss` / `.json` shortcuts redirect because RSS readers and JSON
Feed consumers (Zapier, Pipedream, Slack) expect feed URLs at the site
root, not under a versioned API prefix.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()

_SIGNALS_DIR = Path(__file__).resolve().parent.parent.parent / "signals"
_SIGNALS_PATH = _SIGNALS_DIR / "index.html"
_SIGNALS_EMBED_PATH = _SIGNALS_DIR / "embed" / "index.html"


@router.get("/signals/embed", response_class=HTMLResponse)
async def serve_signals_embed_widget() -> HTMLResponse:
    """Iframe-friendly slim widget. Configurable via query params:
    ?max=8 ?sev=critical,high ?cat=sanctioned_dark,… ?theme=auto|dark|light
    ?title=Custom heading.

    Designed for partner sites to drop into a sidebar or footer:
        <iframe src="https://mewrcreate.com/signals/embed?max=5&sev=critical"
                width="320" height="320" style="border:0"></iframe>

    Transparent background so the embedding page's theme bleeds
    through (theme defaults to auto = honor prefers-color-scheme).
    Refreshes once per minute via background fetch — no SSE so the
    widget doesn't hold an open connection per visitor.
    """
    if not _SIGNALS_EMBED_PATH.exists():
        return HTMLResponse(
            f"<p>Embed widget not found: {_SIGNALS_EMBED_PATH}</p>",
            status_code=404,
        )
    # Allow this page to be iframed cross-origin. The default ASGI
    # response has no X-Frame-Options, but if any future middleware
    # adds DENY/SAMEORIGIN that would silently break embeds — set
    # the explicit allow-list-friendly header here as a contract.
    return HTMLResponse(
        _SIGNALS_EMBED_PATH.read_text(encoding="utf-8"),
        headers={
            "X-Frame-Options": "ALLOWALL",
            "Content-Security-Policy": "frame-ancestors *",
        },
    )


@router.get("/signals", response_class=HTMLResponse)
async def serve_signals_dashboard() -> HTMLResponse:
    """Public-facing 'today's signals' dashboard. Pulls
    /api/v1/signals/today and renders the algorithm-derived findings
    grouped by category. Designed to render top-to-bottom on a single
    screen — no map, no kitchen-sink. Same-origin so the page can hit
    /api/v1/* without CORS."""
    if not _SIGNALS_PATH.exists():
        return HTMLResponse(
            f"<p>Signals page not found: {_SIGNALS_PATH}</p>",
            status_code=404,
        )
    return HTMLResponse(_SIGNALS_PATH.read_text(encoding="utf-8"))


@router.get("/signals.rss", include_in_schema=False)
async def signals_rss_shortcut(request: Request) -> RedirectResponse:
    """Friendly URL alias for /api/v1/signals.rss — RSS readers expect
    feed URLs at the site root, not under a versioned API prefix."""
    qs = request.url.query
    target = "/api/v1/signals.rss" + (f"?{qs}" if qs else "")
    return RedirectResponse(target, status_code=302)


@router.get("/signals.json", include_in_schema=False)
async def signals_json_shortcut(request: Request) -> RedirectResponse:
    """Friendly URL alias for /api/v1/signals.json (JSON Feed v1.1).
    Same root-aliasing rationale as /signals.rss — JSON Feed
    consumers (Zapier, Pipedream, Slack JSON-feed connector) expect
    feed URLs at the site root."""
    qs = request.url.query
    target = "/api/v1/signals.json" + (f"?{qs}" if qs else "")
    return RedirectResponse(target, status_code=302)
