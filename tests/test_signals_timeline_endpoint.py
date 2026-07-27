"""
/api/v1/signals/timeline — time-bucketed counts endpoint test.

Asserts the endpoint:
  - Returns 200 with the documented shape (generated_at, window_hours,
    bucket_min, bucket_count, buckets).
  - Each bucket has ts, total, by_category dict, by_severity dict.
  - Sum of by_severity counts within a bucket equals total.
  - Sum of by_category counts within a bucket equals total.
  - window_hours and bucket_min query validation works (422 on bad).

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_signals_timeline_endpoint.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool  # noqa: E402
from api_v1 import build_router  # noqa: E402


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


def _client():
    app = FastAPI()
    app.include_router(build_router())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_timeline_returns_documented_shape():
    async with _client() as c:
        r = await c.get("/api/v1/signals/timeline?window_hours=24&bucket_min=60")
    assert r.status_code == 200
    body = r.json()
    for key in ("generated_at", "window_hours", "bucket_min", "bucket_count", "buckets"):
        assert key in body, f"missing {key}"
    assert body["window_hours"] == 24
    assert body["bucket_min"] == 60
    assert isinstance(body["buckets"], list)
    assert body["bucket_count"] == len(body["buckets"])


@pytest.mark.asyncio
async def test_timeline_bucket_internal_consistency():
    """Within each bucket, total must equal the sum of by_severity AND
    the sum of by_category. Catches a regression in the projection."""
    async with _client() as c:
        r = await c.get("/api/v1/signals/timeline?window_hours=24&bucket_min=60")
    body = r.json()
    if not body["buckets"]:
        pytest.skip("no events in last 24h to validate against")
    for b in body["buckets"]:
        assert "ts" in b and "total" in b
        assert isinstance(b["by_category"], dict)
        assert isinstance(b["by_severity"], dict)
        assert sum(b["by_severity"].values()) == b["total"], (
            f"by_severity sum {sum(b['by_severity'].values())} != total {b['total']} at {b['ts']}"
        )
        assert sum(b["by_category"].values()) == b["total"], (
            f"by_category sum {sum(b['by_category'].values())} != total {b['total']} at {b['ts']}"
        )


@pytest.mark.asyncio
async def test_timeline_param_validation():
    async with _client() as c:
        r1 = await c.get("/api/v1/signals/timeline?window_hours=0")
        r2 = await c.get("/api/v1/signals/timeline?window_hours=10000")
        r3 = await c.get("/api/v1/signals/timeline?bucket_min=2")
        r4 = await c.get("/api/v1/signals/timeline?bucket_min=99999")
    for r in (r1, r2, r3, r4):
        assert r.status_code == 422
