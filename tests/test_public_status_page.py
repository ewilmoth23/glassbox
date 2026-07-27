"""
GET /status — public-facing status page wiring test.

We don't unit-test JS rendering; we just verify the route is wired,
the file is shipped, and the contract it depends on (link to
/api/v1/health/full + auto-discovery hooks the page promises) is
present in the HTML body. Substantive health logic is covered by
test_health_full.py against the underlying endpoint.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_public_status_page.py -v
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
    # Import inside the function so the module's top-level imports
    # (which spin up ingesters / pull config) don't fire at collection.
    from glassbox_server import app  # noqa: WPS433
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_status_page_serves_html_with_health_full_dependency():
    async with _client() as c:
        r = await c.get("/status")
    assert r.status_code == 200
    body = r.text
    assert body.startswith("<!doctype html>")
    # The page polls this endpoint — keep them coupled by contract test.
    assert "/api/v1/health/full" in body
    # Footer convenience links — if these change, this test catches it.
    assert "/signals" in body
    assert "/signals.rss" in body
    assert "/api/sources" in body
    # Auto-refresh advertised in the header
    assert "refresh in" in body.lower()


@pytest.mark.asyncio
async def test_signals_page_links_to_status():
    """Cross-link contract: the signals footer must include /status so a
    user landing there can find operational state without guessing the
    URL."""
    async with _client() as c:
        r = await c.get("/signals")
    assert r.status_code == 200
    assert "/status" in r.text
