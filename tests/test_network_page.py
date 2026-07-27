"""
GET /network + /network/network.js — wiring tests.

Asserts:
  - The page is served as HTML at /network.
  - The page references the JS file it depends on (/network/network.js)
    and the vis-network CDN (CSS + JS).
  - The JS file is served with the correct content-type at the
    matching URL (so the inline <script src> resolves on a fresh load).
  - The JS contains references to the backend endpoints it consumes —
    drift catcher: if someone renames /api/v1/entities/{id}/cross_domain
    or /api/v1/signals/today, this test catches the broken page link.
  - The landing page links to /network (otherwise nobody finds it).

Substantive graph rendering is browser-only and outside the unit-
test surface — these tests cover wiring + contract only. Mirrors the
same structure as test_monitor_page.py.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_network_page.py -v
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
async def test_network_page_serves_html():
    async with _client() as c:
        r = await c.get("/network")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert body.startswith("<!doctype html>")
    # Page must load vis-network (CSS + JS) + the sibling JS file.
    assert "vis-network@9" in body
    assert "/network/network.js" in body
    # And nav links must include Network as the active item alongside
    # the rest of the surface set so this page is discoverable from
    # any other console page.
    assert ">Monitor<" in body
    assert ">Globe<" in body
    assert ">Network<" in body
    assert ">Signals<" in body
    assert ">Status<" in body
    assert ">API<" in body


@pytest.mark.asyncio
async def test_network_js_served_with_correct_mime():
    async with _client() as c:
        r = await c.get("/network/network.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    js = r.text
    # Drift catcher: the page consumes these endpoints. If any of them
    # gets renamed in api_v1.py, this test fails before the page does.
    assert "/api/v1/signals/today" in js
    assert "/api/v1/entities/" in js          # cross_domain fan-out
    assert "/api/v1/entity/" in js            # hydration pass
    assert "/api/v1/alerts/stream" in js      # SSE live patches
    # Layer registries + boot symbols
    assert "NODE_LAYERS" in js
    assert "EDGE_LAYERS" in js
    assert "renderLayerLists" in js
    assert "initNetwork" in js


@pytest.mark.asyncio
async def test_landing_links_to_network():
    """If /network isn't on the landing nav, no one will find it.
    Catches drift between the new page and its discovery surface."""
    async with _client() as c:
        r = await c.get("/")
    assert "/network" in r.text
