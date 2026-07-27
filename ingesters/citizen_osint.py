"""
MEWR Glassbox — Citizen OSINT Ingester
=======================================
Harvests geotagged intelligence from everyday people with phones:
  - YouTube: videos with location metadata in conflict/disaster zones
  - Bluesky: geotagged posts via AT Protocol (no API key required)
  - Reddit: OSINT subreddits + geotagged posts (no API key required)
  - Telegram: Public OSINT channels (requires API credentials)

All sources produce GlassboxEvent-compatible dicts with confidence scores.
Wire into glassbox_server.py _startup() just like gdelt.py.

API Keys needed:
  YOUTUBE_API_KEY   — Google Cloud Console → YouTube Data API v3 (10K units/day free)
  TELEGRAM_API_ID   — my.telegram.org (free)
  TELEGRAM_API_HASH — my.telegram.org (free)

Optional (gray area — comment out if not needed):
  NITTER_URL        — Self-hosted or public Nitter instance for Twitter/X scraping
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

# Local imports
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from confidence_scorer import score_event

log = logging.getLogger("citizen_osint")

# ─── Config ──────────────────────────────────────────────────────────────────

YOUTUBE_API_KEY  = os.getenv("YOUTUBE_API_KEY", "")
TELEGRAM_API_ID  = os.getenv("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
NITTER_URL       = os.getenv("NITTER_URL", "")  # Optional: "https://nitter.net"

# OSINT search terms — military/disaster/conflict focused
OSINT_KEYWORDS = [
    "explosion", "airstrike", "shelling", "missile", "attack",
    "protest", "riot", "flooding", "earthquake", "fire",
    "military", "troops", "convoy", "refugee", "evacuation",
]

# Known active OSINT Telegram channels (public, no join required)
TELEGRAM_OSINT_CHANNELS = [
    "IntelSlava",          # IntelSlava Z — Ukraine conflict
    "warmonitor1",         # War Monitor
    "CombatAirPatrol",     # Aviation/military
    "OSINTdefender",       # General OSINT
    "GeoConfirmed",        # Geo-verified events
    "MiddleEastSpectator", # Middle East coverage
    "raggedroses",         # Gaza/Palestine
]

# Reddit OSINT subreddits
REDDIT_SUBREDDITS = [
    "UkraineWarVideoReport",
    "CombatFootage",
    "worldnews",
    "geopolitics",
    "OSINT",
    "MilitaryGfys",
]

# Bluesky search terms
BLUESKY_SEARCH_TERMS = [
    "#OSINT", "#Ukraine", "#Gaza", "#Syria",
    "#conflict", "#breaking", "#explosion",
]

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_id(source: str, uid: str) -> str:
    return hashlib.md5(f"{source}:{uid}".encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_coords_from_text(text: str):
    """Try to extract lat/lng from text (e.g. '48.3794, 31.1656' or '48°N 31°E')."""
    # Decimal degrees: "48.3794, 31.1656" or "48.3794 31.1656"
    m = re.search(
        r"(-?\d{1,3}\.\d{2,6})[,\s]+(-?\d{1,3}\.\d{2,6})", text
    )
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng
    return None, None


def _severity_from_keywords(text: str) -> float:
    """Estimate severity 1-10 from keyword presence."""
    text_lower = text.lower()
    high = ["airstrike", "missile", "bomb", "explosion", "massacre", "killed",
            "dead", "strike", "artillery", "shelling"]
    medium = ["attack", "clash", "fire", "protest", "arrest", "detained",
              "wounded", "injured", "evacuated", "flooding"]
    if any(w in text_lower for w in high):
        return 7.0
    if any(w in text_lower for w in medium):
        return 5.0
    return 3.0


# ─── YouTube Geo Source ───────────────────────────────────────────────────────

class YouTubeGeoSource:
    """
    Searches YouTube for recent videos with location metadata.
    Uses the official YouTube Data API v3 — 100 search queries/day free.
    Each search = 100 quota units; 100 free searches/day.

    Videos are geocoded via their location metadata (lat/lng embedded by phone).
    """

    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch(self, session: aiohttp.ClientSession,
                    max_results: int = 10) -> List[Dict[str, Any]]:
        if not self.api_key:
            log.warning("YouTube: no API key — skipping")
            return []

        events = []
        for keyword in OSINT_KEYWORDS[:5]:  # Limit to 5 keywords to stay in quota
            try:
                params = {
                    "part": "snippet",
                    "q": keyword,
                    "type": "video",
                    "order": "date",
                    "publishedAfter": datetime.utcnow().strftime(
                        "%Y-%m-%dT00:00:00Z"
                    ),
                    "maxResults": max_results,
                    "key": self.api_key,
                    "videoDimension": "2d",
                    "videoEmbeddable": "true",
                    "location": "",   # will be ignored without radius
                    "locationRadius": "1000km",
                }
                # First pass: search without location (location search costs more quota)
                params.pop("location", None)
                params.pop("locationRadius", None)

                async with session.get(
                    f"{self.BASE_URL}/search", params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        log.warning("YouTube search HTTP %s for keyword=%s", resp.status, keyword)
                        continue
                    data = await resp.json()

                for item in data.get("items", []):
                    vid_id = item["id"].get("videoId", "")
                    if not vid_id:
                        continue
                    snippet = item.get("snippet", {})
                    title = snippet.get("title", "")
                    desc = snippet.get("description", "")
                    channel = snippet.get("channelTitle", "")
                    published = snippet.get("publishedAt", _now_iso())
                    url = f"https://www.youtube.com/watch?v={vid_id}"

                    # Try to get location from video details (separate API call)
                    lat, lng = await self._get_video_location(session, vid_id)
                    if lat is None:
                        # Try extracting from description
                        lat, lng = _extract_coords_from_text(desc)

                    has_coords = lat is not None
                    conf = score_event(
                        platform="youtube_geo",
                        has_media=True,    # It's a video
                        has_coordinates=has_coords,
                        coordinate_precision_km=1.0 if has_coords else 50.0,
                        source_tier=3,
                        has_url=True,
                        article_count=1,
                        age_hours=0.5,
                    )

                    events.append({
                        "external_id": _make_id("youtube", vid_id),
                        "layer": "youtube_osint",
                        "lat": lat or 0.0,
                        "lng": lng or 0.0,
                        "has_coords": has_coords,
                        "title": title[:200],
                        "summary": desc[:500] if desc else title,
                        "url": url,
                        "source": f"YouTube: {channel}",
                        "platform": "youtube_geo",
                        "severity": _severity_from_keywords(f"{title} {desc}"),
                        "confidence_score": conf.score,
                        "confidence_label": conf.label,
                        "severity_cap": conf.severity_cap,
                        "timestamp": published,
                        "media_type": "video",
                        "has_media": True,
                    })
                    log.debug("YouTube event: %s (conf=%s)", title[:60], conf.label)

            except Exception as exc:
                log.error("YouTube keyword=%s error: %s", keyword, exc)

        return events

    async def _get_video_location(
        self, session: aiohttp.ClientSession, video_id: str
    ) -> tuple:
        """Fetch video details to get recordingDetails.location."""
        try:
            params = {
                "part": "recordingDetails",
                "id": video_id,
                "key": self.api_key,
            }
            async with session.get(
                f"{self.BASE_URL}/videos", params=params,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
                items = data.get("items", [])
                if not items:
                    return None, None
                loc = items[0].get("recordingDetails", {}).get("location", {})
                lat = loc.get("latitude")
                lng = loc.get("longitude")
                if lat is not None and lng is not None:
                    return float(lat), float(lng)
        except Exception:
            pass
        return None, None


# ─── Bluesky AT Protocol Source ───────────────────────────────────────────────

class BlueskyOSINTSource:
    """
    Bluesky public search via AT Protocol (no API key required).
    Rate limit: ~1 req/sec, 3000/hour on the public AppView.
    """

    SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"

    async def fetch(self, session: aiohttp.ClientSession,
                    max_per_term: int = 20) -> List[Dict[str, Any]]:
        events = []
        for term in BLUESKY_SEARCH_TERMS[:4]:
            try:
                params = {"q": term, "limit": max_per_term, "sort": "latest"}
                async with session.get(
                    self.SEARCH_URL, params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        log.warning("Bluesky HTTP %s for term=%s", resp.status, term)
                        continue
                    data = await resp.json()

                for post in data.get("posts", []):
                    record = post.get("record", {})
                    text = record.get("text", "")
                    if not text:
                        continue

                    author = post.get("author", {})
                    handle = author.get("handle", "unknown")
                    post_uri = post.get("uri", "")
                    cid = post.get("cid", _make_id("bsky", post_uri))
                    created_at = record.get("createdAt", _now_iso())

                    # Extract coords from text
                    lat, lng = _extract_coords_from_text(text)
                    has_coords = lat is not None

                    # Skip posts with no location signal and no OSINT keywords
                    if not has_coords and not any(
                        kw in text.lower() for kw in OSINT_KEYWORDS
                    ):
                        continue

                    has_media = bool(record.get("embed"))
                    conf = score_event(
                        platform="bluesky_osint",
                        has_media=has_media,
                        has_coordinates=has_coords,
                        coordinate_precision_km=5.0 if has_coords else 100.0,
                        source_tier=3,
                        has_url=True,
                        article_count=1,
                        age_hours=0.0,
                    )

                    # Build URL from URI (at://did/collection/rkey → bsky.app)
                    rkey = post_uri.split("/")[-1] if "/" in post_uri else ""
                    url = f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else ""

                    events.append({
                        "external_id": _make_id("bluesky", cid),
                        "layer": "bluesky_osint",
                        "lat": lat or 0.0,
                        "lng": lng or 0.0,
                        "has_coords": has_coords,
                        "title": text[:100],
                        "summary": text[:500],
                        "url": url,
                        "source": f"Bluesky: @{handle}",
                        "platform": "bluesky_osint",
                        "severity": _severity_from_keywords(text),
                        "confidence_score": conf.score,
                        "confidence_label": conf.label,
                        "severity_cap": conf.severity_cap,
                        "timestamp": created_at,
                        "has_media": has_media,
                    })

                await asyncio.sleep(0.5)  # Respect rate limit

            except Exception as exc:
                log.error("Bluesky term=%s error: %s", term, exc)

        return events


# ─── Reddit OSINT Source ──────────────────────────────────────────────────────

class RedditOSINTSource:
    """
    Reddit public JSON API — no API key required.
    Uses .json endpoint on public subreddits.
    Rate limit: ~1 req/sec.
    """

    BASE_URL = "https://www.reddit.com/r/{sub}/new.json"
    HEADERS = {"User-Agent": "MEWR-Glassbox/1.0 (contact: hello@mewrcreate.com)"}

    async def fetch(self, session: aiohttp.ClientSession,
                    posts_per_sub: int = 25) -> List[Dict[str, Any]]:
        events = []
        for sub in REDDIT_SUBREDDITS:
            try:
                url = self.BASE_URL.format(sub=sub)
                params = {"limit": posts_per_sub, "sort": "new"}
                async with session.get(
                    url, params=params, headers=self.HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        log.warning("Reddit HTTP %s for r/%s", resp.status, sub)
                        continue
                    data = await resp.json()

                posts = data.get("data", {}).get("children", [])
                for child in posts:
                    post = child.get("data", {})
                    title = post.get("title", "")
                    text = post.get("selftext", "") or ""
                    url_post = f"https://reddit.com{post.get('permalink', '')}"
                    post_id = post.get("id", "")
                    author = post.get("author", "unknown")
                    created = post.get("created_utc", time.time())
                    score = post.get("score", 0)
                    flair = post.get("link_flair_text", "") or ""

                    full_text = f"{title} {text} {flair}"

                    # Skip if no OSINT keywords
                    if not any(kw in full_text.lower() for kw in OSINT_KEYWORDS):
                        continue

                    # Try coords from flair or text
                    lat, lng = _extract_coords_from_text(flair) if flair else (None, None)
                    if lat is None:
                        lat, lng = _extract_coords_from_text(full_text)
                    has_coords = lat is not None

                    # Reddit post link flair often has country/region
                    has_media = bool(post.get("url", "").endswith(
                        (".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webm", "v.redd.it")
                    ))

                    age_hours = (time.time() - created) / 3600.0
                    source_tier = 2 if score > 100 else 3  # High-upvote = known OSINT account

                    conf = score_event(
                        platform="reddit_osint",
                        has_media=has_media,
                        has_coordinates=has_coords,
                        coordinate_precision_km=10.0 if has_coords else 200.0,
                        source_tier=source_tier,
                        has_url=True,
                        article_count=1,
                        age_hours=age_hours,
                    )

                    created_iso = datetime.fromtimestamp(
                        created, tz=timezone.utc
                    ).isoformat()

                    events.append({
                        "external_id": _make_id("reddit", post_id),
                        "layer": "reddit_osint",
                        "lat": lat or 0.0,
                        "lng": lng or 0.0,
                        "has_coords": has_coords,
                        "title": title[:200],
                        "summary": f"[r/{sub}] {text[:400]}" if text else title,
                        "url": url_post,
                        "source": f"Reddit r/{sub}: u/{author}",
                        "platform": "reddit_osint",
                        "severity": _severity_from_keywords(full_text),
                        "confidence_score": conf.score,
                        "confidence_label": conf.label,
                        "severity_cap": conf.severity_cap,
                        "timestamp": created_iso,
                        "has_media": has_media,
                        "upvotes": score,
                    })

                await asyncio.sleep(1.0)  # Reddit rate limit

            except Exception as exc:
                log.error("Reddit r/%s error: %s", sub, exc)

        return events


# ─── Telegram OSINT Source ────────────────────────────────────────────────────

class TelegramOSINTSource:
    """
    Reads public Telegram OSINT channels via Telethon (MTProto).
    Requires TELEGRAM_API_ID + TELEGRAM_API_HASH from my.telegram.org (free).

    Falls back gracefully if telethon not installed or no credentials.
    Uses session file so you only need to auth once (enter phone + code on first run).
    """

    SESSION_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "telegram_osint.session"
    )

    def __init__(self, api_id: str, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self._client = None

    async def fetch(self, max_per_channel: int = 20) -> List[Dict[str, Any]]:
        if not self.api_id or not self.api_hash:
            log.info("Telegram: no credentials — skipping")
            return []

        try:
            from telethon import TelegramClient
            from telethon.errors import FloodWaitError
        except ImportError:
            log.warning("Telegram: telethon not installed — run: pip3 install telethon")
            return []

        events = []
        try:
            client = TelegramClient(
                self.SESSION_FILE,
                int(self.api_id),
                self.api_hash,
            )
            await client.start()

            for channel in TELEGRAM_OSINT_CHANNELS:
                try:
                    entity = await client.get_entity(channel)
                    messages = await client.get_messages(entity, limit=max_per_channel)

                    for msg in messages:
                        if not msg.text:
                            continue
                        text = msg.text

                        # Only include messages with OSINT keywords
                        if not any(kw in text.lower() for kw in OSINT_KEYWORDS):
                            continue

                        msg_id = str(msg.id)
                        msg_url = f"https://t.me/{channel}/{msg.id}"
                        ts = msg.date.isoformat() if msg.date else _now_iso()

                        lat, lng = _extract_coords_from_text(text)
                        if lat is None and msg.geo:
                            lat = msg.geo.lat
                            lng = msg.geo.long

                        has_coords = lat is not None
                        has_media = msg.media is not None
                        age_hours = (
                            (datetime.now(timezone.utc) - msg.date).total_seconds() / 3600.0
                            if msg.date else 0.0
                        )

                        conf = score_event(
                            platform="telegram_osint",
                            has_media=has_media,
                            has_coordinates=has_coords,
                            coordinate_precision_km=1.0 if (msg.geo and has_coords) else 50.0,
                            source_tier=2,   # Known OSINT channels = tier 2
                            is_verified_account=True,  # Curated channel list
                            has_url=True,
                            article_count=1,
                            age_hours=age_hours,
                        )

                        events.append({
                            "external_id": _make_id("telegram", f"{channel}:{msg_id}"),
                            "layer": "telegram_osint",
                            "lat": lat or 0.0,
                            "lng": lng or 0.0,
                            "has_coords": has_coords,
                            "title": text[:100],
                            "summary": text[:500],
                            "url": msg_url,
                            "source": f"Telegram: {channel}",
                            "platform": "telegram_osint",
                            "severity": _severity_from_keywords(text),
                            "confidence_score": conf.score,
                            "confidence_label": conf.label,
                            "severity_cap": conf.severity_cap,
                            "timestamp": ts,
                            "has_media": has_media,
                        })

                    await asyncio.sleep(2.0)  # Telegram rate limit

                except FloodWaitError as e:
                    log.warning("Telegram flood wait %ss for %s", e.seconds, channel)
                    await asyncio.sleep(e.seconds)
                except Exception as exc:
                    log.error("Telegram channel=%s error: %s", channel, exc)

            await client.disconnect()

        except Exception as exc:
            log.error("Telegram client error: %s", exc)

        return events


# ─── Twitter/X via Nitter (Optional) ──────────────────────────────────────────

class NitterOSINTSource:
    """
    Scrapes public Twitter/X content via Nitter instance.
    Nitter is an open-source Twitter frontend.

    LEGAL NOTE: This scrapes public data via an open-source privacy tool.
    ToS gray area — disable if you need strict compliance.
    Set NITTER_URL env var to your self-hosted or public instance.
    Recommended: run your own Nitter on Docker for reliability.

    Public instances: https://github.com/zedeus/nitter/wiki/Instances
    """

    OSINT_ACCOUNTS = [
        "IntelCrab",        # General OSINT
        "OSINTdefender",    # Conflicts
        "TheDeadDistrict",  # Ukraine/Russia
        "UAWeapons",        # Military equipment
        "oryxspioenkop",    # Verified military losses
        "GeoConfirmed",     # Verified geolocation
        "AuroraIntel",      # Conflict zones
    ]

    def __init__(self, nitter_url: str):
        self.nitter_url = nitter_url.rstrip("/") if nitter_url else ""

    async def fetch(self, session: aiohttp.ClientSession,
                    max_per_account: int = 10) -> List[Dict[str, Any]]:
        if not self.nitter_url:
            log.info("Nitter: no URL configured — skipping Twitter/X source")
            return []

        events = []
        headers = {"User-Agent": "Mozilla/5.0 (compatible; MEWR-Glassbox/1.0)"}

        for account in self.OSINT_ACCOUNTS[:5]:  # Limit to 5 accounts
            try:
                url = f"{self.nitter_url}/{account}/rss"
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        log.warning("Nitter HTTP %s for @%s", resp.status, account)
                        continue
                    rss_text = await resp.text()

                # Parse RSS items
                import xml.etree.ElementTree as ET
                try:
                    root = ET.fromstring(rss_text)
                except ET.ParseError:
                    continue

                channel_el = root.find("channel")
                if not channel_el:
                    continue

                count = 0
                for item in channel_el.findall("item"):
                    if count >= max_per_account:
                        break
                    title = (item.findtext("title") or "").strip()
                    desc = (item.findtext("description") or "").strip()
                    link = item.findtext("link") or ""
                    pub_date = item.findtext("pubDate") or _now_iso()

                    full_text = f"{title} {desc}"
                    # Strip HTML tags from description
                    full_text = re.sub(r"<[^>]+>", " ", full_text)

                    if not any(kw in full_text.lower() for kw in OSINT_KEYWORDS):
                        continue

                    lat, lng = _extract_coords_from_text(full_text)
                    has_coords = lat is not None

                    conf = score_event(
                        platform="twitter_nitter",
                        has_media=bool(re.search(r'\.(jpg|png|gif|mp4)', full_text, re.I)),
                        has_coordinates=has_coords,
                        coordinate_precision_km=50.0,
                        source_tier=2,  # Known OSINT accounts = tier 2
                        is_verified_account=True,
                        has_url=bool(link),
                        article_count=1,
                        age_hours=0.5,
                    )

                    events.append({
                        "external_id": _make_id("nitter", link),
                        "layer": "twitter_osint",
                        "lat": lat or 0.0,
                        "lng": lng or 0.0,
                        "has_coords": has_coords,
                        "title": title[:200],
                        "summary": full_text[:500],
                        "url": link.replace(self.nitter_url, "https://twitter.com")
                               if link else "",
                        "source": f"Twitter/X: @{account}",
                        "platform": "twitter_nitter",
                        "severity": _severity_from_keywords(full_text),
                        "confidence_score": conf.score,
                        "confidence_label": conf.label,
                        "severity_cap": conf.severity_cap,
                        "timestamp": _now_iso(),
                        "has_media": False,
                    })
                    count += 1

                await asyncio.sleep(2.0)

            except Exception as exc:
                log.error("Nitter account=%s error: %s", account, exc)

        return events


# ─── Main Harvester ───────────────────────────────────────────────────────────

class CitizenOSINTIngester:
    """
    Orchestrates all citizen OSINT sources.
    Produces a unified list of GlassboxEvent-compatible dicts.
    Called by glassbox_server.py _startup() and harvester_runner.py.
    """

    # Canonical layer name (snake_case) — frontend's LAYER_META expects this.
    # Per-source dicts may override with platform-specific layers
    # (youtube_osint, bluesky_osint, reddit_osint, telegram_osint, twitter_osint)
    # but the orchestrator's identity for status/diagnostic purposes is
    # citizen_osint.
    layer = "citizen_osint"
    source = "YouTube / Bluesky / Reddit / Telegram / Nitter"

    def __init__(self):
        self.youtube  = YouTubeGeoSource(api_key=YOUTUBE_API_KEY)
        self.bluesky  = BlueskyOSINTSource()
        self.reddit   = RedditOSINTSource()
        self.telegram = TelegramOSINTSource(TELEGRAM_API_ID, TELEGRAM_API_HASH)
        self.nitter   = NitterOSINTSource(NITTER_URL)

    async def run(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Returns dict of platform → events for that platform.
        Each event is GlassboxEvent-compatible.
        Only includes events that have location (lat/lng) OR strong OSINT keywords.
        """
        results: Dict[str, List] = {
            "youtube":  [],
            "bluesky":  [],
            "reddit":   [],
            "telegram": [],
            "twitter":  [],
        }

        async with aiohttp.ClientSession() as session:
            # Run non-Telegram sources concurrently
            yt_task  = asyncio.create_task(self.youtube.fetch(session))
            bk_task  = asyncio.create_task(self.bluesky.fetch(session))
            rd_task  = asyncio.create_task(self.reddit.fetch(session))
            nt_task  = asyncio.create_task(self.nitter.fetch(session))

            results["youtube"]  = await yt_task
            results["bluesky"]  = await bk_task
            results["reddit"]   = await rd_task
            results["twitter"]  = await nt_task

        # Telegram uses its own client (not aiohttp)
        results["telegram"] = await self.telegram.fetch()

        total = sum(len(v) for v in results.values())
        with_coords = sum(
            1 for v in results.values() for e in v if e.get("has_coords")
        )
        log.info(
            "CitizenOSINT harvest complete: %d events total, %d with coordinates",
            total, with_coords
        )
        return results

    def all_events(self, run_result: Dict[str, List]) -> List[Dict[str, Any]]:
        """Flatten all platform results into a single sorted list."""
        flat = [e for events in run_result.values() for e in events]
        flat.sort(key=lambda e: e.get("confidence_score", 0), reverse=True)
        return flat


# ─── Standalone test ──────────────────────────────────────────────────────────

async def _test():
    logging.basicConfig(level=logging.INFO)
    ingester = CitizenOSINTIngester()
    result = await ingester.run()
    for platform, events in result.items():
        print(f"\n{'='*60}")
        print(f"{platform.upper()}: {len(events)} events")
        for ev in events[:3]:
            print(f"  [{ev['confidence_label']}] {ev['title'][:80]}")
            if ev.get("has_coords"):
                print(f"    Coords: {ev['lat']:.4f}, {ev['lng']:.4f}")


if __name__ == "__main__":
    asyncio.run(_test())
