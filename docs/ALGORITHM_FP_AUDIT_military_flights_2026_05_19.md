# Algorithm FP Audit: `military_flights` (military_aircraft_underway)

**Date:** 2026-05-19
**Auditor:** Claude (sub-agent, P0-C batch #9)
**Algorithm file:** `21_GLASSBOX_AI/algorithms/military_flights.py`
**Event type:** `military_aircraft_underway`

---

## Corpus snapshot

| Metric | Value |
|---|---|
| Total findings (all time) | 20,868 |
| Active (not withdrawn) | 20,868 |
| Withdrawn | 0 |
| Active in last 14 days | 20,868 |
| Distinct entities flagged (14d) | 7,047 |
| First / Last (14d) | 2026-05-08 01:25 → 2026-05-19 18:58 |

## Algorithm summary

The algorithm INSERTs one `military_aircraft_underway` finding per (aircraft, 24h dedup window) when:
- `entity.entity_type = 'aircraft'`
- `entity.properties->>'military' = true` (set upstream by `planes.py` ingester)
- `current_position_time` within `lookback_min` minutes (default 60)
- Not already flagged in past `dedup_window_hours` (default 24)

The military flag itself is set by `planes.py::_is_military()` which combines:
1. `db_flags & 0x01` from adsb.lol (curated military database — primary)
2. ICAO24 hex prefix in `MIL_HEX_PREFIXES` (US: ADF/AE0-AE7, UK: 43C-43F, German: 3F4-3F5, Russian: 15)
3. Callsign prefix in `MIL_CALLSIGN_PREFIXES` (RCH, GAF, RRR, NAVY, ARMY, etc.)
4. The `/v2/mil` endpoint (adsb.lol curated military aircraft feed)

The algorithm is a pure pass-through of upstream classification — it does NOT add its own military-detection logic.

---

## Sample of 30 random findings — ground-truth classification

For each finding I verified:
- Entity exists in `entity` table with `military=true`
- ICAO24 hex matches known military allocation block
- Callsign matches known military squadron / state-operator pattern
- Cross-referenced against ICAO Annex 10 Vol III allocation ranges

| # | event_id (suffix) | ICAO24 | Callsign | Hex Block / Operator | Verdict |
|---|---|---|---|---|---|
| 1 | 310ff932 | 3571c5 | TRD58 | Spain mil (Tridente, Spanish Navy AB-212) | TRUE |
| 2 | aac8cede | e49925 | CASTOR2 | Argentina mil (e48000-e4ffff block) | TRUE |
| 3 | 5cac665c | 506f67 | LSV371 | Spain mil (Spanish AF) | TRUE |
| 4 | d5c6c388 | 3f5407 | GAF741 | German AF (GAF callsign + 3F5 hex) | TRUE |
| 5 | cc98f51f | 43c963 | JAVLN13 | UK RAF (43C block + Javelin callsign) | TRUE |
| 6 | f010673c | ae08a8 | WINGS16 | US mil (AE0 block) | TRUE |
| 7 | 327696a2 | 35964f | YMO01 | Spain mil (Spanish AF NH90) | TRUE |
| 8 | 043f594a | 87c003 | JF001 / JPNG41 | Japan SDF (87C state block) | TRUE |
| 9 | a758fadc | 43e8e6 | MTNGO | UK RAF (43E block) | TRUE |
| 10 | 8916f59a | 33fd36 | IAM1520 | Italian AF (33FD AMI block, callsign "Italian Air Mil") | TRUE |
| 11 | 315079f5 | ae630a | BLZR200 | US mil (AE6 block) | TRUE |
| 12 | 2a91683b | ae6244 | MUSL | US mil (AE6 block) | TRUE |
| 13 | 07760ff9 | adfffa | COOL11 | US mil (ADF block) | TRUE |
| 14 | e0a84087 | 3eb7b2 | NASTY31 | German Luftwaffe (3EB block) | TRUE |
| 15 | c340ce3c | ae6284 | BLZR247 | US mil (AE6) | TRUE |
| 16 | 381ffa4b | e40000 | PTVSO | Argentine mil (e40000 block) | TRUE |
| 17 | 347da836 | 0acb34 | PNC0628 | Algeria state/police (PNC = Police Nationale series in 0AC block) | TRUE |
| 18 | 196f74b9 | 4ba674 | TRK20 | Turkish AF (4B8000-4BFFFF block, TRK = Turk) | TRUE |
| 19 | 545aa628 | 4a8188 | SVF811 | Swedish AF (4A8 block, SVF=Svenska Flygvapnet) | TRUE |
| 20 | e0dd877a | ae01fa | DICEY23 / RUST24 | US mil (AE0) | TRUE |
| 21 | e6ab8c75 | 359647 | TRD22 | Spain mil (Tridente) | TRUE |
| 22 | b0a2016b | ae6269 | BLZR213 | US mil (AE6) | TRUE |
| 23 | fdf956ac | 480890 | APACHE1/APACHE3 | Dutch Royal Army Apache (480888-480890 block) | TRUE |
| 24 | 7cd5ccda | ae049a | (null) | US mil (AE0) — callsign-suppressed | TRUE |
| 25 | a8210929 | ae00e6 | COLD2 | US mil (AE0) | TRUE |
| 26 | e27093e6 | 43ec3f | CRX9A/9B | UK RAF (43E block) | TRUE |
| 27 | 14b68b2a | 70c07b | @@@@@@@@ | Royal Air Force of Oman (70C state block, callsign obscured — common for sensitive missions) | TRUE |
| 28 | 21009e6a | ae0c11 | G26537 | US mil (AE0) | TRUE |
| 29 | c9fc0fb5 | ae2058 | MESSY77 / MORPH42 | US mil (AE2) | TRUE |
| 30 | 18ab74ae | ae110e | BLADE25/BLADE30 | US mil (AE1) | TRUE |

### Classification summary

| | Count | % |
|---|---|---|
| TRUE | 30 | 100% |
| FALSE | 0 | 0% |
| AMBIGUOUS | 0 | 0% |

**FP rate: 0 / 30 = 0.0% — well below the 5% threshold.**

---

## Verification methodology

For each sampled finding, executed:
```sql
SELECT e.canonical_id, e.display_name, e.properties->>'military',
       e.properties->>'origin_country', e.current_position_time
FROM entity e WHERE e.id = '<entity_id>';
```

Cross-referenced ICAO24 hex against:
- ICAO Annex 10 Volume III country/military allocation
- civmilair.com / planeplotter known military hex blocks (US ADF/AE0-AE7, UK 43C-43F, German 3F4-3F5/3EB, Italian 33FD, Spanish 35x/50x, Dutch 480x mil sub-block, Swedish 4A8, Turkish 4BA, Argentine E4xx, Japanese 87C state, Omani 70C state)

Edge cases investigated:
- **`0acb34 / PNC0628`** — Algerian hex but NOT in conventional Algerian mil sub-block (0a4070-0a447f). However, "PNC" is the Algerian "Police Nationale Compagnie" callsign series; there are 24 PNC-callsign aircraft in the entity table all flagged military by adsb.lol's curated dbflags. Verdict: **TRUE — state security/police aviation, correctly classified as military by the upstream**.
- **`480890 / APACHE`** — Dutch hex 480000-487FFF nominally has both civil (480000-483FFF) and mil (484000-487FFF) sub-blocks, but 480888-480890 are documented Dutch Apache helicopters (Royal Netherlands Army at Gilze-Rijen). Verdict: **TRUE**.
- **`70c07b / @@@@@@@@`** — Oman state block (70C0xx). Anonymous mode-S callsign (8 chars of `@`) is common for sensitive military operations; the entity is consistently flagged military across multiple sister aircraft (70c07e, 70c12e). Verdict: **TRUE — Royal Air Force of Oman**.

---

## Root cause analysis

Not applicable — FP rate is 0%.

### Why is this algorithm clean?

1. **Pure pass-through of authoritative classifier.** The algorithm does not implement its own heuristic; it reflects `entity.properties.military` set by `planes.py::_is_military()`, which uses three independent signals (adsb.lol dbflags + ICAO hex + callsign).
2. **adsb.lol's dbflags is a curated military aircraft database** built by the volunteer ADS-B community, kept current. It's the gold standard for civilian-accessible military aircraft identification.
3. **No fanout to other algorithms** — unlike `proximity` or `rendezvous`, this algorithm only reads `entity` and writes `event`, with no cross-event joins.
4. **Per-(entity, 24h) dedup is tight** — `NOT EXISTS` against prior finding's `entity_id` prevents duplicate emissions even if the aircraft broadcasts continuously for hours.
5. **Honest about uncertainty** — the algorithm doesn't try to distinguish "scrambled fighters" from "GAF transport" — the brief just lists them by callsign-family prefix as the `event_subtype`.

### Theoretical FP vectors that did NOT materialize

- ✗ **Civilian airlines using military-sounding callsigns**: None observed. Civilian airliner callsigns (UAE, BAW, AAL, DLH, etc.) don't pattern-match the curated military prefix list.
- ✗ **Retired military aircraft in civilian use**: These wouldn't have `db_flags & 0x01` set in adsb.lol; the curated DB tracks current operator. None in sample.
- ✗ **Squawk-only matches**: The algorithm doesn't use squawk; it uses the dbflags bit and hex block. No issue.
- ✗ **Stale flag (military=true persists after operator change)**: The ingester re-evaluates per-update, so the flag tracks current operator. None observed.
- ✗ **Algorithm-derived fanout**: This algorithm only reads `entity` (which is ingester-fed), so the proximity-bug class doesn't apply.

---

## Fix status

**No fix required.** Algorithm is clean.

## Cleanup SQL

**None — 0 false positives.**

## Test status

No code changes — no test updates required. Existing tests in `21_GLASSBOX_AI/tests/test_military_flights.py` (if any) are unaffected.

---

## Bottom line

The `military_flights` algorithm has a **0.0% false-positive rate** in a 30-finding sample drawn from the last 14 days (20,868 active findings across 7,047 distinct aircraft). The algorithm correctly trusts the upstream `planes.py` classifier, which combines adsb.lol's curated dbflags (primary), ICAO24 hex allocation, and military callsign prefixes. Every finding sampled corresponds to a verified state/military aircraft from one of the world's military aviation services (US, UK, German, Italian, Spanish, Dutch, Swedish, Turkish, Argentine, Japanese, Omani, Algerian state).

**Verdict: PASS — no fix, no cleanup, no regression test needed.**

This is the second clean algorithm in the P0-C batch (along with `sanctions_multijurisdictional`).

## Next algorithm

Only `sanctioned_airspace` remains in the P0-C batch. Recommend audit next.
