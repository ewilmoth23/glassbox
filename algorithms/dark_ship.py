"""
Dark-ship detection — Phase 4 algorithm #2.

Strategic context: per the 2026-05-07 data-gaps audit, AIS gap detection
("ship goes dark = sanctions evasion / military staging / illegal fishing")
is the **highest-leverage "before MSM" signal** in the maritime domain. News
headlines about "mysterious vessel last seen near contested waters" come AFTER
AIS goes dark in real-time feeds. We detect it the moment it stops.

What we look for:
  - vessel in `entity` with `entity_type='vessel'`
  - `current_position_time` between 6 hours and 14 days ago
    (older = "stale, probably out of coverage forever"; newer = "still active")
  - last known velocity > 0.5 m/s (i.e. was actually moving when it went dark —
    excludes anchored/moored vessels which legitimately stop broadcasting)
  - not already flagged dark in the past 24 hours (dedup)

Each detection is written to the `event` hypertable as event_type='dark_vessel_detected'.
event_subtype categorizes by dark duration: 'short' (6-24h) / 'medium' (1-7d) / 'long' (7-14d).
event.entity_id back-links to the dark vessel.
severity scales linearly with hours_dark, capped at 10.

The bonus signal is when a sanctioned vessel goes dark — this fires
"sanctioned_vessel_went_dark" via cross-reference. Phase 2-G persisted OFAC
SDN entries to entity_type='sanctioned_vessel'; once vessel display_names
work (currently NULL — see audit item #9b), this becomes a single SQL JOIN.
For v1.0 we surface the simpler "any vessel went dark" signal first.

Idempotency:
  Each *distinct dark period* is flagged exactly once. The dedup key is
  (entity_id, last_seen_ais) — a vessel that's been silent since the
  same timestamp is the same dark event regardless of how long it lasts.
  The prior 24h-window-only dedup over-fired catastrophically: a vessel
  silent for 7 days emitted 7 findings (one per 24h cycle), all carrying
  the same last_seen_ais, all reporting the same underlying event.
  See audit 2026-05-13 NIGHT for the 107k findings / 34k unique vessel
  symptom this fix addresses.

  Re-emission DOES happen if a vessel re-emerges (last_seen_ais updates)
  and then goes dark again — that's a genuinely new dark period.

Performance:
  Single SQL statement. Filter is `entity_type='vessel'` (already indexed via
  entity_type_idx) plus a time-window filter on the denormalized
  `current_position_time` column (also indexed via
  entity_type_current_time_idx). Lateral subquery into position_track for
  last velocity uses pt_entity_time_idx. Expected runtime at v1.0 scale
  (~18K vessels): well under 1s.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.dark_ship")


# Window bounds (hours)
DEFAULT_DARK_THRESHOLD_HOURS = 6     # vessel must be >6h dark to qualify
DEFAULT_LOOKBACK_HOURS       = 14 * 24  # but seen at least within past 14 days
# Dedup now keys on (entity_id, last_seen_ais), so the window only needs
# to be long enough to cover the longest plausible dark period. 30 days
# >> 14-day lookback, so any vessel still in lookback range can match
# its prior finding. Was 24h — caused the over-firing audited 2026-05-13.
DEFAULT_DEDUP_WINDOW_HOURS   = 30 * 24


DARK_SHIP_SCAN_SQL = """
-- Two-stage candidate selection so we can compute receiver-shed cohort
-- size with a window function. See
-- ALGORITHM_FP_AUDIT_dark_ship_2026_05_19.md for the empirical analysis
-- behind the cohort-size suppression — the audit found 99.6% of historical
-- findings (~209k of 210k) were AIS-feed-disconnect artifacts rather than
-- real shadow-fleet behavior.
WITH dark_candidates AS (
    SELECT
        e.id                                                  AS entity_id,
        e.canonical_id                                        AS mmsi,
        e.display_name,
        e.current_position_time                               AS last_seen_ais,
        EXTRACT(EPOCH FROM (NOW() - e.current_position_time)) / 3600.0
                                                              AS hours_dark,
        e.current_geom                                        AS last_geom,
        pt.velocity_ms                                        AS last_velocity_ms,
        pt.heading_deg                                        AS last_heading_deg
    FROM entity e
    LEFT JOIN LATERAL (
        SELECT velocity_ms, heading_deg
        FROM position_track
        WHERE entity_id = e.id
        ORDER BY time DESC
        LIMIT 1
    ) pt ON TRUE
    WHERE e.entity_type = 'vessel'
      AND e.current_position_time IS NOT NULL
      AND e.current_geom IS NOT NULL
      AND e.current_position_time < $2::timestamptz
      AND e.current_position_time > $3::timestamptz
      AND pt.velocity_ms IS NOT NULL
      AND pt.velocity_ms > 0.5
      AND ($5::text IS NULL OR e.canonical_id LIKE $5)
),
-- Count how many candidates share each second of last_seen_ais. If
-- ≥6, this is almost certainly an AIS ingester (aisstream / digitraffic
-- / barentswatch / DMA) dropping its websocket connection — the receiver
-- froze and every vessel it was tracking has the same `last_seen_ais`
-- to the second. The binomial p-value for 6 independent vessels going
-- dark in the same one-second window with ~18k tracked vessels is ~1e-8,
-- so this is a reliable signal of infrastructure failure, not fleet
-- behavior. Threshold of 6 was derived from the 2026-05-19 audit:
-- buckets <6 hold 0.4% of historical findings (real-signal density);
-- buckets ≥6 hold 99.6% (receiver artifacts).
candidates_with_cohort AS (
    SELECT
        dc.*,
        COUNT(*) OVER (PARTITION BY date_trunc('second', last_seen_ais))
            AS cohort_size
    FROM dark_candidates dc
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'dark_vessel_detected'                                   AS event_type,
    CASE
        WHEN candidates.hours_dark < 24  THEN 'short'
        WHEN candidates.hours_dark < 168 THEN 'medium'
        ELSE                                  'long'
    END                                                       AS event_subtype,
    NOW()                                                     AS event_time,
    candidates.last_geom                                      AS geom,
    LEAST(10.0, GREATEST(1.0, candidates.hours_dark / 12.0))::real
                                                              AS severity,
    'Vessel went dark: ' ||
        COALESCE(candidates.display_name, 'MMSI ' || candidates.mmsi)
                                                              AS title,
    'Last AIS broadcast ' || ROUND(candidates.hours_dark::numeric, 1) ||
        'h ago (was moving at ' ||
        ROUND(candidates.last_velocity_ms::numeric, 1) || ' m/s); MMSI ' ||
        candidates.mmsi                                       AS description,
    jsonb_build_object(
        'algorithm',           $1::text,
        'mmsi',                candidates.mmsi,
        'last_seen_ais',       candidates.last_seen_ais,
        'hours_dark',          ROUND(candidates.hours_dark::numeric, 2),
        'last_velocity_ms',    ROUND(candidates.last_velocity_ms::numeric, 2),
        'last_heading_deg',    candidates.last_heading_deg,
        'cohort_size',         candidates.cohort_size
    )                                                         AS properties,
    'maritime'                                                AS domain,
    1440                                                      AS decay_half_life_min,
    candidates.entity_id                                      AS entity_id
FROM candidates_with_cohort candidates
WHERE candidates.cohort_size < 6  -- suppress receiver-shed artifacts
  AND NOT EXISTS (
      -- Dedup on (entity_id, last_seen_ais): once we've flagged a
      -- vessel for a given dark-period start, don't re-flag it for
      -- the same period regardless of how long it lasts. Lookback
      -- on $4 (default 30 days) generously covers any single dark
      -- period — if you ever shorten it, vessels could re-fire once
      -- their original finding ages out, which is the old bug.
      SELECT 1 FROM event finding
      WHERE finding.event_type = 'dark_vessel_detected'
        AND finding.properties->>'algorithm' = $1
        AND finding.entity_id = candidates.entity_id
        AND finding.event_time >= $4::timestamptz
        AND (finding.properties->>'last_seen_ais')::timestamptz
            = candidates.last_seen_ais
  )
"""


async def run_dark_ship_scan(
    *,
    dark_threshold_hours: int = DEFAULT_DARK_THRESHOLD_HOURS,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    dedup_window_hours: int = DEFAULT_DEDUP_WINDOW_HOURS,
    algorithm_tag: str = "dark_ship",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Run one dark-ship detection pass. Returns count of new findings inserted.

    Args:
        dark_threshold_hours: vessel must have been silent for at least this
            many hours to qualify. Default 6h. AIS broadcasts every 2-180s
            when underway; 6h of silence is unmistakably abnormal.
        lookback_hours: only consider vessels we've heard from at least this
            recently. Older = "out of coverage indefinitely, not interesting."
            Default 14 days.
        dedup_window_hours: a given vessel is flagged dark at most once per
            this window. Default 24h. Re-emergence + re-disappearance within
            24h is treated as one event.
        algorithm_tag: written to `properties.algorithm` so different runs
            (test vs production) don't dedup against each other.
        entity_canonical_id_like: optional SQL LIKE pattern for tests to
            isolate from production data. Production passes None.

    Returns:
        Count of `dark_vessel_detected` rows inserted on this run.
    """
    # Pre-compute time cutoffs in Python so the prepared statement has
    # unambiguous timestamptz parameters (asyncpg can't always infer the
    # type of `$N::int * INTERVAL` deep inside a NOT EXISTS subquery).
    now = datetime.now(timezone.utc)
    threshold_cutoff = now - timedelta(hours=dark_threshold_hours)
    lookback_cutoff  = now - timedelta(hours=lookback_hours)
    dedup_cutoff     = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            DARK_SHIP_SCAN_SQL,
            algorithm_tag,             # $1
            threshold_cutoff,          # $2 — current_position_time < this
            lookback_cutoff,           # $3 — current_position_time > this
            dedup_cutoff,              # $4 — finding.event_time >= this
            entity_canonical_id_like,  # $5
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"dark-ship scan: {count} vessels flagged dark "
            f"(threshold={dark_threshold_hours}h, lookback={lookback_hours}h)"
        )
    return count
