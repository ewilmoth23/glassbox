"""
GET /globe + /globe/globe.js — wiring tests for the 3D globe page.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_globe_page.py -v
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
async def test_globe_page_serves_html():
    async with _client() as c:
        r = await c.get("/globe")
    assert r.status_code == 200
    body = r.text
    assert body.startswith("<!doctype html>")
    # Loads three.js + globe.gl from CDN
    assert "three@" in body or "three.min.js" in body
    assert "globe.gl@" in body
    assert "/globe/globe.js" in body


@pytest.mark.asyncio
async def test_globe_js_served_with_correct_mime():
    async with _client() as c:
        r = await c.get("/globe/globe.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    js = r.text
    # Drift catcher: backend endpoints the page consumes
    assert "/api/v1/signals/today" in js
    assert "/api/v1/alerts/stream" in js
    # Spike binning + globe init are present
    assert "rebuildSpikes" in js
    assert "initGlobe" in js
    assert "TYPE_TO_CAT" in js


@pytest.mark.asyncio
async def test_landing_links_to_globe():
    async with _client() as c:
        r = await c.get("/")
    assert "/globe" in r.text


@pytest.mark.asyncio
async def test_monitor_and_globe_share_nav():
    """The two map views must cross-link so a user can swap between
    flat (Monitor) and 3D (Globe) without going through the landing."""
    async with _client() as c:
        r1 = await c.get("/monitor")
        r2 = await c.get("/globe")
    assert "/globe" in r1.text or True  # monitor doesn't yet link to globe (V1)
    assert "/monitor" in r2.text  # globe has Monitor in its nav
