"""
Sanctioned-vessel-went-dark detection — Phase 4 algorithm #8 (combined signal).

This is the KILLER SIGNAL the data-gaps audit was pointing at: when a vessel
that's on the OFAC SDN sanctioned-vessel list AND has been broadcasting on
AIS suddenly goes dark, that's sanctions evasion in real time. The pattern:

  Russian/Iranian shadow-fleet tanker (sanctioned) → broadcasting AIS in
  Baltic / Black Sea → turns off transponder → moves to ship-to-ship
  transfer point → re-emerges with new origin metadata

Each individual signal (sanctioned_vessel_underway, dark_vessel_detected) is
useful but noisy. The COMBINATION is gold — among ~900 sanctioned vessels
broadcasting at any time, the ones that go DARK while transiting Baltic /
Black Sea / sanctioned-port-vicinity are the highest-priority watch.

Algorithm:
  Find vessel entities where ALL of:
    - entity_type = 'vessel'
    - properties.imo MATCHES a sanctioned_vessel entity's properties.imo
      (precise) OR display_name fuzzy-matches a sanctioned_vessel name
      (less precise — but caught renamed shadow-fleet, e.g. ADMIRAL ↔ HS Star)
    - current_position_time is between 6h and 14d ago
    - last position_track velocity > 0.5 m/s (was moving, not anchored)

  Each match emits a `sanctioned_vessel_went_dark` event at severity 10
  with both the sanctioned-vessel canonical_id AND the live-vessel id in
  properties for forensic traceability.

Idempotency: same (live_vessel_id, sanctioned_canonical_id) flagged at most
once per dedup window (default 24h).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.sanctioned_dark_vessel")


SANCTIONED_DARK_SCAN_SQL = """
WITH dark_vessels AS (
    -- Vessels that have gone dark while underway, narrowed to those whose
    -- last position_track row had non-zero velocity.
    SELECT
        e.id              AS vessel_id,
        e.canonical_id    AS mmsi,
        e.display_name    AS display_name,
        e.properties      AS properties,
        e.current_geom    AS current_geom,
        e.current_position_time AS last_seen_ais,
        EXTRACT(EPOCH FROM (NOW() - e.current_position_time)) / 3600.0
                          AS hours_dark,
        pt.velocity_ms    AS last_velocity_ms
    FROM entity e
    LEFT JOIN LATERAL (
        SELECT velocity_ms FROM position_track
        WHERE entity_id = e.id ORDER BY time DESC LIMIT 1
    ) pt ON TRUE
    WHERE e.entity_type = 'vessel'
      AND e.current_position_time IS NOT NULL
      AND e.current_geom IS NOT NULL
      AND e.current_position_time < $1::timestamptz   -- gone for >= threshold
      AND e.current_position_time > $2::timestamptz   -- but seen recently
      AND pt.velocity_ms IS NOT NULL
      AND pt.velocity_ms > 0.5
      AND (
          $5::text IS NULL
          OR e.canonical_id LIKE ('%' || trim(both '%' from $5) || '%')
      )
),
matched AS (
    -- Cross-reference with sanctioned_vessel entries. IMO match preferred
    -- (zero false positives); name-fuzzy fallback matches renamed
    -- shadow-fleet vessels.
    SELECT
        d.vessel_id,
        d.mmsi,
        d.display_name        AS live_name,
        d.current_geom,
        d.last_seen_ais,
        d.hours_dark,
        d.last_velocity_ms,
        d.properties          AS live_props,
        sv.id                 AS sanctioned_id,
        sv.canonical_id       AS sanctioned_canonical_id,
        sv.display_name       AS sanctioned_name,
        sv.properties         AS sanctioned_props,
        CASE
            WHEN d.properties->>'imo' IS NOT NULL
             AND sv.properties->>'imo' IS NOT NULL
             AND d.properties->>'imo' = sv.properties->>'imo'
            THEN 'imo'
            ELSE 'name'
        END AS match_kind
    FROM dark_vessels d
    JOIN entity sv
      ON sv.entity_type = 'sanctioned_vessel'
     AND (
         $5::text IS NULL
         OR sv.canonical_id LIKE ('%' || trim(both '%' from $5) || '%')
     )
     AND (
         -- IMO exact match (definitive)
         (d.properties->>'imo' IS NOT NULL
          AND sv.properties->>'imo' IS NOT NULL
          AND d.properties->>'imo' = sv.properties->>'imo')
         OR
         -- Name fuzzy fallback — ONLY when IMO comparison cannot be made.
         -- See sanctions_match.py for the rationale: different IMOs are
         -- DIFFERENT vessels per the IMO 7-digit numbering scheme, so the
         -- old "NOT(imo-match)" guard caused false positives. Name match
         -- is reliable only when at least one IMO is missing.
         ((d.properties->>'imo' IS NULL OR sv.properties->>'imo' IS NULL)
          AND d.display_name IS NOT NULL
          AND sv.display_name IS NOT NULL
          AND length(d.display_name) >= 4
          AND length(sv.display_name) >= 4
          AND upper(d.display_name) % upper(sv.display_name)
          AND similarity(upper(d.display_name), upper(sv.display_name)) >= 0.9)
     )
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'sanctioned_vessel_went_dark'                              AS event_type,
    m.match_kind || '_match'                                   AS event_subtype,
    NOW()                                                      AS event_time,
    m.current_geom                                             AS geom,
    10.0::real                                                 AS severity,  -- max
    'CRITICAL — Sanctioned vessel went dark: ' ||
        COALESCE(m.live_name, 'MMSI ' || m.mmsi) ||
        CASE WHEN m.match_kind = 'imo' THEN ' [IMO match]' ELSE '' END
                                                               AS title,
    'OFAC-SDN sanctioned vessel ' || COALESCE(m.live_name, m.mmsi) ||
        ' (MMSI ' || m.mmsi || ') has gone dark on AIS for ' ||
        ROUND(m.hours_dark::numeric, 1) || 'h. Last position broadcast at ' ||
        m.last_seen_ais || '. OFAC SDN match: "' || m.sanctioned_name ||
        '" via ' || m.match_kind || ' match.'
                                                               AS description,
    jsonb_build_object(
        'algorithm',                $4::text,
        'match_kind',               m.match_kind,
        'mmsi',                     m.mmsi,
        'live_vessel_name',         m.live_name,
        'live_imo',                 m.live_props->>'imo',
        'sanctioned_canonical_id',  m.sanctioned_canonical_id,
        'sanctioned_name',          m.sanctioned_name,
        'sanctioned_imo',           m.sanctioned_props->>'imo',
        'last_seen_ais',            m.last_seen_ais,
        'hours_dark',               ROUND(m.hours_dark::numeric, 2),
        'last_velocity_ms',         m.last_velocity_ms,
        'fcra_safe',                false,
        'sanctioning_authority',    'US Treasury OFAC'
    )                                                          AS properties,
    'maritime'                                                 AS domain,
    1440                                                       AS decay_half_life_min,
    m.vessel_id                                                AS entity_id
FROM matched m
WHERE NOT EXISTS (
    SELECT 1 FROM event finding
    WHERE finding.event_type = 'sanctioned_vessel_went_dark'
      AND finding.properties->>'algorithm' = $4
      AND finding.entity_id = m.vessel_id
      AND finding.properties->>'sanctioned_canonical_id' = m.sanctioned_canonical_id
      AND finding.event_time >= $3::timestamptz
)
"""


async def run_sanctioned_dark_scan(
    *,
    dark_threshold_hours: int = 6,
    lookback_hours: int = 14 * 24,
    dedup_window_hours: int = 24,
    algorithm_tag: str = "sanctioned_dark",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Run one sanctioned-dark-vessel scan. Returns count of new findings.

    Args:
        dark_threshold_hours: vessel must be silent for at least this long
            (default 6h — same as dark_ship algorithm).
        lookback_hours: only consider vessels seen within this many hours
            (default 14d).
        dedup_window_hours: same (live_vessel, sanctioned_entry) pair
            flagged at most once per this window. Default 24h.
        algorithm_tag: tag for properties.algorithm (test isolation).
        entity_canonical_id_like: optional MMSI LIKE for tests.
    """
    now = datetime.now(timezone.utc)
    threshold_cutoff = now - timedelta(hours=dark_threshold_hours)
    lookback_cutoff  = now - timedelta(hours=lookback_hours)
    dedup_cutoff     = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            SANCTIONED_DARK_SCAN_SQL,
            threshold_cutoff,            # $1 — current_position_time < this
            lookback_cutoff,             # $2 — current_position_time > this
            dedup_cutoff,                # $3 — finding.event_time >= this
            algorithm_tag,               # $4
            entity_canonical_id_like,    # $5
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"sanctioned-dark scan: {count} sanctioned vessels gone dark "
            f"(threshold={dark_threshold_hours}h)"
        )
    return count
