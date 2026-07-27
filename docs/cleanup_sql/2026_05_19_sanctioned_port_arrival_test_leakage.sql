-- Cleanup: withdraw 115 historical test-fixture leakage rows from production.
--
-- Audit:    21_GLASSBOX_AI/docs/ALGORITHM_FP_AUDIT_sanctioned_port_arrival_2026_05_19.md
-- Pattern:  Proven 2026-05-14 audit-preserving withdrawal (commit f4dab9a, which
--           retired 2,245 sanctions FPs the same way). UPDATE not DELETE — rows
--           stay in the table for forensic review, but `withdrawn=true` hides them
--           from /signals/today, viewport, and the public site.
--
-- Why it's safe:
--   - Touches only `algorithm = 'sanctioned_port_arrival_test'` rows
--     (cross-algorithm scan 2026-05-19 confirmed no other algorithm has _test
--      leakage; this query cannot touch real production findings).
--   - 79 production-tagged rows (`sanctioned_port_arrival_v1`) untouched.
--   - Reversible: `UPDATE event SET properties = properties - 'withdrawn' - ...`
--     would un-withdraw if needed.
--   - Transaction-wrapped; verifies expected count (115) before commit.
--
-- How to run:
--     psql "$GLASSBOX_DB_URL" -v ON_ERROR_STOP=1 \
--         -f 21_GLASSBOX_AI/docs/cleanup_sql/2026_05_19_sanctioned_port_arrival_test_leakage.sql

BEGIN;

WITH targets AS (
  SELECT id FROM event
  WHERE event_type = 'sanctioned_port_arrival'
    AND properties->>'algorithm' = 'sanctioned_port_arrival_test'
    AND (properties->>'withdrawn') IS NULL
)
UPDATE event
SET properties = properties || jsonb_build_object(
  'withdrawn',          true,
  'withdrawal_reason',  'test_fixture_leakage_into_production_db',
  'withdrawn_at',       now()::text,
  'withdrawn_by_audit', 'ALGORITHM_FP_AUDIT_sanctioned_port_arrival_2026_05_19'
)
WHERE id IN (SELECT id FROM targets);

-- Confirm 115 rows updated and 0 still active before committing.
SELECT
  COUNT(*) FILTER (WHERE COALESCE((properties->>'withdrawn')::bool, false) = true) AS withdrawn_now,
  COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS still_active
FROM event
WHERE event_type = 'sanctioned_port_arrival'
  AND properties->>'algorithm' = 'sanctioned_port_arrival_test';

COMMIT;

-- Post-commit verification: production corpus untouched, test corpus fully withdrawn.
SELECT
  properties->>'algorithm' AS algo,
  COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS active,
  COUNT(*) FILTER (WHERE COALESCE((properties->>'withdrawn')::bool, false) = true) AS withdrawn
FROM event
WHERE event_type = 'sanctioned_port_arrival'
GROUP BY 1
ORDER BY 1;
