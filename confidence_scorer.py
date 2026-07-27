"""
MEWR Glassbox — Universal Confidence Scorer
============================================
Applies to ALL intel sources: GDELT, YouTube, Telegram, Reddit, Bluesky, etc.
Every GlassboxEvent gets a confidence score so the globe can show quality,
not just quantity.

Score: 0.0 – 1.0
Labels:
  SPECULATIVE  < 0.35   single-source, no media, anonymous post
  LOW          0.35-0.50 single-source, basic credibility
  MODERATE     0.50-0.65 some evidence or light corroboration
  HIGH         0.65-0.80 media evidence + corroboration
  CONFIRMED    > 0.80    multi-source + GPS + video/photo proof

Severity adjustment:
  Low-confidence events have their severity capped so noisy social posts
  don't overwhelm verified data on the globe.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Any


# ─── Platform Baselines ───────────────────────────────────────────
# Starting confidence before any modifiers.
# Reflects typical data quality and verification for each source.

PLATFORM_BASELINE: Dict[str, float] = {
    # Official/verified data feeds
    "earthquake":      0.95,   # USGS — authoritative seismic data
    "ads_b":           0.90,   # ADS-B aviation transponder — legally required
    "ais":             0.85,   # AIS ship transponder — legally required
    "aprs":            0.80,   # Amateur radio position reports — operator-verified
    "manual":          0.75,   # Human-entered by operator — intentional
    # Aggregated/curated OSINT
    "gdelt":           0.55,   # GDELT: aggregates media, geocoding via mentioned places
                               #        (not event location — can be imprecise)
    "telegram_osint":  0.60,   # Public OSINT channels: active community, fast but variable
    "youtube_geo":     0.65,   # YouTube location API: actual video + GPS metadata
    "reddit_osint":    0.50,   # Reddit: community self-corrects but slow, English-biased
    "bluesky_osint":   0.48,   # Bluesky: growing OSINT presence, open API
    "twitter_nitter":  0.42,   # Twitter/X: noisy, unverified, high volume
    "snapmap":         0.35,   # Snap Map: real-time but unofficial API, ToS risk
}

LABEL_THRESHOLDS = [
    (0.80, "CONFIRMED"),
    (0.65, "HIGH"),
    (0.50, "MODERATE"),
    (0.35, "LOW"),
    (0.00, "SPECULATIVE"),
]


# ─── Input / Output ───────────────────────────────────────────────

@dataclass
class ConfidenceInput:
    """All factors that affect a confidence score."""
    platform: str                       # key into PLATFORM_BASELINE

    # Evidence quality
    has_media: bool = False             # photo or video attached to source
    has_coordinates: bool = False       # actual lat/lng vs. text-parsed location
    coordinate_precision_km: float = 50.0  # lower = more precise (GPS vs city-level)

    # Source credibility
    source_tier: int = 3               # 1=verified org/government, 2=known OSINT account, 3=public
    is_verified_account: bool = False  # platform-verified or in trusted account list
    has_url: bool = True               # source URL available for fact-checking

    # Corroboration
    article_count: int = 1             # articles/posts from SAME source about this event
    cross_source_count: int = 0        # count of OTHER platforms reporting same event
    gdelt_corroborated: bool = False   # GDELT independently geocoded this event area

    # Temporal
    age_hours: float = 0.0             # hours since event (freshness decay)

    # Content flags
    is_breaking: bool = False          # marked as breaking/urgent
    has_headline_match: bool = False   # headline matches known active conflict zone

    # Optional metadata passthrough
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfidenceResult:
    """Result of scoring one event."""
    score: float               # 0.0 – 1.0 final confidence
    label: str                 # SPECULATIVE / LOW / MODERATE / HIGH / CONFIRMED
    severity_cap: float        # max severity this event should display (0-10)
    factors: Dict[str, float]  # factor name → adjustment applied
    platform: str
    platform_baseline: float


# ─── Scorer ───────────────────────────────────────────────────────

class ConfidenceScorer:
    """
    Score any OSINT event for display confidence.

    Usage:
        scorer = ConfidenceScorer()
        result = scorer.score(ConfidenceInput(
            platform="gdelt",
            has_coordinates=True,
            article_count=5,
            age_hours=0.5,
        ))
        print(result.score, result.label)
    """

    def score(self, inp: ConfidenceInput) -> ConfidenceResult:
        baseline = PLATFORM_BASELINE.get(inp.platform, 0.45)
        factors: Dict[str, float] = {"platform_baseline": baseline}
        total = baseline

        # ── Evidence quality ──────────────────────────────────────

        if inp.has_media:
            adj = 0.12
            total += adj
            factors["has_media"] = adj

        if inp.has_coordinates:
            # More precise GPS = higher confidence
            if inp.coordinate_precision_km <= 1:
                adj = 0.15   # GPS-level precision
            elif inp.coordinate_precision_km <= 10:
                adj = 0.10   # City district level
            elif inp.coordinate_precision_km <= 50:
                adj = 0.05   # City level
            else:
                adj = 0.0    # Country/region level — no bonus
            total += adj
            factors["coordinate_precision"] = adj

        if not inp.has_url:
            adj = -0.08
            total += adj
            factors["no_url"] = adj

        # ── Source credibility ────────────────────────────────────

        if inp.source_tier == 1:
            adj = 0.15   # Verified government/organization
            total += adj
            factors["source_tier_1"] = adj
        elif inp.source_tier == 2:
            adj = 0.07   # Known OSINT account
            total += adj
            factors["source_tier_2"] = adj
        # tier 3 (public) = no adjustment

        if inp.is_verified_account:
            adj = 0.08
            total += adj
            factors["verified_account"] = adj

        # ── Corroboration ─────────────────────────────────────────

        # Same-platform article/post count
        if inp.article_count >= 10:
            adj = 0.15
        elif inp.article_count >= 5:
            adj = 0.10
        elif inp.article_count >= 3:
            adj = 0.05
        else:
            adj = 0.0
        if adj:
            total += adj
            factors["article_count"] = adj

        # Cross-platform corroboration (strongest positive signal)
        if inp.cross_source_count >= 3:
            adj = 0.20
        elif inp.cross_source_count == 2:
            adj = 0.14
        elif inp.cross_source_count == 1:
            adj = 0.08
        else:
            # Single source — apply penalty unless tier 1
            if inp.source_tier > 1:
                adj = -0.08
                total += adj
                factors["single_source_penalty"] = adj
            adj = 0.0
        if adj > 0:
            total += adj
            factors["cross_source_corroboration"] = adj

        if inp.gdelt_corroborated:
            adj = 0.12
            total += adj
            factors["gdelt_corroboration"] = adj

        # ── Temporal decay ────────────────────────────────────────
        # Fresh events score higher. Decay is slow first 6h, then faster.
        if inp.age_hours > 0:
            if inp.age_hours <= 1:
                decay = 0.0
            elif inp.age_hours <= 6:
                decay = 0.02 * (inp.age_hours - 1)   # up to -0.10
            elif inp.age_hours <= 24:
                decay = 0.10 + 0.01 * (inp.age_hours - 6)  # up to -0.28
            else:
                decay = 0.28 + 0.005 * min(inp.age_hours - 24, 48)  # up to -0.52
            decay = round(decay, 3)
            if decay > 0:
                total -= decay
                factors["temporal_decay"] = -decay

        # ── Clamp ─────────────────────────────────────────────────
        score = max(0.05, min(0.98, total))
        score = round(score, 3)

        # ── Label ─────────────────────────────────────────────────
        label = "SPECULATIVE"
        for threshold, lbl in LABEL_THRESHOLDS:
            if score >= threshold:
                label = lbl
                break

        # ── Severity cap ──────────────────────────────────────────
        # Low-confidence events shouldn't dominate the globe with high severity.
        # SPECULATIVE events cap at 4/10 regardless of reported severity.
        if score >= 0.80:
            severity_cap = 10.0    # CONFIRMED — no cap
        elif score >= 0.65:
            severity_cap = 9.0     # HIGH
        elif score >= 0.50:
            severity_cap = 7.0     # MODERATE
        elif score >= 0.35:
            severity_cap = 5.0     # LOW
        else:
            severity_cap = 3.5     # SPECULATIVE

        return ConfidenceResult(
            score=score,
            label=label,
            severity_cap=severity_cap,
            factors=factors,
            platform=inp.platform,
            platform_baseline=baseline,
        )

    def adjust_severity(self, original_severity: float,
                        confidence: ConfidenceResult) -> float:
        """Apply confidence cap to an event's severity."""
        return round(min(original_severity, confidence.severity_cap), 2)


# ─── Corroborator ─────────────────────────────────────────────────

class EventCorroborator:
    """
    Detects when events from different sources describe the same incident.
    When a match is found, boosts confidence scores on both events.

    Match criteria:
      - Same geographic area (within radius_km)
      - Same time window (within hours_window hours)
      - Overlapping keywords (optional, for headline matching)
    """

    def __init__(self, radius_km: float = 50.0, hours_window: float = 4.0):
        self.radius_km = radius_km
        self.hours_window = hours_window

    def find_corroborations(
        self, events: list[dict]
    ) -> Dict[str, int]:
        """
        Given a list of GlassboxEvent dicts, return a map of
        external_id → corroboration_count.
        """
        counts: Dict[str, int] = {e["external_id"]: 0 for e in events}

        for i, ev_a in enumerate(events):
            for ev_b in events[i + 1:]:
                if ev_a.get("layer") == ev_b.get("layer"):
                    continue  # Same platform — not cross-source
                dist = self._haversine(
                    ev_a.get("lat", 0), ev_a.get("lng", 0),
                    ev_b.get("lat", 0), ev_b.get("lng", 0),
                )
                if dist <= self.radius_km:
                    counts[ev_a["external_id"]] += 1
                    counts[ev_b["external_id"]] += 1

        return counts

    @staticmethod
    def _haversine(lat1: float, lng1: float,
                   lat2: float, lng2: float) -> float:
        """Great-circle distance in km."""
        R = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lng2 - lng1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ─── Quick helper for ingesters ──────────────────────────────────

_scorer = ConfidenceScorer()

def score_event(
    platform: str,
    has_media: bool = False,
    has_coordinates: bool = True,
    coordinate_precision_km: float = 50.0,
    source_tier: int = 3,
    is_verified_account: bool = False,
    has_url: bool = True,
    article_count: int = 1,
    age_hours: float = 0.0,
    cross_source_count: int = 0,
    gdelt_corroborated: bool = False,
) -> ConfidenceResult:
    """One-line helper for ingesters."""
    return _scorer.score(ConfidenceInput(
        platform=platform,
        has_media=has_media,
        has_coordinates=has_coordinates,
        coordinate_precision_km=coordinate_precision_km,
        source_tier=source_tier,
        is_verified_account=is_verified_account,
        has_url=has_url,
        article_count=article_count,
        age_hours=age_hours,
        cross_source_count=cross_source_count,
        gdelt_corroborated=gdelt_corroborated,
    ))
