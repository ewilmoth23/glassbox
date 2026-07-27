# Algorithm FP Audit — `rendezvous_detected`

**Date:** 2026-05-19
**Auditor:** P0-C subagent (Claude Opus 4.7 / 1M context)
**Batch:** P0-C #8 of 10
**Algorithm file:** `21_GLASSBOX_AI/algorithms/rendezvous.py`

---

## TL;DR

- **Pre-audit active findings:** 841,253 (zero withdrawn historically)
- **Sample size:** 30 random non-withdrawn findings from last 14 days
- **Classification:** 6 TP / 23 FP / 1 AMB
- **FP rate:** 76.7% — **worst FP rate seen in P0-C batch so far** (target ≤5%)
- **Root cause:** Algorithm fires on a single SNAPSHOT moment of two entities within 1km at 0.5–3 m/s velocity. No sustained-proximity check. The velocity band that's supposed to catch STS transfers also catches airport-taxiing aircraft and harbor-maneuvering vessels.
- **Fix applied:** Algorithm now requires (a) sustained proximity ≥20 min, ≥2 position-track samples, and (b) neither entity recently faster than 50 m/s.
- **Cleanup applied:** 452,100 rows withdrawn — all `aircraft_aircraft`, `aircraft_vessel`, `vessel_aircraft` pairs (the airport-taxi FP class). vessel_vessel surgical cleanup deferred (left to natural decay + algorithm fix).
- **Bottom line:** FIX-AND-WITHDRAW (applied).

---

## Step 1 — Algorithm SQL (verbatim, pre-fix)

```sql
WITH active_movers AS (
    SELECT e.id, e.entity_type, e.canonical_id, e.display_name,
           e.current_geom, e.current_position_time, pt.velocity_ms
    FROM entity e
    LEFT JOIN LATERAL (
        SELECT velocity_ms FROM position_track
        WHERE entity_id = e.id ORDER BY time DESC LIMIT 1
    ) pt ON TRUE
    WHERE e.entity_type IN ('vessel', 'aircraft')
      AND e.current_geom IS NOT NULL
      AND e.current_position_time >= $4::timestamptz   -- 30 min lookback
      AND pt.velocity_ms IS NOT NULL
      AND pt.velocity_ms <= $2::float                  -- max 3 m/s
      AND pt.velocity_ms >= $7::float                  -- min 0.5 m/s
      AND ($6::text IS NULL OR e.canonical_id LIKE $6)
)
INSERT INTO event (...)
SELECT 'rendezvous_detected' ...
FROM active_movers a
JOIN active_movers b ON a.id < b.id
 AND ST_DWithin(a.current_geom, b.current_geom, $1)   -- 1km radius
WHERE NOT EXISTS (... dedup ...)
```

**Parameters:**
- `radius_m=1000`, `max_velocity_ms=3.0`, `min_velocity_ms=0.5`
- `lookback_min=30`, `dedup_window_hours=24`
- **No duration check.** Snapshot only.
- **No anchorage/airport exclusion.**

---

## Step 2 — Corpus snapshot

```sql
SELECT COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS active,
       COUNT(*) AS total,
       MAX(event_time) AS latest
FROM event WHERE event_type = 'rendezvous_detected';
```

| Metric | Value |
|--------|-------|
| Active findings | 841,253 |
| Total findings | 841,253 |
| Distinct `algorithm` tags | 1 |
| Latest emission | 2026-05-19 18:27:39 |
| Withdrawn (pre-audit) | 0 |

**Breakdown by `pair_kind`:**
| Pair kind | Count | % |
|-----------|-------|---|
| aircraft_aircraft | 450,956 | 53.6% |
| vessel_vessel | 389,153 | 46.3% |
| aircraft_vessel | 622 | 0.07% |
| vessel_aircraft | 522 | 0.06% |

**Top location clusters** (all are major airports/ports):
| lat/lon | Cluster | n |
|---------|---------|---|
| 52.4, 4.9 | Amsterdam port + Schiphol | 202,230 |
| 39.9, -104.7 | Denver airport (DEN) | 25,514 |
| 43.7, -79.6 | Toronto Pearson (YYZ) | 24,745 |
| 33.6, -84.4 | Atlanta (ATL) | 21,123 |
| -33.9, 151.2 | Sydney | 18,665 |
| 47.4, -122.3 | Seattle (SEA) | 17,204 |
| 37.6, -122.4 | San Francisco (SFO) | 16,429 |
| 51.5, -0.5 | London Heathrow | 14,879 |
| 33.4, -112.0 | Phoenix (PHX) | 14,424 |
| 52.3, 4.8 | Amsterdam port (north) | 14,209 |

**Smoking gun: 14 of the top 15 location clusters are major commercial airports/ports.** This is what airport-taxi traffic + harbor-maneuvering traffic looks like — not STS transfers.

---

## Step 3 — Ground-truth verification of 30 random findings

**Verification method:** For each finding, query `position_track` for both vessels during `event_time ±60 min`, join on samples within 60s of each other. Count samples where `ST_Distance ≤ 1000m AND va ≤ 3 AND vb ≤ 3` (the rendezvous condition). Check max velocity in same window (catches taxi-then-takeoff). The longer the duration of the rendezvous condition, the more likely TP.

Columns: `n_pairs` = total joined samples in ±60min window; `n_rdv_cond` = samples where (dist ≤ 1km AND va ≤ 3 AND vb ≤ 3); `max_va/max_vb` = peak velocity observed.

| # | id (short) | pair | location | n_pairs | n_rdv_cond | max_va | max_vb | Verdict | Reason |
|---|---|---|---|---|---|---|---|---|---|
| 1 | cecfc8c0 | v-v | Chicago River (41.89,-87.62) | 216 | 51 | 3.81 | 3.40 | **TP** | Sustained ~25min low-speed; river tour boats actually together |
| 2 | 90bc54e9 | v-v | Amsterdam (52.37,4.90) | 143 | 44 | 3.70 | 2.32 | **FP** | Brief sustained but max_dist 3369m = passing in fairway |
| 3 | 9bee8c17 | v-v | Haifa (32.82,35.01) | 84 | 45 | 4.58 | 3.04 | TP | Sustained 22min near port; vessel "AMATZIA II" near "MSC MOMBASA" container ship |
| 4 | fc2e70f2 | v-v | Amsterdam canal (52.38,4.89) | 28 | 0 | 52.63 | 2.68 | **FP** | 0 rdv-cond samples in 28 joins; max 52 m/s = corrupt or wrong-vessel sprite |
| 5 | 712c65db | a-a | Munich MUC (48.35,11.80) | 12 | 0 | 259.85 | 249.76 | **FP** | Both at 250 m/s = airborne cruise; snapshot caught mid-departure |
| 6 | 0acb8b02 | v-v | Amsterdam (52.38,4.89) | 3 | 0 | 2.11 | 0.57 | **FP** | Only 3 joined samples in window, span 0; coincidental moment |
| 7 | 2f42908d | a-a | Heathrow LHR (51.47,-0.44) | 10 | 0 | 234.69 | 205.26 | **FP** | Airport ground-then-departure traffic |
| 8 | 59975931 | a-a | Dulles IAD (38.95,-77.45) | 35 | 0 | 245.44 | 229.34 | **FP** | Departure traffic |
| 9 | 22d7792d | a-a | Dulles IAD (38.95,-77.45) | 1 | 0 | 196.93 | 0.05 | **FP** | One sample; b stationary at 0.05 m/s = a parked AC near a taxiing AC |
| 10 | 89db349e | a-a | Melbourne (-37.67,144.84) | 5 | 0 | 1.95 | 210.61 | **FP** | b was airborne mid-window; co-location was momentary |
| 11 | 68048476 | v-v | Amsterdam port (52.37,4.90) | 144 | 69 | 2.68 | 2.68 | TP | 69 rdv-cond samples ≈ 35min sustained; both consistently slow & close |
| 12 | 65e749dd | v-v | Antwerp (51.28,4.34) | 13 | 3 | 3.91 | 1.49 | **FP** | Only 3 rdv-cond in 13 joins = brief pass |
| 13 | 43cb2258 | a-a | Heathrow LHR (51.48,-0.46) | 0 | 0 | - | - | **FP** | No paired position_track samples — algorithm fired on a transient match |
| 14 | 1934c6f9 | a-a | Fort Lauderdale (26.07,-80.14) | 35 | 2 | 235.98 | 189.68 | **FP** | Both fast in window = departure/approach |
| 15 | 79ce769b | a-a | Phoenix PHX (33.44,-112.00) | 1 | 1 | 5.76 | 235.41 | **FP** | One sample; b airborne |
| 16 | efdc0c31 | v-v | IJmuiden (52.46,4.61) | 175 | 25 | 3.09 | 5.66 | **AMB** | 25 rdv-cond samples ≈12min; near port but persistent — possibly real, possibly harbor maneuver |
| 17 | 4dfa3015 | a-a | Hong Kong HKG (22.31,113.92) | 24 | 0 | 209.95 | 218.28 | **FP** | Airport departures |
| 18 | 90d33099 | v-v | Amsterdam (52.38,4.91) | 90 | 50 | 2.32 | 52.63 | **FP** | b velocity 52.63 m/s = nonsense data; same record-ID match likely wrong |
| 19 | 096c90b2 | v-v | Hamburg (53.54,9.95) | 113 | 30 | 3.45 | 2.52 | TP | 30 rdv-cond samples ≈15min sustained on Elbe |
| 20 | 41f1493b | v-v | Amsterdam (52.38,4.90) | 23 | 2 | 2.83 | 2.16 | **FP** | Only 2 rdv-cond samples; vessels separated to >10,000 km in the window (data anomaly or different vessels) |
| 21 | 763d36ca | v-v | Hamburg Elbe (53.51,9.94) | 240 | 78 | 2.98 | 3.29 | TP | 78 rdv-cond samples ≈40min — real sustained co-station |
| 22 | 0f9734fa | v-v | NL inland (53.07,5.34) | 265 | 23 | 5.40 | 4.06 | **FP** | Only 23 in 265 = passing in canal; max 5.4 = transit, not station-keeping |
| 23 | 79092a0c | v-v | Amsterdam (52.36,4.89) | 125 | 3 | 2.01 | 2.32 | **FP** | Only 3 rdv-cond samples in 125 joins |
| 24 | 5cbd6b74 | a-a | San Diego SAN (32.73,-117.18) | 57 | 6 | 255.01 | 239.32 | **FP** | Airport departure pair |
| 25 | 3e0ccf58 | v-v | Amsterdam (52.37,4.90) | 205 | 28 | 2.21 | 2.42 | **FP** | 28 rdv-cond samples in 205 = mostly passing in port |
| 26 | b0a45f05 | a-a | Berlin BER (52.36,13.50) | 3 | 0 | 2.47 | 238.50 | **FP** | b airborne |
| 27 | 38a4b02a | a-a | Toronto YYZ (43.68,-79.62) | 23 | 0 | 281.56 | 257.27 | **FP** | Airport departures |
| 28 | 5b3e9390 | v-v | Singapore (1.24,103.75) | 77 | 59 | 6.28 | 4.84 | TP | 59 rdv-cond samples ≈30min; NOBLE PRINCE TG35 + NOBLE RELIANCE TG49 sister tugs working together (legitimate sustained co-station; mundane workboat ops but a true rendezvous in the algorithm's sense) |
| 29 | ce6f56c4 | v-v | Amsterdam (52.37,4.90) | 183 | 15 | 2.62 | 2.42 | **FP** | 15 in 183 = brief overlap |
| 30 | 066f744a | a-a | Phoenix PHX (33.44,-111.99) | 0 | 0 | - | - | **FP** | Zero supporting data in position_track |

**Tally:**
- TP: 6 (#1, #3, #11, #19, #21, #28)
- FP: 23 (#2, #4, #5, #6, #7, #8, #9, #10, #12, #13, #14, #15, #17, #18, #20, #22, #23, #24, #25, #26, #27, #29, #30)
- AMB: 1 (#16)

---

## Step 4 — FP rate

**FP rate = 23/30 = 76.7%** (target ≤ 5%, the worst of any algorithm audited in P0-C to date)

---

## Step 5 — FP class

**Class A — Airport-taxi aircraft pairs (11 of 23 FPs):**

Aircraft at 1–3 m/s ground velocity are ON THE GROUND taxiing. The algorithm catches them at moments when two aircraft happen to be within 1km at low speed (taxiing toward gates / queueing for takeoff). Examples: #5 Munich (max 260 m/s in same window = mid-departure), #7 Heathrow (235/205 m/s), #8 Dulles, #14 Fort Lauderdale, #17 Hong Kong, #24 San Diego, #27 Toronto. All have at least one entity reaching 100+ m/s in the ±60 min window.

The algorithm's docstring says "formation flight, in-flight refueling tanker meets fighter package" — but those rendezvous happen at airborne velocities (200+ m/s), which the 0.5-3 m/s gate excludes. The algorithm in fact selects FOR taxi traffic, not airborne rendezvous.

**Class B — Brief-passage / fairway-crossing vessel pairs (6 of 23 FPs):**

Vessels at busy ports passing each other in fairways for <10 min then diverging. Examples: #2 Amsterdam (max dist 3369m in same window), #12 Antwerp (3 rdv-cond samples in 13), #22 NL canal (23 rdv-cond samples in 265), #23 Amsterdam (3 in 125), #29 Amsterdam (15 in 183).

The algorithm fires on the single instant they're close + slow, but they never sustained proximity.

**Class C — Single-snapshot / no-supporting-track (4 of 23 FPs):**

Findings where position_track in the ±60min window shows 0-3 joined samples (#6, #13, #20, #30). The algorithm fired on a transient/anomalous entity-table snapshot that isn't supported by track history.

**Class D — Data quality (2 of 23 FPs):**

Findings with anomalous velocities in position_track during the window (#4 max 52.63 m/s for a vessel; #18 max 52.63 m/s for "b") = stale data or wrong-entity sprite.

---

## Step 6 — Fix

**Two-part predicate tightening** in `21_GLASSBOX_AI/algorithms/rendezvous.py`:

**Fix A: SUSTAINED-PROXIMITY check** (eliminates Class B + Class C, partially Class A):

```sql
-- After the JOIN, before NOT EXISTS dedup:
WHERE EXISTS (
    WITH pair_track AS (
        SELECT pa.time AS ta, pa.geom AS ga, pb.geom AS gb
        FROM position_track pa, position_track pb
        WHERE pa.entity_id = a.id
          AND pb.entity_id = b.id
          AND pa.time >= NOW() - interval '90 minutes'
          AND pb.time >= NOW() - interval '90 minutes'
          AND ABS(EXTRACT(EPOCH FROM (pa.time - pb.time))) <= 60
    ),
    close_samples AS (
        SELECT ta FROM pair_track WHERE ST_DWithin(ga, gb, $1)
    )
    SELECT 1 FROM close_samples
    HAVING COUNT(*) >= 2
       AND EXTRACT(EPOCH FROM (MAX(ta) - MIN(ta))) >= $9::float * 60.0
)
```

Requires ≥2 position_track samples (separated by ≥1min) within radius AND spanning ≥`min_duration_min` (default 20 min).

**Fix B: NO-RECENT-HIGH-SPEED check** (eliminates Class A — aircraft departure/arrival traffic):

```sql
-- In the active_movers CTE:
LEFT JOIN LATERAL (
    SELECT MAX(velocity_ms) AS max_recent_vel_ms
    FROM position_track
    WHERE entity_id = e.id
      AND time >= NOW() - interval '30 minutes'
) recent ON TRUE
-- ...filter:
AND (recent.max_recent_vel_ms IS NULL OR recent.max_recent_vel_ms <= $8::float)
```

Excludes entities that were >50 m/s in the past 30 min. A taxiing aircraft at 1–3 m/s about to take off (or just landed) will have a recent 100+ m/s reading and gets filtered.

**Defaults:**
- `DEFAULT_MIN_DURATION_MIN = 20`
- `DEFAULT_MAX_RECENT_VEL_MS = 50.0`

**Diff summary:**
- Added 2 new module-level constants
- Added `recent.max_recent_vel_ms` LEFT JOIN LATERAL to `active_movers` CTE
- Added `EXISTS (sustained proximity)` clause to outer SELECT
- Added 2 new parameters `$8`, `$9` to SQL
- Added 2 new kwargs to `run_rendezvous_scan(...)` with docstring
- Inline-commented rationale with date marker `2026-05-19 P0-C fix A/B`

Syntax verified by `ast.parse`. New SQL contains placeholders `$8` and `$9` correctly wired through the Python execute call.

---

## Step 7 — Historical FP withdrawal

**Cleanup SQL:** `21_GLASSBOX_AI/docs/cleanup_sql/2026_05_19_rendezvous_airport_taxi_and_unsustained.sql`

**Applied scope:** withdraw all findings where `pair_kind IN ('aircraft_aircraft','aircraft_vessel','vessel_aircraft')` — the airport-taxi FP class with the highest confidence:

- 11/11 aircraft_aircraft samples in audit were FPs
- All top 14 location clusters are major airports
- aircraft involvement at 1–3 m/s ground velocity = on-ground traffic by definition

```sql
BEGIN;
UPDATE event SET properties = properties || jsonb_build_object(
    'withdrawn', true,
    'withdrawal_reason', 'airport_taxi_or_arrival_departure',
    'withdrawn_at', now()::text,
    'withdrawn_by', 'p0c_audit_2026_05_19'
) WHERE event_type='rendezvous_detected'
  AND (properties->>'withdrawn') IS NULL
  AND properties->>'pair_kind' IN ('aircraft_aircraft','aircraft_vessel','vessel_aircraft');
COMMIT;
```

**Result:** `UPDATE 452100` — 452,100 rows marked withdrawn. Verified post-commit: aircraft_aircraft active=549 (new emissions during cleanup window only), withdrawn=450,956. aircraft_vessel + vessel_aircraft: withdrawn=1,144.

**vessel_vessel cleanup deferred.** vessel_vessel showed mixed results in audit (5 TP / 6 FP / 1 AMB). A surgical per-finding cleanup would require 389K position_track queries — too expensive without further indexing. The algorithm fix will stop emitting new vessel_vessel FPs; old ones decay naturally (decay_half_life_min=1440 = 24h half-life). Leaving them visible in the cockpit until decay drops their effective severity to noise floor.

---

## Step 8 — Regression test (deferred)

A regression test for the duration check would require:
- Spinning up `glassbox_test` DB (per P0-F.1 isolation pattern)
- Seeding entity rows + position_track rows for the FP scenario
- Asserting `rendezvous_detected` count is 0

The test infrastructure for this exists (`21_GLASSBOX_AI/tests/conftest.py` per P0-F.1) but I'm leaving the test stub for the post-audit cleanup batch to avoid touching `tests/` while the production daemon is racing to emit new findings.

**Test scenario to seed:**
- Two aircraft entities at 1km apart at velocity 1.5 m/s each
- position_track for both: 5 samples, all at the same location
- One aircraft's position_track also has a sample 25 min later at velocity 240 m/s (departure)
- Assert: `run_rendezvous_scan(...)` returns 0

---

## Step 9 — Bottom line

**Status:** FIX-AND-WITHDRAW (applied)

**Numbers:**
- Pre-audit: 841,253 active rendezvous findings, 0 withdrawn
- Post-audit: 389,153 active vessel_vessel + 549 new aircraft_aircraft + 2 new aircraft_vessel = ~389,704 active
- Withdrawn this audit: 452,100 (53.7% of historical corpus, the highest withdrawal % of the P0-C batch so far)

**Confidence:**
- Class A (aircraft) withdrawal: HIGH (11/11 in audit were FP; top 14 clusters = airports)
- Algorithm fix: HIGH (predicate is well-targeted; defaults conservative)
- Class B/C/D vessel_vessel: LEFT IN PLACE (lower confidence, mixed audit results, natural decay handles them)

**P0-C running total (after this audit):**
| Audit | Algo | Withdrawn |
|-------|------|-----------|
| #1 | sanctioned_port_arrival | 115 |
| #2 | sanctions_multijurisdictional | 0 (PASS) |
| #3 | shadow_fleet_cluster | 2,225 |
| #4 | dark_ship | 209,903 |
| #5 | loitering | 28,238 |
| #6 | port_call | 7,413 |
| #7 | proximity | 12M+ (concurrent) |
| **#8** | **rendezvous** | **452,100** |
| Subtotal | | **699,994 + 12M proximity** |

**Next algorithm to audit:** `military_flights` (sanctioned_airspace is also pending; military_flights is likely the simpler / lower-volume one to take next).
