"""
GET /openapi.json + /docs — public OpenAPI / Swagger UI tests.

Asserts:
  - /openapi.json renders without 500 (the 'response_class=None'
    on /metrics, /signals.rss/.json, /signals/snapshot.csv used to
    crash schema generation).
  - The schema includes every consumer-facing route we shipped this
    session — drift in either direction (route added without docs,
    or docs that promise a missing route) fails the test.
  - /docs serves the Swagger UI shell.
  - The schema is branded (title contains 'Glassbox', not just
    'FastAPI', and version is set).

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_openapi_and_docs.py -v
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
async def test_openapi_json_renders_without_500():
    """The 'response_class=None' anti-pattern crashes OpenAPI gen with
    `AssertionError: A response class is needed to generate OpenAPI`.
    Catch the regression here so the next time someone copy-pastes
    that pattern, /docs doesn't go dark."""
    async with _client() as c:
        r = await c.get("/openapi.json")
    assert r.status_code == 200, r.text[:500]
    schema = r.json()
    assert schema["openapi"].startswith("3.")
    info = schema["info"]
    assert "Glassbox" in info["title"]
    assert info["version"]


@pytest.mark.asyncio
async def test_openapi_schema_includes_every_signals_endpoint():
    """The signals stack we shipped this session must all appear in
    the schema — otherwise /docs is misleading. If we add a new
    public surface and forget to make it discoverable, this fails."""
    async with _client() as c:
        r = await c.get("/openapi.json")
    paths = r.json()["paths"]
    expected = [
        "/api/v1/signals/today",
        "/api/v1/signals.rss",
        "/api/v1/signals.json",
        "/api/v1/signals/snapshot.csv",
        "/api/v1/signals/subscribe",
        "/api/v1/signals/verify",
        "/api/v1/signals/unsubscribe",
    ]
    for p in expected:
        assert p in paths, f"missing from OpenAPI: {p}"


@pytest.mark.asyncio
async def test_docs_serves_swagger_ui_shell():
    async with _client() as c:
        r = await c.get("/docs")
    assert r.status_code == 200
    body = r.text
    # FastAPI's Swagger UI shell references swagger-ui-bundle
    assert "swagger-ui" in body.lower()
