-- ============================================================================
-- 2026-05-19 — Rendezvous algorithm FP withdrawal
-- ============================================================================
--
-- Per P0-C audit (ALGORITHM_FP_AUDIT_rendezvous_2026_05_19.md): 30-sample audit
-- showed FP rate of 76.7% (23/30) with two dominant FP classes:
--
-- Class A: AIRPORT-TAXI AIRCRAFT PAIRS.
--   Aircraft at 1-3 m/s ground speed are taxiing on airport surfaces, not in
--   airborne rendezvous. Algorithm's 0.5-3 m/s velocity gate selects FOR
--   this. 11/11 aircraft_aircraft samples in audit were FPs at major
--   airports (LHR, IAD, MUC, PHX, SAN, YYZ, HKG, FLL, MEL, BER).
--   pair_kind='aircraft_aircraft' total: 450,956 active findings.
--   pair_kind='aircraft_vessel'/'vessel_aircraft' (likely same root cause —
--   aircraft taxiing or landing near a docked vessel at port-adjacent
--   airport): 1,144 active findings.
--
-- Class B: SNAPSHOT-ONLY VESSEL-VESSEL PAIRS.
--   Vessels at port hotspots (Amsterdam 52.37/4.9 had 205K alone, Hamburg
--   had 12K) close together for <10 min then separating. NOT a rendezvous.
--   In audit: 12 vessel-vessel samples, 5 TP, 6 FP, 1 AMB.
--
-- This file withdraws Class A (high-confidence FPs, ~452K rows).
-- Class B is left to natural decay + the algorithm fix to stop emitting new
-- ones; a surgical FP withdraw on 389K vessel-vessel findings is left as a
-- followup (would need per-finding position_track checks).
--
-- Mode: UPDATE with properties.withdrawn=true. Never DELETE.
-- ============================================================================

BEGIN;

UPDATE event
SET properties = properties || jsonb_build_object(
    'withdrawn',         true,
    'withdrawal_reason', 'airport_taxi_or_arrival_departure',
    'withdrawn_at',      now()::text,
    'withdrawn_by',      'p0c_audit_2026_05_19'
)
WHERE event_type = 'rendezvous_detected'
  AND (properties->>'withdrawn') IS NULL
  AND properties->>'pair_kind' IN ('aircraft_aircraft','aircraft_vessel','vessel_aircraft');

-- Expected: ~452,100 rows. Will print COUNT before commit.
-- To verify scope before commit, run separately:
--   SELECT COUNT(*) FROM event WHERE event_type='rendezvous_detected'
--     AND (properties->>'withdrawn') IS NULL
--     AND properties->>'pair_kind' IN ('aircraft_aircraft','aircraft_vessel','vessel_aircraft');

COMMIT;
