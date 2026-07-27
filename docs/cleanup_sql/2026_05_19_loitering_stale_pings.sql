-- =====================================================================
-- Cleanup: loitering — withdraw findings caused by stale AIS-payload pings
-- =====================================================================
-- Issue:  When the AIS feed (or its ingestion layer) redelivers the same
--         payload N times across a window, every redelivery lands in
--         position_track as a distinct row with identical lat/lon/velocity.
--         The loitering algorithm groups these by entity, finds N>=5 pings
--         spanning >=4h with lat_span=0 and lng_span=0, and concludes the
--         vessel "stayed within a small bbox". But the vessel itself is
--         broadcasting `velocity_ms` of 3-7 m/s (6-14 knots — active
--         cruising), not stationary. The receiver-shed produced zero
--         positional motion, not the vessel.
--
-- Evidence (production corpus, 56,495 active findings):
--         28,238 findings (50.0% of corpus) have
--         `lat_span_deg = 0 AND lng_span_deg = 0 AND avg_velocity_ms > 1.0`.
--         Two synchronized batch emissions dominate:
--           - 2026-05-08T15:37:50  → 15,225 findings (in one second)
--           - 2026-05-10T15:01:36  → 12,760 findings
--         Spot-check: entity 2a5e3de8 has 9 position_track rows all at the
--         IDENTICAL geom POINT(21.390293 58.971642) with velocity_ms=5.76,
--         heading=212. Two unrelated vessels (dd0bf9e9, 27ed10de) have
--         12 rows EACH at identical timestamps with identical positions
--         — the upstream payload was redelivered 12× to multiple entities
--         simultaneously, classic receiver-shed re-broadcast pattern.
--
-- Audit:  21_GLASSBOX_AI/docs/ALGORITHM_FP_AUDIT_loitering_2026_05_19.md
--         FP rate in the random sample of 30 = 53% stale-pings (the
--         dominant FP class); additional 17% port-density FPs surfaced
--         but are not addressed in this cleanup pass.
--
-- Fix:    21_GLASSBOX_AI/algorithms/loitering.py — added
--         `AND NOT (pe.lat_span = 0 AND pe.lng_span = 0)` to the WHERE
--         clause. Real loitering means motion within a small bbox; zero
--         positional movement = no evidence of any motion at all.
--
-- Cleanup verified on production DB before commit:
--   BEGIN;
--   <the UPDATE below, with count check>
--   ROLLBACK;
-- =====================================================================

BEGIN;

UPDATE event
SET properties = properties || jsonb_build_object(
    'withdrawn', true,
    'withdrawal_reason', 'stale_pings_zero_bbox',
    'withdrawn_at', now()::text,
    'withdrawal_audit', 'ALGORITHM_FP_AUDIT_loitering_2026_05_19.md'
)
WHERE event_type = 'loitering_detected'
  AND (properties->>'withdrawn') IS NULL
  AND (properties->>'lat_span_deg')::float = 0
  AND (properties->>'lng_span_deg')::float = 0
  AND (properties->>'avg_velocity_ms')::float > 1.0;

-- Inspect count: should be ~28,238 rows
-- SELECT count(*) FROM event
--   WHERE event_type = 'loitering_detected'
--     AND (properties->>'withdrawal_reason') = 'stale_pings_zero_bbox';

-- ROLLBACK;  -- run this first to verify count
-- COMMIT;    -- then re-run with COMMIT after verification

COMMIT;
