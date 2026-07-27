"""
Phase 2 round 2 (2026-05-09) — dual-write coverage for the remaining 7
ingesters: Bluesky / NewsData.io / NASA DONKI / NOAA Aviation Weather (METAR)
/ WAQI air quality / NASA NEO / SEC EDGAR.

For each writer asserts:
  - one event in → one row with expected event_type + subtype
  - re-write is idempotent (deterministic UUID5 + ON CONFLICT DO NOTHING)
  - non-matching layer is silently skipped
  - empty input returns 0

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_phase2_round2_dual_write.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from writers import (  # noqa: E402
    write_social_events,
    write_newsdata_events,
    write_donki_events,
    write_metar_events,
    write_aqi_events,
    write_neo_events,
    write_sec_filing_events,
)


TEST_PREFIX = "p2r2"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE properties->>'external_id' LIKE $1",
            f"%{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Bluesky ──────────────────────────────────────────────────────────────


def _bluesky_event(suffix: str = "1") -> GlassboxEvent:
    return GlassboxEvent(
        layer="social_bluesky",
        external_id=f"bsky:did:plc:{TEST_PREFIX}{suffix}:rkey{suffix}",
        kind="event",
        lat=51.5, lng=-0.12,
        ts=_now(),
        severity=6,
        source="Bluesky Jetstream",
        payload={
            "did": f"did:plc:{TEST_PREFIX}{suffix}",
            "rkey": f"rkey{suffix}",
            "text": "Breaking: explosion reported in central London",
            "lang": "en",
            "_attribution": "Post via Bluesky / ATProto firehose",
        },
        domain="social",
        decay_half_life_min=120,
    )


async def test_bluesky_writer_creates_row(_clean):
    n = await write_social_events([_bluesky_event("a")])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype, severity FROM event "
        "WHERE properties->>'external_id' = $1",
        f"bsky:did:plc:{TEST_PREFIX}a:rkeya",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "bluesky_post"
    assert rows[0]["event_subtype"] == "explosion"
    assert rows[0]["severity"] == pytest.approx(6.0)


async def test_bluesky_writer_idempotent(_clean):
    ev = _bluesky_event("b")
    assert await write_social_events([ev]) == 1
    assert await write_social_events([ev]) == 0


async def test_bluesky_writer_skips_wrong_layer(_clean):
    bogus = GlassboxEvent(layer="planes", external_id="ae012a",
                          kind="position", lat=0, lng=0, ts=_now())
    assert await write_social_events([bogus]) == 0


async def test_bluesky_writer_empty():
    assert await write_social_events([]) == 0


# ─── NewsData ─────────────────────────────────────────────────────────────


def _newsdata_event(suffix: str = "1") -> GlassboxEvent:
    return GlassboxEvent(
        layer="news",
        external_id=f"newsdata:{TEST_PREFIX}article{suffix}",
        kind="alert",
        lat=40.4, lng=-3.7,
        ts=_now(),
        severity=5,
        source="NewsData.io",
        payload={
            "title":       "Madrid summit concludes with new climate accord",
            "description": "EU and Latin American leaders signed a binding pact today.",
            "url":         f"https://example.com/{TEST_PREFIX}{suffix}",
            "language":    "english",
            "country":     "Spain",
            "categories":  ["politics", "environment"],
            "source_name": "Example News",
            "_attribution": "News: NewsData.io",
        },
        domain="geo",
        decay_half_life_min=720,
    )


async def test_newsdata_writer_creates_row(_clean):
    n = await write_newsdata_events([_newsdata_event("c")])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype, title FROM event "
        "WHERE properties->>'external_id' = $1",
        f"newsdata:{TEST_PREFIX}articlec",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "newsdata"
    assert rows[0]["event_subtype"] == "politics"
    assert "Madrid" in rows[0]["title"]


async def test_newsdata_writer_idempotent(_clean):
    ev = _newsdata_event("d")
    assert await write_newsdata_events([ev]) == 1
    assert await write_newsdata_events([ev]) == 0


async def test_newsdata_writer_skips_gdelt_topical(_clean):
    """If a GDELT topical event leaks into this writer, must be skipped (other writer owns it)."""
    bogus = GlassboxEvent(
        layer="news",
        external_id=f"gdelt_topical:{TEST_PREFIX}gdelt",
        kind="alert", lat=0, lng=0, ts=_now(),
        payload={"title": "X", "url": "https://example.com/x"},
    )
    assert await write_newsdata_events([bogus]) == 0


async def test_newsdata_writer_empty():
    assert await write_newsdata_events([]) == 0


# ─── DONKI ────────────────────────────────────────────────────────────────


def _donki_event(suffix: str = "1", ev_type: str = "FLR") -> GlassboxEvent:
    return GlassboxEvent(
        layer="space_weather",
        external_id=f"donki:{ev_type}:{TEST_PREFIX}flr{suffix}",
        kind="watch",
        lat=0.0, lng=0.0,
        ts=_now(),
        severity=6,
        source="NASA DONKI",
        payload={
            "event_type": ev_type,
            "title": f"Solar flare X1.{suffix}",
            "link": f"https://kauai.ccmc.gsfc.nasa.gov/donki/view/?id={suffix}",
            "class": f"X1.{suffix}",
            "source_location": "S20W30",
            "_attribution": "Space weather: NASA DONKI",
        },
        domain="space",
        decay_half_life_min=720,
    )


async def test_donki_writer_creates_row(_clean):
    n = await write_donki_events([_donki_event("e", "FLR")])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype FROM event "
        "WHERE properties->>'external_id' = $1",
        f"donki:FLR:{TEST_PREFIX}flre",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "donki"
    assert rows[0]["event_subtype"] == "FLR"


async def test_donki_writer_idempotent(_clean):
    ev = _donki_event("f")
    assert await write_donki_events([ev]) == 1
    assert await write_donki_events([ev]) == 0


async def test_donki_writer_skips_swpc(_clean):
    """Defensive: SWPC alerts (kind='swpc_alert') must not be picked up by DONKI writer."""
    bogus = GlassboxEvent(
        layer="space_weather",
        external_id=f"swpc:{TEST_PREFIX}",
        kind="swpc_alert",
        lat=0, lng=0, ts=_now(),
    )
    assert await write_donki_events([bogus]) == 0


async def test_donki_writer_empty():
    assert await write_donki_events([]) == 0


# ─── METAR ────────────────────────────────────────────────────────────────


def _metar_event(suffix: str = "1", flight_rules: str = "VFR") -> GlassboxEvent:
    return GlassboxEvent(
        layer="metar",
        external_id=f"metar:{TEST_PREFIX}{suffix}",
        kind="state",
        lat=40.6413, lng=-73.7781,
        ts=_now(),
        severity=2,
        source="NOAA AWC",
        payload={
            "icao":         f"K{suffix.upper()}YZ"[:4],
            "flight_rules": flight_rules,
            "wind_dir":     180,
            "wind_speed":   12,
            "raw_metar":    f"K{suffix.upper()} 091200Z 18012KT 10SM CLR 22/10 A3001",
            "_attribution": "Aviation weather: NOAA AWC (US public domain)",
        },
        domain="geo",
        decay_half_life_min=60,
    )


async def test_metar_writer_creates_row(_clean):
    n = await write_metar_events([_metar_event("g", "VFR")])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype FROM event "
        "WHERE properties->>'external_id' = $1",
        f"metar:{TEST_PREFIX}g",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "metar"
    assert rows[0]["event_subtype"] == "VFR"


async def test_metar_writer_idempotent_per_ts(_clean):
    """Same external_id + same ts = idempotent. Different ts = new row (history)."""
    ev = _metar_event("h")
    assert await write_metar_events([ev]) == 1
    assert await write_metar_events([ev]) == 0


async def test_metar_writer_skips_wrong_layer(_clean):
    bogus = GlassboxEvent(layer="news", external_id="x",
                          kind="alert", lat=0, lng=0, ts=_now())
    assert await write_metar_events([bogus]) == 0


async def test_metar_writer_empty():
    assert await write_metar_events([]) == 0


# ─── WAQI air quality ─────────────────────────────────────────────────────


def _aqi_event(suffix: str = "1", aqi: int = 75) -> GlassboxEvent:
    return GlassboxEvent(
        layer="air_quality",
        external_id=f"waqi:{TEST_PREFIX}{suffix}",
        kind="state",
        lat=39.9, lng=116.4,
        ts=_now(),
        severity=3,
        source="WAQI",
        payload={
            "aqi":           aqi,
            "station_name":  f"Beijing US Embassy {suffix}",
            "station_time":  None,
            "_attribution":  "Air Quality: aqicn.org (WAQI Project)",
        },
        domain="env",
        decay_half_life_min=60,
    )


async def test_aqi_writer_creates_row(_clean):
    n = await write_aqi_events([_aqi_event("i", 75)])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype FROM event "
        "WHERE properties->>'external_id' = $1",
        f"waqi:{TEST_PREFIX}i",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "aqi_reading"
    assert rows[0]["event_subtype"] == "moderate"


async def test_aqi_writer_severity_buckets(_clean):
    cases = [(25, "good"), (75, "moderate"), (125, "unhealthy_sensitive"),
             (175, "unhealthy"), (250, "very_unhealthy"), (350, "hazardous")]
    for i, (aqi, expected) in enumerate(cases):
        await write_aqi_events([_aqi_event(f"bucket{i}", aqi)])
    rows = await fetch(
        "SELECT event_subtype FROM event "
        "WHERE properties->>'external_id' LIKE $1 ORDER BY event_subtype",
        f"waqi:{TEST_PREFIX}bucket%",
    )
    subtypes = sorted(r["event_subtype"] for r in rows)
    assert subtypes == sorted([e for _, e in cases])


async def test_aqi_writer_idempotent_per_ts(_clean):
    ev = _aqi_event("j")
    assert await write_aqi_events([ev]) == 1
    assert await write_aqi_events([ev]) == 0


async def test_aqi_writer_empty():
    assert await write_aqi_events([]) == 0


# ─── NASA NEO ─────────────────────────────────────────────────────────────


def _neo_event(suffix: str = "1", hazardous: bool = False) -> GlassboxEvent:
    return GlassboxEvent(
        layer="neo_asteroids",
        external_id=f"neo:{TEST_PREFIX}{suffix}",
        kind="watch",
        lat=0.0, lng=0.0,
        ts=_now(),
        severity=4 if hazardous else 2,
        source="NASA NEO",
        payload={
            "name":            f"({TEST_PREFIX}{suffix}) Asteroid",
            "hazardous":       hazardous,
            "diameter_m_avg":  140.5,
            "miss_km":         5_000_000,
            "miss_lunar":      13.0,
            "rel_velocity_kmh": 50000.0,
            "orbiting_body":   "Earth",
            "_attribution":    "Asteroid data: NASA NEO Web Service (JPL)",
        },
        domain="space",
        decay_half_life_min=4320,
    )


async def test_neo_writer_creates_row(_clean):
    n = await write_neo_events([_neo_event("k", hazardous=True)])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype FROM event "
        "WHERE properties->>'external_id' = $1",
        f"neo:{TEST_PREFIX}k",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "neo_close_approach"
    assert rows[0]["event_subtype"] == "hazardous"


async def test_neo_writer_normal_subtype(_clean):
    await write_neo_events([_neo_event("l", hazardous=False)])
    rows = await fetch(
        "SELECT event_subtype FROM event "
        "WHERE properties->>'external_id' = $1",
        f"neo:{TEST_PREFIX}l",
    )
    assert rows[0]["event_subtype"] == "normal"


async def test_neo_writer_idempotent(_clean):
    ev = _neo_event("m")
    assert await write_neo_events([ev]) == 1
    assert await write_neo_events([ev]) == 0


async def test_neo_writer_empty():
    assert await write_neo_events([]) == 0


# ─── SEC EDGAR ────────────────────────────────────────────────────────────


def _sec_event(suffix: str = "1", form: str = "8-K") -> GlassboxEvent:
    return GlassboxEvent(
        layer="securities_filings",
        external_id=f"sec:{TEST_PREFIX}{suffix}",
        kind="watch",
        lat=0.0, lng=0.0,
        ts=_now(),
        severity=5 if form == "8-K" else 3,
        source="SEC EDGAR",
        payload={
            "form":  form,
            "title": f"{form} - Acme Corp ({TEST_PREFIX}{suffix})",
            "link":  f"https://www.sec.gov/Archives/edgar/data/{suffix}",
            "_attribution": "Securities filings: SEC EDGAR (US public domain)",
        },
        domain="entity",
        decay_half_life_min=720,
    )


async def test_sec_writer_creates_row(_clean):
    n = await write_sec_filing_events([_sec_event("n", "8-K")])
    assert n == 1
    rows = await fetch(
        "SELECT event_type, event_subtype FROM event "
        "WHERE properties->>'external_id' = $1",
        f"sec:{TEST_PREFIX}n",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "sec_filing"
    assert rows[0]["event_subtype"] == "8-K"


async def test_sec_writer_multiple_forms(_clean):
    forms = ["8-K", "10-Q", "10-K", "S-1"]
    for i, f in enumerate(forms):
        await write_sec_filing_events([_sec_event(f"forms{i}", f)])
    rows = await fetch(
        "SELECT event_subtype FROM event "
        "WHERE properties->>'external_id' LIKE $1 ORDER BY event_subtype",
        f"sec:{TEST_PREFIX}forms%",
    )
    assert sorted(r["event_subtype"] for r in rows) == sorted(forms)


async def test_sec_writer_idempotent(_clean):
    ev = _sec_event("o")
    assert await write_sec_filing_events([ev]) == 1
    assert await write_sec_filing_events([ev]) == 0


async def test_sec_writer_empty():
    assert await write_sec_filing_events([]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
