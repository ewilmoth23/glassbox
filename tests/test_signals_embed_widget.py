"""
GET /signals/embed — iframe-friendly slim widget tests.

Asserts route is wired, headers permit cross-origin embedding, the page
ships configurable params (max, sev, cat, theme, title), and links open
in the parent frame (target=_top) so a click out of the iframe doesn't
leave the user trapped inside it.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_signals_embed_widget.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _client():
    from glassbox_server import app  # noqa: WPS433
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_embed_serves_html():
    async with _client() as c:
        r = await c.get("/signals/embed")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.text.startswith("<!doctype html>")


@pytest.mark.asyncio
async def test_embed_headers_permit_cross_origin_iframe():
    """X-Frame-Options + CSP frame-ancestors must explicitly allow
    embedding from any origin — the whole point of this widget. If
    either header is missing or restrictive, an iframe on a partner
    site renders blank."""
    async with _client() as c:
        r = await c.get("/signals/embed")
    xfo = r.headers.get("x-frame-options", "").upper()
    csp = r.headers.get("content-security-policy", "")
    assert xfo in ("ALLOWALL", "ALLOW-FROM *", ""), (
        "X-Frame-Options too restrictive: " + xfo
    )
    assert "frame-ancestors *" in csp.lower(), (
        "CSP must allow frame-ancestors *"
    )


@pytest.mark.asyncio
async def test_embed_supports_configurable_params():
    """The widget reads max / sev / cat / theme / title from the query
    string. We verify the parsing hooks are present (the live data is
    fetched client-side)."""
    async with _client() as c:
        r = await c.get("/signals/embed?max=5&sev=critical&theme=dark&title=Test")
    body = r.text
    # Each configurable knob must be referenced in the JS
    for token in ("MAX", "SEV_ALLOW", "CAT_ALLOW", "THEME", "TITLE_OVERRIDE"):
        assert token in body, f"missing JS hook for {token}"
    # Calls /api/v1/signals/today
    assert "/api/v1/signals/today" in body


@pytest.mark.asyncio
async def test_embed_links_break_out_of_iframe():
    """Every link in the widget must use target=_top, otherwise a
    click from inside the iframe loads /signals or /entity/{uuid} INSIDE
    the iframe (which is tiny and would be unusable)."""
    async with _client() as c:
        r = await c.get("/signals/embed")
    body = r.text
    # The 'view all' anchor + the per-row anchors both need target=_top.
    assert 'target="_top"' in body
    # And 'noopener' to avoid window.opener leaks
    assert "noopener" in body
