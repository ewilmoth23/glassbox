"""
/api/v1/signals/subscribe + /verify + /unsubscribe endpoint tests.

Asserts:
  - POST /signals/subscribe accepts both JSON and form-encoded bodies.
  - Idempotent on email — repeat subscribe = update, not error.
  - Severity / category_ids validation rejects bad inputs with 400.
  - Email validation rejects obvious garbage.
  - GET /signals/verify?t=... flips verified true on first call,
    returns 'already_verified' on second.
  - GET /signals/unsubscribe?t=... soft-deletes; idempotent.
  - Tokens are NOT echoed back in responses (only the operator can
    issue them via DB inspection in v1.0).

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest \\
        21_GLASSBOX_AI/tests/test_signals_subscribe_endpoint.py -v
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

from db import init_pool, close_pool, execute, fetchval  # noqa: E402
from api_v1 import build_router  # noqa: E402
from web._rate_limit import _REQUEST_BUCKETS as _RATE_BUCKETS  # noqa: E402


_TEST_EMAIL_DOMAIN = "@subscribe-test.example"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture(autouse=True)
def _reset_rate_buckets():
    # The subscribe endpoint is wrapped in @_rate_limit(max_per_window=5,
    # window_sec=300). Tests fire 4-6 rapid POSTs from TestClient (all
    # under request.client.host = 'testclient') and the 2nd-6th request
    # would return 429 instead of the expected 400/200. Clear the in-
    # process bucket dict before each test so every test starts fresh.
    _RATE_BUCKETS.clear()
    yield
    _RATE_BUCKETS.clear()


@pytest.fixture
async def _clean():
    async def _do():
        await execute(
            "DELETE FROM signals_subscription "
            "WHERE email LIKE '%' || $1 || '%'",
            _TEST_EMAIL_DOMAIN,
        )
    await _do()
    yield
    await _do()


def _client():
    app = FastAPI()
    app.include_router(build_router())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ─── Subscribe ──────────────────────────────────────────────────────────


async def test_subscribe_accepts_json_body(_clean):
    async with _client() as c:
        r = await c.post("/api/v1/signals/subscribe", json={
            "email": "a" + _TEST_EMAIL_DOMAIN,
            "severity_floor": "critical",
            "category_ids": ["sanctioned_dark", "shadow_fleet"],
            "source": "landing-test",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "created"
    assert body["verify_required"] is True
    # Token must NOT leak in the response.
    assert "verify_token" not in r.text
    assert "unsubscribe_token" not in r.text
    # Row is in DB
    row_filters = await fetchval(
        "SELECT filters FROM signals_subscription WHERE email=$1",
        "a" + _TEST_EMAIL_DOMAIN,
    )
    assert row_filters is not None


async def test_subscribe_accepts_form_encoded_body(_clean):
    """Real <form> POSTs send application/x-www-form-urlencoded — must
    work without python-multipart in deps."""
    async with _client() as c:
        r = await c.post(
            "/api/v1/signals/subscribe",
            content=("email=b" + _TEST_EMAIL_DOMAIN
                      + "&severity_floor=high"
                      + "&category_ids=sanctioned_dark,sanctioned_rendezvous"
                      + "&source=footer-form"),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "created"


async def test_subscribe_is_idempotent_and_updates_filters(_clean):
    em = "c" + _TEST_EMAIL_DOMAIN
    async with _client() as c:
        # First call → created
        r1 = await c.post("/api/v1/signals/subscribe", json={
            "email": em, "severity_floor": "high",
        })
        assert r1.json()["status"] == "created"
        # Second call → updated, no error
        r2 = await c.post("/api/v1/signals/subscribe", json={
            "email": em, "severity_floor": "critical",
            "category_ids": ["sanctioned_dark"],
        })
        assert r2.status_code == 200
        assert r2.json()["status"] == "updated"
    # DB shows the latest filters
    filters = await fetchval(
        "SELECT filters FROM signals_subscription WHERE email=$1", em,
    )
    import json as _json
    if isinstance(filters, str):
        filters = _json.loads(filters)
    assert filters["severity_floor"] == "critical"
    assert filters["category_ids"] == ["sanctioned_dark"]


async def test_subscribe_validates_email():
    async with _client() as c:
        for bad in ("", "notanemail", "x@", "@y", "a b@c.com", "x" * 300 + "@a.b"):
            # Reset the rate-limit bucket between iterations — the loop hits
            # the same IP (testclient) 6 times in <1s, exceeding the 5/300s
            # cap that protects the live endpoint. The autouse fixture only
            # clears between tests, not within.
            _RATE_BUCKETS.clear()
            r = await c.post("/api/v1/signals/subscribe", json={"email": bad})
            assert r.status_code == 400, f"bad email {bad!r} got {r.status_code}"


async def test_subscribe_validates_severity():
    async with _client() as c:
        r = await c.post("/api/v1/signals/subscribe", json={
            "email": "v" + _TEST_EMAIL_DOMAIN,
            "severity_floor": "bogus",
        })
    assert r.status_code == 400


async def test_subscribe_rejects_unknown_category_ids():
    async with _client() as c:
        r = await c.post("/api/v1/signals/subscribe", json={
            "email": "u" + _TEST_EMAIL_DOMAIN,
            "category_ids": ["sanctioned_dark", "nonexistent_category_xyz"],
        })
    assert r.status_code == 400
    assert "nonexistent_category_xyz" in r.json()["detail"]


# ─── Verify ─────────────────────────────────────────────────────────────


async def test_verify_token_flips_row_to_verified(_clean):
    em = "vf" + _TEST_EMAIL_DOMAIN
    async with _client() as c:
        await c.post("/api/v1/signals/subscribe", json={"email": em})
        token = await fetchval(
            "SELECT verify_token FROM signals_subscription WHERE email=$1", em,
        )
        # First verify → status=verified
        r = await c.get("/api/v1/signals/verify", params={"t": token})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "verified"
        assert r.json()["email"] == em
        # Second call → already_verified, still 200 (idempotent)
        r2 = await c.get("/api/v1/signals/verify", params={"t": token})
        assert r2.status_code == 200
        assert r2.json()["status"] == "already_verified"


async def test_verify_unknown_token_returns_404():
    async with _client() as c:
        r = await c.get("/api/v1/signals/verify",
                         params={"t": "no-such-token-1234567890"})
    assert r.status_code == 404


# ─── Unsubscribe ────────────────────────────────────────────────────────


async def test_unsubscribe_soft_deletes_idempotently(_clean):
    em = "un" + _TEST_EMAIL_DOMAIN
    async with _client() as c:
        await c.post("/api/v1/signals/subscribe", json={"email": em})
        utok = await fetchval(
            "SELECT unsubscribe_token FROM signals_subscription WHERE email=$1", em,
        )
        r1 = await c.get("/api/v1/signals/unsubscribe", params={"t": utok})
        assert r1.status_code == 200
        assert r1.json()["status"] == "unsubscribed"
        # Second call → still 200, still unsubscribed
        r2 = await c.get("/api/v1/signals/unsubscribe", params={"t": utok})
        assert r2.status_code == 200
    # Row still exists, just with unsubscribed_at set
    when = await fetchval(
        "SELECT unsubscribed_at FROM signals_subscription WHERE email=$1", em,
    )
    assert when is not None


async def test_unsubscribe_unknown_token_returns_404():
    async with _client() as c:
        r = await c.get("/api/v1/signals/unsubscribe",
                         params={"t": "fake-unsubscribe-token-9999"})
    assert r.status_code == 404
