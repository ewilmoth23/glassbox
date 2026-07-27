"""
api_v1 route-coverage smoke test — P3-H Phase 2 safety net for the
`api_v1.py` factory-function refactor.

The point of this file: catch the "I silently dropped a route during the
extraction" failure mode. NOT semantic correctness — that's covered by
each per-endpoint test (test_viewport_endpoint.py, test_cross_domain_endpoint.py,
test_signals_today_endpoint.py, etc.).

Three assertions:

1. `test_api_v1_manifest_is_subset_of_registered_v1` — hardcoded list of
   32 (method, "/api/v1/<path>") tuples covering every route that
   `build_router()` produces. Asserts each is registered on the live app.
   If a refactor silently drops a route (forgets `include_router`,
   typos the path, comments out a handler), this fails with the exact
   missing routes in the error message.

2. `test_api_v1_manifest_dual_mounted_at_intel` — the same 32 routes
   must also appear under `/api/intel/*` because `glassbox_server.py`
   calls `build_router(prefix="/api/intel", tag="intel")`. If the
   refactor breaks the factory's prefix-parametrization, this fails.

3. `test_api_v1_route_responds_without_500` — parametrized over each
   `/api/v1/<path>` from the manifest. Hits the URL with safe-default
   inputs and asserts `response.status_code < 500`. Catches partial-
   extraction wiring bugs.

This file is the load-bearing safety net for Phase 2 extractions. After
each extraction commit, this test MUST pass unchanged. If it doesn't,
revert.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_api_v1_routes_smoke.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Set, Tuple

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool  # noqa: E402


@pytest.fixture(autouse=True)
async def _pool():
    """Initialize asyncpg pools per test. Function-scope (not module-scope)
    because pytest-asyncio runs each test in a fresh event loop, and an
    asyncpg pool bound to a closed loop raises "attached to a different
    loop" when reused. Cost: ~100ms per test × 32 ≈ 3s of extra setup,
    acceptable for a once-per-CI smoke suite."""
    await init_pool()
    yield
    await close_pool()


# ---------------------------------------------------------------------------
# Manifest: every route that build_router() registers, mounted at /api/v1.
#
# Source of truth: docs/API_V1_ROUTE_INVENTORY.md (2026-05-27).
# Cluster column maps to the target extraction file under
# web/routes/api_v1/<cluster>.py. When a route is intentionally added or
# removed, update BOTH this list AND the inventory doc.
# ---------------------------------------------------------------------------

# Bare paths (relative to the mount prefix). The factory mounts these at
# BOTH /api/v1/* AND /api/intel/*; the two prefixed sets are derived below.
_BARE_ROUTES: Set[Tuple[str, str]] = {
    # ── core entity lookup (cluster 7) ──
    ("GET", "/viewport"),
    ("GET", "/entity/{entity_id}"),
    ("GET", "/vessel/{mmsi}"),
    ("GET", "/aircraft/{icao24}"),
    ("GET", "/entities/{entity_id}/aliases"),
    ("GET", "/events/similar"),
    ("GET", "/entities/{entity_id}/cross_domain"),
    ("GET", "/event/{event_id}"),
    # ── health + metrics (cluster 5) ──
    ("GET", "/health/db"),
    ("GET", "/metrics"),
    ("GET", "/metrics/prefilter"),
    ("GET", "/health/full"),
    ("GET", "/system-state"),
    # ── sanctions (cluster 4) ──
    ("GET", "/sanctions/breakdown"),
    ("GET", "/sanctions/search"),
    ("GET", "/sanctions/by-regime"),
    # ── alerts (cluster 6) ──
    ("GET", "/alerts/timeseries"),
    ("GET", "/alerts/stream"),
    # ── external lookups (cluster 1) ──
    ("GET", "/lookup/subdomains"),
    ("GET", "/lookup/wayback"),
    ("GET", "/lookup/asn"),
    # ── signals + feeds (cluster 8) ──
    ("GET", "/signals/today"),
    ("POST", "/signals/subscribe"),
    ("GET", "/signals/verify"),
    ("GET", "/signals/unsubscribe"),
    ("GET", "/signals/timeline"),
    ("GET", "/signals/snapshot.csv"),
    ("GET", "/signals.json"),
    ("GET", "/signals.rss"),
    # ── analytics (cluster 2) ──
    ("POST", "/analytics/event"),
    ("GET", "/analytics/summary"),
    # ── dashboard (cluster 3) ──
    ("GET", "/dashboard/summary"),
}

_EXPECTED_V1_ROUTES: Set[Tuple[str, str]] = {
    (m, f"/api/v1{path}") for m, path in _BARE_ROUTES
}
_EXPECTED_INTEL_ROUTES: Set[Tuple[str, str]] = {
    (m, f"/api/intel{path}") for m, path in _BARE_ROUTES
}


# Routes we deliberately do NOT call in the live smoke (they hang, write
# real DB rows we'd rather not pollute, or trigger external systems).
# The MANIFEST check still asserts they're registered; only the live
# response check skips them.
_SKIP_LIVE_CALL: Set[Tuple[str, str]] = {
    ("GET", "/api/v1/alerts/stream"),     # SSE — hangs indefinitely
    ("POST", "/api/v1/signals/subscribe"),  # writes a real subscriber + may email
    ("POST", "/api/v1/analytics/event"),    # writes a row; fine but noisy in dev DB
    # /sanctions/search lazy-loads sentence-transformers on first hit
    # (~3-4s cold; acceptable inside the 10s per-route timeout but
    # avoid in CI where the model may not be cached). Keep for now —
    # remove from skip list once daemons cache it.
}


def _safe_url(path_template: str) -> str:
    """Substitute path params with syntactically-valid values. The handler
    is allowed to return 404/422/400 — we just check < 500."""
    test_uuid = "00000000-0000-0000-0000-000000000001"
    return (
        path_template
        .replace("{entity_id}", test_uuid)
        .replace("{event_id}", test_uuid)
        .replace("{mmsi}", "999999999")
        .replace("{icao24}", "abcdef")
    )


def _get_app():
    """Lazy import so pytest collection doesn't pay the cost of loading
    glassbox_server (which boots ingester modules at import time)."""
    from glassbox_server import app  # noqa: WPS433
    return app


def _live_routes() -> Set[Tuple[str, str]]:
    """Return {(METHOD, path_template), ...} for every APIRoute on the
    real FastAPI app. FastAPI's own routes (/openapi.json, /docs, etc.)
    are excluded."""
    from fastapi.routing import APIRoute
    framework_paths = {
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
    app = _get_app()
    out: Set[Tuple[str, str]] = set()
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        if r.path in framework_paths:
            continue
        for method in r.methods or set():
            if method == "HEAD":  # FastAPI auto-adds HEAD for GETs
                continue
            out.add((method, r.path))
    return out


# ---------------------------------------------------------------------------
# Test 1 — /api/v1/* manifest subset check
# ---------------------------------------------------------------------------

def test_api_v1_manifest_is_subset_of_registered_v1():
    """The 32 expected /api/v1/* routes must all be registered on the
    live app. Any silently-dropped route shows up in the error message."""
    live = _live_routes()
    missing = _EXPECTED_V1_ROUTES - live
    assert not missing, (
        f"{len(missing)} /api/v1/* route(s) expected by the manifest are "
        f"NOT registered on the live app — a P3-H Phase 2 extraction "
        f"probably dropped them:\n"
        + "\n".join(f"  {m} {p}" for m, p in sorted(missing))
    )


# ---------------------------------------------------------------------------
# Test 2 — /api/intel/* dual-mount check (factory contract)
# ---------------------------------------------------------------------------

def test_api_v1_manifest_dual_mounted_at_intel():
    """The factory is called twice (prefix='/api/v1' AND prefix='/api/intel');
    every route must be reachable at BOTH prefixes. If a refactor breaks
    the include_router prefix plumbing, this fails."""
    live = _live_routes()
    missing = _EXPECTED_INTEL_ROUTES - live
    assert not missing, (
        f"{len(missing)} /api/intel/* route(s) (factory dual-mount) are "
        f"NOT registered on the live app — build_router's prefix= arg "
        f"is probably broken:\n"
        + "\n".join(f"  {m} {p}" for m, p in sorted(missing))
    )


# ---------------------------------------------------------------------------
# Test 3 — count floor sanity check
# ---------------------------------------------------------------------------

def test_api_v1_route_count_floor():
    """The factory should produce 32 routes per mount × 2 mounts = 64
    api_v1-managed routes. Allow a small floor of 62 for in-flight
    extractions that might temporarily de-mount one route mid-commit."""
    live = _live_routes()
    api_v1_count = sum(1 for _m, p in live if p.startswith("/api/v1/"))
    api_intel_count = sum(1 for _m, p in live if p.startswith("/api/intel/"))
    # api_v1 has only api_v1 routes; api_intel has api_v1's 32 + intel.py's 10
    assert api_v1_count >= 32, (
        f"Only {api_v1_count} /api/v1/* routes registered — expected "
        f">= 32. Did api_v1.build_router fail to register a route?"
    )
    assert api_intel_count >= 42, (
        f"Only {api_intel_count} /api/intel/* routes registered — "
        f"expected >= 42 (32 from api_v1 dual-mount + 10 from "
        f"web/routes/intel.py). Did the dual-mount break?"
    )


# ---------------------------------------------------------------------------
# Test 4 — parametrized live-call smoke (catches partial wiring bugs)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path_template",
    sorted(_EXPECTED_V1_ROUTES - _SKIP_LIVE_CALL),
    ids=lambda v: v if isinstance(v, str) else "",
)
@pytest.mark.asyncio
async def test_api_v1_route_responds_without_500(method: str, path_template: str):
    """Every /api/v1/* route must respond with status < 500 when called
    with safe-default inputs. 200/400/404/422 all fine — only 500 fails."""
    url = _safe_url(path_template)
    app = _get_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        if method == "GET":
            r = await c.get(url, timeout=15.0)
        elif method == "POST":
            r = await c.post(url, json={}, timeout=15.0)
        elif method == "PUT":
            r = await c.put(url, json={}, timeout=15.0)
        elif method == "DELETE":
            r = await c.delete(url, timeout=15.0)
        else:
            pytest.skip(f"unsupported method {method} in smoke harness")

    assert r.status_code < 500, (
        f"{method} {url} returned {r.status_code} — body[:300]: "
        f"{r.text[:300]!r}"
    )
