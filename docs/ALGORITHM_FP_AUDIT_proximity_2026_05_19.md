# Algorithm FP Audit — proximity (2026-05-19)

**Algorithm:** `proximity` (file: `21_GLASSBOX_AI/algorithms/proximity.py`)
**Event type(s):** `detected_proximity` (both `proximity` and `proximity_cross` algorithm tags)
**Auditor:** Claude (sub-agent)
**Audit context:** P0-C backlog item, algorithm #7 of 10 unaudited algorithms.
**Procedure:** GLASSBOX_BACKEND_BACKLOG.md §P0-C — 30-finding ground-truth audit.

---

## Bottom line

- **30 findings sampled, last 7 days:** 0 TP / 16 FP / 14 AMB
- **Production FP rate: 53.3% (16/30)** — over 10× the 5% threshold.
- **Root cause:** algorithm matches against ALL event types except `detected_proximity`, including its own downstream algorithm output (`rendezvous_detected`, `dark_vessel_detected`, `loitering_detected`, `port_*`, `sanctioned_*`, `military_aircraft_underway`). In busy maritime/aviation lanes this produces massive fanout — a single rendezvous in the English Channel matched **27,034 nearby vessels** within 50km.
- **Algorithm fix:** SHIPPED to `proximity.py` (NOT IN deny-list of 15 algorithm-derived event types).
- **Cleanup:** PROPOSAL ONLY — 12,072,548 rows of algo-derived-match pollution exceed the 500,000-row hard limit by 24×. Held for operator/main-session approval.
- **Regression test:** ADDED, passes.

---

## Corpus snapshot (taken 2026-05-19)

```
event_type          | active     | total      | distinct_algos | latest                        | last_hour
--------------------+------------+------------+----------------+-------------------------------+----------
detected_proximity  | 28,533,892 | 28,533,892 | 2              | 2026-05-19 16:05:24-04        | 29,204
```

**ZERO previously-withdrawn findings** in this corpus — first audit ever.

Algorithm tag breakdown:
| algo            | total       | last_7d    | last_hour |
|-----------------|-------------|------------|-----------|
| proximity       | 12,094,749  | 7,142,547  | 1,240     |
| proximity_cross | 16,439,143  | 6,399,211  | 0         |

**Observation:** `proximity_cross` has stopped emitting since 2026-05-14 21:10. No env-var disable is set; the function is still scheduled in `glassbox_server.py` `_proximity_scan_loop()`. Cause unknown — possibly silently failing/timing out. Not in scope for this audit but worth filing as a follow-up.

---

## Source-event-type breakdown for `proximity` algo (last 7d)

| underlying event type matched | count     | nature              |
|-------------------------------|-----------|---------------------|
| cross-entity aircraft_vessel  | 6,399,211 | (no source event)   |
| rendezvous_detected           | 6,352,577 | algorithm-derived   |
| dark_vessel_detected          |   499,384 | algorithm-derived   |
| loitering_detected            |   188,335 | algorithm-derived   |
| sanctioned_*                  |    40,849 | algorithm-derived   |
| military_aircraft_underway    |    24,259 | algorithm-derived   |
| port_*                        |    24,447 | algorithm-derived   |
| **algo-derived TOTAL**        | **7,129,851** | **53% of all proximity findings last 7d** |
| neo_close_approach            |     1,081 | raw external (KEEP) |
| gdelt_bulk                    |       991 | raw external (KEEP) |
| sec_filing                    |       109 | raw external (KEEP) |
| volcanic_alert                |         2 | raw external (KEEP) |
| **raw external TOTAL**        | **2,183**  | **0.016% of last 7d** |

The original design intent (per `proximity.py` header lines 9-15: *"show me aircraft NEAR something interesting. That something else comes from other domains: an earthquake, a news event, a port strike, a conflict incident"*) is being **swamped 3300:1** by algorithm-derived events.

---

## Fanout analysis (last 7d, `proximity` algo)

| metric                         | value  |
|--------------------------------|--------|
| distinct source events         | 13,771 |
| avg fanout per source          | 518    |
| median fanout                  | 86     |
| p90                            | 921    |
| p99                            | 11,784 |
| max fanout                     | 28,253 |
| findings with src fanout > 100   | 6,894,989 (97%) |
| findings with src fanout > 1000  | 5,561,323 (78%) |
| findings with src fanout > 10000 | 3,196,606 (45%) |

Interpretation: a single algorithm-derived event in a busy area generates up to **28k "proximity" findings** — pure busy-lane noise.

---

## Per-finding ground truth (30-sample, classification)

Sample drawn via `ORDER BY random() LIMIT 30` from `event_type = 'detected_proximity' AND event_time >= now() - interval '7 days' AND (properties->>'withdrawn') IS NULL`.

### `proximity` algo (entity↔event), n=17

Classification rule:
- TP = source-event fanout ≤ 100 AND raw-external source
- FP = source-event fanout > 1000 OR algorithm-derived source with fanout > 100
- AMB = source-event fanout 100-1000 borderline

| # | finding id (8-char) | event_subtype | algo | dist_m | src event_type | src fanout (7d) | entity exists? | src exists? | entity fresh? | src within decay? | class | reasoning |
|---|---------------------|---------------|------|--------|-----------------|-----------------|----------------|-------------|---------------|-------------------|-------|-----------|
| 1 | b7f8b27c | vessel_dark_vessel_detected | proximity | 35232 | dark_vessel_detected | 1,674 | Yes (ZWARTE GANS, 244660128) | Yes (2026-05-18 14:44) | Yes | Yes (decay 1440) | **FP** | algo-derived src; fanout 1,674 = busy area |
| 2 | bb70a160 | vessel_rendezvous_detected | proximity | 26686 | rendezvous_detected | 6,676 | Yes (DEO CONFIDENTES) | Yes | Yes | Yes | **FP** | fanout 6,676 — Dutch coastal lane |
| 3 | c3643c58 | vessel_rendezvous_detected | proximity | 2795 | rendezvous_detected | 1,502 | Yes (ROSALIE) | Yes | Yes | Yes | **FP** | fanout 1,502 |
| 4 | 2d10f2ef | aircraft_rendezvous_detected | proximity | 14491 | rendezvous_detected (aircraft_aircraft) | 1,536 | Yes (KLM9955) | Yes | Yes | Yes | **FP** | fanout 1,536 |
| 5 | 7f01016d | vessel_rendezvous_detected | proximity | 31432 | rendezvous_detected | 27,034 | Yes (ARION) | Yes | Yes | Yes | **FP** | extreme fanout 27,034 |
| 6 | 57805c01 | vessel_rendezvous_detected | proximity | 49779 | rendezvous_detected | 26,986 | Yes (WILHELMINA JACOBA) | Yes | Yes | Yes | **FP** | fanout 26,986; dist 49.8km = at radius edge |
| 7 | 67e79e79 | vessel_rendezvous_detected | proximity | 5039 | rendezvous_detected | 27,008 | Yes (HEIDE) | Yes | Yes | Yes | **FP** | fanout 27,008 |
| 8 | f6bbe0d3 | vessel_rendezvous_detected | proximity | 5377 | rendezvous_detected | 2,148 | Yes (IZAR) | Yes | Yes | Yes | **FP** | fanout 2,148 |
| 9 | a7789bdf | vessel_rendezvous_detected | proximity | 532 | rendezvous_detected | 22,366 | Yes (PRINSES CHRISTINA) | Yes | Yes | Yes | **FP** | fanout 22,366 |
| 10 | b701354b | vessel_rendezvous_detected | proximity | 2598 | rendezvous_detected | 22,381 | Yes (ACTIEF) | Yes | Yes | Yes | **FP** | fanout 22,381 |
| 11 | fb39b0ba | vessel_rendezvous_detected | proximity | 4065 | rendezvous_detected | 8,302 | Yes (SPES) | Yes | Yes | Yes | **FP** | fanout 8,302 |
| 12 | 30518f45 | aircraft_rendezvous_detected | proximity | 23880 | rendezvous_detected (aircraft_aircraft) | 147 | Yes (AAY2825) | Yes | Yes | Yes | **AMB** | borderline fanout |
| 13 | 8a80aa46 | aircraft_loitering_detected | proximity | 16762 | loitering_detected | 1,484 | Yes (TFL511) | Yes | Yes | Yes | **FP** | fanout 1,484 |
| 14 | aa54b60d | vessel_rendezvous_detected | proximity | 44632 | rendezvous_detected | 11,767 | Yes (DEO ANNUENTE) | Yes | Yes | Yes | **FP** | fanout 11,767 |
| 15 | fb9e7ac2 | vessel_rendezvous_detected | proximity | 2152 | rendezvous_detected | 10,200 | Yes (CHATEAUROUX) | Yes | Yes | Yes | **FP** | fanout 10,200 |
| 16 | d997ad2b | vessel_rendezvous_detected | proximity | 26218 | rendezvous_detected | 8,684 | Yes (TUG 21) | Yes | Yes | Yes | **FP** | fanout 8,684 |
| 17 | dd8c95a2 | vessel_rendezvous_detected | proximity | 5179 | rendezvous_detected | 3,007 | Yes (STELLA) | Yes | Yes | Yes | **FP** | fanout 3,007 |

**Subtotals:** 0 TP / 15 FP / 2 AMB (only #12 is moderate-fanout; the rest are clear FP)

Wait — let me re-check finding #4 vs #12. Both are aircraft_rendezvous_detected. #4's source has fanout 1,536, #12's source has fanout 147. So #4 is FP, #12 is AMB. Revised subtotals match what I had: **15 FP, 1 AMB → consolidated as 15 FP / 2 AMB if we treat near-borderline 1,500 as AMB. For conservatism I'm classifying anything ≥1000 as FP; that's 16 FP / 1 AMB at the strict cutoff.**

Going with strict cutoff (fanout > 1000 = FP, fanout 100-1000 = AMB):
- #4 fanout 1,536 → FP (just over)
- #12 fanout 147 → AMB
- All others fanout >1000 → FP

**Revised proximity subtotals: 0 TP / 16 FP / 1 AMB.**

### `proximity_cross` algo (entity↔entity, aircraft↔vessel), n=13

These match the algorithm spec (within 50km of each other within 60min window). However, all 13 are aircraft↔vessel at 50km radius — an aircraft over any coast matches hundreds of vessels by definition. Per-sample fanout query timed out due to JSONB GIN traversal cost; global stats show **51% of all proximity_cross volume came from entities with fanout >1000**.

**Classification:** All 13 are technically algorithm-spec-correct (within radius+window). The deeper question — *is 50km aircraft-over-vessel proximity operationally useful?* — is a policy/scope question, not a per-finding bug. **Classified as 13 AMB** (works as designed, low signal).

| # | finding id | entity_a → entity_b | claimed_dist | current_dist | within radius+window at finding time? | class |
|---|------------|---------------------|--------------|--------------|----------------------------------------|-------|
| 18 | 626ce7cd | aircraft DAL1581 → vessel US GOV VSL 338815000 | 11,602 m | (entities moved) | Yes | AMB |
| 19 | 3e5b8dd2 | aircraft N951HC → vessel ISABEL L | 13,195 m | 64,422 m | Yes | AMB |
| 20 | 6ca24ba6 | aircraft EJA679 → vessel EVENING STAR | 16,702 m | 901,626 m | Yes | AMB |
| 21 | fd69bd74 | aircraft TAP944 → vessel ENTERPRISE | 11,283 m | 680,779 m | Yes | AMB |
| 22 | 715d4383 | aircraft N969TM → vessel UTOPIA IV | 10,430 m | 301,409 m | Yes | AMB |
| 23 | 28811934 | aircraft KLM1301 → vessel TOPAZ | 17,368 m | 462,229 m | Yes | AMB |
| 24 | d0bff345 | aircraft TRA5612 → vessel THALES | 47,745 m | 5,331 m (closer now) | Yes | AMB |
| 25 | 8b7de11f | aircraft SWA3704 → vessel LELA FRANCO | 6,202 m | 1,339,837 m | Yes | AMB |
| 26 | 6bcbef28 | aircraft CHX42 → vessel ATLANTIS | 26,797 m | 7,648 m | Yes | AMB |
| 27 | c5e4b809 | aircraft TAM3010 → vessel OCEANICASUB XIX | 13,294 m | 479,563 m | Yes | AMB |
| 28 | a037da1f | aircraft RPA4540 → vessel HUNTER G | 48,969 m | 1,590,074 m | Yes | AMB |
| 29 | 2baa2a5d | aircraft TWB844 → vessel T-7 | 24,168 m | 111,269 m | Yes | AMB |
| 30 | 3d2776b7 | aircraft UAL201 → vessel TUG 92 | 47,790 m | 11,841,197 m | Yes | AMB |

**Subtotal:** 0 TP / 0 FP / 13 AMB

### Combined sample classification

- **TP: 0**
- **FP: 16** (all from `proximity` algo matching algorithm-derived events with high fanout)
- **AMB: 14** (1 borderline-fanout proximity + 13 cross-entity aircraft↔vessel)

**FP rate: 16/30 = 53.3%** — far over the 5% threshold.

---

## Root cause

`PROXIMITY_SCAN_SQL` in `proximity.py` lines 96-105 (before fix):

```sql
JOIN event ev
  ON ev.event_time >= NOW() - (COALESCE(ev.decay_half_life_min, $1)::int * INTERVAL '1 minute')
 AND ev.event_type <> 'detected_proximity'    -- ONLY THIS exclusion
 AND ev.geom IS NOT NULL
 AND ST_DWithin(lp.position_geom, ev.geom, $2)
```

The single `ev.event_type <> 'detected_proximity'` exclusion prevents recursive matching but does NOT prevent matching against other algorithm-derived events (`rendezvous_detected`, `dark_vessel_detected`, `loitering_detected`, `port_*`, `sanctioned_*`, `military_aircraft_underway`).

These algorithm-derived events:
1. Use 1440-min (24h) `decay_half_life_min`, so they stay "fresh" 24× longer than raw external events (60 min default).
2. Geo-cluster in busy lanes (English Channel, Singapore Strait, Persian Gulf, US East Coast).
3. Each one matches every vessel within 50km of it.

Result: in a busy lane, ONE rendezvous_detected event spawns thousands of proximity findings. Multiplied across 13,771 distinct source events in last 7d, that's 7.13M findings (53% of total proximity output).

---

## Fix applied (`proximity.py`, lines 97-128 in the rewritten block)

```sql
JOIN event ev
  ON ev.event_time >= NOW() - (COALESCE(ev.decay_half_life_min, $1)::int * INTERVAL '1 minute')
 -- 2026-05-19 P0-C audit (algorithm #7): exclude ALL algorithm-derived event
 -- types, not just detected_proximity. [...rationale comment...]
 AND ev.event_type NOT IN (
   'detected_proximity',
   'rendezvous_detected',
   'dark_vessel_detected',
   'loitering_detected',
   'port_call',
   'port_arrival',
   'port_departure',
   'sanctioned_vessel_went_dark',
   'sanctioned_vessel_rendezvous',
   'sanctioned_vessel_underway',
   'sanctioned_port_arrival',
   'aircraft_in_sanctioned_airspace',
   'military_aircraft_underway',
   'shadow_fleet_cluster_detected',
   'sanctions_match',
   'sanctions_multijurisdictional_match'
 )
 AND ev.geom IS NOT NULL
 AND ST_DWithin(lp.position_geom, ev.geom, $2)
```

**Side effect:** the new fix is conservative — it explicitly lists 15 algorithm-derived event types. New algorithm-derived types added in the future need to be added to this list. An alternative would be to switch to an allowlist of raw-external event types, but that risks excluding new external ingesters silently. The deny-list with a comment is the safer trade-off.

The `CROSS_ENTITY_SCAN_SQL` is NOT modified by this fix — cross-entity proximity is a policy/scope debate (see §"Operator decision points" below), not a clear-cut FP class. Plus that scan is silently not emitting anyway since 2026-05-14.

---

## Cleanup: HELD AS PROPOSAL (>500k row limit hit)

**Scope of cleanup at 53% FP rate:**

| class | rows |
|-------|------|
| Algo-derived-match findings | **12,072,548** |
| Cross-entity aircraft_vessel | 16,439,143 |
| **Total possible cleanup** | **28,511,691** |

The unambiguous-FP portion (12.07M rows) **exceeds the 500,000-row hard limit by 24×**. Per audit operating rule: HELD FOR REVIEW.

**Cleanup SQL written as proposal at:**
`21_GLASSBOX_AI/docs/cleanup_sql/2026_05_19_proximity_algo_derived_match_fanout_PROPOSAL.sql`

The file has the verification dry-run (BEGIN/SELECT/ROLLBACK), then a commented-out UPDATE block for operator to uncomment and run during a low-load window. Recommends batching by `event_subtype`.

**Note:** the algorithm fix in `proximity.py` is already applied, so no new pollution will accumulate. Existing findings naturally fall out of operational queries within 60 minutes (`decay_half_life_min=60`). The cleanup is for clean corpus state, not blocking the dashboard.

---

## Regression test

Added `test_proximity_excludes_algorithm_derived_event_types` to `21_GLASSBOX_AI/tests/test_proximity.py`.

The test seeds (a) one raw `usgs_quake` event and (b) one of each of the 15 algorithm-derived event types — all within radius/window of a single aircraft. Asserts exactly 1 finding is produced (the quake), proving every algo-derived type is denied while genuine external events still fire.

```
tests/test_proximity.py::test_proximity_excludes_algorithm_derived_event_types PASSED
tests/test_proximity.py 17 passed in 0.93s
```

All 16 pre-existing tests still pass.

---

## Operator decision points (post-audit, not for sub-agent to apply)

1. **Apply Part 1 cleanup (12.07M algo-derived match rows)?** Audit-evidence supports it. UPDATE on 12M rows during a low-load window. Batched by `event_subtype`.
2. **Audit cross-entity proximity separately?** All 13 sample findings work as designed but match aircraft-over-port at 50km — operationally low-signal. Options: tighten radius to 5-10km for aircraft↔vessel; require relative altitude check; disable entirely; or accept as-is. Independent decision.
3. **Investigate why `proximity_cross` stopped emitting on 2026-05-14 21:10**. No env-var disable found; scheduled at 5min interval in `_proximity_scan_loop()`. Likely silently timing out — file a P3.
4. **Adopt allowlist instead of deny-list?** Current fix is a deny-list of 15 algorithm-derived types. Future algorithm-derived types will silently leak through until added. Operator may prefer maintaining an explicit allowlist of `external_ingested_event_types`.

---

## Audit-trail summary

| field | value |
|-------|-------|
| Algorithm fix location | `21_GLASSBOX_AI/algorithms/proximity.py` lines 97-127 |
| Algorithm fix status | SHIPPED (in working tree, not committed) |
| Regression test | `21_GLASSBOX_AI/tests/test_proximity.py::test_proximity_excludes_algorithm_derived_event_types` PASSING |
| Cleanup SQL | `21_GLASSBOX_AI/docs/cleanup_sql/2026_05_19_proximity_algo_derived_match_fanout_PROPOSAL.sql` |
| Cleanup applied | **NO — HELD FOR REVIEW** (12M+ rows exceeds 500k limit) |
| Withdrawn historical rows | 0 (none until operator approves Part 1) |
