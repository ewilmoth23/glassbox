"""Shared infrastructure for the `web/routes/` route modules.

Lifted from `glassbox_server.py` 2026-05-21 EVE as the P3-H prerequisite
for extracting `/static.py` (static assets) and `/pages.py` (landing +
shell pages) into separate route modules. Both depend on these
constants and helpers; lifting them here is the "right way" — one
source of truth, no per-module re-definition.

Module is underscore-prefixed (`_assets`) to signal it's web-internal
plumbing, not a public surface for outside callers. The names inside
are NOT underscore-prefixed because they're cross-module API.

Contents:

  Paths:
    LANDING_DIR              — `21_GLASSBOX_AI/landing/`
    LANDING_PATH             — `…/landing/index.html` (the `/` page)
    LANDING_ATLAS_JS_PATH    — `…/landing/atlas.js` (served by `/atlas.js`,
                               hashed by `atlas_hash()` for cache-busting)
    LANDING_FAVICON_PATH     — `…/landing/favicon.svg`
    LANDING_JS_PATH          — `…/landing/command.js`
    DASHBOARD_PATH           — `05_WEBSITE_AND_LANDING/glassbox-web.html`
                               (the `/web` legacy dashboard; also the `/`
                               fallback if landing/index.html is missing)
    WEB_ROOT                 — `05_WEBSITE_AND_LANDING/` (the public-site
                               source for /glassbox, /markets, /pro)

  Helpers:
    serve_static_html(name)  — read `WEB_ROOT / name` and return an
                               `HTMLResponse`; 404 if missing
    atlas_hash()             — short SHA-256 of LANDING_ATLAS_JS_PATH;
                               mtime-cached. Used by the `/` handler to
                               inject `?h={hash}` into `<script src="/atlas.js?…">`
                               so the browser cache-busts atomically when
                               atlas.js changes. The contract is: the
                               `?h=` value MUST equal the SHA-256 of the
                               bytes that `/atlas.js` will serve. Both
                               read from `LANDING_ATLAS_JS_PATH`, so they
                               are guaranteed in sync.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict

from fastapi.responses import HTMLResponse

# `web/_assets.py` → parent is `web/` → parent of that is `21_GLASSBOX_AI/`.
_PKG_ROOT = Path(__file__).resolve().parent.parent

# 05_WEBSITE_AND_LANDING is a SIBLING of 21_GLASSBOX_AI, so two .parent steps.
_EMPIRE_ROOT = _PKG_ROOT.parent

LANDING_DIR = _PKG_ROOT / "landing"
LANDING_PATH = LANDING_DIR / "index.html"
LANDING_ATLAS_JS_PATH = LANDING_DIR / "atlas.js"
LANDING_FAVICON_PATH = LANDING_DIR / "favicon.svg"
LANDING_JS_PATH = LANDING_DIR / "command.js"

WEB_ROOT = _EMPIRE_ROOT / "05_WEBSITE_AND_LANDING"
DASHBOARD_PATH = WEB_ROOT / "glassbox-web.html"


def serve_static_html(filename: str) -> HTMLResponse:
    """Read `WEB_ROOT / filename` and return an HTMLResponse. 404 if
    the file doesn't exist. Used by /glassbox, /markets, /pro — they
    each serve a different file from 05_WEBSITE_AND_LANDING/ with the
    same boilerplate."""
    p = WEB_ROOT / filename
    if not p.exists():
        return HTMLResponse(f"<p>Not found: {filename}</p>", status_code=404)
    return HTMLResponse(p.read_text(encoding="utf-8"))


# Cache of the atlas.js content hash, keyed by file mtime. Computed
# once per change. Used by the / handler to inject a content-addressed
# URL into the landing page's <script src> so that every code change
# produces a different URL — and browsers (plus Cloudflare's 31-day
# Browser Cache TTL) treat it as a brand-new asset to fetch, with no
# stale-cache risk.
ATLAS_HASH_CACHE: Dict[str, Any] = {"mtime": None, "hash": "init"}


def atlas_hash() -> str:
    """Return short SHA-256 of LANDING_ATLAS_JS_PATH. Recomputes when
    mtime changes. Safe to call on every request — the stat() + cache
    check is cheap; the hash read+compute only happens on actual edits."""
    try:
        mtime = LANDING_ATLAS_JS_PATH.stat().st_mtime
    except OSError:
        return ATLAS_HASH_CACHE["hash"]
    if ATLAS_HASH_CACHE["mtime"] != mtime:
        data = LANDING_ATLAS_JS_PATH.read_bytes()
        ATLAS_HASH_CACHE["hash"] = hashlib.sha256(data).hexdigest()[:12]
        ATLAS_HASH_CACHE["mtime"] = mtime
    return ATLAS_HASH_CACHE["hash"]
