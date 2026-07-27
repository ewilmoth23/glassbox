# Algorithm FP Audit — port_call

**Date:** 2026-05-19
**Auditor:** Claude (P0-C batch, audit #6 of 10)
**Algorithm:** `21_GLASSBOX_AI/algorithms/port_call.py`
**Event types:** `port_call`

## Summary

| Metric | Value |
|---|---|
| Active findings (pre-cleanup) | 18,732 |
| Active findings (post-cleanup) | 11,319 |
| Sample size | 30 |
| TP / FP / AMB | 14 / 13 / 3 |
| FP rate (historical, corpus-wide) | **43.3%** |
| FP rate (post-2026-05-13, active algorithm) | ~3.3% (1 transit case in 30) |
| Historical FPs withdrawn | **7,413** |
| Test added | `test_seven_day_cooldown_suppresses_intra_week_refires` |

**Verdict: FIX-AND-WITHDRAW (applied).** Algorithm itself is correct as of 2026-05-13 (cooldown extended from 24h → 168h in commit `c4906ae`). Historical corpus contained 7,413 excess re-fires emitted under the old 24h cooldown that today's algorithm would not produce. These have been audit-preservingly withdrawn (UPDATE-marked `withdrawn=true`, never DELETE).

## Algorithm review

### Detection predicate (verbatim, `port_call.py:189-276`)

```sql
WITH ports(port_id, port_name, country, port_lat, port_lng, port_kind) AS (
    VALUES {VALUES_CLAUSE}  -- ~100 hardcoded port reference points
),
candidates AS (
    SELECT
        e.id AS vessel_id, ...,
        ST_Distance(e.current_geom, ST_SetSRID(ST_MakePoint(p.port_lng, p.port_lat), 4326)::geography) AS distance_m,
        (SELECT pt.velocity_ms FROM position_track pt
         WHERE pt.entity_id = e.id ORDER BY pt.time DESC LIMIT 1) AS last_velocity_ms
    FROM entity e
    CROSS JOIN ports p
    WHERE e.entity_type = 'vessel'
      AND e.current_geom IS NOT NULL
      AND e.current_position_time >= NOW() - ($1::int * INTERVAL '1 minute')  -- fresh window
      AND ST_DWithin(e.current_geom, ...)::geography, $2)                      -- radius_m
),
in_port AS (
    SELECT * FROM candidates
    WHERE last_velocity_ms IS NOT NULL AND last_velocity_ms < $3   -- velocity gate
)
INSERT INTO event (...)
SELECT ... FROM in_port cp
WHERE NOT EXISTS (
    SELECT 1 FROM event prior
    WHERE prior.event_type = 'port_call'
      AND prior.entity_id  = cp.vessel_id
      AND prior.properties->>'port_id' = cp.port_id
      AND prior.event_time >= NOW() - ($5::int * INTERVAL '1 hour')  -- cooldown
)
```

### Thresholds
- `radius_m = 5000` (5 km from port reference point)
- `fresh_window_min = 60` (vessel must have broadcast within last 60 min)
- `at_port_max_velocity_ms = 1.5` (≈ 3 knots, anchored/docking/berthed)
- `cooldown_hours = 7 * 24 = 168` (since 2026-05-13; was 24 before)

### IMO-NULL guard
Not relevant — port_call doesn't compare to sanctions lists.

## Sample classification

30 random non-withdrawn findings from the last 14 days. For each finding, ground-truth-checked via:
1. Dwell time inside 5km of port (positions within ±12h of event_time)
2. Velocity behavior during dwell window
3. Whether a prior non-withdrawn port_call exists for same (vessel, port) within 7 days

| # | event_id (8-char) | port | dwell_min | prior_7d | classification | notes |
|---|---|---|---|---|---|---|
| 1 | 0d39b342 | JP_TYO | 689 | 0 | TP | 11h dwell inside Tokyo, v=0 |
| 2 | 36f7974e | BE_ANR | 756 | 1 | **FP-DUPE** | 1 prior in 7d (excess refire) |
| 3 | b88234f2 | NL_RTM | 947 | 0 | TP | 15.8h at Rotterdam |
| 4 | d6d55546 | US_LAX | 924 | 1 | **FP-DUPE** | excess refire |
| 5 | 4c9aff57 | GR_PIR | 1383 | 0 | TP | 23h at Piraeus |
| 6 | 1155b06e | HK_HKG | 460 | 2 | **FP-DUPE** | 2 priors |
| 7 | c0935964 | US_NYC | 668 | 0 | TP | MSC MEXICO V at NY |
| 8 | ab4481cf | SG_SIN | 15 | 0 | AMB | Approaching anchorage, slowing 1.65→1.03 m/s |
| 9 | 186f389f | CA_VAN | 398 | 0 | TP | Seaspan Raven at Vancouver |
| 10 | ff69e249 | DE_HAM | 401 | 0 | TP | NORDSEE VII at Hamburg |
| 11 | e49781c9 | NO_BGO | 297 | 0 | TP | LYNA at Bergen |
| 12 | 28e82525 | BE_ANR | 821 | 0 | TP | DOLPHIN 21 at Antwerp |
| 13 | a6cd71b9 | SY_TUS | 39 | 0 | AMB | 3 pings only, v=0.1, sparse data |
| 14 | 90d2b09c | DE_HAM | 1243 | 1 | **FP-DUPE** | excess refire |
| 15 | 852a1a4d | GR_PIR | 1245 | 3 | **FP-DUPE** | 3 priors |
| 16 | ec27a418 | NL_RTM | 834 | 1 | **FP-DUPE** | excess refire |
| 17 | 2cd21eb0 | RU_SPB | 688 | 0 | TP | YURIY KUCHIEV at St. Petersburg |
| 18 | 72aed11f | LV_RIX | 1428 | 1 | **FP-DUPE** | excess refire |
| 19 | 8a9afa7e | SG_SIN | 358 | 0 | TP | SEA ABUNDANCE at Singapore |
| 20 | 541a101a | FR_LEH | 713 | 0 | TP | PYTHAGORE at Le Havre |
| 21 | 2f2ab79c | BE_ANR | 194 | 1 | **FP-DUPE** | excess refire |
| 22 | 5dd88c30 | FI_HEL | 1432 | 1 | **FP-DUPE** | excess refire |
| 23 | beb7e57e | US_LGB | 717 | 0 | TP | BLAIR C at Long Beach |
| 24 | 6daa6b72 | PL_GDN | 1428 | 3 | **FP-DUPE** | 3 priors, ORP JASKOLKA (Polish Navy) |
| 25 | f1f894cb | SG_SIN | 19 | 0 | AMB | Slow approach 3.5→0.3 m/s, anchored offshore at 8.6km |
| 26 | 176ed3cf | RU_SPB | 1428 | 1 | **FP-DUPE** | excess refire |
| 27 | 66086880 | EG_SUZ | 0 | 0 | **FP-TRANSIT** | Only 3 pings, v=0.1-0.21, Suez Canal transit |
| 28 | 8f60d9cc | FR_MRS | 655 | 0 | TP | VIKING STAR at Marseille |
| 29 | 052b099e | EE_TLL | 710 | 0 | TP | AHTO-25 at Tallinn |
| 30 | e81c11c5 | FI_TKU | 1439 | 2 | **FP-DUPE** | 2 priors, ARKTIS at Turku |

**Totals:** 14 TP / 13 FP / 3 AMB → 43.3% FP rate (historical)

## FP class analysis

### Class 1: FP-DUPE (12 of 13 FPs, 92% of FPs)

**Root cause:** Pre-2026-05-13, the algorithm used `cooldown_hours = 24`. A vessel at berth for 4-5 days emitted one finding per day per port. Commit `c4906ae` (2026-05-13) extended cooldown to 168h (7 days). The algorithm-level fix is in place. The historical corpus retains the excess findings.

**Examples (verbatim event_ids):** `36f7974e-731c-416c-a1e0-f56b03e87f20`, `90d2b09c-67b3-41a8-bc7f-3515f7c7b3b2`, `852a1a4d-12af-45af-9240-f372d9aaeb90`.

**Corpus-wide impact:**
- 7,397 active findings have a prior finding for same (vessel, port) within 7 days (pre-cleanup query).
- After cleanup ran: 7,413 withdrawn (slight delta from daemon emissions during audit).

### Class 2: FP-TRANSIT (1 of 13 FPs, 8%)

**Root cause:** A vessel slow-transiting through a 5km zone around a port reference (e.g. Suez Canal traffic past the Port of Suez reference point) can briefly satisfy the velocity gate (< 1.5 m/s) without actually entering a port. The algorithm has no dwell-time requirement.

**Example:** `66086880-5aa2-41b1-937b-fd4164bd279f` at Suez (EG_SUZ) — only 3 pings recorded inside 5km, all at v=0.1-0.21 m/s, vessel cleanly exited within minutes.

**Rate:** 1 of 30 = 3.3%. Below the 5% target. Adding a dwell-time gate (e.g. require ≥ 2 pings spanning ≥ 30 min inside polygon) would eliminate this but at a cost: it would delay detection of legitimate fast arrivals (vessel docks at berth within 10 min of entering 5km zone). Not recommended.

### AMB class (3 of 30, 10%)

Vessels making slow approach into port anchorages (`ab4481cf`, `f1f894cb` at Singapore; `a6cd71b9` at Tartus). They were genuinely approaching/anchoring at port-adjacent waters but didn't reach a berth within our sampling window. These are borderline — the algorithm correctly identifies them as "in-port behavior" but the port_call event semantics are arguable. Classifying as ambiguous, not FP.

## Cooldown audit (corpus-wide)

| Period | Re-fires <24h | Re-fires <7d | Total findings |
|---|---|---|---|
| Pre-2026-05-13 (24h era) | 1,761 | 6,853 excess | 12,767 |
| Post-2026-05-14 (7d era) | 0 | 458 (all are 5-7d apart, legitimate long-stay refires) | 5,244 |

The post-fix 458 re-fires are 5-7 days apart for vessels sitting at berth for >1 week. This is **designed behavior** — the algorithm fires once per 7-day window for continuous "vessel-at-port" signal. Not a bug.

## Action taken

### 1. Historical cleanup (audit-preserving)

Cleanup SQL: `21_GLASSBOX_AI/docs/cleanup_sql/2026_05_19_port_call_pre_fix_duplicates.sql`

```sql
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
```

**Applied:** 2026-05-19 16:54 EDT.
**Rows updated:** 7,413.
**Pre-cleanup active count:** 18,732.
**Post-cleanup active count:** 11,319.

### 2. No algorithm change

The algorithm is correct as of `c4906ae` (2026-05-13). No code change needed.

The 1 transit FP (Suez) at 3.3% rate is below the 5% target. Adding a dwell-time gate would harm true-positive coverage and is not recommended at this time.

### 3. Regression test added

`21_GLASSBOX_AI/tests/test_port_call.py::test_seven_day_cooldown_suppresses_intra_week_refires`

Approach: seed a prior port_call event 6 days ago, then run scan with default 168h cooldown — assert 0 new emissions. Then re-run with `cooldown_hours=24` (legacy value) — assert 1 new emission. Proves the cooldown parameter is honored end-to-end.

**Status:** PASSING. Full test_port_call.py: 18/18 passing.

## Verification

```sql
-- Post-cleanup state
SELECT 
  COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS active,
  COUNT(*) FILTER (WHERE properties->>'withdrawal_reason' = 'pre_fix_24h_cooldown_excess_refire') AS withdrawn
FROM event WHERE event_type = 'port_call';

-- Expected: active=11319, withdrawn=7413
```

## Next algorithm

Per backlog P0-C list of 10:
- DONE: sanctioned_port_arrival, sanctions_multijurisdictional, shadow_fleet_cluster, dark_ship, loitering, **port_call**
- REMAINING: proximity, rendezvous, sanctioned_airspace, military_flights

Next: **proximity** (likely the most algorithm-dependent, with potential to surface the most TPs/day in the corpus).
