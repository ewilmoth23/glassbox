"""
GET / — Spatial Intelligence cockpit wiring tests.

The page was redesigned (see landing/_archive/) from the cream
Cartographer's Workbench to a dark Spatial Intelligence cockpit:
deep space-black base, gold accents, cyan live signals, Geist sans
typography, glassmorphic floating panels in a bento layout, dense
information surface with Cesium globe + KPI cluster + layers + AI
brief + news video + chronicle + webcams + alert ticker all visible.

These tests assert the new contract AND catch regressions toward the
worldview-lookalike (DEFCON top-bar chip, classification cosplay,
purple/magenta gradients, 3-column Palantir grid).
"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool  # noqa: E402


@pytest.fixture
async def _pool():
    await init_pool()
    yield
    await close_pool()


def _client():
    from glassbox_server import app  # noqa: WPS433
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_root_serves_spatial_intelligence_cockpit():
    """Dark theme + Geist + Cesium + bento panels are all present."""
    async with _client() as c:
        r = await c.get("/")
    assert r.status_code == 200
    body = r.text
    # Brand + framing
    assert "Spatial Intelligence" in body or "SPATIAL INTELLIGENCE" in body
    # Modern geometric sans (NOT Inter / Roboto / Space Grotesk)
    assert "Geist" in body
    # 3D globe
    assert "cesium.com/downloads/cesiumjs" in body
    assert "/atlas.js" in body
    # All eight bento/floating panels present
    for cls in ("bento", "layers", "brief", "news", "chronicle",
                "cams", "atlas", "ticker"):
        assert cls in body, f"missing panel/cluster '{cls}'"
    # Dark theme markers
    assert "--space" in body or "--void" in body
    # KPI cluster slots
    for sid in ("kpi-critical", "kpi-cases", "kpi-signals", "kpi-tracked"):
        assert f'id="{sid}"' in body, f"missing KPI slot {sid}"


@pytest.mark.asyncio
async def test_cockpit_does_not_revive_worldview_cosplay():
    """Regression catcher. The dark redesign must not bring back the
    worldmonitor-style cosplay (TOP SECRET classification banner,
    DEFCON top-bar chip, purple gradient brand, magenta cyberpunk
    palette, the 3-column Palantir rail/map/news grid).

    We assert against ACTUAL elements, not substring matches — the
    source's design-rationale comments may name these patterns
    explicitly to document why we avoid them."""
    async with _client() as c:
        r = await c.get("/")
    body = r.text
    # No fake-classification banner element. Look for either the
    # CSS class signature `.classification` from the previous design
    # OR a visible // SI // NOFORN markup string (the cosplay format).
    assert ".classification" not in body
    assert "// SI //" not in body
    assert "// NOFORN" not in body
    # No DEFCON gauge as a top-bar chip
    assert 'id="defcon-value"' not in body
    assert 'class="chip defcon"' not in body
    # No purple/magenta brand gradients
    assert "linear-gradient(90deg, var(--accent)" not in body
    assert "background: var(--accent-2)" not in body  # the cyberpunk pink
    # Regression catcher in the OTHER direction — must NOT have
    # reverted to the cream Cartographer aesthetic either
    assert "--paper:        #f6f1e7" not in body
    assert "Newsreader" not in body


@pytest.mark.asyncio
async def test_atlas_js_served_with_correct_mime_and_endpoints():
    async with _client() as c:
        r = await c.get("/atlas.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    js = r.text
    for ep in ("/api/v1/signals/today",
               "/api/v1/viewport",
               "/api/v1/dashboard/summary",
               "/api/v1/alerts/stream",
               "/api/v1/brief/latest",
               "/api/v1/cesium-token",
               "/api/v1/health/full"):
        assert ep in js, f"missing endpoint reference {ep}"
    # Cesium motion + layer rendering wired
    assert "Cesium.Viewer" in js
    assert "SampledPositionProperty" in js
    assert "animateBetween" in js
    # The new bento panels are wired
    for fn in ("paintKpis", "paintTicker", "loadBrief",
               "renderWebcams", "wireNewsTabs"):
        assert fn in js, f"missing wiring function {fn}"
    # Idle guard against third-party SSO interception
    assert "idleGuard" in js
    assert "parkedSrc" in js


@pytest.mark.asyncio
async def test_command_js_legacy_alias_still_works():
    async with _client() as c:
        r1 = await c.get("/atlas.js")
        r2 = await c.get("/command.js")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.text == r2.text


@pytest.mark.asyncio
async def test_cockpit_links_every_existing_consumer_surface():
    async with _client() as c:
        r = await c.get("/")
    body = r.text
    for path in ("/monitor", "/globe", "/network", "/signals",
                 "/status", "/docs", "/web",
                 "/signals.rss", "/signals.json",
                 "/api/v1/signals/snapshot.csv", "/signals/embed",
                 "/api/sources"):
        assert path in body, f"cockpit missing link to {path}"


@pytest.mark.asyncio
async def test_dashboard_summary_endpoint_returns_documented_shape(_pool):
    async with _client() as c:
        r = await c.get("/api/v1/dashboard/summary?window_hours=24")
    assert r.status_code == 200
    body = r.json()
    for k in ("generated_at", "window_hours", "signals", "critical",
              "open_cases", "sources", "geolocated", "subscribers"):
        assert k in body, f"missing {k}"
    assert body["window_hours"] == 24
    for k in ("signals", "critical", "open_cases", "sources",
              "geolocated", "subscribers"):
        assert isinstance(body[k], int) and body[k] >= 0


@pytest.mark.asyncio
async def test_cockpit_has_tactical_atlas_overlays():
    """Surveillance-grade polish on the Cesium globe — corner brackets,
    center reticle, lat/lng graticule, range rings on click, and a
    cursor-following hover card.

    These are atlas/analyst-workstation visual cues. Must NOT bring
    back classification cosplay (TOP SECRET / NOFORN / DEFCON chip)
    even while making the globe feel more military.
    """
    async with _client() as c:
        r_html = await c.get("/")
        r_js   = await c.get("/atlas.js")
    body = r_html.text
    js   = r_js.text

    # HTML overlays present
    assert 'tactical-corners' in body, "missing tactical corner-bracket overlay"
    assert 'class="reticle"' in body, "missing center reticle"
    assert 'id="hover-card"' in body, "missing hover-card slot"
    assert 'id="scale-bar-text"' in body, "missing scale-bar slot"

    # JS wires the new tactical features
    for fn in ("_drawGraticule", "_setupRangeRings", "_drawRangeRingsAt",
               "_setupHoverCard", "_hoverCardHTML", "updateScaleBar"):
        assert fn in js, f"missing tactical wiring fn '{fn}'"

    # Range rings cover analyst-useful distances (100/250/500km).
    # Asserting against the constant array, not the literal numbers,
    # so anyone changing them updates the test consciously.
    assert "RANGE_RING_RADII_KM" in js
    assert "[100, 250, 500]" in js

    # The previous "military" imagery treatment (darken/desaturate)
    # was rejected by the user — must NOT be the default any more.
    # Cesium World Terrain + Google 3D Tiles auto-load instead.
    assert "baseLayer.brightness = 0.55" not in js, \
        "must not re-apply the rejected military darken treatment"
    assert "createWorldTerrainAsync" in js, \
        "Cesium World Terrain must be enabled by default"
    assert "_autoEnable3DTiles" in js, \
        "Google 3D Tiles must auto-attempt on boot"

    # The hover card is the Apple-Maps style replacement for click-
    # only deep-dive — REMAINING_WORK item ticked off.
    assert "MOUSE_MOVE" in js, "hover card must use MOUSE_MOVE event"

    # Cosplay regression catcher — defensive against the new tactical
    # work introducing classification / DEFCON ELEMENTS (not source-
    # comment substrings, which legitimately document what we avoid).
    assert 'id="defcon-' not in body
    assert 'class="classification' not in body
    assert "// SI //" not in body and "// SI //" not in js
    assert "// NOFORN" not in body and "// NOFORN" not in js


@pytest.mark.asyncio
async def test_panels_are_draggable_resizable_with_window_controls():
    """Each floating HUD panel must be a movable + resizable window
    with min/max/close buttons + a Layout menu in the bottom HUD that
    toggles visibility per panel and offers reset-to-default. State
    persists to localStorage so customizations survive a reload."""
    async with _client() as c:
        r_html = await c.get("/")
        r_js   = await c.get("/atlas.js")
    body = r_html.text
    js   = r_js.text

    # No third-party drag library — vanilla pointer-events drag/resize
    # so we don't depend on any external CDN (immune to CDN outages,
    # blocked by privacy proxies, etc.).
    assert "interact.min.js" not in body, \
        "must not depend on jsdelivr-hosted interact.js anymore"

    # Every floating panel has data-panel-id + min/max/close buttons
    for pid in ("layers", "brief", "news", "chronicle", "cams"):
        assert f'data-panel-id="{pid}"' in body, f"panel '{pid}' missing data-panel-id"
    assert body.count('class="min"') >= 5
    assert body.count('class="max"') >= 5
    assert body.count('class="close"') >= 5

    # Layout menu in the bottom HUD with toggles + reset
    assert 'id="layout-menu"' in body
    assert 'id="layout-trigger"' in body
    assert 'data-toggle="layers"' in body
    assert 'data-action="reset"' in body

    # JS wires drag, resize, persistence, layout menu — vanilla impl
    for fn in ("initPanelWindowing", "_persistPanel", "_readLayout",
               "_writeLayout", "_refreshLayoutChecks",
               "_makePanelDraggable", "_makePanelResizable",
               "_onResizeDown"):
        assert fn in js, f"missing windowing fn '{fn}'"
    # Vanilla pointer events — not interact.js
    assert "pointerdown" in js
    assert "pointermove" in js
    assert "pointerup" in js
    assert "_RESIZE_HANDLES" in js
    assert "LAYOUT_KEY" in js
    assert "localStorage" in js


@pytest.mark.asyncio
async def test_google_3d_tiles_toggle_wired_via_cesium_ion():
    """3D Tiles button in the bottom HUD toggles Google Photorealistic
    3D Tiles via Cesium Ion (asset 2275207, no separate Google Cloud
    key needed — bundled into the Cesium Ion plan we already have)."""
    async with _client() as c:
        r_html = await c.get("/")
        r_js   = await c.get("/atlas.js")
    body = r_html.text
    js   = r_js.text
    assert 'id="toggle-3d"' in body, "missing 3D Tiles toggle button"
    assert 'class="btn-3d"' in body
    for fn in ("toggle3DTiles", "wire3DTilesToggle"):
        assert fn in js, f"missing 3D Tiles fn '{fn}'"
    # Cesium Ion built-in tileset, NOT a direct Google Maps Tiles API call
    assert "createGooglePhotorealistic3DTileset" in js
    # Defensive: must NOT reference a Google Cloud API key env var,
    # which would imply a separate Google billing path.
    assert "GOOGLE_MAPS_TILES_API_KEY" not in js
    assert "maps.googleapis.com" not in js


@pytest.mark.asyncio
async def test_news_and_webcam_panels_visible_and_wired():
    """The news + webcam panels ship as click-to-play placeholders by
    default (no auto-loaded YouTube iframes on initial render — keeps
    the page lightweight and avoids triggering 3rd-party browser-
    extension proxies). Tabs are present; iframes get injected by JS
    when the user clicks. Verify both the structure and the JS contract."""
    async with _client() as c:
        r_html = await c.get("/")
        r_js   = await c.get("/atlas.js")
    body = r_html.text
    js   = r_js.text
    assert 'id="news-tabs"' in body
    assert 'id="news-frame"' in body
    assert 'id="cam-tabs"' in body
    assert 'id="webcam-grid"' in body
    # No auto-loaded YouTube iframes in the initial HTML
    assert 'youtube.com/embed/live_stream' not in body, \
        "news iframe must not auto-load on page boot (privacy + no-trackers)"
    # JS knows how to lazy-activate them on click — using nocookie domain
    assert "youtube-nocookie" in js
    assert "activateNews" in js
    # Webcam grid is built by JS from the WEBCAM_SETS map
    assert "WEBCAM_SETS" in js
    assert "renderWebcams" in js
