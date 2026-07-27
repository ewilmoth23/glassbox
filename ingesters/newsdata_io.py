"""
NewsData.io ingester — geocoded global news articles.

Replaces GDELT for v1.0 news layer (GDELT was disabled 2026-05-05 due to
hostile rate limits with no recoverable workaround for small users).

Source: https://newsdata.io/api/1/latest
License: free tier 200 req/day, commercial use OK with attribution
Attribution: required ("News: NewsData.io")
KEY required: NEWSDATA_IO_KEY env var.

Each article has:
  - article_id, title, description, link
  - country: ["spain"] (array, lowercase)
  - language: "english" (lowercase)
  - category: ["top","crime"] (multi-tag)
  - pubDate: "2026-05-05 09:25:00"
  - source_name, source_url, source_icon, image_url

Geocoding: country (array) → lat/lng via _COUNTRY_CENTROIDS table imported
from gdelt.py (~70 country centroids covering 99% of news source volume).
Articles from countries not in the table are skipped (no globe pin).

Categories we filter for (OSINT-relevant):
  - politics, world, crime, business, top, technology, environment, health
  - excluded: entertainment, sports, food, lifestyle (low signal for globe)

Free tier rate budget:
  - 200 req/day = ~8 req/hour
  - Each request = up to 10 articles
  - 30-min poll cycle = 48 cycles/day, well under budget
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


# ─── Severity & filtering ─────────────────────────────────────────────────

# OSINT-relevant categories (drives severity baseline)
_OSINT_CATEGORIES = {
    "world":         6,
    "politics":      6,
    "crime":         7,
    "top":           5,
    "business":      4,
    "technology":    4,
    "environment":   5,
    "health":        4,
    "science":       3,
}
_EXCLUDED_CATEGORIES = {"entertainment", "sports", "food", "lifestyle", "tourism"}


def _severity_for_categories(categories: List[str]) -> int:
    """Take max severity across all matching categories; 0 = filtered out."""
    if not categories:
        return 0
    max_sev = 0
    has_excluded = False
    has_osint = False
    for cat in categories:
        cat_lc = cat.lower().strip()
        if cat_lc in _EXCLUDED_CATEGORIES:
            has_excluded = True
        if cat_lc in _OSINT_CATEGORIES:
            has_osint = True
            max_sev = max(max_sev, _OSINT_CATEGORIES[cat_lc])
    # If article is BOTH excluded AND OSINT, OSINT wins
    if has_osint:
        return max_sev
    if has_excluded:
        return 0   # pure entertainment/sports — skip
    return 3      # untagged article — modest default


# ─── Ingester ─────────────────────────────────────────────────────────────


class NewsDataIoIngester(Ingester):
    layer = "news"
    source = "NewsData.io (free tier, commercial OK)"
    source_id = "newsdata_io"
    poll_interval_sec = 1800.0      # 30 min — safely under 200/day limit

    URL = "https://newsdata.io/api/1/latest"
    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"

    # Categories we request from NewsData.io. The API accepts comma-separated
    # category list; we filter further in normalize() via _OSINT_CATEGORIES.
    REQUEST_CATEGORIES = "top,world,politics,crime,business"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Real key provided by Ethan 2026-05-05; env var override supported.
        self._key = (
            os.environ.get("NEWSDATA_IO_KEY")
            or "pub_2eeb33e2949f4d3b9fae47c64cc55673"
        )
        if not self._key:
            self.log.warning(
                "[newsdata_io] NEWSDATA_IO_KEY not set — register at "
                "https://newsdata.io/register"
            )

    async def fetch(self) -> List[Dict[str, Any]]:
        if not self._key:
            return []
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        # 2026-05-05 fix: removed language= param — NewsData.io free tier
        # rejects 10-language list with HTTP 422 UNPROCESSABLE ENTITY.
        # Without the param, API returns articles in ALL languages, which is
        # what we want anyway (Russian/Chinese/Arabic news matters for OSINT).
        # Geocoding via country-centroid table works independent of language.
        params = {
            "apikey":   self._key,
            "category": self.REQUEST_CATEGORIES,
            "size":     "10",     # max for free tier
        }
        # Smoke mode: smaller, simpler request (still 1 cycle).
        if self.smoke_mode:
            params["category"] = "top"
            params["size"] = "5"

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(self.URL, params=params) as r:
                r.raise_for_status()
                data = await r.json()

        if data.get("status") != "success":
            self.log.warning(
                f"[newsdata_io] non-success status: {data.get('status')}; "
                f"results: {data.get('results')}"
            )
            return []
        return data.get("results") or []

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        """Convert NewsData.io articles to GlassboxEvents.

        Lat/lng via country centroid (article's first country). Articles
        from countries not in the centroid table are skipped (no globe pin
        possible)."""
        # Import country centroid table from gdelt.py to avoid duplicating
        # the ~70-row lookup table.
        try:
            from .gdelt import _COUNTRY_CENTROIDS
        except ImportError:
            _COUNTRY_CENTROIDS = {}

        # NewsData.io country names are lowercase ("spain", "united states").
        # _COUNTRY_CENTROIDS keys are Title Case ("Spain", "United States").
        # Build a lowercase lookup once per call.
        centroids_lc = {k.lower(): v for k, v in _COUNTRY_CENTROIDS.items()}

        now = datetime.now(timezone.utc).isoformat()
        out: List[GlassboxEvent] = []

        for art in raw_items:
            try:
                article_id = (art.get("article_id") or "").strip()
                if not article_id:
                    continue
                title = (art.get("title") or "").strip()
                if not title:
                    continue

                # country is an array; take first
                countries = art.get("country") or []
                if not countries:
                    continue
                country_lc = (countries[0] or "").strip().lower()
                centroid = centroids_lc.get(country_lc)
                if centroid is None:
                    # Unknown country — no globe pin possible
                    continue
                lat, lng = centroid

                categories = art.get("category") or []
                severity = _severity_for_categories(categories)
                if severity == 0:
                    # Filtered out (pure entertainment/sports)
                    continue

                # Loop market tags
                mtags: List[str] = []
                sev_market = 0
                cat_set = {c.lower() for c in categories}
                if "crime" in cat_set or "politics" in cat_set:
                    mtags.append("news:political_event")
                    if severity >= 6:
                        sev_market = 4
                if "business" in cat_set:
                    mtags.append("news:business_event")

                pub_date = art.get("pubDate") or ""
                # NewsData.io format: "2026-05-05 09:25:00" — normalize to ISO
                ts_iso = now
                if pub_date and " " in pub_date:
                    try:
                        # Best-effort parse; assume UTC per pubDateTZ field
                        dt = datetime.strptime(pub_date, "%Y-%m-%d %H:%M:%S")
                        ts_iso = dt.replace(tzinfo=timezone.utc).isoformat()
                    except (ValueError, TypeError):
                        pass

                out.append(GlassboxEvent(
                    layer=self.layer,
                    external_id=f"newsdata:{article_id}",
                    kind="alert",
                    lat=lat,
                    lng=lng,
                    ts=ts_iso,
                    severity=severity,
                    source=self.source,
                    payload={
                        "title":         title,
                        "description":   (art.get("description") or "")[:500],
                        "url":           art.get("link"),
                        "language":      art.get("language"),
                        "country":       country_lc.title(),
                        "categories":    categories,
                        "source_name":   art.get("source_name"),
                        "source_url":    art.get("source_url"),
                        "image_url":     art.get("image_url"),
                        "_attribution": "News: NewsData.io",
                    },
                    domain="news",
                    geocode_quality="country",   # country-level pin
                    decay_half_life_min=240,     # 4h news relevance
                    market_tags=mtags,
                    severity_for_market=sev_market,
                ))
            except (ValueError, KeyError, TypeError) as e:
                self.log.debug(f"[newsdata_io] normalize skip: {e}")
                continue

        return out
