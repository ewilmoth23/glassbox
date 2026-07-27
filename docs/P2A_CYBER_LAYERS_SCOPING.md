# P2-A Cyber-Attack Data Layers — Scoping Doc

**Generated:** 2026-05-27 LATE NIGHT (post-trilogy session, pre-feature-work)
**Source backlog item:** `GLASSBOX_BACKEND_BACKLOG.md` § P2-A (line 676)
**Target:** Implement the 5-6 cyber-data layers that v1 glassbox had (`cyberThreats` / `cyberPulses` / `cyberThreatIntel` / `cisaKev`) — "much better than the original" per the operator's ask.

This doc gives the next session a runway. It freezes the source list,
codifies the per-source legal posture, sketches the implementation
phases, and identifies the gates that need operator sign-off before
work begins.

## Why this matters

V1 glassbox surfaced known-exploited vulnerabilities + threat intel
indicators on the cockpit globe. The new cockpit (`atlas.js`) has 5
existing infrastructure layers (military bases, nuclear, cables,
trafficking, pipelines) that prove the geojson-layer pattern works at
scale. Cyber layers slot into the same shape.

**Operator ask (verbatim per `GLASSBOX_NEXT_SESSION_PROMPT.md`):** "much
better than the original" — implying not just feature parity but
operational discipline (current threat intel, license-clean ingestion,
proper attribution on the public surface).

## Source freeze (5 candidates; license posture per source)

| ID | Source | License | Commercial-OK? | Update cadence | Coverage |
|---|---|---|---|---|---|
| 1 | **CISA KEV** | CC0 (US gov public domain) | ✅ Yes — explicit | Daily | ~1100 known-exploited CVEs w/ vendor + product attribution |
| 2 | **Spamhaus DROP/EDROP** | Free, redistributable | ✅ Yes per Spamhaus ToS § "Use of DROP and EDROP lists" | ~Hourly (plain-text feed) | ~500 /24 blocks belonging to hijacked/stolen-from-RIR netspace |
| 3 | **AlienVault OTX** | Free w/ API key, NEEDS RE-VERIFICATION | ⚠️ ToS check required — sells a paid commercial tier so free tier may restrict | Near-real-time (subscription pulses) | ~10k indicators across ~20k pulses |
| 4 | **GreyNoise community** | Free 50 IPs/day, ToS allows attributed redistribution | ⚠️ Verify w/ GreyNoise the 50/day quota suits a daily-refresh of a known-list | Near-real-time | Internet background noise classifier — separates targeted from noise scans |
| 5 | **Shadowserver Foundation** | Free w/ org registration | ⚠️ Org registration required — operator-action gate | Daily reports | Sinkholed-botnet IPs, compromised-host telemetry, etc. |

### Decisions deferred to operator sign-off

Each source carries a non-trivial gate that the next session CANNOT
clear on its own:

1. **OTX ToS re-verification.** The backlog says "free, API key
   required, check ToS before commercial-use mark." That's the gate.
   Operator action: re-read OTX ToS at current revision, save PDF to
   `00_MASTER_DOCS/legal/license_evidence/otx_tos_2026_MM_DD.pdf`,
   set `commercial_use_ok: true|false` in `infra/sources.yaml`.

2. **GreyNoise quota suitability.** 50 IPs/day might or might not
   meet the use case. Operator action: confirm with GreyNoise whether
   their community tier supports a public-facing read of their
   "noise" classifications for our scale. If yes, document; if
   no, defer or pay.

3. **Shadowserver org registration.** Requires a registered org
   account. Operator action: register MEWR Creative Enterprises
   with Shadowserver Foundation, save approval letter to
   `00_MASTER_DOCS/legal/license_evidence/shadowserver_org_approval_2026_MM_DD.pdf`,
   then add API key to `.env`.

**Recommendation:** Start with **CISA KEV + Spamhaus only** (both
have zero gating) as MVP. Layer 3+ requires operator-side legal/admin
work before the next session can touch them.

## Implementation phases

### Phase 1 (MVP — no operator gate): CISA KEV + Spamhaus

**Estimated effort:** 4-6 hours single session.

**CISA KEV deliverable:**
- New ingester at `21_GLASSBOX_AI/ingesters/cisa_kev.py` (24h poll cadence)
- New writer at `21_GLASSBOX_AI/writers/cisa_kev.py` (event-table shape per the post-Phase-3 cluster template)
- New `infra/sources.yaml` entry with full license metadata
- New route at `21_GLASSBOX_AI/web/routes/infrastructure.py`:
  `GET /api/v1/infrastructure/cyber-kev` → serves the KEV catalog as
  geojson w/ point-per-CVE (positioned by primary vendor HQ when
  geocodable, otherwise sentinel) — matches the 5 existing
  infrastructure layers' shape
- Atlas.js layer toggle: `?heat=1&kev=1` URL flag → renders KEV
  points colored by `cvss_score` (yellow → red gradient)

**Spamhaus DROP/EDROP deliverable:**
- New ingester at `21_GLASSBOX_AI/ingesters/spamhaus_drop.py` (1h poll cadence)
- New writer at `21_GLASSBOX_AI/writers/spamhaus_drop.py`
- `infra/sources.yaml` entry (license: "Spamhaus ToS, redistributable")
- New route `GET /api/v1/infrastructure/cyber-spamhaus-drop` →
  /24-block list with primary-RIR jurisdiction + reason code per entry
- Atlas.js layer toggle `?spamhaus=1` → /24 cells rendered as
  semi-transparent red Ellipses on the globe (no actual point — block
  geo is implied by the cell's assigned RIR's HQ region; could be a
  list view in the side panel instead of a globe overlay)

### Phase 2 (post-OTX-ToS-verification): AlienVault OTX

**Estimated effort:** 3-5 hours, blocked on OTX ToS gate.

Same pattern. OTX pulses are richer than KEV — each pulse has tags,
geocoding, IOCs (IPs/domains/hashes). The ingester subscribes to
~50 trusted OTX users + the OTX team's official pulses, normalizes
the IOCs, and writes them as `event_type='otx_pulse'` rows.

Frontend rendering: a cyber-pulses heat overlay similar to atlas.js's
existing density-grid heatmap pattern (see `_loadDensityHeatmap` in
atlas.js).

### Phase 3 (post-org-registration): Shadowserver + GreyNoise

**Estimated effort:** 5-8 hours, blocked on both gates.

The most operationally complex. Both require API keys + careful rate
limit handling. Shadowserver in particular publishes large daily
reports (sinkholed-botnet IPs are typically tens-of-thousands per day);
deciding what to surface vs what to ingest-and-drop is a UX decision.

## File-by-file change list

For the MVP phase (CISA KEV + Spamhaus only):

| File | Action | Effort |
|---|---|---|
| `infra/sources.yaml` | Add 2 entries (cisa_kev, spamhaus_drop) | 5 min |
| `21_GLASSBOX_AI/ingesters/cisa_kev.py` | New ~150 lines (HTTP poll + parse) | 60 min |
| `21_GLASSBOX_AI/ingesters/spamhaus_drop.py` | New ~120 lines (plain-text feed) | 45 min |
| `21_GLASSBOX_AI/writers/cisa_kev.py` | New ~110 lines (event-table cluster template) | 30 min |
| `21_GLASSBOX_AI/writers/spamhaus_drop.py` | New ~100 lines (event-table cluster template) | 30 min |
| `21_GLASSBOX_AI/writers/__init__.py` | Add 2 re-export lines for the new cluster modules | 5 min |
| `21_GLASSBOX_AI/glassbox_server.py` | Import + register 2 new ingesters in startup | 15 min |
| `21_GLASSBOX_AI/web/routes/infrastructure.py` | Add 2 new geojson serving routes | 20 min |
| `21_GLASSBOX_AI/data/cyber_kev.geojson` | Static curated geojson generator (initial seed) | 30 min |
| `21_GLASSBOX_AI/data/cyber_spamhaus_drop.geojson` | Static curated geojson generator | 30 min |
| `21_GLASSBOX_AI/landing/atlas.js` | Add 2 layer toggles + render logic (~60 lines net) | 45 min |
| `21_GLASSBOX_AI/tests/test_cisa_kev_ingester.py` | New unit test (mocked HTTP) | 30 min |
| `21_GLASSBOX_AI/tests/test_spamhaus_drop_ingester.py` | New unit test (mocked feed) | 30 min |
| `21_GLASSBOX_AI/tests/test_cyber_layer_routes.py` | New route smoke tests | 20 min |

**Total Phase 1 effort:** ~6 hours dev + 0.5h test runs + 0.5h review = **~7 hours single focused session**.

## Test plan

### Unit tests (mock HTTP, no network)

- `test_cisa_kev_ingester.py` — mock HTTP returns sample KEV JSON;
  ingester emits N events with expected schema (cve_id, vendor_project,
  product, vulnerability_name, date_added, due_date, required_action,
  notes, known_ransomware_campaign_use)
- `test_spamhaus_drop_ingester.py` — mock plain-text feed; ingester
  emits N events with /24 + sbl_id + assigned_country (when present)

### Writer tests (mocked acquire_write)

Mirror the existing `test_phase2_round2_dual_write.py` template — pin
the INSERT SQL shape, empty-list returns 0, idempotency.

### Route smoke tests

- `GET /api/v1/infrastructure/cyber-kev` returns 200 + valid geojson
- `GET /api/v1/infrastructure/cyber-spamhaus-drop` returns 200 + valid geojson
- Both routes are in the route-coverage manifest at `tests/test_routes_smoke.py`

### Smoke-test extension

Add 2 new entries to `21_GLASSBOX_AI/tests/test_writers_smoke.py`'s
`EXPECTED_WRITERS` tuple:
- `"write_cisa_kev_events"`
- `"write_spamhaus_drop_events"`

This ensures the 24-symbol manifest check at
`test_public_writer_manifest_complete` covers them + the
`test_writer_empty_list_returns_zero` parametrized check runs against
both. (Pytest baseline goes 1178 → 1184 for Phase 1.)

## UI integration

Cockpit layer toggles use the existing `?heat=1`-style URL-flag
pattern documented in atlas.js. Suggested flags:

| Flag | Layer | Visual |
|---|---|---|
| `?kev=1` | CISA KEV | Yellow→red gradient points per CVE (positioned by primary vendor HQ when geocodable) |
| `?spamhaus=1` | Spamhaus DROP | Semi-transparent red Ellipses per /24 block, positioned by assigned RIR's HQ region |

Both layers should have a footer attribution string visible when the
toggle is active. The 5 existing infrastructure layers don't do this
today; this would be a new UX pattern to standardize.

## Risks + unknowns

1. **Geocoding KEV CVEs.** Each KEV entry has a vendor_project field
   (e.g., "Microsoft", "Apache"). Geocoding to a single point is
   ambiguous — Microsoft has 100+ offices globally. **Decision needed:**
   pick a primary HQ (e.g., Redmond for Microsoft) via a curated
   `vendor_to_hq.json` lookup, OR drop the geo dimension entirely and
   render KEVs as a sortable list panel instead of globe points.
   Recommend: side panel list view; the globe overlay adds zero
   intelligence for non-geo-bound entities.

2. **Spamhaus /24 → geo positioning.** A /24 block doesn't have a
   meaningful single geo coordinate. RIR-of-record gives a region
   (ARIN→US, RIPE→EU, etc.) but not a city. **Decision needed:**
   render as a region-level choropleth on the globe (no precise point)
   or as a side panel list. Recommend: side panel; choropleths require
   a polygon dataset (Natural Earth or similar) and a separate render
   path that doesn't exist in atlas.js today.

3. **Threat-intel surveillance ethics.** Cyber-threat layers can imply
   surveillance use (tracking specific IPs / actors). Per
   `00_MASTER_DOCS/legal/LEGAL_COMPLIANCE_REGISTRY.md` Chapter 7
   (DRIFT_PREVENTION Rule on surveillance-prohibited sources),
   verify each source's ToS allows public visualization. CISA KEV is
   safe (it's published CVE data, not actor attribution). Spamhaus is
   safe (block lists, no individual attribution). The other 3 sources
   need explicit ToS review during their gating step.

4. **Refresh cadence vs DB load.** Daily refresh (KEV) is trivial;
   hourly (Spamhaus) is fine; near-real-time (OTX, GreyNoise) needs
   the same db_write_failures-aware backoff pattern as the existing
   AISStream + Bluesky ingesters. Use those as templates.

5. **Test fixtures.** Real cyber-data fixtures must be either
   synthetic (handwritten) or sanitized snapshots — never check in
   real OTX pulse data or Spamhaus DROP entries since those carry
   actor IP info. Synthetic fixtures are cleaner.

## Recommended execution order

1. **Open this doc + the backlog item** at the start of the next P2-A session
2. **Pick Phase 1 only** (CISA KEV + Spamhaus) — both have zero gates
3. **Start with CISA KEV** (simpler — JSON over HTTPS, well-documented
   schema, no rate-limit considerations)
4. **Then Spamhaus DROP** (plain-text feed, easy parser)
5. **DEFER Phase 2 + 3 until operator clears each source's gate** —
   don't write code against an unverified license

**Order of file edits for Phase 1:**

1. `infra/sources.yaml` — add 2 entries (license metadata is the gate)
2. `21_GLASSBOX_AI/ingesters/cisa_kev.py` — TDD: write the test first
   (`test_cisa_kev_ingester.py`), then the ingester
3. `21_GLASSBOX_AI/writers/cisa_kev.py` — copy from `writers/sec.py`
   (similar shape: event-table, no embedding) and adapt the SQL +
   property whitelist
4. `21_GLASSBOX_AI/writers/__init__.py` — add the re-export line
5. `21_GLASSBOX_AI/web/routes/infrastructure.py` — add the geojson
   serving route
6. `21_GLASSBOX_AI/glassbox_server.py` — import + register the new
   ingester in startup
7. Repeat 2-6 for Spamhaus DROP
8. `21_GLASSBOX_AI/landing/atlas.js` — add the 2 layer toggles + render
   logic
9. `21_GLASSBOX_AI/tests/test_writers_smoke.py` — extend
   `EXPECTED_WRITERS` tuple
10. `21_GLASSBOX_AI/tests/test_routes_smoke.py` — extend manifest
11. Full pytest → expect 1184 passed (1178 + 6 new tests)
12. Daemon restart to activate the 2 new ingesters in production
13. Document the deployment in `21_GLASSBOX_AI/CHANGELOG.md`

## Hand-off checklist for the next session

- [ ] Read this doc (P2A_CYBER_LAYERS_SCOPING.md) before starting
- [ ] Confirm operator has cleared the 3 gates for Phase 2+3 sources
      (OR confirm Phase 1 only and defer 2+3)
- [ ] Verify daemon is up + pytest baseline is 1178
- [ ] Pick Phase 1; start with CISA KEV TDD
- [ ] Commit per-source (one ingester + writer + route + atlas toggle
      per commit), follow the per-cluster commit cadence pattern from
      P3-H Phase 3
- [ ] Test the daemon-restart smoke after each ingester lands
      (lsof :8790 + curl /api/health to confirm the new ingester
      appears in the ingesters array w/ health="ok" after 1-2 cycles)

## Related docs

- `GLASSBOX_BACKEND_BACKLOG.md` § P2-A (the backlog entry this doc
  expands on)
- `21_GLASSBOX_AI/web/routes/infrastructure.py` (the existing
  infrastructure layer pattern to mimic)
- `21_GLASSBOX_AI/writers/sec.py` (cleanest event-table cluster module
  to copy from — simple shape, uses `_with_confidence`)
- `21_GLASSBOX_AI/ingesters/usgs_volcano.py` (a clean reference for a
  daily-poll JSON ingester)
- `00_MASTER_DOCS/legal/LEGAL_COMPLIANCE_REGISTRY.md` Chapter 2
  (NEVER-USE) + Chapter 7 (surveillance-prohibited) — verify each
  new source against these before writing ingester code
- `00_MASTER_DOCS/legal/LICENSE_RISK_REGISTER.md` — log every
  source's license + redistribution posture there
