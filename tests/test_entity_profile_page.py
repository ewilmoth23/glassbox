"""
GET /entity/{uuid} — public-facing entity profile page wiring tests.

Doesn't drive a browser; verifies the route is wired, the page is
shipped, and the JS makes the three backend calls the page promises.
Substantive entity-detail logic is covered by the existing
test_entity_detail.py against the underlying endpoint.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_entity_profile_page.py -v
"""

from __future__ import annotations

import sys
import uuid
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
async def test_entity_page_serves_html_for_any_uuid_path():
    """Page is served as static HTML for any UUID; the actual entity
    fetch happens client-side. The page is identical for every UUID
    (the JS reads window.location.pathname)."""
    eid = uuid.uuid4()
    async with _client() as c:
        r = await c.get(f"/entity/{eid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert body.startswith("<!doctype html>")
    # The page must call all three relevant endpoints — these are
    # the contract the page depends on.
    assert "/api/v1/entity/" in body
    assert "/cross_domain" in body
    assert "/aliases" in body
    # And it must extract the UUID from the URL path, not a query
    # parameter — links from /signals/today come in as /entity/<uuid>.
    assert "entityIdFromUrl" in body
    assert "/entity/" in body


@pytest.mark.asyncio
async def test_entity_page_links_back_to_signals():
    """Cross-link contract — entity profile must let the user navigate
    back to /signals (the most common origin for entity-page visits)."""
    eid = uuid.uuid4()
    async with _client() as c:
        r = await c.get(f"/entity/{eid}")
    body = r.text
    # Crumbs link + footer link
    assert 'href="/signals"' in body
    assert 'href="/status"' in body
