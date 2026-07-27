"""
Multi-jurisdictional sanctions match — Phase 4 algorithm #11.

Strategic context: a vessel listed by multiple sanctioning authorities
(OFAC + EU + UK, in any combination) is a stronger signal than a single-
authority listing. It typically means:
  - The vessel is a known shadow-fleet asset surfaced by multiple
    jurisdictions independently.
  - Cross-border financial enforcement has converged.
  - The owner has likely already pivoted aliases / flag of convenience
    (otherwise the authorities would have caught and unified earlier).

This algorithm runs AFTER sanctions_match has emitted single-authority
findings. It groups recent `sanctioned_vessel_underway` events by live
vessel (entity_id) and counts distinct authorities. When ≥2 distinct
authorities have flagged the same vessel within the dedup window, we
emit a `sanctioned_vessel_multijurisdictional` event with severity 10
and a CRITICAL tier flag so it surfaces above single-authority hits.

This is purely a post-processing reduction — no new entity / vessel
joins. The single-authority events are kept for audit and entity-detail
drilldown; the new multi-jurisdictional event is the "lead" signal.

Idempotency:
  Same (live_vessel_id, authority_set) pair flagged at most once per
  24h. Authority set is computed via array_agg DISTINCT and hashed by
  sorting + comma-joining, so re-runs with the same set are no-ops.
  When a new authority joins (new row added to OFSI/CFSP after the
  fact), the authority set changes and a fresh event fires.

Performance:
  Single SQL self-join on `event` table. The filter
  (event_time >= dedup_cutoff AND event_type = 'sanctioned_vessel_underway')
  uses the `idx_event_event_type_event_time` index. At v1.0 scale (~1k
  underway events per 24h, of which only 35 are non-OFAC), the candidate
  set is tiny.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.sanctions_multijurisdictional")


SANCTIONS_MULTIJURISDICTIONAL_SQL = """
-- Group the per-authority sanctioned_vessel_underway events by live vessel.
-- We rely on properties.sanctioning_authority being authoritative on each
-- single-authority event; older OFAC-only events have it hardcoded so they
-- don't bias the count.
WITH per_vessel AS (
    SELECT
        e.entity_id                                         AS v_id,
        e.geom                                              AS v_geom,
        COUNT(DISTINCT e.properties->>'sanctioning_authority')
                                                            AS authority_count,
        array_agg(DISTINCT e.properties->>'sanctioning_authority'
                  ORDER BY e.properties->>'sanctioning_authority')
                                                            AS authorities,
        BOOL_OR(e.event_subtype = 'imo_match')              AS any_imo,
        max(e.event_time)                                   AS most_recent_match,
        -- Pull the live vessel name from the most-recent contributing event
        (array_agg(e.properties->>'live_vessel_name'
                   ORDER BY e.event_time DESC))[1]          AS live_vessel_name,
        (array_agg(e.properties->>'mmsi'
                   ORDER BY e.event_time DESC))[1]          AS mmsi,
        (array_agg(e.properties->>'live_imo'
                   ORDER BY e.event_time DESC))[1]          AS live_imo
    FROM event e
    WHERE e.event_type = 'sanctioned_vessel_underway'
      AND e.event_time >= $2::timestamptz
      AND e.entity_id IS NOT NULL
      AND e.geom IS NOT NULL
    GROUP BY e.entity_id, e.geom
    HAVING COUNT(DISTINCT e.properties->>'sanctioning_authority') >= 2
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'sanctioned_vessel_multijurisdictional'                 AS event_type,
    CASE
        WHEN p.authority_count >= 3 THEN 'tri_listed'
        ELSE 'dual_listed'
    END                                                     AS event_subtype,
    NOW()                                                   AS event_time,
    p.v_geom                                                AS geom,
    -- Tri-listed gets max severity; dual-listed gets 10 too since
    -- multi-jurisdictional alone is the moneyshot signal.
    10.0::real                                              AS severity,
    'CRITICAL — Sanctioned vessel underway, listed by ' ||
        p.authority_count || ' authorities: ' ||
        COALESCE(p.live_vessel_name, 'MMSI ' || p.mmsi) ||
        ' [' || array_to_string(p.authorities, ' + ') || ']'
                                                            AS title,
    'Live AIS vessel ' || COALESCE(p.live_vessel_name, p.mmsi) ||
        ' (MMSI ' || p.mmsi || ')' ||
        COALESCE(', IMO ' || p.live_imo, '') ||
        ' has been independently flagged by ' || p.authority_count ||
        ' sanctioning authorities (' || array_to_string(p.authorities, ', ') ||
        ') in the last ' || $3::int || ' hours. Multi-jurisdictional ' ||
        'convergence on a single vessel is a strong shadow-fleet / ' ||
        'evasion-pattern signal.'                           AS description,
    jsonb_build_object(
        'algorithm',                $1::text,
        'mmsi',                     p.mmsi,
        'live_vessel_name',         p.live_vessel_name,
        'live_imo',                 p.live_imo,
        'authority_count',          p.authority_count,
        'authorities',              p.authorities,
        'authority_set_key',        array_to_string(p.authorities, '|'),
        'any_imo_match',            p.any_imo,
        'most_recent_match',        p.most_recent_match,
        'fcra_safe',                false,
        'multi_jurisdictional',     true
    )                                                       AS properties,
    'maritime'                                              AS domain,
    1440                                                    AS decay_half_life_min,
    p.v_id                                                  AS entity_id
FROM per_vessel p
WHERE (
        $4::text IS NULL
        OR p.mmsi LIKE $4
      )
  AND NOT EXISTS (
      SELECT 1 FROM event prior
      WHERE prior.event_type = 'sanctioned_vessel_multijurisdictional'
        AND prior.properties->>'algorithm' = $1
        AND prior.entity_id = p.v_id
        -- Authority-set-aware dedup: a new authority joining changes
        -- the key and triggers a fresh event.
        AND prior.properties->>'authority_set_key' = array_to_string(p.authorities, '|')
        AND prior.event_time >= $5::timestamptz
  )
"""


async def run_sanctions_multijurisdictional_scan(
    *,
    lookback_hours: int = 24,
    dedup_window_hours: int = 24,
    algorithm_tag: str = "sanctions_multijurisdictional",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Detect live vessels matched by ≥2 sanctioning authorities. Returns
    count of new findings.

    Args:
        lookback_hours: only group sanctioned_vessel_underway events from
            within this window. Default 24h — single-authority dedup is
            also 24h, so this matches the firing rate.
        dedup_window_hours: same (vessel, authority_set) flagged at most
            once per this window. Default 24h.
        algorithm_tag: written to properties.algorithm so test runs don't
            collide with production runs.
        entity_canonical_id_like: optional MMSI LIKE pattern for tests.

    Returns:
        Count of `sanctioned_vessel_multijurisdictional` rows inserted.
    """
    now = datetime.now(timezone.utc)
    lookback_cutoff = now - timedelta(hours=lookback_hours)
    dedup_cutoff    = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            SANCTIONS_MULTIJURISDICTIONAL_SQL,
            algorithm_tag,                # $1
            lookback_cutoff,              # $2
            lookback_hours,               # $3
            entity_canonical_id_like,     # $4
            dedup_cutoff,                 # $5
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"sanctions multijurisdictional: {count} live vessels flagged "
            f"by ≥2 authorities in last {lookback_hours}h"
        )
    return count
