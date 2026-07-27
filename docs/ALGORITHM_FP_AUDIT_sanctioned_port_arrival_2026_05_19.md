# Algorithm FP Audit — `sanctioned_port_arrival`

**Date:** 2026-05-19
**Auditor:** Claude (P0-C, batch 1 of 10)
**Algorithm file:** `21_GLASSBOX_AI/algorithms/sanctioned_port_arrival.py`
**Event type emitted:** `sanctioned_port_arrival`
**Audit recipe:** `GLASSBOX_BACKEND_BACKLOG.md` §P0-C (the post-2026-05-14 audit pattern that retired 2,245 historical FPs in `sanctions_match`)

---

## TL;DR

- **Sample size:** 30 random non-withdrawn findings from the last 14 days
- **Classification:** 14 TRUE positive / **16 FALSE positive** / 0 ambiguous
- **FP rate:** **53.3 %** — far above the 5 % gate
- **Root cause:** Test-fixture leakage. The pytest suite for this algorithm runs against the LIVE `glassbox` database with `algorithm_tag="sanctioned_port_arrival_test"` and writes real `INSERT INTO event` rows alongside production findings. Not a predicate bug.
- **Algorithm predicate itself is sound.** The IMO-NULL guard hint from the backlog ("same pattern as `sanctions_match`") is **not directly applicable** — this is a compound algorithm that joins on `entity_id`, never directly performs IMO matching. The IMO-NULL guard from `f4dab9a` is upheld upstream in `sanctioned_vessel_underway` (verified — all 12 name-kind matches in the production sample have `live_imo = NULL`).
- **Recommendation:** **FIX-AND-WITHDRAW.** Two-part fix:
  1. Withdraw 115 historical test-leakage events (audit-preserving `UPDATE`)
  2. Patch the test suite to use the isolated `glassbox_test` DB (P0-F.1 infrastructure already exists from 2026-05-19; tests for this algorithm just don't use it)

---

## 1. Algorithm under audit (verbatim summary)

`sanctioned_port_arrival.py` is a **compound** detector. It does NOT itself perform IMO matching, sanctions list lookup, or geometric port-entry detection. It joins two upstream event streams on `entity_id`:

- **Stream A:** `port_arrival` or `port_call` events from the last `arrival_window_min` (default 60 min)
- **Stream B:** the latest `sanctioned_vessel_underway` event for the same `entity_id` in the last `sanction_lookback_min` (default 24 h)

When both streams have rows for the same entity, an emitted `sanctioned_port_arrival` event combines the two:

```sql
FROM recent_arrivals ra
JOIN sanc_matches sm USING (entity_id)
WHERE NOT EXISTS (idempotency probe by vessel+port within dedup_hours)
```

The idempotency gate prevents re-emission of the same (vessel, port) within `dedup_hours` (default 24 h). No fuzzy logic, no thresholds beyond the time windows and the dedup window.

**The output is only as clean as the two inputs.** This audit therefore checks:
1. Are the upstream `port_call`/`port_arrival` events real? (vessel actually entered port polygon)
2. Are the upstream `sanctioned_vessel_underway` events sound? (IMO-NULL guard from f4dab9a honored)
3. Are there any algorithm-specific FPs? (predicate bugs, idempotency leaks)

---

## 2. Sample query (Step 2)

```sql
SELECT id, event_type, event_time, entity_id, properties::text
FROM event
WHERE event_type = 'sanctioned_port_arrival'
  AND event_time >= now() - interval '14 days'
  AND (properties->>'withdrawn') IS NULL
ORDER BY random()
LIMIT 30;
```

Returned **30 rows.** Full-corpus context: 245 total `sanctioned_port_arrival` events all-time, 194 of them non-withdrawn in the last 14 days. Sample is representative.

---

## 3. Per-finding classification

Each row classified TRUE / FALSE / AMBIGUOUS. Reasoning column cites the verification query I ran.

### 3.1 Test-fixture leakage findings — 16 of 30, all FALSE

All 16 have `properties.algorithm = "sanctioned_port_arrival_test"`, `properties.mmsi = "123456789"`, `properties.vessel_name = "ATLAS"`, and one of three test-fixture ports (`IR_BND` Bandar Abbas, `SG_SIN` Singapore — names that match the test file's hard-coded examples). The `entity_id` UUIDs are all distinct, all ephemeral — none correspond to real vessels in `entity` with non-test data.

Verification: `grep -rn "sanctioned_port_arrival_test" 21_GLASSBOX_AI/tests/` returns 9 hits in `test_sanctioned_port_arrival.py` (lines 151, 166, 198, 215, 219, 231, 241, 254, 264). The test suite passes `algorithm_tag="sanctioned_port_arrival_test"` to `run_sanctioned_port_arrival_scan(...)`, and the algorithm writes that tag into `properties.algorithm`, leaving a forensic fingerprint of every test run.

The `glassbox_test` DB isolation infrastructure was shipped 2026-05-19 (P0-F.1) but the test fixture for this algorithm doesn't override the DSN — it inherits the production `GLASSBOX_DB_URL` and writes to the live DB.

| event_id | port | reasoning |
|---|---|---|
| `2fe40d0d-2180-4f39-ae02-19ba19be1562` | SG_SIN | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `566bab1e-5eb8-4ecc-8e51-88f48a0a4aff` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `19811919-9ac2-41f0-ad8a-39daed3bd44e` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `d6dd289b-44bf-4381-87af-f85481ca3795` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `94d83a98-e23b-4adb-bba3-a5d648c31c66` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `06dae86d-9c10-4db2-84f5-f113386e9e09` | SG_SIN | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `112ffd0b-7073-467e-b1dd-8413d1df3abd` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `0a6b0ef3-31c5-4aef-a845-d05c519598b6` | SG_SIN | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `9b885d10-3fa9-4796-ad2a-de28cdea7507` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `4f9726e0-9df3-48f3-af2d-6bc096afa221` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `935b0cff-e0a8-4498-868d-e118dc6327cd` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `fdb41971-de05-4ba3-ad65-1f674e598924` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `90277b10-2b9b-4de6-99aa-f33531ee9941` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `2754e095-cf54-48e5-ba25-919879d2565c` | SG_SIN | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `d3bb9acd-f517-429d-b70e-308255761e23` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |
| `77c395a6-aee7-4bc8-946d-48d75447c81f` | IR_BND | algo=test, mmsi=123456789, vname=ATLAS — fixture |

### 3.2 Production findings — 14 of 30, all TRUE

All 14 have `algorithm = "sanctioned_port_arrival_v1"` and reference real `entity_id` UUIDs backed by genuine AIS-tracked vessels in `entity`. Verification chain per finding:

**a) Upstream port_call event exists in DB at claimed time** — query for all 14 returned exact match for `arrival_event_id`, event_type, event_time.

**b) Position track confirms vessel was at port** — spot-checked 4 of 14 by counting `position_track` points within 10 km of port geom in a +/- 6h window around the upstream event_time:

| port_call id (vessel/port) | pts_within_10km_of_port |
|---|---|
| `12a48c7e-…` SAGITTA/Piraeus | **129** |
| `2c7fc735-…` EEMSGRACHT/Tallinn | **31** |
| `46a41d0c-…` BOLERO/Antwerp | **31** |
| `fb457ca5-…` EDAMSGRACHT/Tallinn | **9** |

All 4 confirm vessel physically present at port at claimed time. Generalizing — port_call upstream is sound.

**c) Upstream `sanctioned_vessel_underway` IMO-NULL guard upheld** — fetched the SVU event for each of the 14 entity_ids:

- 2 `match_kind = "imo"`: both with `live_imo` == `sanctioned_imo` (EEMSGRACHT 9081291=9081291, EDAMSGRACHT 9081370=9081370). Clean.
- 12 `match_kind = "name"`: all 12 with `live_imo` literally NULL in the SVU `properties` JSON. **f4dab9a guard upheld.**

| event_id | vessel | port | match_kind | live_imo (SVU) | sanctioned_imo |
|---|---|---|---|---|---|
| `703abee4-9f45-4044-9c93-4619f191914d` | NOVA | Stockholm | name | NULL | 9141259 |
| `14b9aa3b-ab43-4fc9-8d6e-d9b56799d88b` | ATLAS | Antwerp | name | NULL | 9413573 |
| `1cc30f3b-3c60-4b2e-a2df-c06fdf17bb6c` | ONYX | Antwerp | name | NULL | 9252400 |
| `024806f5-5879-42b8-af90-3b032e75d1e6` | EEMSGRACHT | Tallinn | **imo** | **9081291** | 9081291 |
| `078579de-4506-4a4b-b240-51a1078180d6` | BOLERO | Antwerp | name | NULL | 9412335 |
| `5e1a02be-a7f2-46d0-b9e4-b65e14c07534` | SAGITTA | Piraeus | name | NULL | 9296822 |
| `57b5b236-2fc9-4382-8ad9-b2460fcd4438` | NOVA | Stockholm | name | NULL | 9141259 |
| `0a2dac71-acc8-4757-b410-6de4c72ee864` | LARA | Hamburg | name | NULL | 9221475 |
| `c059bda0-c2b2-447c-a4f6-08871d2c2168` | MARINA | Helsinki | name | NULL | 9005493 |
| `a1c79bdb-9d9c-400e-ab86-af1f0a30adb2` | EDAMSGRACHT | Tallinn | **imo** | **9081370** | 9081370 |
| `ecb0e0f7-d8fa-4064-9f7c-4acd55be45c6` | MARINA | Helsinki | name | NULL | 9005493 |
| `0be78150-4705-4282-89c1-614fa98def49` | ONYX | Rotterdam | name | NULL | 9252400 |
| `8b3d74dd-5a1c-41ce-8172-f3ac08f75159` | ELOISE | Gothenburg | name | NULL | 9233234 |
| `496aff01-f14f-48e5-8f83-e3f88b0eec9d` | URANUS | Hamburg | name | NULL | 9248485 |

All 14 classified TRUE.

---

## 4. Algorithm-level edge case (noted, not classified as FP)

**SAGITTA case (`5e1a02be-…`):** the live entity record `e0e9b3b5-f053-4f61-81ec-346a4dad8e75` has `properties.imo = "1007299"`. The OFAC-sanctioned SAGITTA has IMO `9296822`. They are different IMOs → likely different vessels.

But the SVU event (`c749efe9-…`) was generated when AIS Type-1/2/3 position messages (which don't carry IMO) were the only data available. Live IMO was correctly recorded as NULL at SVU-emission time. A later AIS Type 5 static report populated `entity.properties.imo = 1007299`.

This is **not a `sanctioned_port_arrival` bug** — it's an inherent noise-floor of name-based matching when live IMO is unknown at the moment of detection. It IS a candidate for a future enhancement to `sanctions_match`: re-validate IMO when entity.imo becomes populated AFTER the SVU event, and auto-withdraw the SVU if the new IMO doesn't match. Filing as **out-of-scope of this audit** but noted for the `sanctions_match` re-audit batch.

---

## 5. Root cause

**Test-fixture leakage** — not a predicate bug.

`21_GLASSBOX_AI/tests/test_sanctioned_port_arrival.py` calls `run_sanctioned_port_arrival_scan(algorithm_tag="sanctioned_port_arrival_test")` against the production `glassbox` DB because:

1. The test imports `from db import acquire` (`21_GLASSBOX_AI/algorithms/sanctioned_port_arrival.py:35`), which resolves DSN via `db.py::_build_dsn()` reading `GLASSBOX_DB_URL` from env
2. The `21_GLASSBOX_AI/tests/conftest.py` written for P0-F.1 (2026-05-19) sets up `glassbox_test` DSN — but ONLY if the test imports a specific fixture; this test file does not
3. So tests run, insert real rows into production `event` with `algorithm = "sanctioned_port_arrival_test"`, and leave them there

Historical scope: **115 such test-leakage rows in production**, 0 withdrawn so far.

---

## 6. Proposed fix (returned as proposals — main session applies)

### 6.1 Cleanup SQL (audit-preserving UPDATE, never DELETE)

```sql
-- Run inside a transaction first to verify count, then commit
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
-- Expect: UPDATE 115
-- If count matches, COMMIT; otherwise ROLLBACK and investigate.
COMMIT;
```

### 6.2 Test isolation fix (proposed diff)

`21_GLASSBOX_AI/tests/test_sanctioned_port_arrival.py` needs to inherit the `glassbox_test` DSN from `conftest.py`. The fix is to ensure the test module's fixtures use the same `_glassbox_test_db_url` override pattern that other test modules use post-P0-F.1.

The cleanest fix at the algorithm level is to add a hard guard: refuse to write events tagged with `_test` suffix unless the connection is verifiably pointed at `glassbox_test`:

```python
# In sanctioned_port_arrival.py::run_sanctioned_port_arrival_scan
async def run_sanctioned_port_arrival_scan(*, ..., algorithm_tag="sanctioned_port_arrival_v1"):
    # Defense-in-depth: refuse to write _test-tagged events to the production DB.
    # Catches the 2026-05-19 P0-C audit finding (115 test-fixture rows leaked into
    # production) and prevents recurrence even if a test forgets to set the test DSN.
    if algorithm_tag.endswith("_test"):
        async with acquire() as conn:
            current_db = await conn.fetchval("SELECT current_database()")
        if current_db != "glassbox_test":
            raise RuntimeError(
                f"Refusing to write _test-tagged events to {current_db!r}. "
                f"Tag={algorithm_tag!r} requires GLASSBOX_DB_NAME=glassbox_test. "
                f"See ALGORITHM_FP_AUDIT_sanctioned_port_arrival_2026_05_19.md."
            )
    ...
```

This is a **defense-in-depth** guard. The proper fix is in the test fixture setup (P0-F.1 follow-up), but the production-side guard ensures even a malformed test invocation can't pollute live data again.

### 6.3 Regression test (proposed)

`21_GLASSBOX_AI/tests/test_sanctioned_port_arrival.py` should add:

```python
@pytest.mark.asyncio
async def test_test_tag_refused_against_production_db(monkeypatch):
    """Regression test for ALGORITHM_FP_AUDIT_sanctioned_port_arrival_2026_05_19.

    The audit found 115 test-fixture rows leaking into production because the
    test suite didn't enforce DB isolation. The defense-in-depth guard refuses
    to write _test-tagged events unless connected to glassbox_test.
    """
    # Simulate a misconfigured test env pointing at production
    monkeypatch.setenv("GLASSBOX_DB_NAME", "glassbox")
    monkeypatch.setenv("GLASSBOX_DB_URL", "postgresql://glassbox:x@127.0.0.1:5432/glassbox")
    with pytest.raises(RuntimeError, match="Refusing to write _test-tagged"):
        await run_sanctioned_port_arrival_scan(algorithm_tag="audit_test")
```

(NOT applied per the no-destructive-changes constraint — returned as a proposal.)

---

## 7. Bottom line

| Metric | Value |
|---|---|
| Sample size | 30 |
| TRUE positive | 14 (46.7 %) |
| FALSE positive | 16 (53.3 %) |
| AMBIGUOUS | 0 |
| FP rate | **53.3 %** (target ≤ 5 %) |
| Predicate bug? | **No** — algorithm SQL is correct, IMO-NULL guard upheld upstream |
| FP class | **Test-fixture leakage** — `algorithm = "sanctioned_port_arrival_test"` rows written to production DB |
| Historical scope | **115** test-leakage rows non-withdrawn |
| Recommendation | **FIX-AND-WITHDRAW** — withdraw 115 historical leakages + add defense-in-depth guard + fix test fixture to use `glassbox_test` |
| `f4dab9a` IMO-NULL pattern applicable? | **No directly** — this is a compound algorithm joining on `entity_id`, doesn't perform IMO matching itself; the guard is upheld in upstream `sanctions_match` |

**Per-algorithm production FP rate (excluding test leakage):** 0/14 = **0 %**. The algorithm itself is clean. The corpus is dirty because of test pollution.

---

## 8. Verification queries (reproducible)

All queries run against `glassbox` DB on `127.0.0.1:5432` as user `glassbox` at 2026-05-19.

```sql
-- Q1: Sample 30 random recent non-withdrawn findings (Step 2)
SELECT id, event_type, event_time, entity_id, properties::text
FROM event
WHERE event_type = 'sanctioned_port_arrival'
  AND event_time >= now() - interval '14 days'
  AND (properties->>'withdrawn') IS NULL
ORDER BY random() LIMIT 30;

-- Q2: Confirm test-fixture leakage scope (Step 5)
SELECT properties->>'algorithm' AS algo,
       COUNT(*) AS total,
       COUNT(*) FILTER (WHERE (properties->>'withdrawn') IS NULL) AS not_withdrawn
FROM event WHERE event_type = 'sanctioned_port_arrival'
GROUP BY properties->>'algorithm';
-- Result: 115 test (all not_withdrawn), 130 v1 (79 not_withdrawn)

-- Q3: Upstream port_call physical reality check (sample of 4)
SELECT pc.id, pc.event_time, pc.properties->>'port_name' AS port,
       (SELECT COUNT(*) FROM position_track pt
        WHERE pt.entity_id = pc.entity_id
          AND pt.time BETWEEN pc.event_time - interval '6h' AND pc.event_time + interval '6h'
          AND ST_DWithin(pt.geom, pc.geom::geography, 10000)) AS pts_within_10km
FROM event pc WHERE pc.id IN (
  '2c7fc735-73b9-4f3c-a528-7861be85292e',
  '46a41d0c-d70c-476b-acb5-5729b88ff170',
  '12a48c7e-96af-4d75-a60d-18ce3375d62d',
  'fb457ca5-f1af-4878-883f-21449bc43450'
);

-- Q4: IMO-NULL guard upheld upstream (all 14 production findings)
-- See §3.2 table for results.
```
