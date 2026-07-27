"""
Unit tests for ingesters/gdelt_topical.py — the V2 quality-bar reference.

Covers the contract every new ingester must satisfy. The implementation has
been through three rewrites since first authored (2026-05-05): GDELT's
geo/geo endpoint went 404 so the ingester switched to /doc/doc article
shape, then consolidated into mega-queries to dodge GDELT's per-topic rate
limit, then routed through CF Worker proxy. Tests reflect THAT contract:

  - fetch() iterates `MEGA_QUERIES` (currently 3), calls _fetch_one_topic
    once per mega-query with slug "_megaquery_0/1/2"
  - normalize() consumes /doc/doc article shape (title, url, sourcecountry,
    domain, language) — NOT GeoJSON Feature shape
  - normalize() classifies each article into a topic by keyword-matching
    its title against TOPICS, then resolves the lat/lng from the article's
    sourcecountry via gdelt._COUNTRY_CENTROIDS
  - articles without a known sourcecountry, or with no topic keyword match,
    are dropped (intentional — broad-OR matches that aren't topic-relevant)
  - GLASSBOX_GDELT_TOPICAL_DISABLED env var filters in normalize() (since
    fetch() can't skip per-topic with mega-queries)

Run:
    21_GLASSBOX_AI/.venv/bin/python -m pytest 21_GLASSBOX_AI/tests/test_gdelt_topical.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingesters.gdelt_topical import GDELTTopicalIngester  # noqa: E402
from ingesters.base import GlassboxEvent  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

def _article(title: str, url: str, sourcecountry: str,
             domain: str = "example.com", language: str = "English") -> dict:
    """Build a single /doc/doc-shaped article record for tests.

    The new contract: each article comes from GDELT /doc/doc and has these
    fields. The classifier in normalize() reads `title` + `sourcecountry`.
    """
    return {
        "title": title,
        "url": url,
        "sourcecountry": sourcecountry,
        "domain": domain,
        "language": language,
        "_topic_slug": "_unclassified",   # what fetch() stamps in mega-query mode
        "_topic_severity_baseline": 5,
        "_topic_decay_min": 1440,
        "_topic_domain": "geo",
    }


# A terrorism-keyword article from Syria (Syria is in _COUNTRY_CENTROIDS)
SAMPLE_TERRORISM_ARTICLES = [
    _article(
        title="Damascus suicide bombing kills 12 — terrorism strikes capital",
        url="https://example.com/syria-bombing-2026",
        sourcecountry="Syria",
    ),
    _article(
        title="Insurgency strikes military convoy near Bogota",
        url="https://example.com/colombia-attack",
        sourcecountry="Colombia",
    ),
]

# Same URL as terrorism above — should dedup-collapse in normalize
SAMPLE_INFRASTRUCTURE_ARTICLES = [
    _article(
        title="Damascus suicide bombing damages bridge and pipeline explosion",
        url="https://example.com/syria-bombing-2026",
        sourcecountry="Syria",
    ),
]


# ─────────────────────────────────────────────────────────────────────
# Tests — pure logic (no I/O)
# ─────────────────────────────────────────────────────────────────────

class TestNormalize:
    def setup_method(self):
        self.ing = GDELTTopicalIngester()

    def test_normalize_single_topic_produces_events(self):
        """Two articles with terrorism keywords + known countries → 2 events."""
        events = self.ing.normalize(list(SAMPLE_TERRORISM_ARTICLES))

        assert len(events) == 2
        for ev in events:
            assert isinstance(ev, GlassboxEvent)
            assert ev.layer == "news"
            assert ev.kind == "alert"
            assert ev.source == "gdelt_v2_geo_topical"
            assert ev.payload["topic"] == "terrorism"
            assert ev.payload["topics_matched"] == ["terrorism"]
            assert ev.payload["url"].startswith("https://example.com/")
            assert ev.payload["headline"]      # populated from title
            assert ev.payload["country"] in ("Syria", "Colombia")

    def test_severity_uses_topic_baseline_and_caps_at_10(self):
        """Terrorism baseline = 8 in TOPICS. /doc/doc has no per-article mention
        density anymore so severity is the bare baseline (capped 0..10)."""
        events = self.ing.normalize(list(SAMPLE_TERRORISM_ARTICLES))
        # All terrorism-classified events get severity = baseline (8)
        for ev in events:
            assert ev.severity == 8

    def test_dedup_across_topics_collapses_same_url(self):
        """Same URL appears in 'terrorism' AND 'infrastructure' bundle. Note: the
        word 'pipeline explosion' in the second article actually maps to
        'oil_spills' under TOPICS keyword classification (it's the literal
        phrase listed there). The first keyword match wins → terrorism (because
        'terrorism' word appears earlier in title). Either way, same URL =
        single event."""
        raw = list(SAMPLE_TERRORISM_ARTICLES) + list(SAMPLE_INFRASTRUCTURE_ARTICLES)
        events = self.ing.normalize(raw)

        # Damascus URL appears twice → collapse. Plus Bogota = 2 events total.
        assert len(events) == 2
        # The Damascus event's topics_matched should include both topics
        damascus = next(
            (ev for ev in events if "Syria" in ev.payload.get("country", "")),
            None,
        )
        assert damascus is not None
        # Either {terrorism} (if both articles matched terrorism keyword) or
        # {terrorism, oil_spills} (if second article matched 'pipeline explosion'
        # which is in oil_spills TOPICS row). Both are valid dedup outcomes.
        assert "terrorism" in damascus.payload["topics_matched"]

    def test_unknown_country_skipped(self):
        """Article from a country not in _COUNTRY_CENTROIDS is dropped (no
        globe pin possible)."""
        bad = [_article(
            title="terrorism attack in Atlantis",
            url="https://x.com/a",
            sourcecountry="Atlantis",
        )]
        events = self.ing.normalize(bad)
        assert events == []

    def test_no_topic_keyword_match_skipped(self):
        """Article whose title contains no TOPICS keyword is dropped (the
        broad-OR mega-query catches noise that still lacks topical relevance)."""
        unrelated = [_article(
            title="Local bakery wins regional competition",
            url="https://x.com/cake",
            sourcecountry="France",
        )]
        events = self.ing.normalize(unrelated)
        assert events == []

    def test_empty_title_skipped(self):
        bad = [_article(title="", url="https://x.com/a", sourcecountry="Syria")]
        events = self.ing.normalize(bad)
        assert events == []

    def test_disabled_topic_filtered_in_normalize(self, monkeypatch):
        """Setting GLASSBOX_GDELT_TOPICAL_DISABLED=terrorism drops terrorism
        articles even if they match the keyword."""
        monkeypatch.setenv("GLASSBOX_GDELT_TOPICAL_DISABLED", "terrorism")
        ing = GDELTTopicalIngester()
        events = ing.normalize(list(SAMPLE_TERRORISM_ARTICLES))
        # Both seeds matched 'terrorism' but topic is disabled → 0 events
        assert events == []

    def test_helper_extract_headline_unchanged(self):
        """Legacy helper still works — used by other code paths."""
        ing = self.ing
        assert ing._extract_headline('<a href="x">My Headline</a> – source') == "My Headline"
        assert ing._extract_headline("") == ""
        assert ing._extract_headline("no anchor here") == ""

    def test_helper_extract_url_unchanged(self):
        ing = self.ing
        assert ing._extract_url('<a href="https://test.com/x">y</a>') == "https://test.com/x"
        assert ing._extract_url('<a href=\'https://test.com/x\'>y</a>') == "https://test.com/x"
        assert ing._extract_url("") == ""
        assert ing._extract_url("no anchor") == ""


# ─────────────────────────────────────────────────────────────────────
# Tests — fetch() with mocked HTTP (mega-query model)
# ─────────────────────────────────────────────────────────────────────

class TestFetchWithMockedHttp:
    """fetch() now iterates MEGA_QUERIES (3 OR'd queries that batch all
    topics) and calls _fetch_one_topic with slug='_megaquery_N'. Tests mock
    _fetch_one_topic to keep these offline + fast."""

    def test_fetch_iterates_all_megaqueries(self):
        ing = GDELTTopicalIngester()
        called: list = []

        async def fake_fetch_one(session, slug, query):
            called.append(slug)
            return [_article("terrorism news", "https://x.com/a", "Syria")]

        async def runner():
            with patch.object(ing, "_fetch_one_topic", side_effect=fake_fetch_one):
                ing.INTER_QUERY_GAP_MS = 0
                await ing.fetch()

        asyncio.run(runner())

        # Each MEGA_QUERIES entry produces one slug "_megaquery_<idx>"
        expected_slugs = [f"_megaquery_{i}" for i in range(len(ing.MEGA_QUERIES))]
        assert called == expected_slugs

    def test_fetch_respects_disabled_env_via_normalize(self, monkeypatch):
        """With mega-queries, fetch() can't skip per-topic. The disabled env
        instead filters at normalize() time — proven by the normalize-side
        test above. Here we just confirm that fetch() itself is unaffected
        and still hits all mega-queries even when the env is set."""
        monkeypatch.setenv("GLASSBOX_GDELT_TOPICAL_DISABLED", "famine,terrorism")
        ing = GDELTTopicalIngester()
        called: list = []

        async def fake_fetch_one(session, slug, query):
            called.append(slug)
            return []

        async def runner():
            with patch.object(ing, "_fetch_one_topic", side_effect=fake_fetch_one):
                ing.INTER_QUERY_GAP_MS = 0
                await ing.fetch()

        asyncio.run(runner())
        # Mega-queries still all fire — the disable lives at normalize().
        assert len(called) == len(ing.MEGA_QUERIES)

    def test_fetch_one_megaquery_failing_does_not_break_others(self):
        ing = GDELTTopicalIngester()

        async def flaky(session, slug, query):
            if slug == "_megaquery_1":
                raise asyncio.TimeoutError("simulated")
            return [_article("terrorism news", "https://x.com/" + slug, "Syria")]

        async def runner():
            with patch.object(ing, "_fetch_one_topic", side_effect=flaky):
                ing.INTER_QUERY_GAP_MS = 0
                items = await ing.fetch()
            return items

        items = asyncio.run(runner())

        # Failing mega-query shows up in per_topic_stats with error set
        assert ing.per_topic_stats["_megaquery_1"]["ok"] is False
        assert ing.per_topic_stats["_megaquery_1"]["error"] == "timeout"
        # Other mega-queries still produced items
        assert len(items) > 0

    def test_fetch_smoke_mode_runs_only_first_megaquery(self):
        """smoke_mode=True is the path the smoke test runner uses — must be
        fast (one mega-query, not all three) so the smoke pass stays under
        ~30s total across all ingesters."""
        ing = GDELTTopicalIngester(smoke_mode=True)
        called: list = []

        async def fake_fetch_one(session, slug, query):
            called.append(slug)
            return []

        async def runner():
            with patch.object(ing, "_fetch_one_topic", side_effect=fake_fetch_one):
                ing.INTER_QUERY_GAP_MS = 0
                await ing.fetch()

        asyncio.run(runner())
        assert called == ["_megaquery_0"]


# ─────────────────────────────────────────────────────────────────────
# Tests — status surface
# ─────────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_surfaces_per_megaquery(self):
        ing = GDELTTopicalIngester()
        ing.per_topic_stats["_megaquery_0"] = {"ok": True, "raw_count": 5, "error": None}
        st = ing.status()
        assert "per_topic" in st
        assert st["per_topic"]["_megaquery_0"]["raw_count"] == 5
        # topics_enabled / topics_disabled still reflect the TOPICS table
        assert "topics_enabled" in st
        assert "terrorism" in st["topics_enabled"]
        assert "topics_disabled" in st

    def test_status_disabled_topics_split_correctly(self, monkeypatch):
        monkeypatch.setenv("GLASSBOX_GDELT_TOPICAL_DISABLED", "famine")
        ing = GDELTTopicalIngester()
        st = ing.status()
        assert "famine" in st["topics_disabled"]
        assert "famine" not in st["topics_enabled"]
        assert "terrorism" in st["topics_enabled"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
