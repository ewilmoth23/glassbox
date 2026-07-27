"""
Phase 2D — gdelt_topical.py dual-write to the `event` hypertable.

Asserts:
  - GlassboxEvent (layer='news') in → event row with event_type='gdelt_topical'
  - event_subtype = the matched topic ('terrorism', 'cyber_attack', etc.)
  - title = headline; description = country
  - properties carries topic, topics_matched, url, country, mentions
  - Re-running with same external_id is idempotent (deterministic uuid5)
  - Multiple distinct articles → all persist
  - Empty input → no-op, returns 0
  - End-to-end via GDELTTopicalIngester with db_writer hook (mocked HTTP)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_news_dual_write.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from ingesters.gdelt_topical import GDELTTopicalIngester  # noqa: E402
from writers import write_news_events  # noqa: E402


TEST_PREFIX = "test07"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_test_news():
    async def _cleanup():
        # Match by external_id prefix (covers explicitly-tagged tests) AND
        # by url containing TEST_PREFIX (covers the cycle-with-mocked-HTTP
        # test where external_id is a hash derived from the URL).
        await execute(
            "DELETE FROM event WHERE event_type = 'gdelt_topical' "
            "AND (properties->>'external_id' LIKE $1 "
            "  OR properties->>'url' LIKE $2)",
            f"%{TEST_PREFIX}%",
            f"%{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


def _news_event(external_id: str, *, lat: float = 33.5, lng: float = 36.3,
                topic: str = "terrorism", country: str = "Syria",
                headline: str = "Damascus suicide bombing kills 12",
                url: str = None, severity: int = 8) -> GlassboxEvent:
    """Build a GlassboxEvent in the shape GDELTTopicalIngester.normalize() emits."""
    if url is None:
        url = f"https://example.com/{external_id}"
    payload = {
        "topic": topic,
        "topics_matched": [topic],
        "headline": headline,
        "url": url,
        "country": country,
        "language": "English",
        "domain_name": "example.com",
        "social_image": "",
        "mentions": 1,
    }
    return GlassboxEvent(
        layer="news",
        external_id=f"gdelt_topical:{external_id}",
        kind="alert",
        lat=lat,
        lng=lng,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=severity,
        source="gdelt_v2_geo_topical",
        payload=payload,
        domain="geo",
        geocode_quality="country",
        decay_half_life_min=720,
    )


# ─── Tests ────────────────────────────────────────────────────────────────


async def test_write_news_events_creates_event_row(_clean_test_news):
    """One news event → one row with event_type=gdelt_topical, event_subtype=topic."""
    ev = _news_event(f"{TEST_PREFIX}_terror_1", topic="terrorism",
                     country="Syria", headline="Damascus suicide bombing kills 12",
                     url="https://example.com/syria-blast")
    written = await write_news_events([ev])
    assert written == 1

    row = await fetch(
        "SELECT event_type, event_subtype, severity, "
        "       title, description, "
        "       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng, "
        "       properties, domain, decay_half_life_min "
        "FROM event WHERE properties->>'external_id' = $1",
        f"gdelt_topical:{TEST_PREFIX}_terror_1",
    )
    assert len(row) == 1
    r = row[0]
    assert r["event_type"] == "gdelt_topical"
    assert r["event_subtype"] == "terrorism"
    assert r["title"] == "Damascus suicide bombing kills 12"
    assert r["description"] == "Syria"
    assert abs(r["lat"] - 33.5) < 1e-4
    assert abs(r["lng"] - 36.3) < 1e-4
    assert r["severity"] == pytest.approx(8.0)
    assert r["domain"] == "geo"
    assert r["decay_half_life_min"] == 720

    import json
    props = r["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["topic"] == "terrorism"
    assert props["topics_matched"] == ["terrorism"]
    assert props["url"] == "https://example.com/syria-blast"
    assert props["country"] == "Syria"
    assert props["mentions"] == 1


async def test_write_news_events_is_idempotent(_clean_test_news):
    """Same external_id re-submitted → 0 new rows (deterministic uuid5 + ON CONFLICT)."""
    ev = _news_event(f"{TEST_PREFIX}_dedup")
    n1 = await write_news_events([ev])
    assert n1 == 1
    n2 = await write_news_events([ev])
    assert n2 == 0

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' = $1",
        f"gdelt_topical:{TEST_PREFIX}_dedup",
    )
    assert total == 1


async def test_write_news_events_multiple_distinct(_clean_test_news):
    """Five distinct articles → 5 rows."""
    evs = [
        _news_event(
            f"{TEST_PREFIX}_multi{i}",
            url=f"https://example.com/{TEST_PREFIX}{i}",
        )
        for i in range(5)
    ]
    n = await write_news_events(evs)
    assert n == 5

    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'external_id' LIKE $1",
        f"gdelt_topical:{TEST_PREFIX}_multi%",
    )
    assert total == 5


async def test_write_news_events_zero_events_is_noop():
    n = await write_news_events([])
    assert n == 0


async def test_write_news_events_skips_non_news_layer(_clean_test_news):
    """Defensive: passing an aircraft event must not corrupt the event table."""
    bogus = [GlassboxEvent(
        layer="planes",
        external_id="ae012a",
        kind="position",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
    )]
    n = await write_news_events(bogus)
    assert n == 0


async def test_write_news_events_skips_empty_external_id(_clean_test_news):
    """No external_id → can't dedup → skip."""
    ev = _news_event("")
    ev.external_id = ""  # Force empty
    n = await write_news_events([ev])
    assert n == 0


async def test_write_news_events_preserves_topics_matched_array(_clean_test_news):
    """payload.topics_matched is a list (potentially multiple topics for same URL).
    Must round-trip through jsonb intact."""
    ev = _news_event(f"{TEST_PREFIX}_multi_topic")
    ev.payload["topics_matched"] = ["terrorism", "infrastructure"]
    await write_news_events([ev])
    row = await fetch(
        "SELECT properties FROM event WHERE properties->>'external_id' = $1",
        f"gdelt_topical:{TEST_PREFIX}_multi_topic",
    )
    import json
    props = row[0]["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert sorted(props["topics_matched"]) == ["infrastructure", "terrorism"]


async def test_full_news_cycle_with_db_writer_hook(_clean_test_news):
    """End-to-end: GDELTTopicalIngester with db_writer hook, mocked HTTP.
    Confirms the wiring works in the cycle() path."""
    db_writer_calls = []

    async def capture_writer(events):
        db_writer_calls.append(list(events))
        return await write_news_events(events)

    broadcast_log = []
    def noop_b(events):
        if isinstance(events, list):
            broadcast_log.extend(events)
        else:
            broadcast_log.append(events)

    ingester = GDELTTopicalIngester(broadcaster=noop_b, db_writer=capture_writer)

    # Mock _fetch_one_topic to return a single article shaped like /doc/doc
    fake_articles = [{
        "title": "Damascus suicide bombing kills 12 — terrorism strikes capital",
        "url": f"https://example.com/{TEST_PREFIX}_cycle",
        "sourcecountry": "Syria",
        "language": "English",
        "domain": "example.com",
    }]

    async def fake_fetch_one(session, slug, query):
        return fake_articles

    with patch.object(ingester, "_fetch_one_topic", side_effect=fake_fetch_one):
        ingester.INTER_QUERY_GAP_MS = 0
        broadcast_count = await ingester.cycle()

    assert broadcast_count == 1
    assert len(db_writer_calls) == 1
    assert len(db_writer_calls[0]) == 1

    # Confirm the row landed
    total = await fetchval(
        "SELECT count(*) FROM event WHERE properties->>'url' LIKE $1",
        f"%{TEST_PREFIX}_cycle%",
    )
    assert total == 1

    status = ingester.status()
    assert status["db_write_enabled"] is True
    assert status["last_db_write_count"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
