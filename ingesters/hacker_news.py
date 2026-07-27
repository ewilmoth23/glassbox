"""
Hacker News firehose ingester.

Source: https://hacker-news.firebaseio.com/v0/
License: CC0 — public domain, commercial-OK.
NO API key required.

Strategic value: HN's front page often surfaces tech / cyber / outage /
breach news 4-24h before mainstream press picks it up. The signal is
particularly strong for:
  - Major service outages (AWS region down, Cloudflare incident, Slack down)
  - Security breaches (data leaks, ransomware events, vulnerabilities)
  - Emerging tech announcements (AI model releases, framework launches)
  - Engineering post-mortems (often the first detailed analysis of an
    incident days after it happened)

Coverage approach: poll /v0/topstories.json (top ~500 IDs) and fetch any
new ones via /v0/item/{id}.json. Filter to stories with score >= MIN_SCORE
(noise floor). Stories without URLs (text-only "Ask HN" / "Show HN") are
included since they're often the highest-signal posts.

Geographic anchor: HN stories are non-geographic. We emit with a sentinel
location (0, 0) so they appear in any global-bbox query (the brief surfaces
them regardless of map position).

Polling: HN's API is hosted on Google Firebase and rate-limit tolerant for
reasonable use. We poll topstories every 5 min, then make N fetch calls for
new items only (typical: 0-5 new items per cycle). Total cost: ~5 calls/cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .base import GlassboxEvent, Ingester


_TOPSTORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
_ITEM_URL_TEMPLATE = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

# Sentinel coordinates — equator/prime-meridian. Stories appear in any
# bbox query that includes (0, 0). Brief surfaces them regardless of map.
_ANCHOR_LAT = 0.0
_ANCHOR_LNG = 0.0

# How many of the top-N IDs to consider. HN's topstories endpoint returns
# up to 500; a smaller window is plenty for "trending now" coverage.
_TOPSTORIES_WINDOW = 50

# Minimum score to consider a story interesting enough to ingest. Filters
# out fresh posts that haven't gathered upvotes yet.
_MIN_SCORE = 30


def _severity_from_score(score: Optional[int]) -> int:
    """Map HN score → Glassbox severity 0-10. Higher score = more attention."""
    if not score or score < 0:
        return 3
    if score < 100:
        return 4
    if score < 300:
        return 6
    if score < 1000:
        return 8
    return 10


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    """Extract a hostname for grouping (e.g. github.com, nytimes.com)."""
    if not url:
        return None
    # cheap split — don't pull urllib for one extraction
    s = url
    if "://" in s:
        s = s.split("://", 1)[1]
    if "/" in s:
        s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s or None


class HackerNewsIngester(Ingester):
    layer = "hacker_news"
    source = "Hacker News"
    source_id = "hacker_news"
    poll_interval_sec = 300.0   # 5 min — HN front page churns slowly enough

    UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track IDs we've seen so we don't re-fetch on every cycle.
        # Set bounded — purge older entries when crossing a soft cap.
        self._seen_ids: set[int] = set()

    async def fetch(self) -> List[Dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"User-Agent": self.UA, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            # Step 1: pull the top-stories ID list.
            async with s.get(_TOPSTORIES_URL) as r:
                r.raise_for_status()
                ids = await r.json()
            if not isinstance(ids, list):
                self.log.warning(
                    f"[hacker_news] topstories returned non-list "
                    f"({type(ids).__name__})"
                )
                return []
            window = ids[:_TOPSTORIES_WINDOW]

            # Step 2: for IDs we haven't seen, fetch item details.
            new_ids = [i for i in window if i not in self._seen_ids]
            if not new_ids:
                return []

            items: List[Dict[str, Any]] = []
            for sid in new_ids:
                try:
                    async with s.get(_ITEM_URL_TEMPLATE.format(id=sid)) as r:
                        if r.status != 200:
                            continue
                        item = await r.json()
                        if not isinstance(item, dict):
                            continue
                        items.append(item)
                except Exception:
                    continue
                # Mark seen even if we filter it out below — we don't
                # want to re-fetch the same ID next cycle.
                self._seen_ids.add(sid)

        # Soft cap on _seen_ids: prune to most recent 1000 to bound memory.
        if len(self._seen_ids) > 2000:
            # Keep the IDs from our most recent window and drop the rest.
            self._seen_ids = set(window) | set(new_ids)

        return items

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        out: List[GlassboxEvent] = []
        for r in raw_items:
            sid = r.get("id")
            if sid is None:
                continue
            try:
                score = int(r.get("score") or 0)
            except (TypeError, ValueError):
                score = 0
            if score < _MIN_SCORE:
                continue

            # Filter to stories (HN also has comment items, polls, etc.)
            if r.get("type") not in ("story", "poll", None):
                continue

            title = (r.get("title") or "").strip()
            if not title:
                continue

            url = r.get("url") or None
            domain = _domain_from_url(url)
            severity = _severity_from_score(score)
            posted_ts = r.get("time")
            if isinstance(posted_ts, (int, float)):
                ts_iso = datetime.fromtimestamp(posted_ts, tz=timezone.utc).isoformat()
            else:
                ts_iso = datetime.now(timezone.utc).isoformat()

            payload: Dict[str, Any] = {
                "hn_id":         sid,
                "score":         score,
                "by":            r.get("by"),
                "comments":      r.get("descendants"),
                "url":           url,
                "domain":        domain,
                "title":         title,
                "hn_url":        f"https://news.ycombinator.com/item?id={sid}",
                "_attribution":  "Hacker News (CC0)",
            }

            # Tag the type so downstream brief code can group by family
            event_subtype = "story"
            if r.get("type") == "poll":
                event_subtype = "poll"
            elif title.lower().startswith("ask hn"):
                event_subtype = "ask_hn"
            elif title.lower().startswith("show hn"):
                event_subtype = "show_hn"

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=f"hn:{sid}",
                kind="hn_story",
                lat=_ANCHOR_LAT,
                lng=_ANCHOR_LNG,
                ts=ts_iso,
                severity=severity,
                source=self.source,
                payload=payload,
                domain="news",
                geocode_quality="anchor_only",
                # Decay: HN stories stay newsworthy for ~24h.
                decay_half_life_min=1440,
                market_tags=[],
                severity_for_market=0,
            ))
        return out
