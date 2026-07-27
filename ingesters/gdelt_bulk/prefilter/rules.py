# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Stateless prefilter rules. Each ``Rule.check(event)`` returns ``None`` on
pass or ``Rejected(reason)`` on drop. The engine short-circuits on first
reject to keep the cheap-rules-first ordering meaningful.

The Dedup rule (Redis sliding window) is intentionally NOT here — it is
stateful and lands in the follow-up commit per HANDOFF_03 Day 2.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import urlparse

from .config import (
    PRECISION_LEVELS,
    CategoryFilterConfig,
    DedupFilterConfig,
    GDELTEventForPrefilter,
    GeographyFilterConfig,
    RecencyFilterConfig,
    SeverityFilterConfig,
    SourceQualityFilterConfig,
)


@dataclass(frozen=True)
class Rejected:
    """Explicit reject reason. Engine surfaces ``reason`` as a metric label.
    ``metadata`` carries rule-specific context (e.g. dedup's
    ``duplicate_of`` event id) that the engine forwards to its caller
    without coupling rules to the caller shape."""

    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseRule:
    """Subclasses implement ``check(event) -> Rejected | None``.

    ``name`` is the metric/label string used by the engine for observability.
    """

    name: str = "base"

    def check(self, event: GDELTEventForPrefilter) -> Optional[Rejected]:
        raise NotImplementedError


# ─── Rule 1 — Category ───────────────────────────────────────────────────


class CategoryRule(BaseRule):
    """Allow- or block-list on Glassbox subcategories from HANDOFF_02.

    Plus an optional flag-required / flag-blocked layer for defense in
    depth — e.g. allow ``armed_conflict.*`` but require the ``military``
    flag, or allow ``violence_civil.protest`` but block when the
    ``civilian_impact`` flag is missing.
    """

    name = "category"

    def __init__(self, cfg: CategoryFilterConfig) -> None:
        self._mode = cfg.mode
        self._subcats = set(cfg.subcategories)
        self._flags_required = set(cfg.flags_required)
        self._flags_blocked = set(cfg.flags_blocked)

    def check(self, event: GDELTEventForPrefilter) -> Optional[Rejected]:
        in_set = event.subcategory in self._subcats
        if self._mode == "allow" and not in_set:
            return Rejected("category_not_allowed")
        if self._mode == "block" and in_set:
            return Rejected("category_blocked")
        flags = set(event.flags)
        if self._flags_required and not self._flags_required.issubset(flags):
            return Rejected("flag_required_missing")
        if self._flags_blocked and self._flags_blocked & flags:
            return Rejected("flag_blocked")
        return None


# ─── Rule 2 — Severity ───────────────────────────────────────────────────


class SeverityRule(BaseRule):
    """Drop events below the (optionally per-category overridden) severity
    threshold. Severity comes from the HANDOFF_02 lookup; per-category
    overrides let analysts tune e.g. natural-disaster lower than diplomatic."""

    name = "severity"

    def __init__(self, cfg: SeverityFilterConfig) -> None:
        self._default = cfg.min_severity
        self._overrides = dict(cfg.per_category_overrides)

    def check(self, event: GDELTEventForPrefilter) -> Optional[Rejected]:
        threshold = self._overrides.get(event.category, self._default)
        if event.severity < threshold:
            return Rejected("severity_below_threshold")
        return None


# ─── Rule 3 — Geography ──────────────────────────────────────────────────


class GeographyRule(BaseRule):
    """Drop events below the configured precision floor; optional bbox /
    country gates layer on top.

    Precision compare uses the empire's ``GlassboxEvent.geocode_quality``
    vocabulary (exact > city > region > country > unknown).
    """

    name = "geography"

    def __init__(self, cfg: GeographyFilterConfig) -> None:
        self._min_precision = cfg.precision_min
        self._min_idx = PRECISION_LEVELS.index(cfg.precision_min)
        self._bboxes = list(cfg.bbox_allowlist)
        self._allow_iso = {c.upper() for c in cfg.iso_country_allowlist}
        self._block_iso = {c.upper() for c in cfg.iso_country_blocklist}

    def check(self, event: GDELTEventForPrefilter) -> Optional[Rejected]:
        # Unknown precision is always a fail under any non-trivial floor.
        try:
            event_idx = PRECISION_LEVELS.index(event.geocode_quality)
        except ValueError:
            return Rejected("geocode_quality_unrecognized")
        if event_idx > self._min_idx:
            return Rejected("geocode_below_precision_floor")

        if event.iso_country:
            iso_up = event.iso_country.upper()
            if self._block_iso and iso_up in self._block_iso:
                return Rejected("iso_country_blocked")
            if self._allow_iso and iso_up not in self._allow_iso:
                return Rejected("iso_country_not_allowed")
        elif self._allow_iso:
            return Rejected("iso_country_missing_with_allowlist")

        if self._bboxes and event.lat is not None and event.lng is not None:
            in_any = False
            for w, s, e, n in self._bboxes:
                if w <= event.lng <= e and s <= event.lat <= n:
                    in_any = True
                    break
            if not in_any:
                return Rejected("bbox_not_allowed")
        elif self._bboxes:
            return Rejected("coordinates_missing_with_bbox_allowlist")

        return None


# ─── Rule 4 — Source quality ─────────────────────────────────────────────


def extract_domain(source_url: str) -> str:
    """Lowercased registrable-ish domain from a URL.

    Public-suffix-aware parsing would need ``tldextract``; for an exact
    lookup against a curated list, urllib's hostname + 'www.' strip is
    enough. ``bbc.co.uk`` and ``news.bbc.co.uk`` both reduce to the
    hostname as published by GDELT, which is what the source_quality.json
    keys against.
    """
    if not source_url:
        return ""
    try:
        parsed = urlparse(source_url)
    except (TypeError, ValueError):
        return ""
    host = (parsed.netloc or parsed.path or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


class SourceQualityRule(BaseRule):
    """Look up the source URL's domain in source_quality.json. Drop if
    below ``min_score``. Unknown domains get ``unknown_domain_score``."""

    name = "source_quality"

    def __init__(
        self,
        cfg: SourceQualityFilterConfig,
        data_dir: Path,
    ) -> None:
        self._min = cfg.min_score
        self._unknown = cfg.unknown_domain_score
        path = (data_dir / cfg.data_file).resolve()
        with path.open("r", encoding="utf-8") as f:
            doc = json.load(f)
        # Schema: {"version": "...", "domains": {"reuters.com": 0.95, ...}}
        # Domains are stored lowercased.
        self._domains: dict = {k.lower(): float(v) for k, v in doc["domains"].items()}

    def score_for(self, source_url: str) -> float:
        domain = extract_domain(source_url)
        return self._domains.get(domain, self._unknown)

    def check(self, event: GDELTEventForPrefilter) -> Optional[Rejected]:
        if self.score_for(event.source_url) < self._min:
            return Rejected("source_quality_below_min")
        return None


# ─── Rule 6 — Recency (Rule 5 = Dedup is deferred) ───────────────────────


class RecencyRule(BaseRule):
    """Drop events older than ``max_age_hours``. GDELT occasionally
    back-publishes; we don't want stale events polluting the LLM queue."""

    name = "recency"

    def __init__(self, cfg: RecencyFilterConfig, *, now_fn=None) -> None:
        self._max_age_seconds = cfg.max_age_hours * 3600.0
        # Injectable now() for deterministic tests.
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def check(self, event: GDELTEventForPrefilter) -> Optional[Rejected]:
        ts = event.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (self._now_fn() - ts).total_seconds()
        if age > self._max_age_seconds:
            return Rejected("event_too_old")
        return None


# ─── Rule 5 — Dedup (sliding-window, in-process for v1.0) ────────────────


_TOKEN_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)


def _tokenize(text: str) -> set:
    """Lowercased word tokens, dedup'd. Punctuation/symbols are
    separators. Returns an empty set on empty input — token_set_jaccard
    handles the degenerate case."""
    if not text:
        return set()
    return {tok for tok in _TOKEN_SPLIT_RE.split(text.lower()) if tok}


def token_set_jaccard(a: str, b: str) -> float:
    """Jaccard similarity on tokenized strings. Approximates rapidfuzz's
    ``token_set_ratio / 100`` for our purposes — close enough at our
    scale (post-CategoryRule traffic is ≤ 1 event/sec entering dedup)
    that the rapidfuzz dependency isn't worth carrying in v1.0."""
    sa = _tokenize(a)
    sb = _tokenize(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km, WGS84-spherical approximation."""
    R = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2)
    return 2.0 * R * math.asin(math.sqrt(a))


@dataclass
class _DedupEntry:
    event_id: str
    title: str
    lat: Optional[float]
    lng: Optional[float]
    ts_epoch: float
    duplicate_count: int = 0


class DedupRule(BaseRule):
    """Sliding-window per-subcategory deduplication.

    For each new event, we look back ``window_minutes`` within its
    subcategory bucket and check whether a prior entry matches on:

      - token-set Jaccard similarity ≥ ``similarity_threshold``
      - haversine distance ≤ ``geo_threshold_km`` (when both events
        have coordinates; otherwise geo-check is skipped)

    Match → reject the new event, increment the prior entry's
    ``duplicate_count``, and surface ``duplicate_of`` in Rejected.metadata.
    No match → record the new entry in the cache and pass.

    State is a per-subcategory deque ordered by event timestamp; on each
    call we pop expired entries from the left. At our scale (~1 event/sec
    entering dedup with ~60 entries in a 60-min window) this is O(60)
    per call — fine.

    v1.0 backing is in-process; multi-worker future would back this with
    a Redis hash keyed by (subcategory, hour-bucket). Same interface.
    """

    name = "dedup"

    def __init__(self, cfg: DedupFilterConfig, *, now_fn=None) -> None:
        self._enabled = cfg.enabled
        self._window_seconds = cfg.window_minutes * 60.0
        self._sim_threshold = cfg.similarity_threshold
        self._geo_threshold_km = cfg.geo_threshold_km
        self._fields = list(cfg.fields_compared)
        self._cache: Dict[str, Deque[_DedupEntry]] = defaultdict(deque)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _composite_text(self, event: GDELTEventForPrefilter) -> str:
        """Build the comparison text from the configured field set.
        Empty / missing fields are skipped silently."""
        parts: List[str] = []
        for field_name in self._fields:
            if field_name == "title":
                parts.append(event.title)
            elif field_name == "actor1":
                parts.append(event.actor1_name or "")
            elif field_name == "actor2":
                parts.append(event.actor2_name or "")
            elif field_name == "subcategory":
                parts.append(event.subcategory)
            # Unknown field names are ignored — config schema can be tightened
            # later without breaking events that get pushed through here.
        return " ".join(p for p in parts if p)

    def _expire(self, bucket: Deque[_DedupEntry], cutoff_epoch: float) -> None:
        while bucket and bucket[0].ts_epoch < cutoff_epoch:
            bucket.popleft()

    def check(self, event: GDELTEventForPrefilter) -> Optional[Rejected]:
        if not self._enabled:
            return None

        bucket = self._cache[event.subcategory]
        now_epoch = self._now_fn().timestamp()
        cutoff = now_epoch - self._window_seconds
        self._expire(bucket, cutoff)

        new_text = self._composite_text(event)
        for entry in reversed(bucket):
            # Geo gate first — cheaper than tokenization
            if (event.lat is not None and event.lng is not None
                    and entry.lat is not None and entry.lng is not None):
                if haversine_km(event.lat, event.lng,
                                entry.lat, entry.lng) > self._geo_threshold_km:
                    continue
            sim = token_set_jaccard(new_text, entry.title)
            if sim >= self._sim_threshold:
                entry.duplicate_count += 1
                return Rejected(
                    reason="duplicate",
                    metadata={
                        "duplicate_of": entry.event_id,
                        "duplicate_count": entry.duplicate_count,
                        "similarity": round(sim, 3),
                    },
                )

        # Not a duplicate — record + pass.
        ts = event.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        bucket.append(_DedupEntry(
            event_id=event.event_id,
            title=new_text,
            lat=event.lat,
            lng=event.lng,
            ts_epoch=ts.timestamp(),
        ))
        return None

    # Diagnostics surface for engine.health()
    def cache_size(self) -> Dict[str, int]:
        return {k: len(v) for k, v in self._cache.items() if v}
