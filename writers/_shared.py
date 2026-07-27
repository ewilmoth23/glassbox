"""
Shared helpers for the `writers` package — P3-H Phase 3 Option-A lift.

Every cross-cutting symbol that more than one writer cluster consumes
lives here, so future per-cluster modules (`writers/positions.py`,
`writers/news.py`, etc.) can import the helpers directly instead of
relying on a circular re-export through `writers/__init__.py`.

Symbols exported:

- `_parse_ts(ts_str)` — ISO-8601 → datetime, tolerates Z suffix and
  single-digit fractional seconds (EMSC quirk). Used by ALL 24 writers.

- `_EVENT_UUID_NAMESPACE` — frozen UUID namespace for deterministic
  `(event_type, external_id) → uuid` derivation. Used by all 20
  event-table writers (the 4 entity-position writers compute canonical
  ids without it).

- `_sort_batch_for_upsert(events)` — orders a batch by
  `(canonical_id, ts)` to prevent cross-writer INSERT-ON-CONFLICT
  deadlocks (P1-B, 2026-05-20). Used by the 4 ENTITY+POSITION writers
  (aircraft / vessel / satellite / sanction_entities).

- `_maybe_embed(*parts)` — best-effort sentence-transformers embedding,
  returns a pgvector literal or None when embeddings unavailable. Used
  by the 5 text-heavy writers (news, gdelt_bulk, hn, newsdata, sec_filing).

- `_LAYER_TO_PLATFORM` + `_with_confidence(props, layer)` — confidence
  scoring lookup + mutator. Used by 21 of 24 writers (P3-N, 2026-05-20).

Optional dependencies (`confidence_scorer`, `embeddings`) are loaded
lazily with try/except; failures yield no-op helpers so the writer
pipeline never blocks on missing ML deps.

This module imports ONLY stdlib + `ingesters.base.GlassboxEvent` +
two optional first-party modules. It must NEVER import from `writers`
itself (would create a cycle).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ingesters.base import GlassboxEvent

# ─── Optional embedding dependency ────────────────────────────────────────
# Phase 4a (2026-05-09): writers for text-heavy event types (news, gdelt_bulk,
# hn, newsdata, sec_filing) compute and persist a 384-dim vector when
# sentence-transformers is loadable. Failures are silent — embedding is
# best-effort and never blocks the write.
try:
    from embeddings import embed_text as _embed_text, to_pgvector_literal as _to_pgvec
except Exception:  # noqa: BLE001
    _embed_text = None
    _to_pgvec = None


def _maybe_embed(*parts: Optional[str]) -> Optional[str]:
    """Compose text from non-empty parts and return its pgvector literal,
    or None if embedding is unavailable / text is empty."""
    if _embed_text is None or _to_pgvec is None:
        return None
    bits = [s.strip() for s in parts if s and s.strip()]
    if not bits:
        return None
    vec = _embed_text(". ".join(bits))
    return _to_pgvec(vec) if vec else None


# Stable namespace for deterministic event-row UUIDs derived from
# (event_type, external_id). Generated once and frozen — must not change
# across releases or it'd break dedup semantics.
_EVENT_UUID_NAMESPACE = uuid.UUID("a4d92b16-1c4f-4e2c-8f3a-1f0e2c4d8b6a")


def _parse_ts(ts_str: str) -> datetime:
    """Parse a GlassboxEvent.ts (always ISO-8601 UTC) into a datetime.

    Tolerates a few real-world variants seen across ingesters:
      - 'Z' suffix vs '+00:00'
      - Single-digit fractional seconds ('2026-05-08T01:00:00.0Z') — EMSC
        emits these and Python 3.9's fromisoformat() rejects them.
        We normalize to 6 digits.
    """
    import re as _re
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    # Normalize fractional seconds to 6 digits (Python 3.9 requires 0/3/6).
    ts_str = _re.sub(
        r"(\.\d+)(?=[+\-Z]|$)",
        lambda m: ("." + (m.group(1)[1:] + "000000")[:6]),
        ts_str,
    )
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ─── Confidence scoring (P3-N, 2026-05-20) ────────────────────────────────
# Map each writer's `layer` to a PLATFORM_BASELINE key in confidence_scorer.py.
# This gives every event a first-pass confidence score persisted in its
# `properties` jsonb. Existing social-media ingesters (citizen_*, traffic_cams)
# compute confidence at ingest time with rich source signals (has_media,
# source_tier, etc.) and inject `confidence_score`/`confidence_label` directly
# into the event payload. For the non-social writers, the ingester doesn't
# expose those signals, so the writer-side scorer uses the layer baseline + the
# GlassboxEvent's lat/lng (always present for these layers) as inputs. Crude
# but useful: an earthquake gets ~0.95, a GDELT news event gets ~0.55, a SEC
# filing gets ~0.75. Downstream consumers can stratify on these without
# touching every ingester.
#
# Adding a new event writer? Add a `layer → platform` entry to _LAYER_TO_PLATFORM
# and call `_with_confidence(props_dict, ev.layer)` right before the
# `json.dumps(props_dict)` line. The helper mutates in place and is a no-op
# when the layer has no mapping (so partial coverage doesn't break anything).
try:
    from confidence_scorer import score_event as _score_event
    _CONFIDENCE_OK = True
except ImportError:
    _score_event = None  # type: ignore[assignment]
    _CONFIDENCE_OK = False

_LAYER_TO_PLATFORM: Dict[str, str] = {
    # Authoritative real-time feeds
    "planes":             "ads_b",       # 0.90 — ADS-B legally required for tx
    "ships":              "ais",         # 0.85 — AIS legally required for tx
    "earthquakes":        "earthquake",  # 0.95 — USGS / EMSC authoritative
    # Predicted/calculated positions from trusted source
    "satellites":         "manual",      # 0.75 — SGP4 propagated from official TLE
    # OSINT aggregators (variable upstream signal)
    "news":               "gdelt",       # 0.55 — GDELT mention-geocoded
    # Single-platform OSINT
    "hacker_news":        "reddit_osint",   # 0.50 — community self-corrects, slow
    "social_bluesky":     "bluesky_osint",  # 0.48 — Bluesky OSINT presence growing
    # Government / official curated feeds (no dedicated "gov authoritative" key
    # in PLATFORM_BASELINE; using `manual` 0.75 as the closest analog — official
    # data sets but not real-time transponder-style)
    "weather_alerts":     "manual",  # NOAA NWS
    "space_weather":      "manual",  # NOAA DONKI
    "wildfires":          "manual",  # NASA FIRMS
    "natural_events":     "manual",  # NASA EONET
    "volcanic_activity":  "manual",  # Smithsonian GVP
    "gdacs":              "manual",
    "tropical_storms":    "manual",  # NHC / JTWC
    "fema_declarations":  "manual",  # FEMA OpenFEMA
    "metar":              "manual",  # NOAA aviation weather
    "air_quality":        "manual",  # WAQI / OpenAQ
    "neo_asteroids":      "manual",  # NASA NEO
    "securities_filings": "manual",  # SEC EDGAR
    # Sanctions entity layer — these are list-membership facts, not events;
    # confidence here would mean "is the listing authoritative" which is 1.0
    # by definition (OFAC/UK/EU official lists). Use `manual` 0.75 since we
    # don't have a dedicated "official-list" key.
    "sanctions":          "manual",
    # Cyber-attack data layers (P2-A, 2026-05-27). CISA KEV + Spamhaus DROP
    # are both official curated catalogs; analogous to NOAA NWS / SEC EDGAR
    # in trust posture, so use the `manual` 0.75 baseline.
    "cyber_kev":          "manual",
    "cyber_spamhaus_drop": "manual",
    # P2-B Phase 1.5 — live ingester upgrade for the climate_forecast static layer
    "climate_forecast":   "manual",
}


def _with_confidence(props_dict: dict, layer: str) -> dict:
    """Mutate `props_dict` to add `confidence_score` + `confidence_label`
    if `layer` has a PLATFORM_BASELINE mapping. No-op when the layer is
    unmapped (so writers that haven't been wired yet still work). Returns
    the same dict for convenient chaining."""
    if not _CONFIDENCE_OK or _score_event is None:
        return props_dict
    platform = _LAYER_TO_PLATFORM.get(layer)
    if platform is None:
        return props_dict
    try:
        result = _score_event(
            platform=platform,
            has_coordinates=True,
            coordinate_precision_km=1.0,
        )
        props_dict["confidence_score"] = round(float(result.score), 3)
        props_dict["confidence_label"] = result.label
    except Exception:
        # Defensive — confidence scoring is best-effort. If it crashes
        # we'd rather drop the field than block the event INSERT.
        pass
    return props_dict


def _sort_batch_for_upsert(events: List[GlassboxEvent]) -> List[GlassboxEvent]:
    """Order a batch of position-entity events by (canonical_id, ts) so that
    any two concurrent writers process shared canonical_ids in the same order.

    P1-B (2026-05-20): Postgres deadlock log analysis showed two
    `INSERT INTO entity ... ON CONFLICT DO UPDATE` from different writers
    waiting on each other's transaction (ShareLock on xid). Root cause:
    each writer batches multiple entities per transaction, and two batches
    from different upstream feeds (e.g., aisstream + digitraffic both
    observing the same MMSI within the same scan tick) could touch shared
    rows in opposite orders → A locks X then waits for Y, B locks Y then
    waits for X → deadlock. Pre-fix rate: ~32/day across 12.7 days of logs
    (406 events, postgres+server in lockstep).

    Sorting once per batch is O(n log n) and Free at the runtime cost
    compared to a Postgres-aborted transaction that has to retry. Crucially,
    both writers (anywhere this helper is invoked) must use the SAME ordering
    function — if any one skips, deadlock potential returns.

    Events missing external_id are kept in their original positions but at
    the end of the sort order (they're skipped inside the per-event loop
    anyway, but the sort is stable around them). ts is the tiebreaker so
    multi-position-same-MMSI batches process chronologically; the entity
    row's GREATEST/CASE guards make this advisory rather than required.
    """
    return sorted(events, key=lambda e: (e.external_id or "￿", e.ts or ""))
