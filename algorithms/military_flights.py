"""
Military aircraft tracking — Phase 4 algorithm #4.

Strategic context (per the 2026-05-07 data-gaps audit, item #4):
Surfaces military aircraft currently broadcasting on ADS-B. This is the
foundation of leadership-flight / military-repositioning intel — the kind
of "before MSM" signal that catches:
  - Russian/Chinese mil aircraft transiting contested airspace
  - US/NATO repositioning to forward staging areas
  - Refueling-tanker formations preceding strike packages
  - Transport-aircraft surges preceding troop deployments

Each detection writes a `military_aircraft_underway` event to the event
hypertable, deduped once per (aircraft, day). The brief surfaces these
as a tier-1 callout listing callsigns + counts by callsign-prefix family
(e.g. "20 active military aircraft: 5 GAF, 4 VIPR, 3 SHWK, ...").

What we use:
  - entity.entity_type = 'aircraft'
  - properties.military = true (adsb.lol's dbflags bit)
  - current_position_time within `lookback_min` minutes
  - dedup against prior `military_aircraft_underway` events in past
    `dedup_window_hours` for the same entity_id

What this is NOT yet (Phase 2 enhancement):
  - Curated leadership-jet watchlist (Air Force One, Russian Presidential,
    sanctioned-oligarch jets) — would prefer over the broad "all military"
    filter for higher-precision signals.
  - "Unusual departure" detector — aircraft starts moving from atypical
    airport. Requires baselining each aircraft's home base.
  - Sanctioned-airspace routing detector. Requires geofence library.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.military_flights")


DEFAULT_LOOKBACK_MIN     = 60      # vessel must have broadcast within past hour
DEFAULT_DEDUP_WINDOW_HRS = 24      # one finding per (aircraft, day)


MILITARY_FLIGHTS_SCAN_SQL = """
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'military_aircraft_underway'                              AS event_type,
    -- Subtype = first 4 chars of callsign or 'unknown' (groups by squadron-
    -- ish family so the brief can summarize "5 GAF, 3 VIPR, ..." cheaply).
    CASE
        WHEN e.display_name IS NOT NULL AND length(e.display_name) >= 4
        THEN substring(upper(e.display_name) FROM '^[A-Z]+')
        ELSE 'unknown'
    END                                                       AS event_subtype,
    NOW()                                                     AS event_time,
    e.current_geom                                            AS geom,
    5.0::real                                                 AS severity,
    'Military aircraft underway: ' ||
        COALESCE(e.display_name, 'ICAO ' || e.canonical_id)   AS title,
    'Military aircraft (ICAO ' || e.canonical_id || ') broadcasting ADS-B' ||
        COALESCE(' as ' || e.display_name, '') ||
        '. Origin: ' || COALESCE(e.properties->>'origin_country', 'unknown')
                                                              AS description,
    jsonb_build_object(
        'algorithm',           $1::text,
        'icao24',              e.canonical_id,
        'callsign',            e.display_name,
        'origin_country',      e.properties->>'origin_country',
        'last_seen',           e.current_position_time,
        'lookback_min',        $2::int,
        'attribution',         'Military flag from adsb.lol dbflags'
    )                                                         AS properties,
    'aviation'                                                AS domain,
    1440                                                      AS decay_half_life_min,
    e.id                                                      AS entity_id
FROM entity e
WHERE e.entity_type = 'aircraft'
  AND (e.properties->>'military')::boolean = true
  AND e.current_position_time IS NOT NULL
  AND e.current_geom IS NOT NULL
  AND e.current_position_time >= $3::timestamptz
  AND ($5::text IS NULL OR e.canonical_id LIKE $5)
  AND NOT EXISTS (
      SELECT 1 FROM event finding
      WHERE finding.event_type = 'military_aircraft_underway'
        AND finding.properties->>'algorithm' = $1
        AND finding.entity_id = e.id
        AND finding.event_time >= $4::timestamptz
  )
"""


async def run_military_flights_scan(
    *,
    lookback_min: int = DEFAULT_LOOKBACK_MIN,
    dedup_window_hours: int = DEFAULT_DEDUP_WINDOW_HRS,
    algorithm_tag: str = "military_flights",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Run one military-flights scan. Returns count of new findings.

    Args:
        lookback_min: only aircraft broadcasting within this many minutes
            are flagged. Default 60min — captures currently-active flights.
        dedup_window_hours: a given aircraft is flagged at most once per
            this window. Default 24h — re-emerging mil aircraft = new event.
        algorithm_tag: tagged into properties.algorithm for dedup.
        entity_canonical_id_like: optional ICAO LIKE pattern for tests.

    Returns:
        Count of `military_aircraft_underway` rows inserted on this run.
    """
    now = datetime.now(timezone.utc)
    lookback_cutoff = now - timedelta(minutes=lookback_min)
    dedup_cutoff    = now - timedelta(hours=dedup_window_hours)

    async with acquire_write() as conn:
        result = await conn.execute(
            MILITARY_FLIGHTS_SCAN_SQL,
            algorithm_tag,             # $1
            lookback_min,              # $2
            lookback_cutoff,           # $3
            dedup_cutoff,              # $4
            entity_canonical_id_like,  # $5
        )
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        _log.info(
            f"military-flights scan: {count} aircraft flagged "
            f"(lookback={lookback_min}min)"
        )
    return count
