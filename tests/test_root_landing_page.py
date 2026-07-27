"""
GET / — public-facing landing page wiring tests.

Note (2026-05-10): the root page was wholesale-rewritten as the
unified Command Dashboard. The previous landing (hero + ticker +
subscribe form) is preserved as a snapshot at
21_GLASSBOX_AI/landing/_archive/index_2026-05-10__pre-command-dashboard.html
per 00_MASTER_DOCS/ARCHIVE_CONVENTION.md. This test file now covers
ONLY the back-compat checks that survive the rewrite (legacy
dashboard at /web, links to every consumer surface). Substantive
cockpit assertions live in test_command_dashboard.py.

Added 2026-05-20 (P1-D regression suite): three tests pinning the
content-hash cache-bust behavior from commit 6183a24. Without these,
a future refactor could silently remove the regex-injection or the
no-cache HTML headers and we'd be back to the "I see old behavior"
tax that motivated the fix.
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


def _client():
    from glassbox_server import app  # noqa: WPS433
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_legacy_dashboard_preserved_at_web():
    """The /web route must keep serving the original glassbox-web.html
    so existing bookmarks don't 404."""
    async with _client() as c:
        r = await c.get("/web")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_landing_links_every_consumer_surface():
    """Each surface we built must have a discoverable link from /,
    whether the rendering style is hero+ticker or full cockpit."""
    async with _client() as c:
        r = await c.get("/")
    body = r.text
    for path in (
        "/monitor", "/globe", "/network", "/signals",
        "/signals.rss", "/signals.json", "/signals/embed",
        "/api/v1/signals/snapshot.csv",
        "/status", "/api/sources", "/docs",
    ):
        assert path in body, f"missing landing link for {path}"


# ─── P1-D content-hash cache-bust regression suite ───
# These three tests lock the behavior shipped in 6183a24 (2026-05-13).
# Risk being mitigated: a future edit to root_landing() or to the
# landing/index.html template silently breaks the cache-bust without
# the regular test suite noticing — bringing back the "I see old JS"
# tax that motivated the fix.


@pytest.mark.asyncio
async def test_landing_injects_content_hash_into_atlas_js_src():
    """The served HTML must rewrite `<script src="/atlas.js?...">` to
    use `?h={12-hex}`, NOT the static `?v=YYYYMMDD-...` placeholder
    that lives in the HTML on disk.

    This is the contract that makes Cloudflare's 31-day Browser Cache
    TTL harmless: every atlas.js change → new SHA-256[:12] → new URL
    → cache miss → fresh fetch. If this regression catches, the
    operator is back to needing cmd+shift+R after every cockpit edit.
    """
    async with _client() as c:
        r = await c.get("/")
    body = r.text
    # New, content-addressed form (the contract):
    matches = re.findall(r'/atlas\.js\?h=([0-9a-f]{12})', body)
    assert matches, (
        "no /atlas.js?h={hash} reference in served HTML — "
        "hash injection at root_landing() is broken"
    )
    # The 12-char hash is the SHA-256[:12] from _atlas_hash().
    assert all(re.fullmatch(r'[0-9a-f]{12}', h) for h in matches)
    # Old, static placeholder must be gone — if it leaks through, the
    # injection regex stopped matching and we're shipping the literal
    # ?v=... again.
    assert '/atlas.js?v=' not in body, (
        "stale `?v=` placeholder leaked through — regex in "
        "root_landing() failed to rewrite it"
    )


@pytest.mark.asyncio
async def test_landing_html_serves_with_no_cache_headers():
    """The root HTML itself must be served `Cache-Control: no-cache`
    so browsers + Cloudflare always re-fetch and see the latest
    injected ?h=... value. Without this, the hash injection is moot:
    Cloudflare would serve a stale HTML pointing at a stale URL."""
    async with _client() as c:
        r = await c.get("/")
    cc = r.headers.get("cache-control", "").lower()
    assert "no-cache" in cc, f"missing no-cache on root HTML; got {cc!r}"
    # Cloudflare-specific override headers — these beat their Browser
    # Cache TTL default (which silently overrides plain Cache-Control).
    cdn_cc = r.headers.get("cdn-cache-control", "").lower()
    cf_cc = r.headers.get("cloudflare-cdn-cache-control", "").lower()
    assert "no-cache" in cdn_cc, f"missing CDN-Cache-Control no-cache; got {cdn_cc!r}"
    assert "no-cache" in cf_cc, f"missing Cloudflare-CDN-Cache-Control no-cache; got {cf_cc!r}"


def test_atlas_hash_recomputes_when_file_mtime_changes(tmp_path, monkeypatch):
    """`_atlas_hash()` is keyed on mtime so it only re-hashes when the
    file actually changes. Verify both halves of the contract:
      (a) same mtime → cached hash returned without re-reading disk
      (b) bumped mtime → fresh sha256[:12] computed from new content

    Without this, a refactor could turn the cache into a permanent
    pin (hash never updates) or remove the cache (re-reads on every
    request — minor perf bug). Both are silent failures."""
    import os

    # The hash cache + function live in web._assets as of the P3-H
    # prerequisite that lifted shared landing-page infrastructure.
    # glassbox_server still re-imports them under the legacy underscore
    # names for the in-file / handler, but the canonical location is
    # web._assets — and the monkeypatch MUST target the module that
    # defines `atlas_hash` (Python attribute lookup inside a function
    # body resolves against the defining module's globals, not the
    # importing module's aliases).
    from web._assets import atlas_hash, ATLAS_HASH_CACHE  # noqa: WPS433
    import web._assets as wa  # noqa: WPS433

    # Tempfile stand-in for atlas.js. Monkeypatch the canonical
    # `web._assets.LANDING_ATLAS_JS_PATH` so `atlas_hash()` picks it up.
    fake = tmp_path / "atlas.js"
    fake.write_text("/* version A */\n", encoding="utf-8")
    monkeypatch.setattr(wa, "LANDING_ATLAS_JS_PATH", fake)
    # Reset the cache so the test isn't polluted by prior calls in
    # this process (test_landing_* above hit the real atlas.js).
    ATLAS_HASH_CACHE["mtime"] = None
    ATLAS_HASH_CACHE["hash"] = "init"

    h1 = atlas_hash()
    assert re.fullmatch(r'[0-9a-f]{12}', h1), f"expected 12-hex, got {h1!r}"

    # (a) Same file, same mtime → same hash, returned from cache.
    h2 = atlas_hash()
    assert h1 == h2

    # (b) New content + bumped mtime → fresh hash.
    fake.write_text("/* version B — different content */\n", encoding="utf-8")
    new_mtime = fake.stat().st_mtime + 1.0
    os.utime(fake, (new_mtime, new_mtime))
    h3 = atlas_hash()
    assert re.fullmatch(r'[0-9a-f]{12}', h3)
    assert h3 != h1, "hash did not refresh after mtime bump + content change"
