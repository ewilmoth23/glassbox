-- =====================================================================
-- Proximity algo-derived-match fanout cleanup — BATCHED VERSION
-- =====================================================================
--
-- Background: the original cleanup
-- (2026_05_19_proximity_algo_derived_match_fanout_PROPOSAL.sql) tried to
-- UPDATE all 12,072,548 rows in a single transaction with
-- `SET LOCAL statement_timeout = '3600s'`. Operator ran it at 2026-05-19;
-- the transaction was active for ~45-60+ minutes and ultimately rolled
-- back (DB verified: 0 rows withdrawn, no `withdrawal_reason` tag set).
-- Most likely cause: statement_timeout fired, or the operator Ctrl+C'd.
--
-- This batched version processes the same WHERE filter but in 500,000-row
-- chunks with COMMIT between batches. The 1-hour statement_timeout applies
-- per batch, and 500k jsonb-concat updates complete in 2-5 minutes per
-- batch (well within timeout). Progress is logged via RAISE NOTICE.
--
-- Same audit-preserving UPDATE pattern as the proven 2026-05-14 sanctions
-- cleanup (`f4dab9a`). Never DELETE.
--
-- Total expected: ~12,072,548 rows across ~25 batches.
-- Total runtime estimate: 1-2 hours (sequential).
-- Each batch's progress prints to your terminal via NOTICE.
--
-- Safe to interrupt: each completed batch has already committed. If you
-- Ctrl+C mid-batch, you lose at most the in-flight batch's rows; re-running
-- the script picks up where it left off (the WHERE clause skips already-
-- withdrawn rows).
-- =====================================================================

\set ON_ERROR_STOP on
\timing on

\echo
\echo '======================================================================'
\echo 'Proximity cleanup — batched (500k rows/batch)'
\echo '======================================================================'

DO $$
DECLARE
  batch_size  INT := 500000;
  this_batch  INT;
  total       INT := 0;
  start_ts    TIMESTAMPTZ := clock_timestamp();
  batch_start TIMESTAMPTZ;
BEGIN
  LOOP
    batch_start := clock_timestamp();

    UPDATE event SET properties = properties || jsonb_build_object(
        'withdrawn',          true,
        'withdrawal_reason',  'proximity_algo_derived_match_fanout_2026_05_19',
        'withdrawn_at',       now()::text,
        'withdrawal_audit',   'ALGORITHM_FP_AUDIT_proximity_2026_05_19.md'
    )
    WHERE id IN (
        SELECT id FROM event
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
        LIMIT batch_size
    );

    GET DIAGNOSTICS this_batch = ROW_COUNT;
    total := total + this_batch;

    RAISE NOTICE 'batch: % rows in % | cumulative: % | elapsed: %',
      this_batch,
      clock_timestamp() - batch_start,
      total,
      clock_timestamp() - start_ts;

    EXIT WHEN this_batch = 0;
    COMMIT;
  END LOOP;

  RAISE NOTICE '';
  RAISE NOTICE '=======================================';
  RAISE NOTICE 'DONE — % rows total in %', total, clock_timestamp() - start_ts;
  RAISE NOTICE '=======================================';
END
$$;

-- Final verification: should show ~12M withdrawn under the new reason tag,
-- and 16.4M still-active (the Part-2 cross-entity rows we intentionally kept).
SELECT
  COUNT(*) FILTER (WHERE properties->>'withdrawal_reason' = 'proximity_algo_derived_match_fanout_2026_05_19') AS withdrawn_this_pass,
  COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS still_active,
  COUNT(*) AS total
FROM event WHERE event_type = 'detected_proximity';
