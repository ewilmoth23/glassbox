# Algorithm FP Audit — `shadow_fleet_cluster`

**Date:** 2026-05-19
**Auditor:** Claude (P0-C, batch 3 of 10)
**Algorithm file:** `21_GLASSBOX_AI/algorithms/shadow_fleet_cluster.py`
**Event type emitted:** `shadow_fleet_cluster`
**Audit recipe:** `GLASSBOX_BACKEND_BACKLOG.md` §P0-C (post-2026-05-14 audit pattern)

---

## TL;DR

- **Sample size:** 30 random non-withdrawn findings from last 14 days
- **Classification:** **5 TRUE positive / 24 FALSE positive / 1 ambiguous**
- **Production FP rate:** **80%** (24 of 30) — **catastrophic**
- **Root cause:** **DBSCAN density-reachability transitive chaining.** `ST_ClusterDBSCAN(geom, eps=10_000m, minpts=3)` in `shadow_fleet_cluster.py` lines 74-80 groups any point that is within 10 km of *at least one* cluster member. In sanctioned-vessel populations (concentrated along global shipping lanes), points form long density-reachable chains. The largest sampled cluster claims "130 sanctioned vessels within 10 km" but spans **152° longitude × 94° latitude** (Atlantic → East Asia → Antarctic). Title is technically meaningless.
- **Duplication ratio:** 1.000× (the 2026-05-13 ST_ClusterDBSCAN+dedup rewrite IS holding — this audit's regression is orthogonal to that fix; it's the eps/minpts choice itself, not the dedup).
- **Test pollution:** none. All 2,748 historical events tagged `algorithm='shadow_fleet_cluster'` (no `_test` variants).
- **Latest emission:** 2026-05-12 03:54:29 — algorithm has been silent for ~7 days. Either upstream `sanctioned_vessel_underway` has dried up, or the scheduled job is wedged. Worth flagging to ops separately.
- **Recommendation:** **FIX-AND-WITHDRAW.** Predicate fix proposed (post-DBSCAN geometric span filter + tighter minpts on shipping lanes). Cleanup SQL drafted at `21_GLASSBOX_AI/docs/cleanup_sql/2026_05_19_shadow_fleet_cluster_dbscan_chain_chaining.sql` — expected ~2,000-2,200 historical events flagged withdrawn.

---

## 1. Algorithm under audit (verbatim)

`shadow_fleet_cluster.py` runs `ST_ClusterDBSCAN` over `sanctioned_vessel_underway` events from the last `lookback_hours` (6 by default), grouping vessels into clusters where each cluster member is within `eps=10_000m` of at least one other member, with `minpts=3` minimum.

Key code (lines 74-80):
```sql
dbscan_clusters AS (
    SELECT
        entity_id, geom, authority, name, mmsi,
        ST_ClusterDBSCAN(ST_Transform(geom::geometry, 3857), $2::float, $3::int)
            OVER ()  AS cluster_id
    FROM recent_sanc
)
```

The title emitted (lines 127-132):
```sql
'CRITICAL — Shadow-fleet cluster: ' || c.cluster_size ||
    ' sanctioned vessels within ' ||
    ROUND(($2::numeric / 1000.0), 1) || ' km' ||
    CASE WHEN array_length(c.authorities, 1) >= 2
         THEN ' [multi-jurisdictional]'
         ELSE '' END
```

The claim "**within 10 km**" is the FP — DBSCAN doesn't guarantee bounded diameter, only edge-wise density-reachability.

---

## 2. Corpus stats

```
Total events:                    2,748
Non-withdrawn:                   2,748   (100% — no prior withdrawal pass)
Distinct algorithm tags:         1       (no test pollution)
Earliest:                        2026-05-09 00:56:18
Latest:                          2026-05-12 03:54:29  (algorithm silent 7 days)
Per-day emissions:               690/888/643/527 (5/9, 5/10, 5/11, 5/12)
```

Cluster size distribution:
```
sz=3:        170  (6.2%)
sz=4-5:      255  (9.3%)
sz=6-9:      244  (8.9%)
sz=10-19:    142  (5.2%)
sz=20-49:    869  (31.6%)
sz=50+:    1,068  (38.9%)   ← geometrically impossible at 10 km radius
```

---

## 3. Sample query (Step 2)

```sql
SELECT id, event_subtype, event_time, entity_id,
       (properties->>'cluster_size')::int AS sz,
       properties->'member_entity_ids' AS members,
       jsonb_array_length(properties->'authorities') AS auth_count
FROM event
WHERE event_type = 'shadow_fleet_cluster'
  AND event_time >= now() - interval '14 days'
  AND (properties->>'withdrawn') IS NULL
ORDER BY random() LIMIT 30;
```

---

## 4. Per-finding ground-truth verdicts

**Verification method:** for each finding, joined `properties->'member_entity_ids'` back to `entity.current_geom` and computed `MAX(lon)-MIN(lon)` and `MAX(lat)-MIN(lat)` across all members. A genuine 10 km cluster must have spans < ~0.3° at mid-latitudes. Verdict rules:
- **TP** — both lon_span < 0.3° AND lat_span < 0.3° (~33 km × 33 km — generous)
- **AMB** — one of lon/lat span between 0.3° and 1.0°
- **FP** — either lon_span > 1.0° OR lat_span > 1.0° (~111 km — 11× claimed radius)

```
   evt_id                                | subtype     | event_time              | sz  | auth_n | lon_span | lat_span | verdict
   ----------------------------------------+-------------+-------------------------+-----+--------+----------+----------+--------
   d7fbae30-4fb7-4224-b669-f7a3e153c84d  | fleet       | 2026-05-09 00:56:18.52  |   4 |      1 |    15.91 |    23.33 | FP
   19bb5d5f-89c7-4063-9aea-a65f6c9f7665  | large_fleet | 2026-05-09 00:56:18.52  |  16 |      1 |     0.22 |     0.08 | TP
   964b3427-8a2d-46ab-af8e-40bd4e264567  | large_fleet | 2026-05-09 00:56:18.52  |  22 |      1 |   122.04 |    36.23 | FP
   608e35d7-1185-4793-a163-8d8edecb9588  | large_fleet | 2026-05-09 00:56:18.52  |  32 |      1 |    29.98 |     9.63 | FP
   0465e722-25e9-4aae-b9fa-ac79ce573385  | large_fleet | 2026-05-09 00:56:18.52  |  50 |      1 |    25.95 |    27.32 | FP
   6041d1e8-bc1b-4e24-a779-dfef863b3630  | large_fleet | 2026-05-09 00:56:18.52  |  96 |      1 |   129.24 |    57.70 | FP
   f1e8a9d1-914c-4bf0-bcc6-88299cafd718  | large_fleet | 2026-05-09 00:56:18.52  | 125 |      1 |   152.48 |    59.13 | FP
   04c76094-4b5c-4d8d-bd52-3eaeb2f5262c  | large_fleet | 2026-05-09 00:56:18.52  | 130 |      1 |   152.48 |    94.32 | FP
   065f73c6-bf6d-4943-a286-b445bcb7c3d2  | large_fleet | 2026-05-09 00:56:18.52  | 138 |      1 |   152.48 |    94.32 | FP
   8484d230-f2a4-407c-a38f-088d74bd01ed  | cluster     | 2026-05-09 14:32:21.33  |   3 |      1 |     1.31 |     0.37 | FP
   697bf19d-ad40-4979-872a-19efc09f09fa  | fleet       | 2026-05-10 01:03:27.85  |   4 |      1 |     0.30 |     0.04 | AMB
   28e6bab0-f9c4-4c6a-b5e5-2dbf035595c4  | fleet       | 2026-05-10 01:03:27.85  |   5 |      1 |     0.04 |     0.06 | TP
   09f8f618-6641-48e7-b92c-8fa121eb65cd  | large_fleet | 2026-05-10 01:03:27.85  |  17 |      1 |   122.04 |    38.06 | FP
   c9121dcb-20d0-4f34-9220-11dd211ed69c  | large_fleet | 2026-05-10 01:03:27.85  |  35 |      1 |   100.93 |    64.02 | FP
   1ca07e57-16e2-444e-8553-0492317b24b4  | large_fleet | 2026-05-10 01:03:27.85  |  35 |      1 |   127.74 |    64.02 | FP
   09d2bd5f-b33e-4ca4-bd2f-8598b786f9d6  | large_fleet | 2026-05-10 01:03:27.85  |  51 |      1 |   197.76 |    57.57 | FP
   d0a19be8-d955-4002-8b18-fdedb02926e7  | large_fleet | 2026-05-10 01:03:27.85  |  52 |      1 |    16.26 |    39.20 | FP
   6fc6f7ef-74a4-4472-b801-a326bfb1ba00  | large_fleet | 2026-05-10 01:03:27.85  |  54 |      1 |   197.76 |    57.57 | FP
   4d7b41b3-bdf9-4d3f-a4a3-45e12685935a  | large_fleet | 2026-05-10 01:03:27.85  |  92 |      1 |   240.17 |    94.35 | FP
   c784f7e9-1bc1-4aeb-8198-ab147a4bf115  | large_fleet | 2026-05-10 01:03:27.85  |  95 |      1 |   129.24 |    57.72 | FP
   1ef75448-f94b-4cd7-8212-3a491f74d99a  | large_fleet | 2026-05-10 05:04:41.39  |   9 |      1 |     0.15 |     0.15 | TP
   3695bfee-e28c-4d2a-b15e-bf013b6fcf7f  | large_fleet | 2026-05-10 21:03:14.76  |   9 |      1 |    32.12 |     5.21 | FP
   1d10d67f-e738-4983-a843-e1edea8009d4  | cluster     | 2026-05-11 01:04:16.59  |   3 |      1 |     0.19 |     0.09 | TP
   49ad6752-dc0f-4d0a-9790-0d7b5ffa3e09  | fleet       | 2026-05-11 01:04:16.59  |   5 |      1 |     0.13 |     0.14 | TP
   5ad76350-db99-4644-8fe5-ec1267948d10  | large_fleet | 2026-05-11 01:04:16.59  |  46 |      1 |    16.26 |    38.94 | FP
   a5f94127-03b2-4251-a076-38ff9e429dd3  | large_fleet | 2026-05-11 01:04:16.59  |  94 |      1 |   107.96 |    57.64 | FP
   f93f54b9-781b-4644-af0f-c586b1e0a60f  | fleet       | 2026-05-11 15:06:10.39  |   5 |      1 |     0.20 |     1.14 | FP
   ecd62ff7-bea1-48f8-93d5-60da9b26151d  | cluster     | 2026-05-12 01:06:39.46  |   3 |      1 |     8.61 |     5.33 | FP
   366da3f8-1bc3-4cf3-848f-3df42f9c3636  | large_fleet | 2026-05-12 01:06:39.46  |   6 |      1 |    33.58 |     4.85 | FP
   eabd9c8d-dba2-4822-b2a3-6879d6e2637a  | large_fleet | 2026-05-12 01:06:39.46  |  45 |      1 |    16.26 |    38.94 | FP
```

Verdicts: **TP=5, FP=24, AMB=1**. FP rate = 24/30 = **80.0%**.

---

## 5. Evidence — verbatim one TP, one FP

### TP example — `1d10d67f-e738-4983-a843-e1edea8009d4`

```
event_time:  2026-05-11 01:04:16.59
cluster_size: 3 sanctioned vessels
member_names: ["ANAYA", "AQUATICA", "SOLARIS"]
authorities:  ["US Treasury OFAC"]
title:        "CRITICAL — Shadow-fleet cluster: 3 sanctioned vessels within 10.0 km"
```

Verification:
```sql
SELECT canonical_id, display_name, ST_X(current_geom::geometry), ST_Y(current_geom::geometry)
FROM entity WHERE id IN (...);
-- AQUATICA  MMSI 631010100  lon=32.2573  lat=31.6172
-- ANAYA     MMSI 352001906  lon=32.2976  lat=31.5296
-- SOLARIS   MMSI 461000248  lon=32.4444  lat=31.6077
```

All three at Port Said / Suez Canal northern approach. Max pairwise distance via `ST_DistanceSphere`: **17,743 m** (~18 km). DBSCAN density-reachable via overlapping 10 km balls — each vessel is within 10 km of at least one other, but pair A-C is 18 km apart. Borderline (the title says "within 10.0 km" — strictly false for the diameter, true for the eps). Verdict TRUE because vessels are in the same maritime operational area (Suez), the cluster fits the original intent of "STS transfer / dark-cluster gathering."

### FP example — `04c76094-4b5c-4d8d-bd52-3eaeb2f5262c`

```
event_time:   2026-05-09 00:56:18.52
cluster_size: 130 sanctioned vessels
authorities:  ["US Treasury OFAC"]
title:        "CRITICAL — Shadow-fleet cluster: 130 sanctioned vessels within 10.0 km"
member_names: ["ABHRA", "AEGEAN FREEDOM", ..., "BARENTS", "BERING", "FJORD SEAL",
               "KAZAN", "NS ANTARCTIC", "SIBERIA", "ZALIV AMERIKA", "ZALIV AMURSKIY"]
```

Verification:
```sql
WITH mids AS (SELECT (jsonb_array_elements_text(...))::uuid AS m_id)
SELECT ROUND((MAX(lon)-MIN(lon))::numeric,2) AS lon_span,
       ROUND((MAX(lat)-MIN(lat))::numeric,2) AS lat_span,
       ROUND(MIN(lon)::numeric,2) AS lon_min, ROUND(MAX(lon)::numeric,2) AS lon_max
FROM entity JOIN mids ON entity.id = mids.m_id;
-- lon_span=152.48  lat_span=94.32  lon_min=-9.75  lon_max=128.11
```

Members span **Atlantic (~ -10° E) to East Asia (~128° E)** and **lat 1°N to 60°N**. Max pairwise distance computed via `ST_MaxDistance(ST_Collect)` = **15,347 km**. The title claim "within 10.0 km" is off by a factor of **1,535×**. Verdict FALSE — pure DBSCAN density-reachability artifact: sanctioned vessels happen to be densely deployed along major shipping lanes, so chains of overlapping 10 km neighborhoods stitch the whole global fleet into one "cluster."

---

## 6. Duplication ratio (rewrite regression check)

The 2026-05-13/14 ST_ClusterDBSCAN rewrite was supposed to take the duplication ratio from **4.1× → 1.0×**.

```sql
WITH per_set AS (
  SELECT properties->'member_entity_ids' AS members,
         date_trunc('day', event_time) AS d,
         COUNT(*) AS n
  FROM event WHERE event_type = 'shadow_fleet_cluster' GROUP BY 1,2
)
SELECT COUNT(*) AS distinct_set_day, SUM(n) AS total, MAX(n) AS max_per_set
FROM per_set;
-- distinct_set_day=2748, total=2748, max_per_set=1
```

**Duplication ratio = 1.000×.** The rewrite is intact. The 80% FP rate is NOT a regression of the dedup fix — it is a separate, original-from-day-1 bug in the DBSCAN parameter choice.

---

## 7. Root cause + fix proposal

### Root cause (file:line)

`21_GLASSBOX_AI/algorithms/shadow_fleet_cluster.py` lines 74-80:

```sql
dbscan_clusters AS (
    SELECT
        entity_id, geom, authority, name, mmsi,
        ST_ClusterDBSCAN(ST_Transform(geom::geometry, 3857), $2::float, $3::int)
            OVER ()  AS cluster_id
    FROM recent_sanc
)
```

DBSCAN with `eps=10_000m, minpts=3` over a population where ~1,500 sanctioned vessels are scattered along high-density global shipping lanes is virtually guaranteed to produce mega-clusters via density-reachability chains. The choice of DBSCAN (vs e.g. fixed-radius QUERY+filter) is the bug: DBSCAN's premise (density-reachable = same cluster) is incompatible with the algorithm's marketing claim ("vessels within X km").

### Proposed fix

**Option A (minimum-change, preferred):** Add a post-DBSCAN bounding-diameter filter.

After the existing `clusters` CTE, add:
```sql
clusters_bounded AS (
    SELECT c.*
    FROM clusters c
    JOIN dbscan_clusters d ON d.cluster_id = c.cluster_id
    GROUP BY c.cluster_id, c.anchor_id, c.anchor_geom, c.anchor_name,
             c.anchor_mmsi, c.member_ids, c.member_names, c.authorities,
             c.cluster_size
    HAVING ST_MaxDistance(
              ST_Collect(d.geom::geometry),
              ST_Collect(d.geom::geometry)
           ) <= $2::float * 3   -- cap diameter at 3x eps (~30 km)
),
```

Then change `canonical_clusters` to read from `clusters_bounded` instead of `clusters`. A 3× eps diameter cap (~30 km) is a generous interpretation of "vessels within 10 km of each other" that still permits ANAYA/AQUATICA/SOLARIS-style real overlaps in port approaches.

**Option B (more conservative):** Raise `minpts` from 3 to e.g. 5 AND add the bounding-diameter filter. minpts=5 produces fewer but tighter clusters.

**Option C (rewrite):** Drop DBSCAN entirely, use a fixed-radius self-join (anchor vessel → all neighbors within R) with HAVING n >= min_cluster_size. This is what the original (pre-rewrite, pre-2026-05-13) algorithm did before it was rewritten to fix the 4.1× duplication. The duplication can be re-fixed via the `(sorted set, 24h)` dedup, which we already have at lines 166-173.

**Recommendation: Option A.** Smallest delta from current code, preserves the DBSCAN-clustering infrastructure (and the dedup logic), eliminates the FP class. Pseudo-diff:

```diff
@@ -99,7 +99,18 @@ clusters AS (
     GROUP BY cluster_id
     HAVING COUNT(DISTINCT entity_id) >= $3::int
 ),
+-- Reject "clusters" whose actual geographic diameter exceeds 3× eps.
+-- ST_ClusterDBSCAN only guarantees density-reachability (each point
+-- within eps of *some* cluster member), NOT bounded diameter — chains of
+-- overlapping 10 km balls along global shipping lanes produced false
+-- clusters of 100+ vessels spanning continents (audit 2026-05-19).
+clusters_bounded AS (
+    SELECT c.*
+    FROM clusters c
+    WHERE ST_MaxDistance(
+              (SELECT ST_Collect(d.geom::geometry) FROM dbscan_clusters d
+               WHERE d.cluster_id = c.cluster_id),
+              (SELECT ST_Collect(d.geom::geometry) FROM dbscan_clusters d
+               WHERE d.cluster_id = c.cluster_id)
+          ) <= $2::float * 3
+),
 canonical_clusters AS (
     SELECT DISTINCT ON (member_ids)
         anchor_id, anchor_geom, anchor_name, anchor_mmsi,
         member_ids, member_names, authorities, cluster_size
-    FROM clusters
+    FROM clusters_bounded
     ORDER BY member_ids, anchor_id
 )
```

(Per audit recipe: returned as proposal, NOT applied. Main session decides scope and timing.)

---

## 8. Cleanup SQL

Drafted at `21_GLASSBOX_AI/docs/cleanup_sql/2026_05_19_shadow_fleet_cluster_dbscan_chain_chaining.sql`. Expected scope: ~2,000-2,200 of the 2,748 historical events flagged `withdrawn=true`. Filter is geometric (per-cluster lon_span > 1° OR lat_span > 1°), not size-based — so small clusters that happen to be globally scattered are also caught, and large clusters that are actually a real port stack would be preserved (none observed in sample).

Script wrapped in `BEGIN ... ROLLBACK` for manual count verification before commit.

---

## 9. Test additions (recommendation)

Add regression test in `21_GLASSBOX_AI/tests/test_shadow_fleet_cluster.py`:

```python
async def test_dbscan_chain_does_not_produce_global_cluster(temp_pool):
    # Insert 5 sanctioned_vessel_underway events spaced along a 100 km chain
    # (each ~9 km from the next — all within DBSCAN eps=10 km transitively,
    #  but cluster diameter = 36 km > 3 × eps).
    # After fix: NO shadow_fleet_cluster emitted.
    # Before fix: ONE 5-vessel cluster spanning 36 km emitted.
    ...
    n = await run_shadow_fleet_cluster_scan(
        radius_m=10_000, min_cluster_size=3,
        algorithm_tag='shadow_fleet_cluster_test_chain',
    )
    assert n == 0, "chained density-reachability must not produce cluster"
```

Plus the existing dense-port test (3 vessels within 5 km in Suez/Port Said) — should still emit 1.

---

## 10. Bottom line

**FIX-AND-WITHDRAW.** Production FP rate = 80% from a clear, fixable predicate bug. Algorithm has been silent for 7 days so there's no urgent flood, but the 2,748 historical findings are misleading on the cockpit and should be withdrawn. Predicate fix (Option A) is ~10 lines of SQL.

**Next algorithm to audit:** `dark_ship` (per backlog P0-C unaudited list; tractability hint "verify dedup_window param holds — was 24h, changed to per-event").
