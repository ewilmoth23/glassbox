"""
Route-coverage smoke test — P3-H safety net for the
`glassbox_server.py` god-file refactor.

The point of this file: catch the "I silently dropped a route during the
refactor" failure mode. NOT semantic correctness — that's what each
per-page / per-endpoint test covers (test_network_page.py,
test_cross_domain_endpoint.py, etc.).

Two assertions:

1. `test_all_routes_respond_without_500` — parametrized over every route
   currently registered on `app.routes`. For each, hit the URL with
   safe-default inputs and assert `response.status_code < 500`. If an
   extraction breaks a route's internal wiring (missed import, wrong
   dependency, registered under wrong path), this catches it.

2. `test_route_manifest_is_subset_of_registered` — hardcoded list of
   (method, path_template) tuples from the pre-refactor inventory at
   `21_GLASSBOX_AI/docs/GLASSBOX_SERVER_ROUTE_INVENTORY.md`. The check
   asserts this manifest is a SUBSET of the live `app.routes`. If a
   refactor commit silently drops a route (forgets `include_router`,
   typos the prefix, comments out a handler), the manifest test fails
   with the exact missing routes in the error message.

The manifest is updated by EXPLICIT code edit, not by re-reading the
running app — that's the point. An extraction is allowed to ADD routes,
not silently REMOVE them.

After the P3-H extractions land, this file SHOULD STILL PASS unchanged.
If it doesn't, revert the offending extraction commit.

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_routes_smoke.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Set, Tuple

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Manifest: every route the pre-refactor glassbox_server.py registers.
#
# Source of truth: docs/GLASSBOX_SERVER_ROUTE_INVENTORY.md (2026-05-21).
# When a route is intentionally added or removed, update BOTH this list
# AND the inventory doc — the test that the count goes up by N catches
# accidental drops; explicit edits document intentional ones.
# ---------------------------------------------------------------------------

_EXPECTED_ROUTES: Set[Tuple[str, str]] = {
    # /api/v1/* (the few that haven't moved to api_v1.py)
    ("GET", "/api/v1/brief/latest"),
    ("GET", "/api/v1/recent-cycle"),
    ("GET", "/api/v1/cesium-token"),
    ("GET", "/api/v1/satellites/tle"),
    ("GET", "/api/v1/infrastructure/military-bases"),
    ("GET", "/api/v1/infrastructure/nuclear"),
    ("GET", "/api/v1/infrastructure/cables"),
    ("GET", "/api/v1/infrastructure/trafficking"),
    ("GET", "/api/v1/infrastructure/pipelines"),
    # P2-A Phase 1 MVP (cyber-attack data layers, 2026-05-27)
    ("GET", "/api/v1/infrastructure/cyber-kev"),
    ("GET", "/api/v1/infrastructure/cyber-spamhaus-drop"),
    # P2-B Phase 1 (unported v2 layers, 2026-05-27 NIGHT)
    ("GET", "/api/v1/infrastructure/conflict-zones"),
    ("GET", "/api/v1/infrastructure/diplomatic-posts"),
    ("GET", "/api/v1/infrastructure/un-missions"),
    ("GET", "/api/v1/infrastructure/disputed-zones"),
    ("GET", "/api/v1/infrastructure/state-media"),
    ("GET", "/api/v1/infrastructure/sanction-targets"),
    ("GET", "/api/v1/infrastructure/noaa-buoys"),
    ("GET", "/api/v1/infrastructure/climate-forecast"),
    # /api/sources + /api/markets
    ("GET", "/api/sources"),
    ("POST", "/api/markets/edges/email-capture"),
    # Static / favicons / feeds
    ("GET", "/favicon.svg"),
    ("GET", "/favicon.ico"),
    ("GET", "/atlas.js"),
    ("GET", "/command.js"),
    ("GET", "/track.js"),
    ("GET", "/satellite.min.js"),
    ("GET", "/satellites_worker.js"),
    ("GET", "/og-image.png"),
    ("GET", "/robots.txt"),
    ("GET", "/sitemap.xml"),
    # Page handlers
    ("GET", "/"),
    ("GET", "/web"),
    ("GET", "/glassbox"),
    ("GET", "/markets"),
    ("GET", "/pro"),
    ("GET", "/console"),
    ("GET", "/demo"),
    ("GET", "/pricing"),
    ("GET", "/status"),
    ("GET", "/admin/analytics"),
    ("GET", "/entity/{entity_id}"),
    # /globe + /monitor + /network groups
    ("GET", "/globe"),
    ("GET", "/globe/globe.js"),
    ("GET", "/monitor"),
    ("GET", "/monitor/monitor.js"),
    ("GET", "/monitor/countries.geojson"),
    ("GET", "/network"),
    ("GET", "/network/network.js"),
    # /signals group
    ("GET", "/signals"),
    ("GET", "/signals/embed"),
    ("GET", "/signals.rss"),
    ("GET", "/signals.json"),
    # Health
    ("GET", "/api/health"),
    ("GET", "/health"),
    # /api/glassbox/* (21)
    ("GET", "/api/glassbox/diagnostic"),
    ("GET", "/api/glassbox/layers"),
    ("GET", "/api/glassbox/layer/{name}"),
    ("GET", "/api/glassbox/entities"),
    ("POST", "/api/glassbox/sitrep/publish"),
    ("GET", "/api/glassbox/sitrep/latest"),
    ("GET", "/api/glassbox/state"),
    ("GET", "/api/glassbox/anomalies/latest"),
    ("GET", "/api/glassbox/correlations/latest"),
    ("POST", "/api/glassbox/watchlist"),
    ("GET", "/api/glassbox/watchlist"),
    ("GET", "/api/glassbox/watchlist/{wl_id}"),
    ("DELETE", "/api/glassbox/watchlist/{wl_id}"),
    ("POST", "/api/glassbox/ask"),
    ("GET", "/api/glassbox/forecast/latest"),
    ("GET", "/api/glassbox/pro-status"),
    ("POST", "/api/glassbox/pro/activate"),
    ("POST", "/api/glassbox/pro/cancel"),
    ("GET", "/api/glassbox/news-manifest"),
    ("GET", "/api/glassbox/history/{layer}"),
    ("GET", "/api/glassbox/stream"),
    # /api/intel/* (10)
    ("GET", "/api/intel/latest"),
    ("GET", "/api/intel/anomalies"),
    ("GET", "/api/intel/predictions"),
    ("GET", "/api/intel/threat-briefing"),
    ("GET", "/api/intel/alerts"),
    ("GET", "/api/intel/alerts/poll"),
    ("GET", "/api/intel/confidence"),
    ("GET", "/api/intel/accuracy"),
    ("GET", "/api/intel/type/{intel_type}"),
    ("POST", "/api/intel/query"),
    # /api/briefings/* + /api/issues/*
    ("POST", "/api/issues/report"),
    ("GET", "/api/issues/open"),
    ("GET", "/api/briefings/latest"),
    ("GET", "/api/briefings/{slug}"),
    ("GET", "/api/briefings/track-record/summary"),
}


# Routes we deliberately do NOT call in the live smoke (they hang or
# require real external services). The MANIFEST check still asserts
# they're registered; only the live response check skips them.
_SKIP_LIVE_CALL = {
    ("GET", "/api/glassbox/stream"),       # SSE — hangs indefinitely
    ("POST", "/api/glassbox/sitrep/publish"),  # writes; keep daemon clean
}


def _safe_url(path_template: str) -> str:
    """Substitute path params with values that are syntactically valid
    so FastAPI's routing accepts them. Whether the handler returns 200
    or 404 doesn't matter — we only assert < 500."""
    test_uuid = "00000000-0000-0000-0000-000000000001"
    return (
        path_template
        .replace("{entity_id}", test_uuid)
        .replace("{wl_id}", test_uuid)
        .replace("{name}", "test")
        .replace("{intel_type}", "test")
        .replace("{slug}", "test-slug")
        .replace("{layer}", "vessel_positions")
    )


def _get_app():
    """Lazy import so pytest collection doesn't pay the cost of loading
    glassbox_server (which boots ingester modules at import time)."""
    from glassbox_server import app  # noqa: WPS433
    return app


def _live_routes() -> Set[Tuple[str, str]]:
    """Return {(METHOD, path_template), ...} for every APIRoute on the
    real FastAPI app. FastAPI's own routes (/openapi.json, /docs, etc.)
    are excluded — we test ours only."""
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
# Test 1 — manifest subset check (load-bearing safety net for the refactor)
# ---------------------------------------------------------------------------

def test_route_manifest_is_subset_of_registered():
    """If a refactor silently drops a route from the app, this fails
    with the exact missing routes in the error message."""
    live = _live_routes()
    missing = _EXPECTED_ROUTES - live
    assert not missing, (
        f"{len(missing)} route(s) expected by the manifest are NOT "
        f"registered on the live app — a refactor probably dropped them:\n"
        + "\n".join(f"  {m} {p}" for m, p in sorted(missing))
    )


def test_route_count_floor():
    """Cheap sanity check that the app didn't lose half its routes.
    The manifest covers 81 known routes; allow for 2-3 framework /
    test-only deviations and require >= 78."""
    live = _live_routes()
    assert len(live) >= 78, (
        f"Only {len(live)} app routes registered — expected >= 78. "
        f"Did glassbox_server.py fail to import a router?"
    )


# ---------------------------------------------------------------------------
# Test 2 — parametrized live-call smoke (catches 500s introduced by
# half-finished extraction)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path_template",
    sorted(_EXPECTED_ROUTES - _SKIP_LIVE_CALL),
    ids=lambda v: v if isinstance(v, str) else "",
)
@pytest.mark.asyncio
async def test_route_responds_without_500(method: str, path_template: str):
    """Every registered route must respond with status < 500 when
    called with safe-default inputs. The handler is allowed to return
    200, 400, 404, 422, etc. — we just don't tolerate a 500."""
    url = _safe_url(path_template)
    app = _get_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        if method == "GET":
            r = await c.get(url, timeout=10.0)
        elif method == "POST":
            r = await c.post(url, json={}, timeout=10.0)
        elif method == "PUT":
            r = await c.put(url, json={}, timeout=10.0)
        elif method == "DELETE":
            r = await c.delete(url, timeout=10.0)
        else:
            pytest.skip(f"unsupported method {method} in smoke harness")

    assert r.status_code < 500, (
        f"{method} {url} returned {r.status_code} — body[:300]: "
        f"{r.text[:300]!r}"
    )
