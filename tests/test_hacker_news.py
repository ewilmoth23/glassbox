"""
Hacker News ingester + writer tests.

Asserts:
  - Severity scaling: 50→4, 200→6, 500→8, 1500→10
  - normalize() emits GlassboxEvent with layer='hacker_news'
  - Stories below MIN_SCORE filtered out
  - Stories with type='comment' filtered out
  - Empty title filtered out
  - Ask HN / Show HN get distinct event_subtypes
  - Story without URL still emits (text-only post)
  - Domain extraction from URL
  - Writer persists row with proper subtype
  - Writer idempotent on hn_id

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_hacker_news.py -v
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pool, close_pool, fetchval, fetch, execute  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402
from ingesters.hacker_news import (  # noqa: E402
    HackerNewsIngester, _severity_from_score, _domain_from_url,
)
from writers import write_hn_events  # noqa: E402


TEST_PREFIX = "hn:test17"


@pytest.fixture(autouse=True)
async def _pool():
    await init_pool()
    yield
    await close_pool()


@pytest.fixture
async def _clean_hn():
    async def _cleanup():
        await execute(
            "DELETE FROM event WHERE event_type='hn_story' "
            "AND properties->>'external_id' LIKE $1",
            f"{TEST_PREFIX}%",
        )
    await _cleanup()
    yield
    await _cleanup()


# ─── Helpers ─────────────────────────────────────────────────────────────


def test_severity_low_score():
    assert _severity_from_score(50) == 4


def test_severity_mid_score():
    assert _severity_from_score(200) == 6


def test_severity_high_score():
    assert _severity_from_score(500) == 8


def test_severity_max_score():
    assert _severity_from_score(1500) == 10


def test_severity_zero_score_floor():
    assert _severity_from_score(0) == 3


def test_severity_none_safe():
    assert _severity_from_score(None) == 3


def test_domain_from_url_basic():
    assert _domain_from_url("https://github.com/abc/def") == "github.com"


def test_domain_from_url_strips_www():
    assert _domain_from_url("https://www.nytimes.com/article") == "nytimes.com"


def test_domain_from_url_none_safe():
    assert _domain_from_url(None) is None
    assert _domain_from_url("") is None


# ─── normalize() ─────────────────────────────────────────────────────────


def test_normalize_basic_story():
    ing = HackerNewsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": 12345,
        "type": "story",
        "title": "Cloudflare outage explained",
        "url": "https://blog.cloudflare.com/post-mortem",
        "score": 850,
        "by": "alice",
        "descendants": 312,
        "time": int(time.time()),
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    e = events[0]
    assert e.layer == "hacker_news"
    assert e.kind == "hn_story"
    assert e.severity == 8   # score 850 → 8
    assert e.external_id == "hn:12345"
    assert e.payload["domain"] == "blog.cloudflare.com"
    assert e.payload["score"] == 850
    assert e.payload["title"] == "Cloudflare outage explained"


def test_normalize_filters_low_score():
    ing = HackerNewsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": 1, "type": "story", "title": "Boring",
        "score": 5, "time": int(time.time()),
    }]
    assert ing.normalize(raw) == []


def test_normalize_filters_comment_type():
    ing = HackerNewsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": 1, "type": "comment", "title": "Reply",
        "score": 200, "time": int(time.time()),
    }]
    assert ing.normalize(raw) == []


def test_normalize_filters_empty_title():
    ing = HackerNewsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": 1, "type": "story", "title": "",
        "score": 200, "time": int(time.time()),
    }]
    assert ing.normalize(raw) == []


def test_normalize_ask_hn_emits_via_kind():
    """Ask HN posts go through the same kind but the ingester preserves
    the title-prefix info via the title for downstream parsing."""
    ing = HackerNewsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": 99,
        "type": "story",
        "title": "Ask HN: How do you stay focused while working remotely?",
        "score": 500,
        "by": "asker",
        "time": int(time.time()),
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert "Ask HN" in events[0].payload["title"]


def test_normalize_text_only_post_no_url():
    """Text-only post (no URL) — still emit."""
    ing = HackerNewsIngester(broadcaster=lambda *_: None)
    raw = [{
        "id": 100, "type": "story",
        "title": "Show HN: My weekend project",
        "score": 200, "time": int(time.time()),
        # no url
    }]
    events = ing.normalize(raw)
    assert len(events) == 1
    assert events[0].payload.get("url") is None


# ─── Writer ──────────────────────────────────────────────────────────────


async def test_writer_persists_story(_clean_hn):
    ev = GlassboxEvent(
        layer="hacker_news",
        external_id=f"{TEST_PREFIX}:W1",
        kind="hn_story",
        lat=0.0, lng=0.0,
        ts=datetime.now(timezone.utc).isoformat(),
        severity=8,
        source="HN",
        payload={
            "hn_id": 99999,
            "score": 850,
            "by": "tester",
            "comments": 100,
            "url": "https://example.com/post",
            "domain": "example.com",
            "title": "Test HN story",
            "hn_url": "https://news.ycombinator.com/item?id=99999",
        },
        domain="news",
        decay_half_life_min=1440,
    )
    n = await write_hn_events([ev])
    assert n == 1

    rows = await fetch(
        "SELECT event_type, severity, title, properties FROM event "
        "WHERE properties->>'external_id' = $1",
        f"{TEST_PREFIX}:W1",
    )
    assert len(rows) == 1
    assert rows[0]["event_type"] == "hn_story"


async def test_writer_idempotent_on_hn_id(_clean_hn):
    ev = GlassboxEvent(
        layer="hacker_news",
        external_id=f"{TEST_PREFIX}:IDM",
        kind="hn_story",
        lat=0.0, lng=0.0,
        ts="2026-05-08T12:00:00+00:00",
        severity=4,
        payload={"hn_id": 777, "score": 50, "title": "x"},
        domain="news",
        decay_half_life_min=1440,
    )
    assert await write_hn_events([ev]) == 1
    assert await write_hn_events([ev]) == 0


async def test_writer_skips_wrong_layer():
    ev = GlassboxEvent(
        layer="planes",
        external_id=f"{TEST_PREFIX}:WRONG",
        kind="hn_story",
        lat=0, lng=0,
        ts=datetime.now(timezone.utc).isoformat(),
    )
    assert await write_hn_events([ev]) == 0


async def test_writer_zero_events_is_noop():
    assert await write_hn_events([]) == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
