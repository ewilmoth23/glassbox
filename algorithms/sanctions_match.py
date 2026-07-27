"""
Sanctions match — Phase 4 algorithm #3.

Strategic context: cross-references live AIS-broadcasting vessels against the
OFAC SDN sanctioned-vessel list. When a live vessel's name matches a
sanctioned entry with high similarity, we emit a `sanctioned_vessel_underway`
event — surfacing in real-time which sanctioned vessels are currently
transiting our AIS coverage.

This is the first half of the "before MSM" maritime signal. The second half
(sanctioned-vessel-went-dark) falls out automatically: if a sanctioned vessel
stops broadcasting, the dark_ship algorithm flags it as dark, and a SQL JOIN
of `dark_vessel_detected` ↔ `sanctioned_vessel_underway` events surfaces the
evasion-in-progress signal.

Match criteria (v1 — name-only):
  - vessel.entity_type = 'vessel' (live AIS feed)
  - sv.entity_type    = 'sanctioned_vessel' (OFAC SDN)
  - upper(vessel.display_name) % upper(sv.display_name)  (trigram threshold)
  - similarity >= 0.9 (filters most false positives; "Astra"/"ASTRA" = 1.0,
    "AmberStar"/"AMBER" ≈ 0.4 wouldn't pass)
  - vessel.current_position_time within past `lookback_min` minutes (default
    24h — vessel must have broadcast recently to count as "underway")

Phase 2 enhancement (TBD): extract IMO from OFAC SDN XML and prefer
IMO-exact match over name-fuzzy. OFAC stores IMO under
<IDRegistrationDocument> with DocumentTypeID=1626 ("Vessel Registration
Identification"). Until that's wired, name-fuzzy is the primary key.

Idempotency:
  Same (live_vessel_id, sanctioned_vessel_id) pair flagged at most once per
  24h via NOT EXISTS clause. This keeps the firehose from re-emitting the
  same 20+ findings every 5 minutes. Re-emergence after 24h flagged again.

Performance:
  Single SQL self-join on `entity` table. The `entity_display_trgm` GIN
  index (created in init.sql) makes the trigram match fast. At v1.0 scale
  (1,481 sanctioned + 18,334 vessels) the candidate set is tiny.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import acquire_write


_log = logging.getLogger("algorithms.sanctions_match")


SANCTIONS_MATCH_SQL = """
-- IMO-priority vessel/sanction match. Two paths fold into one query via
-- a single OR'd JOIN condition + a CASE expression that picks the match kind.
--   IMO path: both sides have IMO populated AND IMOs are equal — zero
--             false positives possible (IMO is globally unique).
--   Name path: trigram similarity >= threshold; used only when IMO match
--             would NOT have caught this exact pair (the NOT-imo-match
--             clause inside the OR).
WITH matched AS (
    SELECT
        v.id           AS v_id,
        v.canonical_id AS v_canonical_id,
        v.display_name AS v_display_name,
        v.current_geom AS v_current_geom,
        v.current_position_time AS v_current_position_time,
        v.properties   AS v_props,
        sv.id          AS sv_id,
        sv.canonical_id AS sv_canonical_id,
        sv.display_name AS sv_display_name,
        sv.properties  AS sv_props,
        CASE
            WHEN v.properties->>'imo'  IS NOT NULL
             AND sv.properties->>'imo' IS NOT NULL
             AND v.properties->>'imo'  = sv.properties->>'imo'
            THEN 'imo'
            ELSE 'name'
        END AS match_kind
    FROM entity v
    JOIN entity sv
      ON sv.entity_type = 'sanctioned_vessel'
     AND (
         -- IMO exact match (definitive; covers renamed shadow fleet that
         -- kept its IMO — common pattern, IMO is permanent per IMO 7-digit
         -- numbering scheme).
         (v.properties->>'imo'  IS NOT NULL
          AND sv.properties->>'imo' IS NOT NULL
          AND v.properties->>'imo' = sv.properties->>'imo')
         OR
         -- Name fuzzy match — ONLY when at least one IMO is unknown.
         -- Critical safety filter (2026-05-14): the prior guard was
         -- `NOT (imo-match-condition)` which fires whenever IMOs DIFFER
         -- in addition to when one is NULL. That caused false positives
         -- for vessels that genuinely share a common name (e.g. two
         -- unrelated vessels both named "Antey", "Jamaica", etc.) when
         -- both have IMOs that just happen to differ. Different IMOs =
         -- DIFFERENT vessels per the IMO scheme. The correct condition
         -- is: name fallback fires when IMO comparison cannot be made.
         ((v.properties->>'imo'  IS NULL OR sv.properties->>'imo' IS NULL)
          AND v.display_name IS NOT NULL
          AND sv.display_name IS NOT NULL
          AND length(v.display_name) >= 4
          AND length(sv.display_name) >= 4
          AND upper(v.display_name) % upper(sv.display_name)
          AND similarity(upper(v.display_name), upper(sv.display_name)) >= $2::float)
     )
    WHERE v.entity_type = 'vessel'
)
INSERT INTO event (
    event_type, event_subtype, event_time, geom, severity,
    title, description, properties, domain, decay_half_life_min, entity_id
)
SELECT
    'sanctioned_vessel_underway'                              AS event_type,
    CASE WHEN m.match_kind = 'imo' THEN 'imo_match' ELSE 'name_match' END
                                                              AS event_subtype,
    NOW()                                                     AS event_time,
    m.v_current_geom                                          AS geom,
    -- IMO match = severity 10 (max); name match stays at 9.
    CASE WHEN m.match_kind = 'imo' THEN 10.0 ELSE 9.0 END::real AS severity,
    'Sanctioned vessel underway: ' || m.v_display_name ||
        CASE WHEN m.match_kind = 'imo' THEN ' [IMO match]' ELSE '' END
                                                              AS title,
    'OFAC-SDN-sanctioned vessel ' || m.v_display_name ||
        ' (MMSI ' || m.v_canonical_id || ') is currently broadcasting AIS — '
        'matches OFAC entry "' || m.sv_display_name || '" via ' || m.match_kind
        || COALESCE(' (IMO ' || (m.v_props->>'imo') || ')', '')
                                                              AS description,
    jsonb_build_object(
        'algorithm',                $1::text,
        'match_kind',               m.match_kind,
        'mmsi',                     m.v_canonical_id,
        'live_vessel_name',         m.v_display_name,
        'ofac_sdn_match_name',      m.sv_display_name,
        'ofac_sdn_canonical_id',    m.sv_canonical_id,
        'similarity',               CASE WHEN m.match_kind = 'name'
                                          THEN ROUND(
                                                  similarity(upper(m.v_display_name),
                                                             upper(m.sv_display_name))::numeric,
                                                  3)
                                          ELSE NULL END,
        'live_imo',                 m.v_props->>'imo',
        'sanctioned_imo',           m.sv_props->>'imo',
        'live_callsign',            m.v_props->>'callsign',
        'live_destination',         m.v_props->>'destination',
        'similarity_threshold',     $2::float,
        'fcra_safe',                false,
        -- Pull authority from the matched sanctioned-vessel row. Falls back to
        -- the historical OFAC label if the row predates the multi-authority
        -- writer. UK + EU rows correctly carry their own authority strings.
        'sanctioning_authority',    COALESCE(
                                       m.sv_props->>'sanctioning_authority',
                                       'US Treasury OFAC'
                                    ),
        'canonical_id_type',        COALESCE(
                                       m.sv_props->>'canonical_id_type',
                                       'ofac_sdn_id'
                                    ),
        -- Regime / program copied through from the sanctioned-vessel row
        -- so the entity drawer can render them without an extra fetch.
        -- 'regime' is the unified authority-agnostic field (RUSSIA, IRAN,
        -- DPRK, etc.); 'sanction_program' is the OFAC EO-derived code or
        -- the EU programme letter; 'sanction_programs' is the full list
        -- when a party is listed under multiple EOs.
        'match_regime',             m.sv_props->>'regime',
        'match_program',            COALESCE(
                                       m.sv_props->>'ofac_program',
                                       m.sv_props->>'programme'
                                    ),
        'match_programs',           m.sv_props->'ofac_programs'
    )                                                         AS properties,
    'maritime'                                                AS domain,
    1440                                                      AS decay_half_life_min,
    m.v_id                                                    AS entity_id
FROM matched m
WHERE m.v_current_geom IS NOT NULL
  AND m.v_current_position_time IS NOT NULL
  AND m.v_current_position_time >= $3::timestamptz
  AND (
        $5::text IS NULL
        OR (m.v_canonical_id LIKE $5 AND m.sv_canonical_id LIKE $5)
      )
  AND NOT EXISTS (
      SELECT 1 FROM event finding
      WHERE finding.event_type = 'sanctioned_vessel_underway'
        AND finding.properties->>'algorithm' = $1
        AND finding.entity_id = m.v_id
        AND finding.properties->>'ofac_sdn_canonical_id' = m.sv_canonical_id
        AND finding.event_time >= $4::timestamptz
  )
"""


async def run_sanctions_match_scan(
    *,
    similarity_threshold: float = 0.9,
    lookback_min: int = 24 * 60,
    # 30-day dedup (was 24h — caused 4.05x over-firing per audit
    # 2026-05-13). A vessel still broadcasting AIS against the same
    # OFAC SDN entry is the same match; refire only when the prior
    # finding ages past 30d (vessel must have stopped + restarted).
    dedup_window_hours: int = 30 * 24,
    algorithm_tag: str = "sanctions_match",
    entity_canonical_id_like: str | None = None,
) -> int:
    """Run one sanctions-match pass. Returns count of new findings.

    Two-phase as of 2026-05-09:
      1. Run the existing INSERT...SELECT to land all candidate matches
      2. Read back the just-inserted name-only matches and check each
         against the maritime_mid flag-vs-regime safety rule. DELETE
         rows that fail (e.g. a US-flagged tug name-matched against a
         Ukraine-regime sanctioned vessel — different vessels sharing
         a generic name like "ATLAS").

    Args:
        similarity_threshold: trigram similarity floor for a name match
            to count. 1.0 = exact, 0.9 catches case-only differences and
            small typos but rejects most generic-name false positives.
        lookback_min: only consider live vessels with current_position_time
            within this many minutes. Default 24h — must have broadcast
            recently.
        dedup_window_hours: same (vessel, sanctioned-entry) pair gets
            flagged at most once per this window. Default 24h — prevents
            firehose spam.
        algorithm_tag: written to properties.algorithm so test runs don't
            dedup against production runs.
        entity_canonical_id_like: optional MMSI LIKE pattern for tests.

    Returns:
        Count of `sanctioned_vessel_underway` rows kept after the flag
        safety filter (i.e. rows actually visible to consumers).
    """
    from maritime_mid import is_flag_consistent_with_regime  # local import

    now = datetime.now(timezone.utc)
    lookback_cutoff = now - timedelta(minutes=lookback_min)
    dedup_cutoff    = now - timedelta(hours=dedup_window_hours)
    scan_started_at = now

    async with acquire_write() as conn:
        result = await conn.execute(
            SANCTIONS_MATCH_SQL,
            algorithm_tag,                # $1
            similarity_threshold,         # $2
            lookback_cutoff,              # $3
            dedup_cutoff,                 # $4
            entity_canonical_id_like,     # $5
        )
    try:
        inserted = int(result.split()[-1])
    except (ValueError, IndexError):
        inserted = 0

    if inserted == 0:
        return 0

    # Phase 2: pull back the rows from the recent window and apply the
    # flag-vs-regime safety filter on name-only matches. IMO-exact
    # matches always pass (IMO is globally unique).
    #
    # We look back 10 minutes (not just `event_time >= scan_started_at`)
    # because in production we observed rows freshly inserted with
    # event_time slightly before scan_started_at — likely a transaction-
    # timestamp-vs-Python-clock skew (Postgres NOW() = transaction start,
    # Python datetime.now() can be later if the connection acquire took
    # time). The algorithm_tag filter already isolates this scan from
    # other tag-spaces, so the wider window is safe.
    async with acquire_write() as conn:
        candidates = await conn.fetch(
            """
            SELECT id, event_time, properties
            FROM event
            WHERE event_type = 'sanctioned_vessel_underway'
              AND properties->>'algorithm' = $1
              AND event_time >= NOW() - INTERVAL '10 minutes'
            """,
            algorithm_tag,
        )

    import json as _json
    withdrawn_ids = []
    for row in candidates:
        props = row["properties"]
        if isinstance(props, str):
            try:
                props = _json.loads(props)
            except (TypeError, ValueError):
                continue
        # IMO matches always trustworthy
        if props.get("match_kind") == "imo":
            continue
        mmsi = props.get("mmsi")
        regime = props.get("match_regime")
        if not is_flag_consistent_with_regime(mmsi, regime):
            withdrawn_ids.append((row["id"], row["event_time"]))

    if withdrawn_ids:
        async with acquire_write() as conn:
            async with conn.transaction():
                for eid, ts in withdrawn_ids:
                    await conn.execute(
                        "DELETE FROM event WHERE id = $1::uuid AND event_time = $2",
                        eid, ts,
                    )
        _log.info(
            f"sanctions match: withdrew {len(withdrawn_ids)} false-positive "
            f"name-only matches (flag/regime mismatch)"
        )

    kept = inserted - len(withdrawn_ids)
    if kept > 0:
        _log.info(
            f"sanctions match: {kept} live vessels matched OFAC SDN "
            f"(similarity ≥ {similarity_threshold}; "
            f"{len(withdrawn_ids)} withdrawn by flag filter)"
        )
    return kept
