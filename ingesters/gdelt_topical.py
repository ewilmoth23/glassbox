"""
GDELT Topical Ingester — consolidates ~12 direct-browser-side GDELT queries
into one server-side ingester with consistent dedup, classification, broadcast,
and diagnostic surfacing.

V1 (still live in glassbox.html as of 2026-05-03):
    Browser called https://api.gdeltproject.org/api/v2/geo/geo separately for
    nuclear, data_centers, oil_spills, mining, drugs, human_trafficking,
    deforestation, border_crisis, terrorism, infrastructure, embassy, famine
    — 12 separate fetch() calls per page load PER VISITOR. Rate-limited per
    visitor IP. CORS-fragile. No persistence. No deduplication across queries
    (the same event appearing in two topical queries gets rendered twice).

V2 (this ingester):
    Mac Mini owns ALL GDELT topical fetching. Runs every 15 minutes (matches
    GDELT's own update cadence — no point polling faster). Each event gets
    classified by topic + severity baseline. Dedup is shared across topics
    via the standard Ingester base-class machinery, so an event matching
    both `terrorism` and `infrastructure` queries fires ONCE with both topics
    in payload.

Output shape:
    GlassboxEvent(
        layer="news",                       # existing layer key — no frontend changes needed
        external_id="gdelt:<eid>",          # GDELT event id
        kind="alert",
        lat=..., lng=...,
        severity=topic_severity_baseline + (mention_density_modifier),
        ts="...",
        source="gdelt_v2_geo",
        payload={
            "topic": "terrorism",                # primary topic that matched
            "topics_matched": ["terrorism"],     # all topics that matched
            "headline": "...",                   # first GDELT headline
            "url": "...",                        # source article
            "country": "...",                    # 2-letter ISO
            "mentions": int,                     # GDELT mention count
            "tone": float,                       # GDELT tone score (-100 to +100)
        },
    )

Relationship to existing `gdelt.py`:
    `gdelt.py` runs general-news GDELT queries (the broad query). THIS module
    runs topical/themed queries on a separate cadence. Events flow into the
    same "news" layer. The `topic` payload field distinguishes them so the
    frontend can chip/filter if it wants to.

Operator notes:
    No credentials required — GDELT is open. Rate limit is generous
    (~1 req/sec/IP from the Mac Mini's IP) but we batch our 12 queries
    sequentially with a small gap to be polite.

    To disable individual topics, comment them out of TOPICS below or set
    GLASSBOX_GDELT_TOPICAL_DISABLED env var to a comma-list of slugs:
        export GLASSBOX_GDELT_TOPICAL_DISABLED="famine,deforestation"
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .base import GlassboxEvent, Ingester


class GDELTTopicalIngester(Ingester):
    layer = "news"
    source = "gdelt_v2_geo_topical"
    source_id = "gdelt_topical"         # gates against sources.yaml
    poll_interval_sec = 900  # 15 min — GDELT publishes every 15 min

    GDELT_BASE = "https://api.gdeltproject.org/api/v2/geo/geo"
    MAX_ROWS_PER_TOPIC = 50
    PER_QUERY_TIMEOUT_SEC = 25.0
    # 2026-05-05 (third pass): 10s pacing STILL had every topical query 429.
    # Bumped to 12s — matches new gdelt.py pacing. The cycle gets longer
    # (6 × 12s = 72s + query latency = ~90-100s total) but reliability matters
    # more than speed for v1.0. poll_interval is 900s (15 min).
    INTER_QUERY_GAP_MS = 12000

    # (slug, query_string, severity_baseline_0_to_10, decay_half_life_minutes, market_domain)
    #
    # 2026-05-05 (4th pass): CONSOLIDATED from 6 sequential queries → ONE
    # mega-query. GDELT's rate limit gate is so aggressive that even 12s
    # pacing 429'd 5/6 topics. ONE big OR-query covers all topics in a
    # single request — well within ANY rate limit.
    #
    # Topic classification happens in normalize() via keyword matching against
    # article titles (we lose strict per-topic counts but gain reliability).
    # Each TOPICS entry below still feeds the keyword classifier — same
    # phrases used to bucket articles by topic at the normalize step.
    TOPICS: List[Tuple[str, str, int, int, str]] = [
        ("terrorism",          '(terrorism OR insurgency OR "suicide attack")',               8, 720,  "geo"),
        ("nuclear",            '("nuclear incident" OR "radiation leak")',                    7, 1440, "geo"),
        ("cyber_attack",       '(cyberattack OR ransomware OR "data breach")',                6, 720,  "tech"),
        ("oil_spills",         '("oil spill" OR "pipeline explosion")',                       6, 1440, "geo"),
        ("famine",             '(famine OR "food crisis")',                                   7, 2880, "geo"),
        ("central_bank",       '("central bank" OR "rate decision" OR "currency crisis")',    6, 4320, "macro"),
    ]

    # 2026-05-05 (6th-pass): mega-query 429'd even via Worker proxy. GDELT's
    # parser rejects very long OR queries. Split into 3 medium-sized queries
    # that the parser accepts. CF Worker edge caches each one for 5 min.
    MEGA_QUERIES = [
        '(terrorism OR insurgency OR "suicide attack")',
        '(cyberattack OR ransomware OR "data breach")',
        '("oil spill" OR famine OR "central bank")',
    ]
    # Backwards-compat alias for any code referencing the singular MEGA_QUERY
    MEGA_QUERY = MEGA_QUERIES[0]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Honor disabled list from env
        disabled_raw = os.environ.get("GLASSBOX_GDELT_TOPICAL_DISABLED", "")
        self._disabled = {
            slug.strip().lower() for slug in disabled_raw.split(",") if slug.strip()
        }
        # Track per-topic last cycle stats so /api/glassbox/diagnostic can show them
        self.per_topic_stats: Dict[str, Dict[str, Any]] = {}

    # ─────────────────────────────────────────────────────────────────
    # Fetch
    # ─────────────────────────────────────────────────────────────────

    async def fetch(self) -> List[Dict[str, Any]]:
        """ONE consolidated mega-query against GDELT /doc/doc.

        2026-05-05 (4th-pass refactor): GDELT's rate limit gate is so
        aggressive that 6 sequential queries with 12s pacing still 429'd
        5/6. By collapsing to a single OR'd query we make 1 request per
        cycle — well within ANY rate limit. Topic classification then
        happens in normalize() via keyword matching on article titles.

        Returns flat list of article dicts. Each gets `_topic_slug` set
        to "_unclassified" here; normalize() sets it to actual matching topic.
        """
        all_items: List[Dict[str, Any]] = []
        timeout = aiohttp.ClientTimeout(total=self.PER_QUERY_TIMEOUT_SEC)

        # Smoke mode: single representative mega-query (production runs all 3)
        queries_to_run = self.MEGA_QUERIES[:1] if self.smoke_mode else self.MEGA_QUERIES

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for mq_idx, mega_query in enumerate(queries_to_run):
                if mq_idx > 0:
                    # Pacing between sub-queries — Worker caches each, but
                    # if cache miss happens GDELT still rate-limits.
                    await asyncio.sleep(self.INTER_QUERY_GAP_MS / 1000.0)

                stat_key = f"_megaquery_{mq_idx}"
                stat: Dict[str, Any] = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "ok": False,
                    "raw_count": 0,
                    "error": None,
                }
                self.per_topic_stats[stat_key] = stat

                try:
                    items = await self._fetch_one_topic(
                        session, stat_key, mega_query
                    )
                    for it in items:
                        it["_topic_slug"] = "_unclassified"
                        it["_topic_severity_baseline"] = 5
                        it["_topic_decay_min"] = 1440
                        it["_topic_domain"] = "geo"
                    all_items.extend(items)
                    stat["ok"] = True
                    stat["raw_count"] = len(items)
                    self.log.info(
                        f"gdelt_topical[{mq_idx}]: {len(items)} articles"
                    )
                except asyncio.TimeoutError:
                    stat["error"] = "timeout"
                    self.log.warning(
                        f"gdelt_topical[{mq_idx}] timeout after {self.PER_QUERY_TIMEOUT_SEC}s"
                    )
                except aiohttp.ClientResponseError as e:
                    stat["error"] = f"http_{e.status}"
                    self.log.warning(
                        f"gdelt_topical[{mq_idx}] HTTP {e.status}: {e.message}"
                    )
                except Exception as e:
                    stat["error"] = f"{type(e).__name__}: {e}"
                    self.log.warning(f"gdelt_topical[{mq_idx}] failed: {e}")

        return all_items

    async def _fetch_one_topic(
        self,
        session: aiohttp.ClientSession,
        slug: str,
        query: str,
    ) -> List[Dict[str, Any]]:
        """Fetch one topical query as ARTICLES (no per-event geo).

        2026-05-05 00:08 ET: GDELT /api/v2/geo/geo endpoint family is DEAD
        (returns 404 for every query). Switched to /doc/doc which returns
        articles with sourcecountry. Caller's normalize() synthesizes
        lat/lng from country centroids in gdelt.py's _COUNTRY_CENTROIDS.
        """
        # 2026-05-05 (5th-pass): route through CF Worker proxy. Ethan's IP
        # got rate-limited by GDELT — CF data center IPs aren't in the gate.
        DOC_URL = "https://mewr-news-api.mewrcreate.workers.dev/api/proxy/gdelt/doc"
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "timespan": "1d",
            "maxrecords": str(self.MAX_ROWS_PER_TOPIC),
            "sort": "hybridrel",
        }
        headers = {
            "User-Agent": "Glassbox/2.0 (+https://mewrcreate.com/glassbox; contact: hello@mewrcreate.com)",
        }
        async with session.get(DOC_URL, params=params, headers=headers, raise_for_status=True) as resp:
            # 2026-05-05 fix: GDELT sometimes returns 200 OK with EMPTY body
            # when rate-limited (instead of 429). aiohttp's .json() then
            # throws "Expecting value: line 1 column 1 (char 0)". Pull text
            # first, check it parses, then try JSON.
            import json as _json
            text = await resp.text()
            if not text or text.strip() == "":
                # Empty body = silent rate limit. Surface as warning so
                # operator sees that GDELT is throttling silently (not just
                # via 429s — they sometimes return 200 with empty body).
                self.log.warning(f"gdelt_topical[{slug}] empty body (silent rate limit?)")
                return []
            try:
                data = _json.loads(text)
            except _json.JSONDecodeError:
                self.log.warning(
                    f"gdelt_topical[{slug}] non-JSON body (preview: {text[:80]!r})"
                )
                return []
        # GDELT /doc/doc returns {"articles": [...]} OR empty {} when no results
        articles = data.get("articles", []) if isinstance(data, dict) else []
        # Stamp each article with the topic slug for the topic_matched logic in normalize()
        for art in articles:
            art["_topic_slug"] = slug
        return articles

    # ─────────────────────────────────────────────────────────────────
    # Normalize
    # ─────────────────────────────────────────────────────────────────

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        """Convert raw GDELT GeoJSON features to GlassboxEvents.

        GDELT's pointdata mode returns features like:
            {
              "type": "Feature",
              "geometry": {"type": "Point", "coordinates": [lng, lat]},
              "properties": {
                "name": "Country/City",
                "count": 12,            # mention count
                "shareimage": "...url...",
                "html": "<a href=...>Headline</a>",
                ...
              }
            }
        """
        events: List[GlassboxEvent] = []
        # Aggregate same article across topics (same URL appearing in two
        # topical queries collapses to one event with topics_matched listing both).
        by_id: Dict[str, GlassboxEvent] = {}

        # Import country centroid table from the general gdelt module
        # (avoids duplicating the 70-country lookup table here)
        try:
            from .gdelt import _COUNTRY_CENTROIDS
        except ImportError:
            _COUNTRY_CENTROIDS = {}

        # Build keyword-classification table from TOPICS for post-fetch
        # topic assignment (the mega-query lost per-topic categorization).
        # Each entry: lowercase phrase → (slug, sev_base, decay_min, domain)
        _topic_keywords: List[Tuple[str, str, int, int, str]] = []
        import re as _re
        for slug_def, query_def, sev_def, decay_def, domain_def in self.TOPICS:
            # Extract quoted phrases first ("suicide attack" etc.)
            for m in _re.finditer(r'"([^"]+)"', query_def):
                _topic_keywords.append(
                    (m.group(1).lower(), slug_def, sev_def, decay_def, domain_def)
                )
            # Strip quoted phrases + parens, then split on `OR` boundaries.
            # Using a word-boundary-anchored regex split handles trailing-OR
            # cases like `(a OR b OR "X")` where removing "X" leaves a dangling
            # `OR ` that simple `split(" OR ")` would attach to the previous
            # token (turning `b` into `b OR`).
            stripped = _re.sub(r'"[^"]+"', '', query_def)
            stripped = _re.sub(r'[()]', ' ', stripped)
            for term in _re.split(r'\bOR\b', stripped, flags=_re.IGNORECASE):
                term = term.strip()
                if term and len(term) > 3:
                    _topic_keywords.append(
                        (term.lower(), slug_def, sev_def, decay_def, domain_def)
                    )

        for art in raw_items:
            try:
                # 2026-05-05 (4th-pass): mega-query input. Classify topic
                # via keyword match against title.
                title_lc = (art.get("title") or "").lower()
                matched: Optional[Tuple[str, int, int, str]] = None
                for kw, ks, kv, kd, kdom in _topic_keywords:
                    if kw in title_lc:
                        matched = (ks, kv, kd, kdom)
                        break

                if matched:
                    slug, sev_base, decay_min, domain = matched
                else:
                    # Title didn't match any topic keyword. Skip — these are
                    # broad-OR matches that snuck through GDELT's relevance
                    # filter but aren't topic-relevant for our globe.
                    continue

                # Honor GLASSBOX_GDELT_TOPICAL_DISABLED — set in __init__ from env.
                # Operators expect setting this to drop those topics from output.
                # With mega-queries, fetch() can't easily skip per-topic, so the
                # filter happens here at normalize-time on the matched slug.
                if slug in self._disabled:
                    continue

                source_country = (art.get("sourcecountry") or "").strip()
                centroid = _COUNTRY_CENTROIDS.get(source_country)
                if centroid is None:
                    # Unknown country = no globe pin possible
                    continue
                lat, lng = centroid

                url = (art.get("url") or "").strip()
                title = (art.get("title") or "").strip()
                if not title:
                    continue

                eid = self._synth_id(url, source_country, slug)

                # Severity baseline from topic; no per-article mention count
                # in /doc/doc shape, so we just use base severity.
                severity = min(10, sev_base)

                if eid in by_id:
                    ev = by_id[eid]
                    if slug not in ev.payload["topics_matched"]:
                        ev.payload["topics_matched"].append(slug)
                    if severity > ev.severity:
                        ev.severity = severity
                    continue

                ev = GlassboxEvent(
                    layer=self.layer,
                    external_id=f"gdelt_topical:{eid}",
                    kind="alert",
                    lat=lat,
                    lng=lng,
                    ts=datetime.now(timezone.utc).isoformat(),
                    severity=severity,
                    source=self.source,
                    payload={
                        "topic": slug,                          # primary
                        "topics_matched": [slug],               # may grow during this loop
                        "headline": title,
                        "url": url,
                        "country": source_country,
                        "language": art.get("language", ""),
                        "domain_name": art.get("domain", ""),
                        "social_image": art.get("socialimage", ""),
                        "mentions": 1,                          # /doc/doc gives per-article, not cluster
                    },
                    # Loop classification hints
                    domain=domain,
                    decay_half_life_min=decay_min,
                    geocode_quality="country",  # /doc/doc gives source country, not event location
                )
                by_id[eid] = ev
            except Exception as e:
                # Don't let one malformed feature kill the whole batch
                self.log.warning(f"gdelt_topical normalize: skipping feature ({e})")
                continue

        events = list(by_id.values())
        return events

    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────

    def _extract_headline(self, html: str) -> str:
        """GDELT 'html' field is like '<a href=URL>Headline</a> – source'.
        Pull out the inner text between the first <a>...</a>."""
        if not html:
            return ""
        try:
            i1 = html.find(">")
            i2 = html.find("</a>", i1)
            if i1 != -1 and i2 != -1 and i2 > i1:
                return html[i1 + 1 : i2].strip()
        except Exception:
            pass
        return ""

    def _extract_url(self, html: str) -> str:
        """Pull the first href out of the 'html' field."""
        if not html:
            return ""
        try:
            i1 = html.find('href=')
            if i1 == -1:
                return ""
            quote_char = html[i1 + 5]
            i2 = html.find(quote_char, i1 + 6)
            if i2 == -1:
                return ""
            return html[i1 + 6 : i2]
        except Exception:
            return ""

    def _synth_id(self, url: str, name: str, slug: str) -> str:
        """Stable id across cycles + across topics so dedup collapses correctly.
        URL is the strongest dedup key when present; fall back to name+slug."""
        if url:
            # Short hash of URL — full URL is too long for ev.id
            return self._short_hash(url)
        return self._short_hash(f"{name}|{slug}")

    @staticmethod
    def _short_hash(s: str) -> str:
        import hashlib
        return hashlib.md5(s.encode("utf-8", errors="replace")).hexdigest()[:14]

    # ─────────────────────────────────────────────────────────────────
    # Status surface (extends base.status() with per-topic detail)
    # ─────────────────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        base = super().status()
        # Surface per-topic stats so /api/glassbox/diagnostic shows them
        base["per_topic"] = self.per_topic_stats
        base["topics_enabled"] = [
            t[0] for t in self.TOPICS if t[0] not in self._disabled
        ]
        base["topics_disabled"] = sorted(self._disabled)
        return base
