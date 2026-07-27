-- PART 1 APPROVED AND ACTIVE (operator decision 2026-05-19); Part 2 still deferred.
-- (File retains "_PROPOSAL.sql" suffix in name to preserve any external references.)
--
-- Audit: ALGORITHM_FP_AUDIT_proximity_2026_05_19.md (P0-C, algorithm #7).
-- FP rate in 30-sample audit: 16/30 = 53%.
-- Root cause: proximity algorithm was matching against algorithm-derived
-- events (rendezvous_detected, dark_vessel_detected, loitering_detected,
-- port_*, sanctioned_*, military_aircraft_underway) in addition to raw
-- external events. In busy maritime/aviation lanes, a single source event
-- produces 1,000-27,000 "nearby" entity findings — pure fanout noise.
--
-- Scope of this cleanup:
--   * Algo-derived event matches:        12,072,548 rows
--   * Cross-entity aircraft_vessel:      16,439,143 rows
--   * Total:                             28,511,691 rows
--
-- That's 24x the agent's 500,000-row hard limit. This SQL is HELD AS A
-- PROPOSAL pending main-session + operator approval. The algorithm fix
-- (proximity.py NOT IN deny-list) is already applied and stops further
-- pollution at the source. Existing findings will naturally age out of
-- operational queries via decay_half_life_min=60 within ~1 hour, but
-- they remain in the corpus indefinitely without this cleanup.
--
-- Recommended approach for operator/main session:
--   (A) Apply Part 1 only (algo-derived matches, 12M rows) — these are
--       unambiguously FP per the audit. Skip Part 2 (cross-entity
--       aircraft_vessel) until cross-entity scope is debated separately
--       (cross-entity matches algorithm spec but is operationally low-signal
--       at 50km radius; deserves its own audit/policy decision).
--   (B) Apply in batches if needed — UPDATE on 28M rows will be slow.
--       Suggest batch by event_subtype to monitor progress.
--   (C) Run during low-load window. Daemon writes ~30k proximity findings/hour.

-- ============================================================================
-- VERIFICATION (run first — should match audit totals)
-- ============================================================================

BEGIN;
SET LOCAL statement_timeout = '300s';

\echo '=== Part 1 dry run: rows that would be withdrawn (algo-derived matches) ==='
SELECT
  CASE
    WHEN event_subtype LIKE '%_rendezvous_detected' THEN 'rendezvous_match'
    WHEN event_subtype LIKE '%_dark_vessel_detected' THEN 'dark_vessel_match'
    WHEN event_subtype LIKE '%_loitering_detected' THEN 'loitering_match'
    WHEN event_subtype LIKE '%_port_call' THEN 'port_call_match'
    WHEN event_subtype LIKE '%_port_arrival' THEN 'port_arrival_match'
    WHEN event_subtype LIKE '%_port_departure' THEN 'port_departure_match'
    WHEN event_subtype LIKE '%_sanctioned_vessel_%' THEN 'sanctioned_vessel_match'
    WHEN event_subtype LIKE '%_sanctioned_port_%' THEN 'sanctioned_port_match'
    WHEN event_subtype LIKE '%_military_aircraft_underway' THEN 'military_match'
    WHEN event_subtype LIKE '%_aircraft_in_sanctioned_airspace' THEN 'sanctioned_airspace_match'
  END AS class,
  COUNT(*) AS n
FROM event
WHERE event_type = 'detected_proximity'
  AND (properties->>'withdrawn') IS NULL
  AND (
    event_subtype LIKE '%_rendezvous_detected'
    OR event_subtype LIKE '%_dark_vessel_detected'
    OR event_subtype LIKE '%_loitering_detected'
    OR event_subtype LIKE '%_port_call'
    OR event_subtype LIKE '%_port_arrival'
    OR event_subtype LIKE '%_port_departure'
    OR event_subtype LIKE '%_sanctioned_vessel_%'
    OR event_subtype LIKE '%_sanctioned_port_%'
    OR event_subtype LIKE '%_military_aircraft_underway'
    OR event_subtype LIKE '%_aircraft_in_sanctioned_airspace'
  )
GROUP BY 1
ORDER BY 2 DESC;

ROLLBACK;

-- ============================================================================
-- PART 1 — ALGO-DERIVED MATCH CLEANUP (12,072,548 rows expected)
-- Operator approved 2026-05-19 ("idk choose best" → main session selected
-- Part 1 only as the conservative-FP-only option, deferring Part 2's
-- cross-entity scope as policy-decision-pending).
-- ============================================================================

BEGIN;
SET LOCAL statement_timeout = '3600s';  -- 1 hour; 12M-row UPDATE may need it

UPDATE event
SET properties = properties || jsonb_build_object(
  'withdrawn',          true,
  'withdrawal_reason',  'proximity_algo_derived_match_fanout_2026_05_19',
  'withdrawn_at',       now()::text,
  'withdrawal_audit',   'ALGORITHM_FP_AUDIT_proximity_2026_05_19.md'
)
WHERE event_type = 'detected_proximity'
  AND (properties->>'withdrawn') IS NULL
  AND (
    event_subtype LIKE '%_rendezvous_detected'
    OR event_subtype LIKE '%_dark_vessel_detected'
    OR event_subtype LIKE '%_loitering_detected'
    OR event_subtype LIKE '%_port_call'
    OR event_subtype LIKE '%_port_arrival'
    OR event_subtype LIKE '%_port_departure'
    OR event_subtype LIKE '%_sanctioned_vessel_%'
    OR event_subtype LIKE '%_sanctioned_port_%'
    OR event_subtype LIKE '%_military_aircraft_underway'
    OR event_subtype LIKE '%_aircraft_in_sanctioned_airspace'
  );

-- Sanity check before commit (expect ~12,072,548):
SELECT COUNT(*) FILTER (WHERE properties->>'withdrawal_reason' = 'proximity_algo_derived_match_fanout_2026_05_19') AS withdrawn,
       COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS still_active
FROM event WHERE event_type = 'detected_proximity';

COMMIT;
-- ============================================================================

-- ============================================================================
-- PART 2 — CROSS-ENTITY aircraft_vessel CLEANUP (16,439,143 rows)
-- DEFERRED. Cross-entity proximity matches algorithm spec (entities within
-- 50km of each other within window). Whether 50km aircraft-over-vessel is
-- "interesting" is a policy question; defer to operator.
-- Also: proximity_cross has been silently NOT emitting since 2026-05-14
-- (last finding at 21:10:18). The volume is purely historical pollution.
-- ============================================================================
