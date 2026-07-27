"""
/signals page — live-SSE wiring contract test.

Doesn't drive a browser; checks that the page actually subscribes to
the SSE stream we said it does, and that the type→category map covers
every category the backend can produce. If a future change breaks
either side of that contract, this test catches it.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_signals_page_live_wiring.py -v
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web._signals_categories import SIGNALS_CATEGORY_ORDER as _SIGNALS_CATEGORY_ORDER  # noqa: E402


def _client():
    from glassbox_server import app  # noqa: WPS433
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_signals_page_subscribes_to_alerts_stream():
    async with _client() as c:
        r = await c.get("/signals")
    assert r.status_code == 200
    body = r.text
    # The page MUST open an EventSource against the existing
    # alerts-stream endpoint (NOT a custom signals-only stream).
    assert "/api/v1/alerts/stream" in body
    assert "EventSource" in body
    # Live indicator hooks
    assert 'id="live-pill"' in body
    assert "fresh-row" not in body  # we use class "fresh", not id


@pytest.mark.asyncio
async def test_signals_page_includes_search_bar():
    """Free-text search input + JS filter functions are wired."""
    async with _client() as c:
        r = await c.get("/signals")
    body = r.text
    assert 'id="search"' in body
    assert 'id="search-hits"' in body
    assert 'id="search-clear"' in body
    assert "applySearch" in body
    assert "wireSearch" in body
    # URL hash persistence (#q=...) so a search query can be shared.
    assert "q=" in body


@pytest.mark.asyncio
async def test_signals_page_includes_filter_chips():
    """The page renders severity + category filter chips and the
    URL-hash persistence wiring (so a shared link reproduces the
    view). We don't assert chip counts (those depend on live data);
    we just assert the structure is present."""
    async with _client() as c:
        r = await c.get("/signals")
    body = r.text
    assert 'id="filters"' in body
    assert "renderFilters" in body
    assert "applyFilters" in body
    assert "wireFilterChips" in body
    # URL-hash persistence so links are shareable
    assert "readFiltersFromHash" in body
    assert "writeFiltersToHash" in body
    # Each severity bucket must produce a chip
    for sev in ("critical", "high", "medium", "low"):
        assert f'data-value="{sev}"' in body or sev in body  # JS literal


@pytest.mark.asyncio
async def test_signals_page_includes_leaflet_map():
    """The page renders a Leaflet map of finding locations. We don't
    test rendering — just contract: Leaflet is loaded, the map div
    exists, a tile provider is wired (CARTO dark-matter since
    2026-05-10 — OSM before), and pins use class names that match
    the severity buckets."""
    async with _client() as c:
        r = await c.get("/signals")
    body = r.text
    assert "leaflet@1.9.4/dist/leaflet.css" in body
    assert "leaflet@1.9.4/dist/leaflet.js" in body
    assert 'id="map"' in body
    # Tile source — CARTO dark-matter base tiles since 2026-05-10.
    # (The OSM attribution still appears in the attribution string,
    # but the actual tile URL is basemaps.cartocdn.com.)
    assert "basemaps.cartocdn.com" in body
    # Pin classes for each severity bucket — drift between CSS class
    # names and JS pinIconFor() will silently break colored pins.
    for sev in ("critical", "high", "medium", "low"):
        assert f".pin.{sev}" in body, f"missing CSS for .pin.{sev}"


@pytest.mark.asyncio
async def test_signals_page_type_to_cat_map_covers_every_backend_category():
    """If a category is added on the backend (api_v1._SIGNALS_CATEGORY_ORDER)
    without a matching entry in the page's TYPE_TO_CAT map, SSE pushes
    will silently drop. Catch that drift here."""
    async with _client() as c:
        r = await c.get("/signals")
    body = r.text
    # Pull out the JS object literal
    m = re.search(
        r"const TYPE_TO_CAT\s*=\s*\{([^}]*)\}",
        body, re.DOTALL,
    )
    assert m, "TYPE_TO_CAT object not found in /signals page"
    js_block = m.group(1)
    for cat in _SIGNALS_CATEGORY_ORDER:
        et = cat["event_type"]
        cid = cat["id"]
        # Match e.g.  sanctioned_vessel_went_dark: "sanctioned_dark"
        pat = rf"{re.escape(et)}\s*:\s*\"{re.escape(cid)}\""
        assert re.search(pat, js_block), (
            f"backend category {et!r} → {cid!r} is not in the page's "
            f"TYPE_TO_CAT map; SSE pushes for this type will be dropped"
        )
