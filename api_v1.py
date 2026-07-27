"""
Glassbox v1 public API — factory shell.

This module is a thin assembler post-P3-H Phase 2 + post-cleanup
(commits `486bcec`..`e5dbd45`). `build_router(prefix, tag)` composes
8 cluster routers from `web/routes/api_v1/*.py` and returns the
resulting `APIRouter`. The factory shape is preserved so
`glassbox_server.py` can dual-mount the same 32 routes at both
`/api/v1/*` (industry-standard REST versioning) and `/api/intel/*`
(Glassbox-branded alias).

Module-level contents that did NOT extract:
  - `_parse_bbox`, `_parse_iso`, `_parse_types` — bbox + time + type
    parsing helpers. `core.py` imports them at module top via a
    deferred-resolution shape that depends on api_v1 being loaded
    first; could be lifted to `web/_parsers.py` in a future cleanup
    pass if we re-need them elsewhere.

The test-only re-export shims that lived here through 2026-05-27 were
all dropped in commit `e5dbd45` (post-Phase-3 audit cleanup); test
consumers now import directly from the extracted module paths
(`web/routes/api_v1/<cluster>.py` or `web/_<helper>.py`).

For the route inventory + extraction history, see
`docs/API_V1_ROUTE_INVENTORY.md`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import APIRouter, HTTPException


# ─── Bbox + types parsing helpers ─────────────────────────────────────────


def _parse_bbox(bbox_str: str) -> Tuple[float, float, float, float]:
    """Parse 'west,south,east,north' into 4 floats. Raises HTTPException on bad input."""
    try:
        parts = [float(x) for x in bbox_str.split(",")]
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="bbox must be 'west,south,east,north' floats")
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must have exactly 4 comma-separated values")
    west, south, east, north = parts
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise HTTPException(status_code=400, detail="longitude must be in [-180, 180]")
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        raise HTTPException(status_code=400, detail="latitude must be in [-90, 90]")
    if south > north:
        raise HTTPException(status_code=400, detail="south must be <= north")
    return west, south, east, north


def _parse_types(types_str: Optional[str]) -> List[str]:
    if not types_str:
        return ["aircraft"]
    out = [t.strip() for t in types_str.split(",") if t.strip()]
    allowed = {"aircraft", "vessel", "satellite", "event", "location", "organization", "person", "infrastructure"}
    bad = [t for t in out if t not in allowed]
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown entity types: {bad}")
    return out


def _parse_iso(name: str, value: Optional[str], default: datetime) -> datetime:
    if not value:
        return default
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be ISO-8601 timestamp")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─── Router ───────────────────────────────────────────────────────────────


def build_router(prefix: str = "/api/v1", tag: str = "v1") -> APIRouter:
    """Construct the API router. Function-form so tests can build it on
    demand AND so we can mount the same 32-route surface under multiple
    prefixes (`/api/v1` for industry-standard REST versioning AND
    `/api/intel` for a Glassbox-branded alias). All Glassbox public
    surfaces resolve to the same handlers.

    Post-P3-H Phase 2 (2026-05-27): every route is now defined in a
    cluster module under `web/routes/api_v1/`. This function does
    nothing but `include_router` them onto a fresh APIRouter at the
    requested prefix. See `docs/API_V1_ROUTE_INVENTORY.md` for the
    inventory + extraction history.
    """
    router = APIRouter(prefix=prefix, tags=[tag])

    # ── Cluster mounts (see docs/API_V1_ROUTE_INVENTORY.md) ──
    from web.routes.api_v1 import alerts as _alerts  # noqa: WPS433
    from web.routes.api_v1 import analytics as _analytics  # noqa: WPS433
    from web.routes.api_v1 import core as _core  # noqa: WPS433
    from web.routes.api_v1 import dashboard as _dashboard  # noqa: WPS433
    from web.routes.api_v1 import health_metrics as _health_metrics  # noqa: WPS433
    from web.routes.api_v1 import lookups as _lookups  # noqa: WPS433
    from web.routes.api_v1 import sanctions as _sanctions  # noqa: WPS433
    from web.routes.api_v1 import signals as _signals  # noqa: WPS433
    router.include_router(_lookups.router)
    router.include_router(_sanctions.router)
    router.include_router(_health_metrics.router)
    router.include_router(_alerts.router)
    router.include_router(_analytics.router)
    router.include_router(_dashboard.router)
    router.include_router(_core.router)
    router.include_router(_signals.router)

    # /signals/today carries a module-level 30s TTL cache. In production
    # build_router is called twice at startup (for /api/v1 + /api/intel,
    # which share the same cache — the dict is keyed by query args, not
    # mount prefix, so the second mount benefits from the first's hits).
    # Tests call build_router per-test against a fresh FastAPI() app; if
    # we don't .clear() here the next test sees stale rows seeded by the
    # previous test. Original implementation made the cache build-local;
    # this preserves that semantic without forcing the route into a
    # nested-in-build_router closure.
    _signals._SIGNALS_TODAY_CACHE.clear()

    return router


