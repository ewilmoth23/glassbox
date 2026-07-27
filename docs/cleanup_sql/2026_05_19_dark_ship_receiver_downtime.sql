-- =====================================================================
-- Cleanup: dark_ship — withdraw findings caused by AIS receiver downtime
-- =====================================================================
-- Issue:  When the AIS ingester (aisstream / digitraffic / barentswatch /
--         DMA) drops its websocket connection or restarts, EVERY vessel
--         the receiver was tracking has its `current_position_time`
--         frozen at the disconnect instant. Six hours later the
--         dark_ship algorithm wakes up and emits a "dark_vessel_detected"
--         finding for each of those vessels — typically 10,000+ at once,
--         all sharing the exact same `last_seen_ais` timestamp.
--
-- Evidence (production corpus, 209k findings):
--         The largest 13 "buckets" (same last_seen_ais to the second)
--         account for 95,954 findings (45.7% of corpus). The top three:
--           - 2026-05-08T01:32:31  → 15,042 vessels (in one second)
--           - 2026-05-09T00:47:08  → 14,942 vessels
--           - 2026-05-13T13:29:10  → 13,064 vessels
--
-- Audit:  21_GLASSBOX_AI/docs/ALGORITHM_FP_AUDIT_dark_ship_2026_05_19.md
--
-- Policy: Mark withdrawn (audit-preserving). DO NOT delete. The findings
--         remain queryable for cleanup-rollback, while every consumer
--         endpoint (viewport, signals/today, /signals.rss, etc.) already
--         filters `(properties->>'withdrawn') IS NULL`.
--
-- Threshold: ≥6 vessels sharing the same `last_seen_ais` second. The
--         binomial p-value for 6 independent vessels going dark in the
--         same one-second window with 18k tracked vessels is ~1e-8;
--         this is reliably a receiver artifact, not signal.
-- ---------------------------------------------------------------------

BEGIN;

-- Step 1: enumerate the receiver-downtime buckets
CREATE TEMP TABLE downtime_buckets AS
SELECT
    date_trunc('second', (properties->>'last_seen_ais')::timestamptz) AS sec_bucket,
    COUNT(DISTINCT entity_id) AS vessels
FROM event
WHERE event_type = 'dark_vessel_detected'
  AND (properties->>'withdrawn') IS NULL
GROUP BY 1
HAVING COUNT(DISTINCT entity_id) >= 6;

SELECT 'downtime buckets:' AS label, COUNT(*) AS n FROM downtime_buckets;
SELECT 'total findings in those buckets:' AS label, SUM(vessels) AS n FROM downtime_buckets;

-- Step 2: count affected rows BEFORE updating (verify before commit)
WITH affected AS (
    SELECT e.id
    FROM event e
    JOIN downtime_buckets d
      ON date_trunc('second', (e.properties->>'last_seen_ais')::timestamptz) = d.sec_bucket
    WHERE e.event_type = 'dark_vessel_detected'
      AND (e.properties->>'withdrawn') IS NULL
)
SELECT 'rows to mark withdrawn:' AS label, COUNT(*) FROM affected;

-- Step 3: the withdrawal UPDATE (NO RETURNING — use the UPDATE N count)
UPDATE event e
SET properties = e.properties || jsonb_build_object(
    'withdrawn', true,
    'withdrawal_reason', 'receiver_downtime_artifact',
    'withdrawal_audit', 'ALGORITHM_FP_AUDIT_dark_ship_2026_05_19.md',
    'withdrawn_at', now()::text,
    'cluster_size_at_withdrawal', d.vessels
)
FROM downtime_buckets d
WHERE e.event_type = 'dark_vessel_detected'
  AND (e.properties->>'withdrawn') IS NULL
  AND date_trunc('second', (e.properties->>'last_seen_ais')::timestamptz) = d.sec_bucket;

-- Step 4: post-update verification — should be 0 non-withdrawn findings
--         in buckets of size >= 6
SELECT 'post-update active findings in big buckets:' AS label,
       COUNT(*) AS n
FROM event e
JOIN downtime_buckets d
  ON date_trunc('second', (e.properties->>'last_seen_ais')::timestamptz) = d.sec_bucket
WHERE e.event_type = 'dark_vessel_detected'
  AND (e.properties->>'withdrawn') IS NULL;

-- Step 5: corpus health after
WITH per_pair AS (
  SELECT entity_id, properties->>'last_seen_ais' AS last_seen, COUNT(*) AS n
  FROM event
  WHERE event_type = 'dark_vessel_detected' AND (properties->>'withdrawn') IS NULL
  GROUP BY 1, 2
)
SELECT
    'post-cleanup active:'       AS label,
    SUM(n)                       AS active,
    COUNT(*)                     AS distinct_pairs,
    ROUND(SUM(n)::numeric / NULLIF(COUNT(*), 0), 4) AS dup_ratio,
    MAX(n)                       AS max_per_pair
FROM per_pair;

-- Audit: run the SELECTs, eyeball the numbers, THEN commit.
-- If counts look wrong, ROLLBACK and re-investigate.
-- ROLLBACK;
COMMIT;

-- =====================================================================
-- Optional follow-up (a separate, tighter pass for partial-receiver
-- artifacts in buckets of size 2-5): NOT included in this cleanup.
-- Those 117 findings are too few to confidently classify as receiver
-- vs. real correlated-darkness behavior (e.g. a small smuggling
-- coordinated AIS-off). Left in-corpus pending future refinement.
-- =====================================================================
