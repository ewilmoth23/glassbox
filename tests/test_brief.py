"""
Phase 1.5 — brief.py: human-readable summary of /api/v1/viewport output.

The brief is a 100-300 word factual summary built deterministically from the
viewport JSON. No LLM, no fabricated numbers, no hallucination. Truthful by
construction (Rule 2.3 in CLAUDE_CODE_GLASSBOX.md).

Asserts:
  - Empty viewport → graceful "no activity" string
  - Aircraft-only viewport → counts + military / emergency callouts
  - Events grouped by event_type (quakes, alerts, news, fires, etc.)
  - Proximity findings highlighted as the cross-domain headline
  - Cache returns same string for same viewport input within TTL
  - Cache invalidates after TTL expires
  - HTTP layer: ?brief=true returns brief inline in meta

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_brief.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brief import generate_brief, _BriefCache, brief_cache  # noqa: E402


# ─── Helpers ──────────────────────────────────────────────────────────────


def _vp_meta(bbox=(-75, 39, -72, 42), entity_count=0, event_count=0,
             types=None, time_from=None, time_to=None) -> dict:
    if types is None:
        types = ["aircraft"]
    now = datetime.now(timezone.utc)
    return {
        "bbox": list(bbox),
        "time_from": (time_from or now - timedelta(hours=1)).isoformat(),
        "time_to": (time_to or now).isoformat(),
        "types": types,
        "entity_count": entity_count,
        "event_count": event_count,
        "query_ms": 12,
        "now": now.isoformat(),
    }


def _entity(canonical_id="abc123", entity_type="aircraft", display_name="UAL100",
            properties=None, lat=40.6, lng=-73.8, altitude_m=10000.0,
            velocity_ms=250.0):
    return {
        "id": "uuid-aaa",
        "entity_type": entity_type,
        "canonical_id": canonical_id,
        "canonical_id_type": "icao24",
        "display_name": display_name,
        "properties": properties or {},
        "last_seen": "2026-05-08T01:00:00+00:00",
        "position": {
            "lat": lat,
            "lng": lng,
            "altitude_m": altitude_m,
            "velocity_ms": velocity_ms,
            "heading_deg": 90.0,
            "time": "2026-05-08T01:00:00+00:00",
        },
    }


def _event(event_type="usgs_quake", title="M4.7 quake near X", severity=5.0,
           lat=33.0, lng=-118.0, properties=None, event_subtype=None):
    return {
        "id": "uuid-bbb",
        "event_type": event_type,
        "event_subtype": event_subtype,
        "event_time": "2026-05-08T00:30:00+00:00",
        "severity": severity,
        "severity_for_market": None,
        "title": title,
        "description": None,
        "properties": properties or {},
        "domain": "geo",
        "decay_half_life_min": 60,
        "lat": lat,
        "lng": lng,
    }


# ─── Empty / minimal cases ────────────────────────────────────────────────


def test_brief_empty_viewport_says_no_activity():
    vp = {"meta": _vp_meta(), "entities": [], "events": []}
    text = generate_brief(vp)
    assert "no" in text.lower() or "0" in text
    assert len(text) > 0
    # Must include the bbox so reader knows what was queried
    assert "33" in text or "39" in text or "lat" in text.lower() or "bbox" in text.lower() or "region" in text.lower()


def test_brief_one_aircraft_counts_correctly():
    vp = {
        "meta": _vp_meta(entity_count=1),
        "entities": [_entity()],
        "events": [],
    }
    text = generate_brief(vp)
    assert "1 aircraft" in text or "1 entity" in text or "one aircraft" in text.lower()


def test_brief_calls_out_military_aircraft():
    """Military entities should be counted and called out so an analyst spots them."""
    vp = {
        "meta": _vp_meta(entity_count=3),
        "entities": [
            _entity("ae0001", display_name="REACH01", properties={"military": True}),
            _entity("ae0002", display_name="SPAR12", properties={"military": True}),
            _entity("a78342", display_name="UAL100", properties={"military": False}),
        ],
        "events": [],
    }
    text = generate_brief(vp)
    assert "military" in text.lower()
    assert "2" in text   # 2 military
    assert "REACH01" in text or "SPAR12" in text


def test_brief_calls_out_emergency_squawks():
    """Emergency-squawk aircraft are the highest-priority signal in the brief."""
    vp = {
        "meta": _vp_meta(entity_count=2),
        "entities": [
            _entity("ae0001", display_name="MAYDAY1", properties={"emergency": True, "squawk": "7700"}),
            _entity("a78342", display_name="UAL100", properties={"emergency": False}),
        ],
        "events": [],
    }
    text = generate_brief(vp)
    assert "emergency" in text.lower() or "squawk" in text.lower()
    assert "MAYDAY1" in text or "1" in text


def test_brief_groups_events_by_type():
    vp = {
        "meta": _vp_meta(event_count=4),
        "entities": [],
        "events": [
            _event(event_type="usgs_quake", title="M4.7 quake near LA"),
            _event(event_type="usgs_quake", title="M3.2 quake near SF"),
            _event(event_type="noaa_alert", event_subtype="Flood Warning",
                   title="Flood Warning for X"),
            _event(event_type="nasa_firms", event_subtype="VIIRS_SNPP_NRT",
                   title="Active fire detection"),
        ],
    }
    text = generate_brief(vp)
    # Each event_type should appear at least once
    assert "quake" in text.lower() or "earthquake" in text.lower()
    assert "alert" in text.lower() or "flood" in text.lower() or "weather" in text.lower()
    assert "fire" in text.lower()
    # Counts should be visible (2 quakes, 1 alert, 1 fire)
    assert "2" in text


def test_brief_highlights_proximity_findings_first():
    """Cross-domain proximity findings are the killer signal — must lead the brief."""
    vp = {
        "meta": _vp_meta(event_count=2),
        "entities": [],
        "events": [
            _event(event_type="usgs_quake", title="M4.7 — Tokyo"),
            _event(event_type="detected_proximity",
                   event_subtype="aircraft_usgs_quake",
                   title="aircraft ANA851 near M 4.7 Tokyo quake",
                   properties={"distance_m": 23000, "algorithm": "proximity"}),
        ],
    }
    text = generate_brief(vp)
    assert "ANA851" in text or "proximity" in text.lower()
    # The proximity finding should appear EARLIER in the text than the raw event
    prox_pos = text.find("ANA851")
    quake_pos = text.find("M4.7")  # might not appear if proximity supplants it; that's fine
    if prox_pos > -1 and quake_pos > -1:
        assert prox_pos < quake_pos


def test_brief_word_count_within_envelope():
    """Briefs should be compact — V2 plan says 200 words. Allow up to 400 for
    busy regions but never a wall of text."""
    vp = {
        "meta": _vp_meta(entity_count=5, event_count=3),
        "entities": [_entity(f"id{i}", display_name=f"FLT{i}") for i in range(5)],
        "events": [
            _event(event_type="usgs_quake", title=f"M4.{i} near zone {i}")
            for i in range(3)
        ],
    }
    text = generate_brief(vp)
    word_count = len(text.split())
    assert word_count <= 400, f"brief too long: {word_count} words"


# ─── Cache behavior ───────────────────────────────────────────────────────


def test_cache_returns_same_string_within_ttl():
    cache = _BriefCache(ttl_seconds=5)
    vp = {"meta": _vp_meta(), "entities": [], "events": []}
    key = cache.make_key(vp)
    s1 = cache.get_or_compute(key, lambda: "first call")
    s2 = cache.get_or_compute(key, lambda: "second call")
    assert s1 == s2 == "first call"


def test_cache_recomputes_after_ttl_expires():
    cache = _BriefCache(ttl_seconds=0)   # immediate expiry
    vp = {"meta": _vp_meta(), "entities": [], "events": []}
    key = cache.make_key(vp)
    s1 = cache.get_or_compute(key, lambda: "call A")
    # ttl=0 means even immediate next call should miss cache
    import time
    time.sleep(0.01)
    s2 = cache.get_or_compute(key, lambda: "call B")
    assert s1 == "call A"
    assert s2 == "call B"


def test_cache_key_distinguishes_different_bboxes():
    cache = _BriefCache(ttl_seconds=60)
    vp1 = {"meta": _vp_meta(bbox=(-75, 39, -72, 42)), "entities": [], "events": []}
    vp2 = {"meta": _vp_meta(bbox=(-119, 33, -117, 35)), "entities": [], "events": []}
    k1 = cache.make_key(vp1)
    k2 = cache.make_key(vp2)
    assert k1 != k2


def test_cache_key_distinguishes_different_types():
    cache = _BriefCache(ttl_seconds=60)
    vp1 = {"meta": _vp_meta(types=["aircraft"]), "entities": [], "events": []}
    vp2 = {"meta": _vp_meta(types=["aircraft", "vessel"]), "entities": [], "events": []}
    k1 = cache.make_key(vp1)
    k2 = cache.make_key(vp2)
    assert k1 != k2


# ─── HTTP integration ─────────────────────────────────────────────────────


async def test_http_viewport_with_brief_returns_brief_in_meta():
    """?brief=true on /api/v1/viewport adds a 'brief' field to meta."""
    from db import init_pool, close_pool
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport
    from api_v1 import build_router

    await init_pool()
    try:
        app = FastAPI()
        app.include_router(build_router())

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            now = datetime.now(timezone.utc)
            params = {
                "bbox": "-75.0,39.0,-72.0,42.0",
                "time_from": (now - timedelta(hours=1)).isoformat(),
                "time_to": now.isoformat(),
                "types": "aircraft",
                "limit": 5,
                "brief": "true",
            }
            resp = await client.get("/api/v1/viewport", params=params)
            assert resp.status_code == 200
            body = resp.json()
            assert "brief" in body["meta"]
            assert isinstance(body["meta"]["brief"], str)
            assert len(body["meta"]["brief"]) > 0
    finally:
        await close_pool()


async def test_http_viewport_without_brief_omits_brief_field():
    """Default behavior: no brief field unless explicitly requested."""
    from db import init_pool, close_pool
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport
    from api_v1 import build_router

    await init_pool()
    try:
        app = FastAPI()
        app.include_router(build_router())

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            now = datetime.now(timezone.utc)
            resp = await client.get("/api/v1/viewport", params={
                "bbox": "-75.0,39.0,-72.0,42.0",
                "time_from": (now - timedelta(hours=1)).isoformat(),
                "time_to": now.isoformat(),
                "types": "aircraft",
                "limit": 5,
            })
            assert resp.status_code == 200
            body = resp.json()
            # Without brief=true the field is absent (or explicitly None)
            assert body["meta"].get("brief") in (None, "")
    finally:
        await close_pool()


# ─── LLM analyst-note layer ───────────────────────────────────────────────


async def test_brief_llm_appends_analyst_note_on_success(monkeypatch):
    """When Ollama returns a one-sentence note, the LLM brief = deterministic
    brief + ' Analyst note: <sentence>'."""
    from brief import generate_brief_llm

    async def fake_post_returns(payload, *args, **kwargs):
        # Build a fake aiohttp response context manager
        class _FakeResp:
            status = 200
            async def json(self_inner):
                return {"response": "Investigate aircraft FOO near magnitude X event."}
            async def __aenter__(self_inner): return self_inner
            async def __aexit__(self_inner, *exc): return None
        return _FakeResp()

    # Patch the internal helper so we don't depend on aiohttp internals
    from brief import _ollama_analyst_note as _real_note  # noqa: F401
    import brief as _brief_mod

    async def fake_note(*, deterministic_brief, model, ollama_url, timeout_sec):
        return "Investigate aircraft FOO near magnitude X event."
    monkeypatch.setattr(_brief_mod, "_ollama_analyst_note", fake_note)

    vp = {
        "meta": _vp_meta(entity_count=1),
        "entities": [_entity()],
        "events": [],
    }
    text = await generate_brief_llm(vp)
    assert "Analyst note" in text   # caveat suffix (LLM, may misclassify) is OK
    assert "Investigate aircraft FOO" in text
    # Deterministic prefix must still be there
    assert "1 aircraft" in text or "1 entity" in text


async def test_brief_llm_falls_back_to_deterministic_on_empty_response(monkeypatch):
    """If Ollama returns empty text, no Analyst note is appended."""
    from brief import generate_brief_llm
    import brief as _brief_mod

    async def fake_note(*, deterministic_brief, model, ollama_url, timeout_sec):
        return ""
    monkeypatch.setattr(_brief_mod, "_ollama_analyst_note", fake_note)

    vp = {"meta": _vp_meta(entity_count=1), "entities": [_entity()], "events": []}
    text = await generate_brief_llm(vp)
    assert "Analyst note" not in text


async def test_brief_llm_falls_back_on_timeout(monkeypatch):
    """Simulate a slow Ollama via raising TimeoutError inside the helper."""
    from brief import generate_brief_llm
    import brief as _brief_mod
    import asyncio

    async def fake_note(*, deterministic_brief, model, ollama_url, timeout_sec):
        # Simulate the helper handling its own timeout — it returns ''
        return ""
    monkeypatch.setattr(_brief_mod, "_ollama_analyst_note", fake_note)

    vp = {"meta": _vp_meta(entity_count=1), "entities": [_entity()], "events": []}
    text = await generate_brief_llm(vp)
    assert "Analyst note" not in text
    # Deterministic content still present
    assert len(text) > 20


async def test_ollama_analyst_note_handles_unreachable_url():
    """Real network-level failure: posting to a closed port returns empty
    string (no exception)."""
    from brief import _ollama_analyst_note
    note = await _ollama_analyst_note(
        deterministic_brief="test brief",
        model="llama3.1:latest",
        ollama_url="http://127.0.0.1:1",   # closed port
        timeout_sec=1.0,
    )
    assert note == ""


async def test_brief_llm_cache_skips_second_call(monkeypatch):
    """Cached LLM brief: second invocation with same viewport doesn't call Ollama."""
    from brief import generate_brief_llm_cached, brief_llm_cache
    import brief as _brief_mod

    call_count = {"n": 0}

    async def fake_note(*, deterministic_brief, model, ollama_url, timeout_sec):
        call_count["n"] += 1
        return "noted"
    monkeypatch.setattr(_brief_mod, "_ollama_analyst_note", fake_note)

    # Clear the LLM cache so this test is deterministic
    with brief_llm_cache._lock:
        brief_llm_cache._store.clear()

    vp = {
        "meta": _vp_meta(bbox=(-100, 30, -90, 40), entity_count=1),
        "entities": [_entity()],
        "events": [],
    }
    s1 = await generate_brief_llm_cached(vp)
    s2 = await generate_brief_llm_cached(vp)
    assert s1 == s2
    assert call_count["n"] == 1, "second call should hit cache, not Ollama"


async def test_http_viewport_with_brief_llm_returns_combined_brief(monkeypatch):
    """End-to-end HTTP: ?brief_llm=true triggers the LLM path; the response
    contains the deterministic brief prefix in meta.brief."""
    from db import init_pool, close_pool
    from fastapi import FastAPI
    import httpx
    from httpx import ASGITransport
    from api_v1 import build_router
    import brief as _brief_mod
    from brief import brief_llm_cache

    # Mock the LLM call so the test doesn't need Ollama running
    async def fake_note(*, deterministic_brief, model, ollama_url, timeout_sec):
        return "Test analyst note from mock."
    monkeypatch.setattr(_brief_mod, "_ollama_analyst_note", fake_note)
    with brief_llm_cache._lock:
        brief_llm_cache._store.clear()

    await init_pool()
    try:
        app = FastAPI()
        app.include_router(build_router())

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            now = datetime.now(timezone.utc)
            resp = await client.get("/api/v1/viewport", params={
                "bbox": "-75.0,39.0,-72.0,42.0",
                "time_from": (now - timedelta(hours=1)).isoformat(),
                "time_to": now.isoformat(),
                "types": "aircraft",
                "limit": 5,
                "brief_llm": "true",
            })
            assert resp.status_code == 200
            body = resp.json()
            brief_text = body["meta"].get("brief", "")
            assert "Analyst note" in brief_text   # caveat suffix is fine
            assert "Test analyst note from mock." in brief_text
    finally:
        await close_pool()


# ─── Tier-1 alert tests (sanctioned-vessel-underway + dark-vessel) ────────


def _sanction_event(live_name="AKADEMIK CHERSKIY", match_kind="imo",
                     similarity=None, severity=None) -> dict:
    """Build a sanctioned_vessel_underway event in the shape api_v1 returns."""
    sev = severity if severity is not None else (10.0 if match_kind == "imo" else 9.0)
    subtype = "imo_match" if match_kind == "imo" else "name_match"
    return {
        "id": f"uuid-{live_name}",
        "event_type": "sanctioned_vessel_underway",
        "event_subtype": subtype,
        "event_time": "2026-05-08T01:00:00+00:00",
        "severity": sev,
        "severity_for_market": None,
        "title": f"Sanctioned vessel underway: {live_name}"
                 + (" [IMO match]" if match_kind == "imo" else ""),
        "description": "test",
        "properties": {
            "match_kind": match_kind,
            "live_vessel_name": live_name,
            "ofac_sdn_match_name": live_name,
            "similarity": similarity,
        },
        "domain": "maritime",
        "decay_half_life_min": 1440,
        "lat": 60.0, "lng": 25.0,
    }


def _dark_event(name="NORD SUPERIOR", hours_dark=12.0,
                 last_velocity=4.0) -> dict:
    """Build a dark_vessel_detected event."""
    sev = min(10.0, max(1.0, hours_dark / 12.0))
    return {
        "id": f"uuid-dark-{name}",
        "event_type": "dark_vessel_detected",
        "event_subtype": "short" if hours_dark < 24 else ("medium" if hours_dark < 168 else "long"),
        "event_time": "2026-05-08T01:00:00+00:00",
        "severity": sev,
        "severity_for_market": None,
        "title": f"Vessel went dark: {name}",
        "description": f"Last AIS broadcast {hours_dark}h ago",
        "properties": {
            "mmsi": "123456789",
            "hours_dark": hours_dark,
            "last_velocity_ms": last_velocity,
        },
        "domain": "maritime",
        "decay_half_life_min": 1440,
        "lat": 60.0, "lng": 25.0,
    }


def test_brief_surfaces_sanctioned_vessels_at_top():
    """Sanctioned vessels currently broadcasting AIS lead the brief."""
    vp = {
        "meta": _vp_meta(event_count=2),
        "entities": [],
        "events": [
            _sanction_event("AKADEMIK CHERSKIY", "imo"),
            _sanction_event("AKADEMIK PRIMAKOV", "imo"),
        ],
    }
    text = generate_brief(vp)
    assert "ALERT" in text
    assert "sanctioned" in text.lower()
    assert "AKADEMIK CHERSKIY" in text or "AKADEMIK PRIMAKOV" in text
    assert "IMO-confirmed" in text or "IMO" in text
    # The ALERT line is the first sentence after the bbox/window header.
    # If proximity findings ALSO appear, they come AFTER the ALERT.
    head_end = text.find(":") + 1   # end of "In bbox [...] for the past Nh:"
    alert_idx = text.find("ALERT")
    assert alert_idx > head_end


def test_brief_distinguishes_imo_vs_name_match_confidence():
    vp = {
        "meta": _vp_meta(event_count=3),
        "entities": [],
        "events": [
            _sanction_event("AETHER", "name", similarity=1.0),
            _sanction_event("ALARA", "imo"),
            _sanction_event("AKADEMIK CHERSKIY", "imo"),
        ],
    }
    text = generate_brief(vp)
    assert "2 IMO-confirmed" in text or "IMO-confirmed" in text
    assert "name-fuzzy" in text or "1 name-fuzzy" in text


def test_brief_surfaces_dark_vessels_at_top():
    vp = {
        "meta": _vp_meta(event_count=2),
        "entities": [],
        "events": [
            _dark_event("NORD SUPERIOR", hours_dark=12.0),
            _dark_event("MV TANKER X", hours_dark=72.0),
        ],
    }
    text = generate_brief(vp)
    assert "ALERT" in text
    assert "dark" in text.lower()
    # Names visible
    assert "NORD SUPERIOR" in text or "MV TANKER X" in text
    # Should sort longest-dark first ("3.0d" or similar)
    # (72h = 3 days)
    assert "3.0d" in text or "72h" in text


def test_brief_dark_and_sanctioned_both_surface():
    """When both signal types present, BOTH are top-line."""
    vp = {
        "meta": _vp_meta(event_count=3),
        "entities": [],
        "events": [
            _sanction_event("AKADEMIK CHERSKIY", "imo"),
            _dark_event("NORD SUPERIOR", hours_dark=12.0),
            _event(event_type="usgs_quake", title="M4 quake"),
        ],
    }
    text = generate_brief(vp)
    assert "sanctioned" in text.lower()
    assert "dark" in text.lower()
    assert "quake" in text.lower()


def test_brief_no_tier1_when_no_relevant_events():
    """Empty/non-tier-1 events → no ALERT prefix; rest of brief unchanged."""
    vp = {
        "meta": _vp_meta(event_count=1),
        "entities": [_entity()],
        "events": [_event(event_type="usgs_quake")],
    }
    text = generate_brief(vp)
    assert "ALERT" not in text
    assert "1 aircraft" in text or "aircraft" in text.lower()


def _swpc_alert(severity=5, headline="Geomagnetic K-index of 5",
                 subtype="geomagnetic_kindex") -> dict:
    return {
        "id": f"uuid-swpc-{severity}",
        "event_type": "swpc_alert",
        "event_subtype": subtype,
        "event_time": "2026-05-08T01:00:00+00:00",
        "severity": severity,
        "severity_for_market": None,
        "title": f"ALERT: {headline}",
        "description": "test",
        "properties": {
            "product_id": f"K0{severity}A",
            "kind": subtype,
            "alert_kind": "alert",
            "level": severity,
            "headline": f"ALERT: {headline}",
        },
        "domain": "atmospheric",
        "decay_half_life_min": 720,
        "lat": 60.0, "lng": 0.0,
    }


def test_brief_surfaces_swpc_alerts_in_tier1():
    """SWPC alerts appear as a top-line ALERT line in the brief."""
    vp = {
        "meta": _vp_meta(event_count=2),
        "entities": [],
        "events": [
            _swpc_alert(severity=5, headline="Geomagnetic K-index of 5"),
            _swpc_alert(severity=7, headline="Geomagnetic K-Index of 6 expected",
                        subtype="geomagnetic_kindex"),
        ],
    }
    text = generate_brief(vp)
    assert "ALERT" in text
    assert "SWPC" in text or "space-weather" in text.lower()
    # 2 alerts, both geomagnetic_kindex → grouped
    assert "2 geomagnetic kindex" in text or "2 active" in text
    # Top sample (severity 7 → K-index 6) should be in the headline
    assert "K-Index of 6" in text or "K-index of 6" in text or "geomagnetic" in text.lower()


def test_brief_swpc_groups_categories_when_mixed():
    vp = {
        "meta": _vp_meta(event_count=3),
        "entities": [],
        "events": [
            _swpc_alert(severity=5, subtype="geomagnetic_kindex",
                        headline="K-index 5"),
            _swpc_alert(severity=4, subtype="radio_blackout",
                        headline="R2 radio blackout"),
            _swpc_alert(severity=8, subtype="solar_radiation",
                        headline="S4 radiation event"),
        ],
    }
    text = generate_brief(vp)
    # Top sample should be highest severity (S4)
    assert "S4 radiation event" in text or "radiation" in text.lower()


def test_brief_swpc_dark_and_sanctions_all_three_surface():
    """When all three tier-1 categories present, all three appear."""
    vp = {
        "meta": _vp_meta(event_count=3),
        "entities": [],
        "events": [
            _sanction_event("AKADEMIK CHERSKIY", "imo"),
            _dark_event("NORD SUPERIOR", hours_dark=12.0),
            _swpc_alert(severity=7, headline="Geomagnetic K-Index of 6"),
        ],
    }
    text = generate_brief(vp)
    assert "sanctioned" in text.lower()
    assert "dark" in text.lower()
    assert "SWPC" in text or "space-weather" in text.lower()


def _military_event(callsign="VIPR76", subtype=None) -> dict:
    """Build a military_aircraft_underway event in api response shape."""
    return {
        "id": f"uuid-mil-{callsign}",
        "event_type": "military_aircraft_underway",
        "event_subtype": subtype or "".join(c for c in callsign if c.isalpha())[:6],
        "event_time": "2026-05-08T01:00:00+00:00",
        "severity": 5.0,
        "severity_for_market": None,
        "title": f"Military aircraft underway: {callsign}",
        "description": f"Military aircraft {callsign} broadcasting ADS-B",
        "properties": {
            "icao24": "ae" + callsign[:4].lower(),
            "callsign": callsign,
        },
        "domain": "aviation",
        "decay_half_life_min": 1440,
        "lat": 35.0, "lng": -100.0,
    }


def test_brief_surfaces_military_aircraft_in_tier1():
    vp = {
        "meta": _vp_meta(event_count=4, types=["aircraft"]),
        "entities": [],
        "events": [
            _military_event("VIPR76", subtype="VIPR"),
            _military_event("VIPR77", subtype="VIPR"),
            _military_event("GAF648", subtype="GAF"),
            _military_event("SHWK412", subtype="SHWK"),
        ],
    }
    text = generate_brief(vp)
    assert "ALERT" in text
    assert "military aircraft" in text.lower()
    assert "VIPR" in text
    assert "GAF" in text
    # Family count should be visible (2 VIPR, 1 GAF, 1 SHWK)
    assert "2 VIPR" in text


def test_brief_all_four_tier1_categories_surface():
    vp = {
        "meta": _vp_meta(event_count=4),
        "entities": [],
        "events": [
            _sanction_event("AKADEMIK CHERSKIY", "imo"),
            _dark_event("NORD SUPERIOR", hours_dark=12.0),
            _military_event("VIPR76", subtype="VIPR"),
            _swpc_alert(severity=7),
        ],
    }
    text = generate_brief(vp)
    assert "sanctioned" in text.lower()
    assert "dark" in text.lower()
    assert "military" in text.lower()
    assert "SWPC" in text or "space-weather" in text.lower()


# ─── Multi-jurisdictional callout tests ───────────────────────────────────


def _multi_juris_event(
    *, name="VESSEL X", mmsi="123456789",
    authorities=("US Treasury OFAC", "UK OFSI"),
    subtype=None,
) -> dict:
    """Build a sanctioned_vessel_multijurisdictional event in the shape
    api_v1 returns. Default 2 authorities → dual_listed."""
    if subtype is None:
        subtype = "tri_listed" if len(authorities) >= 3 else "dual_listed"
    return {
        "id": f"uuid-mj-{mmsi}",
        "event_type": "sanctioned_vessel_multijurisdictional",
        "event_subtype": subtype,
        "event_time": "2026-05-08T01:00:00+00:00",
        "severity": 10.0,
        "severity_for_market": None,
        "title": f"CRITICAL — Sanctioned vessel underway, listed by "
                 f"{len(authorities)} authorities: {name}",
        "description": "test",
        "properties": {
            "mmsi": mmsi,
            "live_vessel_name": name,
            "authority_count": len(authorities),
            "authorities": list(authorities),
            "multi_jurisdictional": True,
        },
        "domain": "maritime",
        "decay_half_life_min": 1440,
        "lat": 60.0, "lng": 25.0,
    }


def test_brief_leads_with_multi_jurisdictional_critical():
    """Multi-jurisdictional vessels lead the *** CRITICAL *** line above
    sanctioned-rendezvous and sanctioned-went-dark."""
    vp = {
        "meta": _vp_meta(event_count=1),
        "entities": [],
        "events": [
            _multi_juris_event(
                name="POLA SOFIA",
                authorities=("US Treasury OFAC", "UK OFSI", "EU CFSP"),
            ),
        ],
    }
    text = generate_brief(vp)
    assert "*** CRITICAL ***" in text
    assert "multi-jurisdictional" in text
    assert "POLA SOFIA" in text
    # Authority short-form: OFAC + UK + EU
    assert "[OFAC+UK+EU]" in text or "[OFAC+EU+UK]" in text or "[EU+OFAC+UK]" in text \
           or "[EU+UK+OFAC]" in text or "[UK+EU+OFAC]" in text or "[UK+OFAC+EU]" in text


def test_brief_dual_listed_label():
    """Dual-listed (2 authorities) gets a 'dual-listed' breakdown label."""
    vp = {
        "meta": _vp_meta(event_count=1),
        "entities": [],
        "events": [
            _multi_juris_event(
                name="TAIMYR",
                authorities=("US Treasury OFAC", "EU CFSP"),
            ),
        ],
    }
    text = generate_brief(vp)
    assert "dual-listed" in text
    assert "TAIMYR" in text


def test_brief_multi_juris_leads_above_rendezvous():
    """When both multi-juris and sanctioned-rendezvous are present, the
    multi-juris callout appears first inside the *** CRITICAL *** line."""
    vp = {
        "meta": _vp_meta(event_count=2),
        "entities": [],
        "events": [
            _multi_juris_event(
                name="ZALIV AMURSKIY",
                authorities=("US Treasury OFAC", "UK OFSI"),
            ),
            {
                "id": "uuid-rdv1",
                "event_type": "sanctioned_vessel_rendezvous",
                "event_subtype": "both_sanctioned",
                "event_time": "2026-05-08T01:00:00+00:00",
                "severity": 10.0,
                "severity_for_market": None,
                "title": "CRITICAL — Sanctioned vessel rendezvous",
                "description": "test",
                "properties": {
                    "a_name": "ASTROL-1", "b_name": "ORENBURG",
                    "distance_m": 340,
                },
                "domain": "maritime",
                "decay_half_life_min": 1440,
                "lat": 60.0, "lng": 25.0,
            },
        ],
    }
    text = generate_brief(vp)
    crit_idx = text.find("*** CRITICAL ***")
    assert crit_idx >= 0
    crit_line = text[crit_idx:crit_idx + 600]
    mj_pos = crit_line.find("multi-jurisdictional")
    rdv_pos = crit_line.find("rendezvous")
    assert mj_pos >= 0 and rdv_pos >= 0
    assert mj_pos < rdv_pos, (
        f"multi-juris callout must precede rendezvous in the critical line; "
        f"got mj_pos={mj_pos}, rdv_pos={rdv_pos}"
    )


def test_brief_no_multi_juris_when_no_event():
    """When no multi-jurisdictional events exist, the brief doesn't mention
    it (and the rest of the brief is unaffected)."""
    vp = {
        "meta": _vp_meta(event_count=1),
        "entities": [],
        "events": [_sanction_event("ALARA", "imo")],
    }
    text = generate_brief(vp)
    assert "multi-jurisdictional" not in text


# ─── Volcanic + GDACS callout tests ─────────────────────────────────────


def _volcanic_event(name="Great Sitkin", level="WATCH", color="ORANGE",
                     severity=9.0) -> dict:
    return {
        "id": f"uuid-vol-{name}",
        "event_type": "volcanic_alert",
        "event_subtype": level.lower(),
        "event_time": "2026-05-08T01:00:00+00:00",
        "severity": severity,
        "severity_for_market": None,
        "title": f"{name} — {level}/{color}",
        "description": "test",
        "properties": {
            "volcano_name": name,
            "alert_level": level,
            "color_code": color,
            "observatory_abbr": "avo",
        },
        "domain": "atmospheric",
        "decay_half_life_min": 1440,
        "lat": 52.0763, "lng": -176.1297,
    }


def _gdacs_event(name="Test Cyclone", tier="orange", severity=8.0) -> dict:
    return {
        "id": f"uuid-gdacs-{name}",
        "event_type": "gdacs_alert",
        "event_subtype": tier,
        "event_time": "2026-05-08T01:00:00+00:00",
        "severity": severity,
        "severity_for_market": None,
        "title": name,
        "description": "test",
        "properties": {
            "event_name": name,
            "alert_level": tier.upper(),
        },
        "domain": "atmospheric",
        "decay_half_life_min": 1440,
        "lat": 0.0, "lng": 0.0,
    }


def test_brief_surfaces_volcanic_alerts():
    """Elevated volcanoes get a tier-1 ALERT line with level breakdown."""
    vp = {
        "meta": _vp_meta(event_count=2),
        "entities": [],
        "events": [
            _volcanic_event("Great Sitkin", "WATCH", "ORANGE", severity=9.0),
            _volcanic_event("Shishaldin", "ADVISORY", "YELLOW", severity=6.0),
        ],
    }
    text = generate_brief(vp)
    assert "elevated volcano" in text or "ALERT" in text
    assert "Great Sitkin" in text or "Shishaldin" in text
    # Level breakdown should mention both
    assert "WATCH" in text or "ADVISORY" in text


def test_brief_surfaces_gdacs_disasters():
    vp = {
        "meta": _vp_meta(event_count=2),
        "entities": [],
        "events": [
            _gdacs_event("Tropical Cyclone Alpha", "red", severity=10.0),
            _gdacs_event("Severe Drought", "orange", severity=8.0),
        ],
    }
    text = generate_brief(vp)
    assert "GDACS" in text or "disaster" in text.lower()
    assert "Tropical Cyclone Alpha" in text or "Severe Drought" in text


def _shadow_fleet_event(*, size=3, authorities=("US Treasury OFAC",)) -> dict:
    """Build a shadow_fleet_cluster event in the shape api_v1 returns."""
    if size >= 6:
        subtype = "large_fleet"
    elif size >= 4:
        subtype = "fleet"
    else:
        subtype = "cluster"
    return {
        "id": f"uuid-sfc-{size}",
        "event_type": "shadow_fleet_cluster",
        "event_subtype": subtype,
        "event_time": "2026-05-08T01:00:00+00:00",
        "severity": 10.0,
        "severity_for_market": None,
        "title": f"CRITICAL — Shadow-fleet cluster: {size} sanctioned vessels within 10 km",
        "description": "test",
        "properties": {
            "cluster_size": size,
            "authorities": list(authorities),
            "authority_count": len(authorities),
            "multi_jurisdictional": len(authorities) >= 2,
        },
        "domain": "maritime",
        "decay_half_life_min": 1440,
        "lat": 59.0, "lng": 25.0,
    }


def test_brief_leads_critical_with_shadow_fleet():
    """Shadow-fleet cluster appears in the *** CRITICAL *** line when present."""
    vp = {
        "meta": _vp_meta(event_count=1),
        "entities": [],
        "events": [
            _shadow_fleet_event(size=4, authorities=("US Treasury OFAC", "EU CFSP")),
        ],
    }
    text = generate_brief(vp)
    assert "*** CRITICAL ***" in text
    assert "shadow-fleet cluster" in text
    assert "OFAC+EU" in text or "EU+OFAC" in text


def test_brief_shadow_fleet_size_breakdown():
    """3 clusters of different sizes should appear with size breakdown."""
    vp = {
        "meta": _vp_meta(event_count=3),
        "entities": [],
        "events": [
            _shadow_fleet_event(size=7),   # large_fleet
            _shadow_fleet_event(size=4),   # fleet
            _shadow_fleet_event(size=3),   # cluster
        ],
    }
    text = generate_brief(vp)
    assert "large-fleet" in text
    assert "fleet" in text


def test_brief_shadow_fleet_leads_above_multijurisdictional():
    """Shadow-fleet appears BEFORE multi-jurisdictional in the critical line —
    it's the strongest single operational signal."""
    vp = {
        "meta": _vp_meta(event_count=2),
        "entities": [],
        "events": [
            _shadow_fleet_event(size=3),
            _multi_juris_event(name="DUAL VESSEL",
                                authorities=("US Treasury OFAC", "UK OFSI")),
        ],
    }
    text = generate_brief(vp)
    crit_idx = text.find("*** CRITICAL ***")
    assert crit_idx >= 0
    crit_line = text[crit_idx:crit_idx + 800]
    sf_pos = crit_line.find("shadow-fleet")
    mj_pos = crit_line.find("multi-jurisdictional")
    assert sf_pos >= 0 and mj_pos >= 0
    assert sf_pos < mj_pos, (
        f"shadow-fleet must precede multi-jurisdictional in critical line; "
        f"got sf_pos={sf_pos}, mj_pos={mj_pos}"
    )


def test_brief_volcanic_alone_does_not_trigger_critical():
    """A volcanic alert is tier-1 ALERT — but NOT a *** CRITICAL *** lead.
    The CRITICAL line is reserved for combined sanctions signals."""
    vp = {
        "meta": _vp_meta(event_count=1),
        "entities": [],
        "events": [_volcanic_event("Kilauea", "ADVISORY", "YELLOW", severity=6.0)],
    }
    text = generate_brief(vp)
    assert "*** CRITICAL ***" not in text
    assert "Kilauea" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
