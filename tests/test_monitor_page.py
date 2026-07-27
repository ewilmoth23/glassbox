"""
GET /monitor + /monitor/monitor.js — wiring tests.

Asserts:
  - The page is served as HTML at /monitor.
  - The page references the JS file it depends on (/monitor/monitor.js)
    and the MapLibre CSS/JS CDN.
  - The JS file is served with the correct content-type at the matching
    URL (so the inline <script src> resolves on a fresh load).
  - The JS contains references to the backend endpoints it consumes —
    drift catcher: if someone renames /api/v1/signals/today, this test
    catches the broken page link.
  - The landing page links to /monitor (otherwise nobody finds it).

Substantive map rendering is browser-only and outside the unit-test
surface — these tests cover wiring + contract only.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_monitor_page.py -v
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
async def test_monitor_page_serves_html():
    async with _client() as c:
        r = await c.get("/monitor")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert body.startswith("<!doctype html>")
    # Page must load MapLibre + the sibling JS file.
    assert "maplibre-gl@" in body
    assert "/monitor/monitor.js" in body


@pytest.mark.asyncio
async def test_monitor_js_served_with_correct_mime():
    async with _client() as c:
        r = await c.get("/monitor/monitor.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    js = r.text
    # Drift catcher: the page consumes these endpoints.
    assert "/api/v1/signals/today" in js
    assert "/api/v1/viewport" in js
    # And the layer registry exists
    assert "LAYERS" in js
    assert "renderLayerList" in js


@pytest.mark.asyncio
async def test_landing_links_to_monitor():
    """If /monitor isn't on the landing nav, no one will find it.
    Catches drift between the new page and its discovery surface."""
    async with _client() as c:
        r = await c.get("/")
    assert "/monitor" in r.text
