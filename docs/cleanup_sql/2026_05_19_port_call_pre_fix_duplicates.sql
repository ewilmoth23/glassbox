-- ─────────────────────────────────────────────────────────────────────
-- port_call cleanup — withdraw duplicate re-fires per 7-day cooldown
-- ─────────────────────────────────────────────────────────────────────
-- Date:       2026-05-19
-- Reason:     Pre-2026-05-13 the algorithm used a 24h cooldown; vessels
--             at berth for multiple days emitted one finding per day per
--             port. Commit c4906ae extended cooldown to 7 days (7*24h),
--             but pre-existing findings from the 24h era remain in the
--             corpus as excess re-fires.
--             Audit 2026-05-19 (per GLASSBOX_BACKEND_BACKLOG P0-C audit #6):
--             12 of 30 random recent findings (43%) had a prior finding
--             within 7d for the same (vessel, port) — i.e. would not be
--             emitted by today's algorithm.
-- Mechanism:  Mark as withdrawn any port_call finding that has a prior
--             non-withdrawn port_call finding for the same (vessel, port)
--             within 7 days. This mirrors the algorithm's own
--             NOT EXISTS cooldown predicate exactly.
-- Audit-preserving: UPDATE only. Never DELETE. Sets
--             properties.withdrawn = true + reason + timestamp.
-- Expected:   ~7,397 rows withdrawn (verified via BEGIN/ROLLBACK pre-run).
-- ─────────────────────────────────────────────────────────────────────

BEGIN;

-- Sanity: confirm pre-withdrawal active count
SELECT COUNT(*) AS active_before
FROM event WHERE event_type='port_call' AND (properties->>'withdrawn') IS NULL;

-- Withdraw excess re-fires (one finding per 7-day window per vessel-port)
UPDATE event a
SET properties = a.properties || jsonb_build_object(
    'withdrawn', true,
    'withdrawal_reason', 'pre_fix_24h_cooldown_excess_refire',
    'withdrawn_at', now()::text,
    'withdrawal_audit', 'ALGORITHM_FP_AUDIT_port_call_2026_05_19'
)
WHERE a.event_type = 'port_call'
  AND (a.properties->>'withdrawn') IS NULL
  AND EXISTS (
    SELECT 1 FROM event b
    WHERE b.event_type = 'port_call'
      AND b.entity_id = a.entity_id
      AND (b.properties->>'port_id') = (a.properties->>'port_id')
      AND b.event_time < a.event_time
      AND b.event_time >= a.event_time - interval '7 days'
      AND (b.properties->>'withdrawn') IS NULL
  );

-- Sanity: confirm post-withdrawal active count
SELECT COUNT(*) AS active_after
FROM event WHERE event_type='port_call' AND (properties->>'withdrawn') IS NULL;

SELECT COUNT(*) AS withdrawn_total
FROM event WHERE event_type='port_call'
  AND (properties->>'withdrawal_reason') = 'pre_fix_24h_cooldown_excess_refire';

COMMIT;
