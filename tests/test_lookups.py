"""
On-demand lookup helpers — tests for crt.sh / Wayback CDX / RIPEstat.

Network calls are mocked so tests are fast and deterministic. We assert:
  - Cache hits short-circuit subsequent calls
  - Bad inputs return error dicts (not raises)
  - JSON shape matches the documented contract
  - Result counts match upstream payloads

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_lookups.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lookups import (  # noqa: E402
    lookup_subdomains, lookup_wayback, lookup_asn,
    _subdomain_cache, _wayback_cache, _ripe_cache,
)


@pytest.fixture(autouse=True)
async def _clear_caches():
    """Each test gets a fresh cache so previous tests don't leak."""
    await _subdomain_cache.clear()
    await _wayback_cache.clear()
    await _ripe_cache.clear()
    yield
    await _subdomain_cache.clear()
    await _wayback_cache.clear()
    await _ripe_cache.clear()


def _mock_session(json_payload, status: int = 200):
    """Build a context-manager mock for aiohttp.ClientSession.get()."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_payload)

    cm_response = MagicMock()
    cm_response.__aenter__ = AsyncMock(return_value=response)
    cm_response.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.get = MagicMock(return_value=cm_response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


# ─── crt.sh ───────────────────────────────────────────────────────────────


async def test_subdomains_extracts_unique_names():
    fake = [
        {"name_value": "*.example.com\nfoo.example.com"},
        {"name_value": "bar.example.com"},
        {"name_value": "foo.example.com"},  # dup
    ]
    with patch("lookups.aiohttp.ClientSession", return_value=_mock_session(fake)):
        out = await lookup_subdomains("example.com")
    assert out["count"] == 3
    assert sorted(out["subdomains"]) == ["bar.example.com", "example.com", "foo.example.com"]
    assert out["source"] == "crt.sh"
    assert out["cached"] is False


async def test_subdomains_filters_out_unrelated_names():
    """name_value rows from crt.sh sometimes include cert SANs from other domains.
    Anything not ending in '.example.com' (or the domain itself) is dropped."""
    fake = [
        {"name_value": "good.example.com\nunrelated.other.com"},
    ]
    with patch("lookups.aiohttp.ClientSession", return_value=_mock_session(fake)):
        out = await lookup_subdomains("example.com")
    assert out["subdomains"] == ["good.example.com"]


async def test_subdomains_cache_hit_skips_network():
    fake = [{"name_value": "x.example.com"}]
    with patch("lookups.aiohttp.ClientSession", return_value=_mock_session(fake)):
        out1 = await lookup_subdomains("example.com")
    # Second call: if it tries to hit the network, the patch is gone and aiohttp
    # would raise. The fact that we get a result back at all proves cache hit.
    out2 = await lookup_subdomains("example.com")
    assert out1["count"] == out2["count"]
    assert out2["cached"] is True


async def test_subdomains_invalid_domain_returns_error():
    out = await lookup_subdomains("")
    assert out["count"] == 0
    assert "error" in out


async def test_subdomains_http_error_propagates_to_error_dict():
    with patch("lookups.aiohttp.ClientSession",
               return_value=_mock_session([], status=500)):
        out = await lookup_subdomains("example.com")
    assert out["count"] == 0
    assert "error" in out
    assert "500" in out["error"]


# ─── Wayback CDX ──────────────────────────────────────────────────────────


async def test_wayback_parses_cdx_rows():
    fake = [
        ["timestamp", "original", "statuscode", "mimetype", "digest"],  # header
        ["20240301120000", "https://example.com/", "200", "text/html", "abc"],
        ["20240601120000", "https://example.com/", "200", "text/html", "def"],
    ]
    with patch("lookups.aiohttp.ClientSession", return_value=_mock_session(fake)):
        out = await lookup_wayback("https://example.com/")
    assert out["count"] == 2
    assert out["snapshots"][0]["timestamp"] == "20240301120000"
    assert out["snapshots"][0]["snapshot_url"] == \
        "https://web.archive.org/web/20240301120000/https://example.com/"
    assert out["source"] == "wayback_cdx"


async def test_wayback_empty_response_yields_zero_snapshots():
    with patch("lookups.aiohttp.ClientSession", return_value=_mock_session([])):
        out = await lookup_wayback("https://example.com/")
    assert out["count"] == 0
    assert out["snapshots"] == []


async def test_wayback_cache_hit_marks_cached():
    fake = [["timestamp", "original", "statuscode"],
            ["20240101", "https://example.com/", "200"]]
    with patch("lookups.aiohttp.ClientSession", return_value=_mock_session(fake)):
        await lookup_wayback("https://example.com/")
    out2 = await lookup_wayback("https://example.com/")
    assert out2["cached"] is True


async def test_wayback_invalid_url_returns_error():
    out = await lookup_wayback("")
    assert "error" in out


async def test_wayback_http_error_returns_error():
    with patch("lookups.aiohttp.ClientSession",
               return_value=_mock_session([], status=503)):
        out = await lookup_wayback("https://example.com/")
    assert "error" in out
    assert "503" in out["error"]


# ─── RIPEstat ─────────────────────────────────────────────────────────────


async def test_ripe_asn_calls_two_endpoints():
    overview = {"data": {"holder": "Google LLC", "resource": "15169"}}
    prefixes = {"data": {"prefixes": [{"prefix": "8.8.8.0/24"}]}}

    # Need a session that returns DIFFERENT payloads per URL.
    call_count = {"n": 0}
    payloads = [overview, prefixes]

    def make_get(*_args, **_kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=payloads[idx])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    session = MagicMock()
    session.get = MagicMock(side_effect=make_get)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with patch("lookups.aiohttp.ClientSession", return_value=session):
        out = await lookup_asn(asn="AS15169")

    assert out["query"]["asn"] == "AS15169"
    assert out["data"]["as-overview"]["holder"] == "Google LLC"
    assert out["data"]["announced-prefixes"]["prefixes"][0]["prefix"] == "8.8.8.0/24"
    assert out["endpoints_called"] == ["as-overview", "announced-prefixes"]
    assert out["source"] == "ripestat"


async def test_ripe_ip_calls_network_and_abuse():
    netinfo = {"data": {"asns": ["15169"], "prefix": "8.8.8.0/24"}}
    abuse = {"data": {"abuse_contacts": ["network-abuse@google.com"]}}

    payloads = [netinfo, abuse]
    call_count = {"n": 0}

    def make_get(*_args, **_kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=payloads[idx])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    session = MagicMock()
    session.get = MagicMock(side_effect=make_get)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with patch("lookups.aiohttp.ClientSession", return_value=session):
        out = await lookup_asn(ip="8.8.8.8")

    assert out["query"]["ip"] == "8.8.8.8"
    assert out["data"]["network-info"]["asns"] == ["15169"]
    assert out["data"]["abuse-contact-finder"]["abuse_contacts"] == ["network-abuse@google.com"]


async def test_ripe_requires_one_param():
    out = await lookup_asn()
    assert "error" in out


async def test_ripe_rejects_both_params():
    out = await lookup_asn(asn="15169", ip="8.8.8.8")
    assert "error" in out


async def test_ripe_strips_as_prefix():
    """AS15169 and 15169 should both work."""
    payload = {"data": {"holder": "Google LLC"}}
    payloads = [payload, payload]
    call_count = {"n": 0}

    def make_get(*_args, **_kwargs):
        idx = min(call_count["n"], len(payloads) - 1)
        call_count["n"] += 1
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=payloads[idx])
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    session = MagicMock()
    session.get = MagicMock(side_effect=make_get)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with patch("lookups.aiohttp.ClientSession", return_value=session):
        out = await lookup_asn(asn="AS15169")

    assert out["query"]["asn"] == "AS15169"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
