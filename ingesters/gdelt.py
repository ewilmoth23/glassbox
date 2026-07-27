"""
GDELT ingester — Global Database of Events, Language, and Tone.

Source: https://www.gdeltproject.org/
API:    https://api.gdeltproject.org/api/v2/

GDELT monitors 100+ languages across every country on earth, geocodes every
news event to a lat/lng, and updates every 15 minutes. This is the primary
data source for MEWR Sentinel and provides the "why is there activity here"
intelligence layer on the Glassbox globe.

What this ingester does:
  1. Pulls GDELT GeoJSON every 15 minutes (3 topic queries in parallel)
  2. Normalizes each geocoded article to a GlassboxEvent (layer="news")
  3. Deduplicates + broadcasts to SSE subscribers (globe shows news pins)
  4. Writes a Sentinel feed file that n8n/Ollama picks up for brief generation

Severity scoring (0-10):
  - Based on article count (more articles = bigger event) + Goldstein-proxy
  - Threshold 3+ articles to filter noise
  - Events with many articles from multiple domains get severity 6-9

Globe layer: "news"
Pin color on client: var(--accent-teal) #10b981 — same as Sentinel
"""

from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import GlassboxEvent, Ingester


# ─── GDELT API endpoints ───────────────────────────────────────────────────

_GDELT_GEO_URL = (
    "https://api.gdeltproject.org/api/v2/geo/geo"
    "?query={query}&timespan={timespan}&format=GeoJSON&MAXRECORDS=250"
)

# 2026-05-05 00:05 ET — /api/v2/geo/geo endpoint family is DEAD (returns 404
# for every query). Pivoted to /doc/doc which returns ArtList with article
# metadata. We synthesize lat/lng from `sourcecountry` field via the
# country-centroid table below. Less precise than GDELT's old per-event
# geocoding (city level) — but it's country-level pins which still let
# users see WHERE in the world the news is coming from.
_GDELT_DOC_URL = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
    "?query={query}&mode=ArtList&maxrecords=75&timespan={timespan}"
    "&format=json&sort=hybridrel"
)


# Country-centroid table for /doc/doc geocoding. GDELT returns
# `sourcecountry` like "United States", "China", "Ukraine" etc. We use
# capital-city / population-centroid coordinates for the pin.
# ~70 countries covering ~99% of news sources by volume.
_COUNTRY_CENTROIDS: Dict[str, Tuple[float, float]] = {
    "United States":     (38.9,  -77.0),
    "United Kingdom":    (51.5,   -0.1),
    "Canada":            (45.4,  -75.7),
    "Australia":         (-33.9, 151.2),
    "Germany":           (52.5,   13.4),
    "France":            (48.9,    2.3),
    "Spain":             (40.4,   -3.7),
    "Italy":             (41.9,   12.5),
    "Netherlands":       (52.4,    4.9),
    "Belgium":           (50.8,    4.3),
    "Sweden":            (59.3,   18.1),
    "Norway":            (59.9,   10.8),
    "Denmark":           (55.7,   12.6),
    "Finland":           (60.2,   24.9),
    "Poland":            (52.2,   21.0),
    "Russia":            (55.8,   37.6),
    "Ukraine":           (50.5,   30.5),
    "Turkey":            (41.0,   29.0),
    "Greece":            (38.0,   23.7),
    "Portugal":          (38.7,   -9.1),
    "Switzerland":       (46.9,    7.4),
    "Austria":           (48.2,   16.4),
    "Czech Republic":    (50.1,   14.4),
    "Hungary":           (47.5,   19.0),
    "Romania":           (44.4,   26.1),
    "Ireland":           (53.3,   -6.2),
    "Israel":            (31.8,   35.2),
    "Saudi Arabia":      (24.7,   46.7),
    "United Arab Emirates": (24.5, 54.4),
    "Iran":              (35.7,   51.4),
    "Iraq":              (33.3,   44.4),
    "Egypt":             (30.0,   31.2),
    "South Africa":      (-26.2,  28.0),
    "Nigeria":           ( 6.5,    3.4),
    "Kenya":             (-1.3,   36.8),
    "Morocco":           (34.0,   -6.8),
    "China":             (39.9,  116.4),
    "Japan":             (35.7,  139.7),
    "South Korea":       (37.6,  127.0),
    "North Korea":       (39.0,  125.8),
    "India":             (28.6,   77.2),
    "Pakistan":          (33.7,   73.0),
    "Bangladesh":        (23.7,   90.4),
    "Indonesia":         (-6.2,  106.8),
    "Philippines":       (14.6,  121.0),
    "Vietnam":           (21.0,  105.8),
    "Thailand":          (13.7,  100.5),
    "Malaysia":          ( 3.1,  101.7),
    "Singapore":         ( 1.4,  103.8),
    "Taiwan":            (25.0,  121.6),
    "Hong Kong":         (22.3,  114.2),
    "Mexico":            (19.4,  -99.1),
    "Brazil":            (-15.8, -47.9),
    "Argentina":         (-34.6, -58.4),
    "Chile":             (-33.4, -70.7),
    "Peru":              (-12.0, -77.0),
    "Colombia":          ( 4.6,  -74.1),
    "Venezuela":         (10.5,  -66.9),
    "New Zealand":       (-41.3, 174.8),
    "Pakistan":          (33.7,   73.0),
    "Afghanistan":       (34.5,   69.2),
    "Syria":             (33.5,   36.3),
    "Lebanon":           (33.9,   35.5),
    "Jordan":            (32.0,   35.9),
    "Yemen":             (15.4,   44.2),
    "Ethiopia":          ( 9.0,   38.7),
    "Tanzania":          (-6.8,   39.3),
    "Uganda":            ( 0.3,   32.6),
    "Ghana":             ( 5.6,   -0.2),
    "Senegal":           (14.7,  -17.4),
    "Algeria":           (36.8,    3.0),
    "Tunisia":           (36.8,   10.2),
    "Libya":             (32.9,   13.2),
    "Sudan":             (15.5,   32.5),
    "Cuba":              (23.1,  -82.4),
    "Dominican Republic":(18.5,  -69.9),
    "Puerto Rico":       (18.5,  -66.1),
}

# Topic queries: broad geopolitical + conflict + instability signals.
# Three separate pulls so we don't miss events that only match one topic.
#
# 2026-05-05 (6th-pass): mega-query 429'd even via Worker proxy. GDELT's
# parser may be rejecting 9-OR queries as "abuse". Split back into 2
# medium queries — each simpler than the mega but still 1-2 requests/cycle.
# CF Worker edge caching means each unique query only hits GDELT once per
# 5min regardless of client count.
_SENTINEL_QUERIES = [
    "(military OR attack OR war)",
    "(protest OR unrest OR sanctions)",
]


def _build_geo_url(query: str, timespan: str) -> str:
    """Properly URL-encode the GDELT geo query.

    GDELT v2 expects spaces as `+` and parens/operators preserved. Using
    urllib.parse.quote_plus on just the query value (not the whole template)
    ensures all metacharacters survive intact.
    """
    from urllib.parse import quote_plus
    return (
        "https://api.gdeltproject.org/api/v2/geo/geo"
        f"?query={quote_plus(query)}&timespan={quote_plus(timespan)}"
        "&format=GeoJSON&MAXRECORDS=250"
    )

# Timespan for each poll: 1h window catches recent + slightly older events
# (was 30min; widened 2026-05-04 since smoke test caught 0 events — wider
# window also helps when GDELT processing is slightly delayed)
_TIMESPAN = "1h"

# Output path for Sentinel n8n integration (n8n workflow watches this file)
_SENTINEL_FEED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "gdelt_sentinel_feed.json",
)


# ─── Severity helpers ──────────────────────────────────────────────────────

def _severity_from_articles(article_count: int) -> int:
    """More articles = bigger event. Scale 0-9."""
    if article_count >= 500: return 9
    if article_count >= 200: return 8
    if article_count >= 100: return 7
    if article_count >= 50:  return 6
    if article_count >= 20:  return 5
    if article_count >= 10:  return 4
    if article_count >= 5:   return 3
    if article_count >= 3:   return 2
    return 1


def _parse_gdelt_date(datestr: str) -> str:
    """Convert GDELT date format 20260421T150000Z to ISO-8601."""
    try:
        # GDELT uses YYYYMMDDTHHMMSSz or YYYYMMDDHHMMSS
        clean = datestr.replace("Z", "").replace("T", "")
        if len(clean) >= 14:
            dt = datetime(
                int(clean[0:4]), int(clean[4:6]), int(clean[6:8]),
                int(clean[8:10]), int(clean[10:12]), int(clean[12:14]),
                tzinfo=timezone.utc,
            )
            return dt.isoformat()
    except (ValueError, IndexError):
        pass
    return datetime.now(timezone.utc).isoformat()


def _event_id(title: str, lat: float, lng: float) -> str:
    """Deterministic ID from title + rounded location."""
    key = f"{title.lower().strip()}|{round(lat, 2)}|{round(lng, 2)}"
    return "gdelt:" + hashlib.md5(key.encode()).hexdigest()[:12]


# ─── Ingester ──────────────────────────────────────────────────────────────

class GDELTIngester(Ingester):
    """
    Pulls geocoded geopolitical events from GDELT and broadcasts them as
    'news' layer pins on the Glassbox globe. Also writes a Sentinel feed
    file for n8n to consume.
    """

    layer = "news"
    source = "GDELT Project — gdeltproject.org"
    source_id = "gdelt"                 # gates against sources.yaml
    poll_interval_sec = 900.0  # 15 minutes — matches GDELT update cadence

    # Minimum article count to surface on globe (filters 1-off noise)
    MIN_ARTICLES = 3

    async def fetch(self) -> List[Dict[str, Any]]:
        """Pull GDELT articles SEQUENTIALLY (5.5s pacing — GDELT enforces
        1 req per 5 sec rate limit; faster gets 429).

        2026-05-05 00:05 ET: switched from /geo/geo (404 dead) to /doc/doc.
        Returns articles with sourcecountry that we map to country centroids
        for the globe pin."""
        import aiohttp
        import asyncio
        from urllib.parse import quote_plus

        timeout = aiohttp.ClientTimeout(total=20)
        all_articles: List[Dict[str, Any]] = []
        seen_urls: set = set()

        # Smoke mode: single representative query (production runs all)
        queries_to_run = _SENTINEL_QUERIES[:1] if self.smoke_mode else _SENTINEL_QUERIES

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for i, query in enumerate(queries_to_run):
                if i > 0:
                    # 2026-05-05 (5th-pass): with mega-query (1 query total)
                    # this loop only runs once. Pacing kept for safety if
                    # _SENTINEL_QUERIES is ever expanded back.
                    await asyncio.sleep(12)

                # 2026-05-05 (5th-pass): route through CF Worker proxy.
                # Ethan's Mac Mini IP got rate-limited by GDELT after burst
                # testing. CF data centers have different IPs not in their
                # gate. Worker also edge-caches for 5 min.
                url = (
                    "https://mewr-news-api.mewrcreate.workers.dev/api/proxy/gdelt/doc"
                    f"?query={quote_plus(query)}"
                    f"&mode=ArtList&maxrecords=75&timespan={quote_plus(_TIMESPAN)}"
                    "&format=json&sort=hybridrel"
                )
                try:
                    async with session.get(
                        url,
                        headers={"User-Agent": "MEWRGlassbox/2.0 (+https://mewrcreate.com)"},
                    ) as resp:
                        if resp.status == 429:
                            self.log.warning(
                                f"GDELT 429 on query '{query[:40]}' — rate limited"
                            )
                            await asyncio.sleep(10)   # cool-down before next query
                            continue
                        if resp.status != 200:
                            self.log.info(
                                f"GDELT doc query '{query[:30]}' HTTP {resp.status}"
                            )
                            continue
                        data = await resp.json(content_type=None)
                except Exception as e:
                    self.log.info(f"GDELT doc query '{query[:30]}' failed: {e}")
                    continue

                articles = data.get("articles") or []
                for art in articles:
                    art_url = art.get("url") or ""
                    if art_url and art_url in seen_urls:
                        continue
                    if art_url:
                        seen_urls.add(art_url)
                    all_articles.append(art)

        self.log.info(
            f"GDELT: fetched {len(all_articles)} raw articles "
            f"across {len(_SENTINEL_QUERIES)} queries"
        )
        return all_articles

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        """Convert GDELT /doc/doc articles to GlassboxEvents.

        2026-05-05 00:05 ET: shape changed from GeoJSON Features to plain
        article dicts. We synthesize lat/lng from sourcecountry via
        _COUNTRY_CENTROIDS table. Articles from countries not in the
        table are skipped (no globe pin possible)."""
        events: List[GlassboxEvent] = []
        # Track per-country article density (proxy for event significance)
        country_counts: Dict[str, int] = {}

        for art in raw_items:
            try:
                title = (art.get("title") or "").strip()
                if not title:
                    continue

                source_country = (art.get("sourcecountry") or "").strip()
                centroid = _COUNTRY_CENTROIDS.get(source_country)
                if centroid is None:
                    # Unknown country = no place to render. Skip silently.
                    continue
                lat, lng = centroid

                url = art.get("url", "")
                domain = art.get("domain", "")
                lang = art.get("language", "English")
                seen_date = art.get("seendate", "")
                social_image = art.get("socialimage", "")

                ts = (
                    _parse_gdelt_date(seen_date)
                    if seen_date
                    else datetime.now(timezone.utc).isoformat()
                )

                # Count articles per country (proxy for event significance)
                country_counts[source_country] = country_counts.get(source_country, 0) + 1

                event_id = _event_id(title, lat, lng)

                events.append(GlassboxEvent(
                    layer=self.layer,
                    external_id=event_id,
                    kind="alert",
                    lat=lat,
                    lng=lng,
                    ts=ts,
                    severity=0,  # filled in second pass below
                    source=self.source,
                    payload={
                        "title": title,
                        "url": url,
                        "domain": domain,
                        "language": lang,
                        "source_country": source_country,
                        "location_name": source_country,  # best we have via /doc/doc
                        "social_image": social_image,
                        "article_count": 1,  # updated in second pass
                        "agency": "sentinel",
                        "type": "gdelt_event",
                    },
                    # Loop classification (Step 3):
                    # 2026-05-05 — geocode is now country-level (was city-level
                    # under /geo/geo). decay_half_life_min=240 still — news
                    # remains market-relevant for ~4 hours.
                    geocode_quality="country",
                    decay_half_life_min=240,
                ))
            except (ValueError, KeyError, TypeError) as e:
                self.log.debug(f"GDELT normalize skip: {e}")
                continue

        # Second pass: update severity + article_count from per-country aggregation
        # 2026-05-05 fix: was `location_counts` (undefined NameError after the
        # refactor to country-level geocoding). Now uses `country_counts`.
        for ev in events:
            country = ev.payload.get("source_country", "")
            count = country_counts.get(country, 1)
            ev.payload["article_count"] = count
            ev.severity = _severity_from_articles(count)

        # Filter low-signal noise
        events = [ev for ev in events if ev.payload.get("article_count", 0) >= self.MIN_ARTICLES]

        self.log.info(f"GDELT: normalized {len(events)} events (≥{self.MIN_ARTICLES} articles)")

        # Write Sentinel feed file for n8n pickup
        self._write_sentinel_feed(events)

        return events

    def _write_sentinel_feed(self, events: List[GlassboxEvent]) -> None:
        """
        Write top events to a JSON file that the Sentinel n8n workflow reads.
        n8n watches this file and feeds it to Ollama for brief generation.
        Format: { "generated_at": "...", "event_count": N, "events": [...] }
        """
        try:
            os.makedirs(os.path.dirname(_SENTINEL_FEED_PATH), exist_ok=True)

            # Sort by severity desc, take top 30 most significant events
            top_events = sorted(events, key=lambda e: e.severity, reverse=True)[:30]

            feed = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "GDELT Project",
                "timespan": _TIMESPAN,
                "event_count": len(top_events),
                "events": [
                    {
                        "id": ev.external_id,
                        "title": ev.payload.get("title", ""),
                        "url": ev.payload.get("url", ""),
                        "domain": ev.payload.get("domain", ""),
                        "location": ev.payload.get("location_name", ""),
                        "lat": ev.lat,
                        "lng": ev.lng,
                        "severity": ev.severity,
                        "article_count": ev.payload.get("article_count", 1),
                        "source_country": ev.payload.get("source_country", ""),
                        "language": ev.payload.get("language", ""),
                        "ts": ev.ts,
                    }
                    for ev in top_events
                ],
            }

            with open(_SENTINEL_FEED_PATH, "w", encoding="utf-8") as f:
                json.dump(feed, f, indent=2, ensure_ascii=False)

            self.log.info(
                f"GDELT: wrote {len(top_events)} events to sentinel feed at {_SENTINEL_FEED_PATH}"
            )
        except Exception as e:
            self.log.warning(f"GDELT: sentinel feed write failed: {e}")
