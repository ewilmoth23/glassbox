# GLASSBOX V2 MIGRATION PLAN — the master roadmap (v2 — full scope)

**Date:** 2026-05-04 (revised after PROJECT_HANDOFF integration)
**Status:** APPROVED. Items 1-8 from the external-Claude review are all in scope.
**Author:** Claude (post-Pass-1 audit + post-handoff-doc review, after Ethan's "do it correctly" mandate)
**Supersedes:** Original `GLASSBOX_V2_MIGRATION_PLAN.md` (2026-05-03) and `GLASSBOX_SHIP_PLAN.md` (demo-scoped, deprecated).

---

## What changed in v2 of this plan

After review of `PROJECT_HANDOFF.md` from external Claude, Ethan approved 8 net-new architecture decisions on 2026-05-04 that materially expand scope to make Glassbox a real, defensible, monetizable product:

1. ✅ **PostgreSQL+PostGIS+TimescaleDB** as durable spatial+time-series store (was: KV + in-memory deque only)
2. ✅ **Decouple Glassbox from Prediqt/Loop/Kalshi** — clean platform/consumer separation
3. ✅ **deck.gl** as a second renderer alongside Cesium for dense layers
4. ✅ **Statistical algorithms layer** — loitering, rendezvous, AIS gap, anomaly, clustering (NOT LLM)
5. ✅ **Entity ontology + provenance tables** (entity / source / entity_attribute / position_track / event)
6. ✅ **Vertical slice methodology** — build one killer query end-to-end before broad expansion
7. ✅ **Concrete perf budgets** as CI gates
8. ✅ **Multi-user architecture from day 1** even though v1.0 launches single-user

Explicitly REJECTED for v1.0: GraphQL, Apache AGE, MinIO, Docker Compose, MLX-LM migration without measured benefit. Add later if specific need emerges.

**Timeline impact:** 10-12 weeks → **14-18 weeks** for v1.0 production launch with full scope. This is the "do it correctly" timeline. Anything faster cuts items 4 + 5 (algorithms + ontology), which are exactly what makes Pro tier credible. Don't cut them.

---

## OPERATING PRINCIPLES (locked, do not relitigate)

1. **Correctness over speed.** Every fix is the right fix. Half-built = redo work.
2. **Performance is the precondition for monetization.** A laggy globe is not a product.
3. **Add data, don't subtract.** The April 27 cut-list (kill ships/sats/traffic_cams) REJECTED.
4. **Activate every dormant credential.** See `GLASSBOX_API_CREDENTIALS_CHECKLIST.md`.
5. **Hard separation: platform vs consumers.** Glassbox detects and surfaces events. It NEVER knows what consumers do with them. Prediqt, sports edge, content gen, enterprise — each a separate consumer of the same firehose.
6. **Build INTO existing code, don't rewrite from scratch.** v141-v145 work is preserved. We add layers; we don't bulldoze.
7. **Tests are non-negotiable.** Every new ingester, every algorithm, every API endpoint ships with a test suite. The `gdelt_topical.py` ingester sets the bar.
8. **Vertical slice first.** Build the killer query end-to-end before broadening.
9. **Snapshot before every destructive change. Verify the revert.** No exceptions. Every edit to `glassbox.html` snapshots to `16_GLASSBOX_VERSIONS/`. Every schema migration is reversible (Alembic up + down). Every ingester migration tests rollback to KV-only behavior. We never leave the system in a state that can't be reverted in <60 seconds.
10. **Document for the year-from-now-Ethan.** Every product gets a `<PRODUCT>_BIBLE.md` — a comprehensive knowledge base that a year-from-now operator (you, an employee, an acquirer doing due diligence) could read top-to-bottom and understand: what it is, why it exists, how it's architected, what every file does, every credential needed, every operational procedure, every known failure mode, every business decision and its rationale. Treated as a living document; updated with every material change. Glassbox bible is the first deliverable.
11. **Operator runbook is the final delivery.** When v1.0 is shipped, hand off `OPERATOR_RUNBOOK.md` — every command Ethan needs to copy-paste to operate, deploy, monitor, recover, rotate credentials, and onboard help. No "you should know" implicit knowledge.
12. **v1.0 ships with ZERO recurring paid API costs.** Every ingester must satisfy: free tier + commercial-use-OK + no FCRA-restriction violation + no login-to-scrape pattern. Paid sources (OpenSanctions, Shodan, VirusTotal, Datalastic, OpenSky, etc.) are deferred to v1.2 Pro tier where customer revenue funds them. **The only "paid" item allowed in v1.0 is Cesium Ion's free tier (5GB/100k req/mo) — watch quota.** Cost discipline is a moat, not a constraint.
13. **Defensible legal posture, not lawsuit-proof.** Anyone can sue anyone. The bar is: every activity that creates real exposure (FCRA-as-CRA, BIPA face recognition, CFAA logged-in scraping, GDPR processing without basis) is **structurally impossible because the code refuses to do it** — not just discouraged. See `LEGAL_COMPLIANCE_REGISTRY.md` for the 12 lawsuit vectors and per-vector mitigations. `infra/sources.yaml` is the operational compliance gate. Pre-release checklist (Chapter 7 of LEGAL_COMPLIANCE_REGISTRY) runs before every deploy.

---

## THE KILLER QUERY (the vertical slice)

> **"Show me what's happening near this point in space and time."**

Click a location, drag a time range (default last 24h). System returns:
- All entities (aircraft / vessels / satellites) within bbox + timerange
- All events (GDELT, USGS, NOAA, ACLED, etc.) geocoded within bbox + timerange
- Algorithm-flagged anomalies (loitering ships, unusual flight patterns, news cluster spikes, cross-domain proximity)
- LLM-generated 200-word brief summarizing what's notable
- Every fact citation-linked back to its source

This forces every layer to actually work: ingestion, durable storage, spatial query, algorithms, LLM, viz. Every additional data source plugs into the same pattern. This is also the demo that sells the product — non-technical viewers immediately understand the value.

**Definition of done for v1.0:**
- Globe loads in <3 s, shows live aircraft + vessels in current viewport
- Click any entity → identity + recent track + related events + source citations
- Time scrubber lets user replay last 24 h in any region
- Drawing bbox + selecting time range → entities + events + algorithm findings + LLM brief
- Natural-language query box accepts a sentence, returns the same
- All shown facts have clickable source citations
- System runs on Mac Mini for 24 h without intervention
- At least one cross-domain finding surfaced in the brief that wasn't trivially obvious from any single source

---

## ARCHITECTURE (the V2 stack)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  BROWSER (mewrcreate.com/glassbox)                                       │
│                                                                          │
│  CesiumJS (3D globe, terrain, satellite, camera)                         │
│  + deck.gl (dense overlays — planes, ships, news pins, heatmaps)         │
│  + WebSocket subscriber (live entity updates per viewport)               │
│  + REST queries (viewport snapshot, NL queries, source citations)        │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ WSS + HTTPS
┌────────────────────────────────────┴─────────────────────────────────────┐
│  CLOUDFLARE WORKER (mewr-news-api.mewrcreate.workers.dev)                │
│                                                                          │
│  - Public read API (rate-limited, served from KV cache)                  │
│  - Auth gate for Pro endpoints (when enabled in v1.2)                    │
│  - Stripe webhook receiver (when enabled in v1.2)                        │
│  - Edge layer for browser; never holds state                             │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ HTTPS (publisher push)
┌────────────────────────────────────┴─────────────────────────────────────┐
│  MAC MINI (always-on intelligence backbone)                              │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │ INGESTERS (one per data source)                                  │    │
│  │   planes / ships / satellites / earthquakes / gdelt / gdelt_topical│   │
│  │   citizen_osint / acled / fred / noaa / + ~10 new ones to build  │    │
│  └────────────────────────────┬─────────────────────────────────────┘    │
│                               │ GlassboxEvent (canonical shape)           │
│                ┌──────────────┼──────────────┐                            │
│                ▼              ▼              ▼                            │
│         ┌───────────┐  ┌────────────┐  ┌──────────────┐                   │
│         │ Hot cache │  │ Postgres   │  │ Publisher    │                   │
│         │ (deque,   │  │ + PostGIS  │  │ (POSTs to    │                   │
│         │  in-mem)  │  │ + TimescaleDB │  Worker KV   │                   │
│         │           │  │ + pgvector │  │  every 90s)  │                   │
│         └───────────┘  └─────┬──────┘  └──────────────┘                   │
│                              │                                            │
│                              │ spatial joins, temporal queries,           │
│                              │ vector similarity                          │
│                              ▼                                            │
│         ┌────────────────────────────────────────────────┐                │
│         │ ALGORITHMS LAYER (pure code, NOT LLM)          │                │
│         │   loitering / rendezvous / AIS gap /           │                │
│         │   flight anomaly / DBSCAN clustering /          │                │
│         │   cross-domain proximity                       │                │
│         └────────────────────┬───────────────────────────┘                │
│                              │ findings → event table                     │
│                              ▼                                            │
│         ┌────────────────────────────────────────────────┐                │
│         │ LLM (Ollama qwen2.5:14b)                       │                │
│         │   brief generation / NL→query plan /           │                │
│         │   article extraction / entity disambig         │                │
│         │   (NEVER does anomaly detection or stats)      │                │
│         └────────────────────┬───────────────────────────┘                │
│                              │                                            │
│                              ▼                                            │
│         ┌────────────────────────────────────────────────┐                │
│         │ EVENT FIRE HOSE (WebSocket /events/subscribe)  │                │
│         │   Single API contract for all consumers        │                │
│         │   Filter by category + confidence + bbox       │                │
│         └────────────────────┬───────────────────────────┘                │
└──────────────────────────────┼────────────────────────────────────────────┘
                               │ WSS
            ┌──────────────────┼──────────────────┬─────────────────────┐
            ▼                  ▼                  ▼                     ▼
    ┌──────────────┐   ┌─────────────┐   ┌──────────────┐   ┌────────────────┐
    │ Glassbox     │   │ Prediqt     │   │ Sports       │   │ Future:        │
    │ public globe │   │ (separate   │   │ Edge         │   │ Enterprise     │
    │ + UI         │   │  repo)      │   │ (separate)   │   │ buyers         │
    │              │   │             │   │              │   │ (their own     │
    │ The reference│   │ Consumes    │   │ Consumes     │   │  consumers)    │
    │ consumer     │   │ events for  │   │ events for   │   │                │
    │              │   │ Kalshi/Poly │   │ sports edge  │   │                │
    └──────────────┘   └─────────────┘   └──────────────┘   └────────────────┘
```

Key insight: **Glassbox the platform is the same product whether the customer is a journalist, a hedge fund, an OSINT analyst, or our own Prediqt program.** The customer-specific logic lives in the consumer, never in Glassbox. This is what makes it sellable.

---

## EXECUTION PHASES (revised, 14-18 weeks)

### PHASE 0 — Foundation (1.5-2 weeks, must come first)

**Goal:** Make every subsequent phase possible. No product features yet.

| Task | Owner | Deliverable | Done When |
|---|---|---|---|
| 0.1 Activate Phase 1 credentials | Ethan | `.env.glassbox` per `GLASSBOX_API_CREDENTIALS_CHECKLIST.md` | NASA + WAQI + OpenSky + Cesium Ion all set, restricted, in env vars |
| 0.2 Move Cesium Ion token out of source | Claude | Build-time substitution in deploy script | `glassbox.html` has no hardcoded token |
| 0.3 Mac Mini launchd for `glassbox_server.py` | Claude+Ethan | Plist installed (already written 2026-05-03) | `pkill` + 30s → server back up |
| 0.4 Mac Mini supervisor for `glassbox_publisher.py` | Claude+Ethan | Either embedded in server (current) or separate plist | Verified via `CHECK_GLASSBOX_PRODUCTION.sh` |
| 0.5 Snapshot baseline | Done 2026-05-03 | `glassbox_v146_pre_v2_migration.html` | ✓ |
| 0.6 Performance baseline measurement | Claude+Ethan | Lighthouse + custom test → captures current cold boot, FPS, layer-toggle latency | Numbers logged to `PERF_BASELINE.md` for comparison |
| **0.7 PostgreSQL + PostGIS + TimescaleDB stack** | **Claude+Ethan** | **Postgres 16 running on Mac Mini, all extensions installed, schema initialized via `infra/postgres/init.sql`. Backup script + launchd autostart. Verified by smoke test.** | **`psql -c "SELECT PostGIS_Version();"` returns; `SELECT * FROM entity LIMIT 1` returns empty without error.** |
| 0.8 Healthcheck integration | Claude | Extend `CHECK_GLASSBOX_PRODUCTION.sh` to probe Postgres + extensions | Section 10 of healthcheck shows DB green |

### PHASE 0.9 — Legal posture (3 days, runs parallel to Phase 0)

**Goal:** Lock in the legal/compliance posture before any new public surface ships. Per Rules 12 + 13.

| Task | Owner | Deliverable | Done When |
|---|---|---|---|
| 0.9.1 Write LEGAL_COMPLIANCE_REGISTRY.md | Done 2026-05-04 | The 12-vector lawsuit playbook + NEVER-USE list + ToS clauses | ✓ Shipped |
| 0.9.2 Write infra/sources.yaml | Done 2026-05-04 | Per-source license inventory; backend startup-gate | ✓ Shipped |
| 0.9.3 Add FCRA disclaimer to UC pages | Done 2026-05-04 | MEWR + Fulcrum index.html footers | ✓ Shipped |
| 0.9.4 Backend: Ingester base validates against sources.yaml at startup | Claude | New `ingesters/_compliance.py` module that loads sources.yaml + refuses to start any ingester missing entry, disabled, non-commercial, or build_status mismatch | Test fails when an ingester missing entry tries to register |
| 0.9.5 Refactor planes.py to use adsb.lol primary | Claude (Phase 1) | adsb.lol primary + airplanes.live fallback; OpenSky removed from default code path; OPENSKY_* env vars removed from START_GLASSBOX_WITH_PUBLISHER.sh | grep `21_GLASSBOX_AI/` for `opensky` returns ZERO matches outside disabled-source comments |
| 0.9.6 Refactor ships.py: verify Datalastic terms; switch to AISStream.io if non-commercial | Claude+Ethan (Phase 1) | Either: keep Datalastic + verify commercial license, OR switch primary to AISStream.io | sources.yaml + planes.py + ships.py reflect actual decision |
| 0.9.7 Draft v1.0 ToS + Privacy Policy (legal review pending) | Claude drafts; **Ethan must engage attorney before going public** | `legal/terms.md` + `legal/privacy.md` with the FCRA + AUP + DSAR + DMCA clauses from LEGAL_COMPLIANCE_REGISTRY Chapter 4 | Drafts shipped; explicit "ATTORNEY REVIEW REQUIRED" header preserved until counsel signs off |
| 0.9.8 Register DMCA designated agent | Ethan ($6 every 3 years) | US Copyright Office filing | Receipt logged in LEGAL_COMPLIANCE_REGISTRY Chapter 5 |
| 0.9.9 Geo-block sanctioned countries via Cloudflare WAF | Ethan | Cloudflare → Security → WAF → Custom rule blocking Cuba/Iran/N.Korea/Syria/Russia-occupied Ukraine | Test request from VPN exits in those geos returns 403 |
| 0.9.10 Pre-release legal checklist (Chapter 7) | Ongoing | Becomes the gate for every Phase X deploy | Checklist passes before merge to production |

### PHASE 0.5 — Decoupling (1 week, runs parallel to Phase 0)

**Goal:** Extract Prediqt/Loop/Kalshi out of `21_GLASSBOX_AI/glassbox_server.py`. Glassbox becomes a pure intelligence platform.

Per `GLASSBOX_DECOUPLING_PLAN.md` (separate doc — to be written next).

| Task | Deliverable | Done When |
|---|---|---|
| 0.5.1 Map all entanglement points | `GLASSBOX_DECOUPLING_PLAN.md` lists every line in glassbox_server.py / bridge / publisher importing Prediqt/Loop/Kalshi/PaperBroker | Doc complete with file:line references |
| 0.5.2 Define event firehose contract | `CONSUMER_API_CONTRACT.md` — WebSocket endpoint + auth + schema + versioning + stability guarantees (per PROJECT_HANDOFF section 18) | Contract published, ready for consumers to code against |
| 0.5.3 Build the firehose endpoint | New `/events/subscribe` WebSocket route on glassbox_server.py | Test consumer can subscribe, receive events with category + confidence filters |
| 0.5.4 Move Loop/Prediqt code to separate repo dir | Cut from `21_GLASSBOX_AI/`, paste to `30_PREDIQT/consumers/glassbox_loop_consumer.py`, rewire to consume from firehose | Prediqt's paper-broker still gets Glassbox-derived alerts via the new API |
| 0.5.5 Strip imports from glassbox_server.py | Remove all `from agents.loop_subscriber import LoopAlertSubscriber` etc. | grep returns no Prediqt/Kalshi/PaperBroker imports |

After Phase 0.5: `glassbox_server.py` is a pure OSINT intelligence platform. Prediqt is one consumer in its own repo.

### PHASE 1 — Vertical slice MVP (2-3 weeks)

**Goal:** The killer query works end-to-end with ONE entity type (planes) before we add others.

| Task | Deliverable | Done When |
|---|---|---|
| 1.1 Postgres-backed `entity` table writes | `planes.py` ingester writes to BOTH KV (hot cache) AND `entity` + `position_track` tables | New aircraft persists across server restart; `position_track` rows accumulate |
| 1.2 Spatial viewport query | New REST endpoint: `GET /api/v1/viewport?bbox=...&time_from=...&time_to=...&types=aircraft` returns entities + positions within bbox+timerange | Test query returns aircraft for Tokyo bbox, last 24h, in <300ms p95 |
| 1.3 Entity detail endpoint | `GET /api/v1/entity/{id}` returns identity + recent track + related events + source citations | Detail page renders for any aircraft with full provenance |
| 1.4 First algorithm: cross-domain proximity | New `algorithms/proximity.py` — for each aircraft, find vessels + events within 50km within same hour. Writes findings to `event` table as `event_type="proximity_finding"`. | Worker runs every 5min; findings appear in viewport query |
| 1.5 LLM brief generation | New `llm/brief.py` — given algorithm output + entity counts, generates 200-word brief via Ollama. Cached per (bbox, time-range) for 5min. | Brief renders in <15s; cache hit serves <50ms |
| 1.6 Frontend viewport panel | Modify `glassbox.html` — when user drags bbox + selects time range, sidebar populates with entities + events + brief from new endpoints | Works end-to-end in Chrome on cold cache |

**Phase 1 gate:** The killer query works for aircraft + cross-domain findings. Demo this. Validate with Ethan before Phase 2.

### PHASE 2 — Add the other entity types (3-4 weeks)

**Goal:** Vessels, satellites, earthquakes, news all flow through the same Postgres+algorithms+LLM pipeline established in Phase 1.

| Sprint | Task | Done When |
|---|---|---|
| 2A | `ships.py` → dual-write (KV + Postgres). Vessel-specific algorithms: loitering (movingpandas), rendezvous, AIS gap. **Verify Datalastic commercial terms; switch to AISStream.io if needed.** | Ships visible in viewport query; loitering vessels flagged |
| 2B | `satellites.py` → dual-write. SGP4 propagation cached in Redis (or in-process); positions on demand. | Satellites render; clickable for orbit + pass predictions |
| 2C | `earthquakes.py` → dual-write. EMSC + GeoNet expanded to one ingester. | All quakes worldwide in viewport query |
| 2D | `gdelt.py` + `gdelt_topical.py` → dual-write to `event` table with embeddings. DBSCAN event clustering algorithm. | News pins render; event hotspots clustered |
| 2E | `acled_conflict.py` → dual-write. Conflict-density algorithm. | ACLED events in viewport |
| 2F | NEW `noaa_nws.py` (replaces direct browser Open-Meteo fetches — non-commercial license). Dual-write. Severe weather alerts in viewport. | NOAA NWS alerts render; Open-Meteo direct fetches removed from glassbox.html |
| 2G | NEW `bluesky_jetstream.py` (best free real-time social firehose). Dual-write. | Bluesky events in viewport |
| 2H | NEW `companies_house_uk.py` (free real-time corporate registry streaming). Dual-write. | UK PSC events in viewport |
| 2I | NEW `sec_edgar.py` (free 10 req/sec, declared User-Agent). Dual-write. | SEC filings in viewport |
| 2J | NEW `courtlistener.py` (free 5,000 req/hr). Dual-write. PII-heavy — coordinate with LEGAL_COMPLIANCE_REGISTRY Chapter 1 Vector #1 (FCRA exposure). | Court records in viewport with prominent FCRA disclaimer surface |
| 2K | NEW `ofac_sdn.py` advanced XML (joins SDN.CSV + ADD.CSV + ALT.CSV + SDN_COMMENTS.CSV via ENT_NUM). Dual-write. | US sanctions in viewport |
| 2L | NEW `eu_uk_sanctions.py` (EU Consolidated FSF + UK Sanctions List FCDO — free + redistributable). Dual-write. | EU/UK sanctions in viewport |
| 2M | NEW `ripestat.py` (free Maltego back end — network/BGP/abuse intel). Dual-write or on-demand. | Network intel surfaces in entity detail |
| 2N | NEW `crt_sh.py` (CT log subdomain enumeration via direct PostgreSQL :5432). On-demand. | Subdomain enumeration available |
| 2O | NEW `wayback_cdx.py` (web archive lookup). On-demand. | Historical web snapshots accessible |
| 2P | NEW `nasa_firms.py` (wildfire pixel data). Dual-write. | Active fires in viewport |
| 2Q | Citizen OSINT → dual-write. (Bluesky already covered in 2G; Reddit/YouTube via citizen_osint.py refactor.) | Citizen events in viewport |

**Each migrated ingester gets:**
- Dual-write (KV hot cache + Postgres durable)
- Test suite (per `tests/test_<ingester>.py` — `gdelt_topical` is the reference)
- Diagnostic surface (per-source stats in `/api/glassbox/diagnostic`)
- Documentation in `INGESTER_HEALTH.md`

### PHASE 3 — deck.gl + frontend perf (2-3 weeks)

**Goal:** Hit the perf budgets. Adding deck.gl as a second renderer is the central change.

| Task | Deliverable | Done When |
|---|---|---|
| 3.1 Integrate `@deck.gl/cesium` | deck.gl mounts as overlay on top of Cesium scene | Single test layer (planes IconLayer) renders in deck.gl |
| 3.2 Migrate dense layers to deck.gl | Planes (>5K), ships, news pins, GDELT events use deck.gl IconLayer / ScatterplotLayer / HexagonLayer based on zoom | Globe sustained 60fps with 20K+ entities |
| 3.3 Server-side viewport filtering | All viewport queries return ONLY entities within bbox; browser never receives the full firehose | Network tab shows <500KB per viewport query, regardless of total entity count |
| 3.4 LOD by zoom | Auto-switch raw → H3 hex aggregation at zoom <8 | Globe view shows count bubbles, not 50k individual dots |
| 3.5 Throttled live updates | WebSocket batches updates at 1Hz max | No frame drops under high update volume |
| 3.6 Web Worker parsing | GeoJSON / event JSON parsing off main thread | Main thread time per frame drops measurably |
| 3.7 Time scrubber | Top bar slider drives Cesium clock; viewport query refires on time change | User can replay last 24h of any region smoothly |

**Phase 3 gates (CI-enforced perf budgets):**

| Metric | Target | Hard ceiling |
|---|---|---|
| Cold-cache boot to interactive globe | <5 s | 10 s |
| Warm-cache boot | <2 s | 5 s |
| Time to first live event | <5 s | 30 s |
| Sustained FPS at globe view (5k+ entities) | ≥50 | ≥30 |
| Layer toggle latency | <200 ms | 500 ms |
| Viewport query p95 (Postgres) | <300 ms | 1 s |
| WebSocket update latency (source → client) | <2 s | 5 s |
| LLM brief generation (cached hit) | <50 ms | 200 ms |
| LLM brief generation (cold) | <15 s | 45 s |
| Memory at 1 hour idle | <1 GB | 2 GB |

CI script `PERFORMANCE_TESTS.sh` runs each weekly + before any release. Failing target = block release.

### PHASE 4 — Full algorithms layer + Entity Resolution (2.5 weeks)

**Goal:** Build out the remaining algorithms + adopt battle-tested ER stack. Each is a Python module with tests. NONE use LLM for the deterministic math.

**Library adoptions confirmed 2026-05-04** (per external Claude review of OSINT API landscape):
- **Splink** (MIT, MOJ Analytical Services, DuckDB backend) for entity resolution. 1M records linked in ~1 minute on a laptop. Implements Fellegi-Sunter probabilistic matching. Replaces hand-rolled ER. Two-stage pipeline: Splink for cheap probabilistic match → local Ollama qwen2.5:14b for ambiguous-pair adjudication.
- **FollowTheMoney (FtM) ontology** (OpenSanctions' open standard) as canonical entity schema. Person, Company, Address, LegalEntity with strict property names. Replaces rolling our own. The closest open-source equivalent to Palantir Foundry's ontology — and free.
- **`yente`** (free FastAPI reconciliation API) + **`nomenklatura`** (manual adjudication) for ER tooling.

| Algorithm | Library | Trigger | Output |
|---|---|---|---|
| **Entity resolution** | **Splink** + Ollama for ambiguous pairs | Every 30 min | Merge edges in `entity_relation` table |
| Loitering detection | `movingpandas` `TrajectoryStopDetector` | Every 5 min on past 6h vessel tracks | event_type=`detected_loiter` |
| Rendezvous detection | PostGIS `ST_DWithin` + speed match | Every 5 min | `detected_rendezvous` |
| AIS gap detection | Custom — MMSI silent >30 min near sensitive area | Every 5 min | `detected_ais_gap` |
| Flight pattern anomaly | `pyod.IsolationForest` on cohort altitude/speed/track | Every 30 min | `detected_flight_anomaly` |
| GDELT event clustering | `sklearn.DBSCAN` on (lat, lon, time) | Every 15 min after GDELT pull | `detected_event_cluster` |
| Cross-domain proximity | PostGIS spatial join + temporal | Every 15 min | `detected_proximity` |
| Embedding similarity | pgvector cosine + sentence-transformers `all-MiniLM-L6-v2` | On demand | served via `/api/v1/events/similar/{id}` |

**Each algorithm:**
- Lives in `21_GLASSBOX_AI/algorithms/<name>.py`
- Has unit tests in `tests/test_<name>.py` against fixture data
- Documented in `21_GLASSBOX_AI/algorithms/README.md`
- Output rows go to `event` table with consistent schema
- Surfaced in viewport query with no special path

### PHASE 5 — LLM-to-code migration (2 weeks, runs parallel to Phase 4)

**Goal:** Replace LLM where code wins. Per the analysis from earlier in this engagement.

| Task | Deliverable |
|---|---|
| 5.1 Audit `anomaly.py` + `correlator.py` + `confidence_scorer.py` + `forecaster.py` | Confirm what's LLM vs already-statistical |
| 5.2 Replace LLM-based anomaly with `scipy.stats` | Z-score, EWMA, isolation forest as appropriate |
| 5.3 Replace LLM-based correlation with windowed Pearson + spatial NN | scipy + sklearn |
| 5.4 Replace LLM-based confidence with Bayesian update | ~20 lines of arithmetic |
| 5.5 Replace forecaster math with KDE + Poisson process | statsmodels; LLM only for narrative wrapper |
| 5.6 Migrate 7 n8n intel workflows to Python scripts | Under `21_GLASSBOX_AI/scheduled/` + launchd plists each |
| 5.7 EventClassifier: 4 of 5 dims as rule tables | `geocode_quality`, `domain`, `decay_half_life_min`, `severity_for_market` deterministic |
| 5.8 EventClassifier: market_tags via embedding similarity | sentence-transformers + cosine, NOT LLM-per-call |
| 5.9 Cache AI SITREP in Worker KV | Generate once per cycle, serve from KV |

**Quality bar:** every replacement has a unit test asserting identical-or-better output on a held-out sample. Code that performs WORSE than the LLM it replaced is rejected.

### PHASE 6 — Production infrastructure (1.5 weeks)

| Task | Deliverable |
|---|---|
| 6.1 Mac Mini supervisor + alerting | launchd plists + Slack webhook alerts on crash/restart |
| 6.2 Per-layer SLA monitor | `sla_glassbox.py` based on existing pattern; emits ALERT events when layer hasn't emitted in 3× cadence |
| 6.3 Public status page | `/glassbox/status` endpoint linking ingester health |
| 6.4 Structured logging | `structlog` with JSON output + correlation_id threaded through requests |
| 6.5 Prometheus metrics | Endpoint on every service for ingestion rates, query latency, algorithm duration, LLM TPS |
| 6.6 Backup script | Postgres `pg_dump` + Cesium Ion token rotation reminder + .env backup |
| 6.7 Disaster recovery runbook | `OPERATIONS.md` — Mac Mini dies / KV empty / OpenSky banned / etc. |
| 6.8 Multi-user architecture skeleton | Every API call has implicit `user_id` (default = "system" for v1.0). Auth gate is no-op pass-through but the seam exists. Adding real auth in v1.2 = one env var flip |

### PHASE 7 — Truthfulness + UX polish (1 week)

| Task | Deliverable |
|---|---|
| 7.1 Truthfulness pass | Every numeric claim on glassbox.html / glassbox-pro.html / glassbox-web.html points at a real `/api/v1/...` URL receipt |
| 7.2 Replace stale "60+ OSINT sources" claims | Use real count from `/api/glassbox/diagnostic` |
| 7.3 Pull Stripe button + replace with Pro waitlist email capture | `source: glassbox-pro-waitlist` |
| 7.4 Customer-grade UX | No debug strings in UI, no console errors, no UUIDs in popup titles, mobile-responsive verified on real iPhone + Android |
| 7.5 Source citation everywhere | Every fact in viewport result has clickable source — links back to `source` table row |
| 7.6 Privacy policy + terms of service | Lists every data source ingested. GDPR data-deletion endpoint. CA SB-1001 compliance if AI features. |

### PHASE 8 — Hardening + launch readiness (1 week)

| Task | Deliverable |
|---|---|
| 8.1 Security pass | All hardcoded tokens in env vars; CORS limited to mewrcreate.com; rate limits on every public endpoint |
| 8.2 Cost monitoring | Cloudflare bill, Cesium Ion usage, OpenSky quota — all monitored monthly |
| 8.3 24-hour soak test | System runs 24h without intervention; no memory growth, no silent failures |
| 8.4 Dress rehearsal | Cold-boot in 3 browsers, 5-min observation, click-through smoke, AI brief sanity, mobile, copy truthfulness |
| 8.5 Launch decision | Go/no-go meeting. All gates green OR explicit accept of degraded item → ship |

### PHASE 9 — POST-LAUNCH (ongoing)

- On-call playbook + monitoring rotations
- Weekly perf benchmark cron (results to Slack)
- Quarterly credential rotation
- Customer feedback loop (`/api/issues/report` → inbox digest)
- Layer expansion (every new credential added in CHECKLIST → new ingester sprint)
- v1.1 Desktop App (Tauri) build-out
- v1.2 Pro tier (real auth + Stripe + history queries)

---

## TIMELINE (revised — 2026-05-09 research integration adds Phase 9 + Phase 10)

| Phase | Weeks | Cumulative |
|---|---|---|
| 0 — Foundation (incl. Postgres) | 1.5-2 | wk 2 |
| 0.5 — Decoupling (parallel) | 1 | wk 2 |
| 0.9 — Legal posture (parallel) | 0.5 | wk 2 |
| 1 — Vertical slice MVP | 2-3 | wk 5 |
| 2 — Other entity types (now 25+ ingesters) | 4-5 | wk 10 |
| 3 — deck.gl + perf + .html migration to firehose | 2-3 | wk 13 |
| 4 — Algorithms + ER + GDELT-bulk re-enable + MobilityDB + outlines | 3.5 | wk 16.5 |
| 4.5 — MCP servers (HANDOFF_04) | 0.5-1 | wk 17 |
| 5 — LLM-to-code (parallel with 4) | 2 | wk 16.5 |
| 6 — Production infra | 1.5 | wk 18 |
| 7 — Truthfulness + UX | 1 | wk 19 |
| 8 — Hardening + launch | 1 | wk 20 |
| **Glassbox v1.0 LAUNCH** | — | **week 19-20 (≈ 5 months)** |
| 9 — Satellite imagery + EO foundation models | 4-6 | v1.0 + 1-1.5mo |
| 10 — Agent layer (LangGraph + deepagents + MetaMCP) | 3-4 | v1.0 + 2-2.5mo |

**Timeline grew 1-2 weeks** vs prior estimate because Phase 2 added 10 new ingesters (RIPEstat, crt.sh, CourtListener, Companies House Streaming, OFAC SDN advanced, EU/UK sanctions, NewsData, NOAA NWS, Bluesky Jetstream, Wayback CDX, NASA FIRMS) and Phase 4 added Splink + FtM adoption. Net: more product capability + better legal posture for the same shipping discipline.

If Ethan can give 20 hours/week of focused execution + I produce all the code + docs, this is realistic for late August / early September 2026 launch. If we're slower or hit unknown unknowns, late September / early October.

This is the "do it correctly" timeline. Half-built in 4 weeks would deliver something fragile and unmonetizable. Done correctly in 17-18 weeks delivers a real product with a real moat.

---

## WHAT v1.1, v1.2, v2.0 LOOK LIKE (unchanged from prior plan)

| Version | Scope | T+ from v1.0 launch |
|---|---|---|
| v1.0 | Free tier, production-grade. THIS plan. | T+0 |
| v1.1 | Glassbox Desktop (Tauri) shipped. Same data pipeline, native shell. One-time purchase $99-149. | T+2 mo |
| v1.2 | Pro tier (web). Real history archive (Postgres-backed already from v1.0!), saved workspaces, geofence alerts that fire reliably, Stripe + license persistence. | T+3-4 mo |
| v2.0 | Enterprise tier. SLA, audit logs, SSO, white-label, custom data sources, dedicated Slack. Sales-led. | T+6-9 mo |

**Note on v1.2:** because the v1.0 plan now includes the real database, history archive, and entity ontology, v1.2 (Pro launch) is significantly easier than it would have been on the original 10-12 week plan. The hard infrastructure work is front-loaded into v1.0.

---

## WHAT NOT TO DO DURING THIS SPRINT (locked rules)

- Do not refactor `glassbox.html` line-by-line. The file is 2 MB; that's a separate quarter of work. Frontend changes are surgical: add Postgres-backed viewport endpoint consumption, integrate deck.gl, retire `loadXXX` functions one at a time as their server-side ingester goes live.
- Do not touch `glassbox-markets.html` (Prediqt fork). Per perf-plan rule #2.
- Do not bump Cesium 1.120 unless a known regression demands it.
- Do not introduce new ingesters mid-sprint OUTSIDE the planned list. Defer "wouldn't it be cool to add X" to post-launch.
- Do not redesign the brand. Glassbox visual identity stays.
- Do not add GraphQL, AGE, MinIO, Docker, MLX-LM swap (per items rejected from PROJECT_HANDOFF). **Updated 2026-05-09:** MLX-LM is *measure, then decide* — see `00_MASTER_DOCS/RESEARCH_INTEGRATION_PROPOSAL.md` Section 4 for the benchmark spike. NATS+FastStream+Sequin still rejected for v1.0; revisit at v1.2 multi-consumer pressure. MinIO revisits at Phase 9 (vs SeaweedFS Apache-2.0).
- Do not ship anything without tests. The `gdelt_topical.py` + its test suite is the bar.
- Do not couple Glassbox to Prediqt/Kalshi ever again. The decoupling in Phase 0.5 is permanent.

---

## PHASE 4 ADDITIONS (2026-05-09 research integration)

Added to the existing Phase 4 algorithms-and-ER scope:

### 4.A — GDELT bulk CSV re-enablement (NEW; per Ethan 2026-05-09 "add GDELT in")
**Why:** GDELT is public domain (license-clean) but `/api/v2/doc/doc` rate-limit-banned us in May. Research M6 specifies polling `data.gdeltproject.org/gdeltv2/lastupdate.txt` every 5 min and downloading Events + GKG zip files — different operational characteristics than the API path.

**Steps:**
1. Build `21_GLASSBOX_AI/ingesters/gdelt_bulk.py` per research M6 spec.
2. Build `21_GLASSBOX_AI/glassbox_taxonomy/cameo_lookup.json` + Pydantic loader per HANDOFF_02. ~1 day.
3. Build `21_GLASSBOX_AI/ingesters/gdelt_bulk/prefilter/` per HANDOFF_03 (rules engine, priority scoring, A/B testing harness, Redis-backed sliding-window dedup, queue tail-drop on overflow). ~2-3 days.
4. Wire `gdelt_bulk` into `glassbox_server.py` startup ingester list, behind a per-ingester sources.yaml `enabled: true` flag.
5. Mark `gdelt` + `gdelt_topical` in sources.yaml with `disabled_reason: 'replaced by gdelt_bulk; see HANDOFF_02 + HANDOFF_03 + RESEARCH_INTEGRATION_PROPOSAL'`. Keep ingester files as deprecated reference; do not delete.
6. Tests: GDELT parser, pre-filter rule unit tests, end-to-end on `gdelt_sample_24h.jsonl` fixture, perf > 1000 events/sec on a single worker.
7. Verify 1-2K events/day pass through to LLM extraction queue (the research's expected pass rate of 0.5-1.5%).

### 4.B — `outlines` for structured LLM output (NEW)
**Why:** Tighter JSON validity than retry+repair. Works with Ollama via OpenAI-compatible endpoint. No mlx-lm swap required.

**Steps:**
1. Add `outlines>=0.0.40` to `requirements.txt`.
2. Wrap `brief.py`, Splink LLM-disambiguation, and any future structured-output prompts with `outlines.from_openai(...)` + Pydantic schemas.
3. Existing tolerant JSON parsing stays as fallback.

### 4.C — MobilityDB extension + `vessel_trajectory` table (NEW)
**Why:** `tgeogpoint` (temporal geography point) lets us write trajectory queries like "vessels that traveled >20kn for >3h within polygon Z" in a single SQL statement. Empire's port_call/loitering/rendezvous algos all benefit.

**Steps:**
1. Install MobilityDB on Mac Mini Postgres 17 (PostgreSQL license — no AGPL/GPL issue).
2. Migration `003_mobilitydb_vessel_trajectory.sql`: install extension + create `vessel_trajectory(entity_id, time_range, trajectory tgeogpoint, ...)` hypertable.
3. Nightly trajectory builder in `algo_worker`: take last hour of `position_track` per active MMSI, build tgeogpoint, upsert.
4. Refactor port_call / loitering / rendezvous to use tgeogpoint predicates where it makes the query cleaner. Benchmark before/after.

### 4.D — `mlx-lm` benchmark spike (NEW, 1-day)
**Why:** V2 plan rejected mlx-lm "without measured benefit." Research argues mlx-lm wins on Apple Silicon. **Decide based on measurement.**

**Steps:**
1. Install `mlx-lm` + Qwen3-14B-Instruct-4bit alongside Ollama.
2. Run side-by-side on the actual `brief.py` prompt + Splink LLM-disambiguation prompt.
3. Measure: tokens/sec, p95 latency, JSON validity rate, total cost per 1K calls.
4. If mlx-lm ≥ 2× faster at equivalent quality: swap. Update `21_GLASSBOX_AI/docs/llm-benchmarks.md` either way.

---

## PHASE 4.5 — MCP SERVERS (NEW, ~1 week)

**Why:** Per HANDOFF_04. Three MCP servers expose the empire's `/api/v1/*` REST surface to agents (Claude Desktop, Claude Code, future LangGraph layer). Adapt the handoff for REST not GraphQL.

**Steps:**
1. New folder `21_GLASSBOX_AI/mcp_servers/{shared,entities,events,investigation}/`.
2. Common: auth (Bearer token from `.env`), HTTP client (httpx + tenacity), audit log (new `mcp_audit_log` table), token-bucket rate limit (Redis), Pydantic schemas mirroring `/api/v1/*` response shapes.
3. **glassbox-entities-mcp (port 7301):** `viewport`, `entity.detail`, `entity.recent_track`, `entity.related`, `entity.search`.
4. **glassbox-events-mcp (port 7302):** `events.search` (semantic), `events.in_bbox`, `events.algorithm_findings`, `events.detail`, `events.similar`.
5. **glassbox-investigation-mcp (port 7303):** `cross_domain`, `nl_query`, `brief`, `match_sanctions` (yente NOT used; bespoke OFAC/EU/UK matchers wrap), `entity_resolution` (Splink).
6. Migration: `004_mcp_audit_log.sql`.
7. Each server runs as a daemonized `launchd` plist or under `supervisor.sh`.
8. Tests per HANDOFF_04 + a real-agent end-to-end test.

---

## PHASE 9 — SATELLITE IMAGERY + EO FOUNDATION MODELS (NEW, post-v1.0, 4-6 weeks)

**Why:** Single biggest moat opportunity vs Palantir/MarineTraffic/Flightradar24. We have zero imagery layer today.

**Hardware note:** TerraMind 1.0 base needs ~16 GB VRAM-equivalent. Mac Mini M4 Pro 24 GB unified can run TerraMind Tiny/Small. Mac Studio 64 GB (when added) fits TerraMind 1.0.

**Steps (research M9):**
1. **eoAPI (Apache-2.0)** — STAC catalog + titiler. Add as separate Docker compose at `infra/docker-compose.imagery.yml` (optional include, AGPL boundary clean — eoAPI is MIT). The first MinIO-or-equivalent decision lives here: evaluate **MinIO (AGPL)** vs **SeaweedFS (Apache-2.0)** for tile blob storage.
2. **Imagery sources (all permissive):**
   - Microsoft Planetary Computer (free + SAS token rotation)
   - Sentinel-2 L2A via Copernicus Data Space Ecoverse (free)
   - Maxar Open Data event-triggered archive (free, attribution)
3. **Foundation models:**
   - TerraMind Tiny/Small via TerraTorch (Apache-2.0)
   - AnySat (MIT)
   - samgeo / SAM 2.1 (MIT/Apache-2.0)
   - RemoteCLIP (Apache-2.0) for text↔imagery
   - GeoCLIP (MIT) for image→GPS
4. **Detector:** RT-DETRv2 (Apache-2.0) trained on **HRSID** (Apache-2.0) for ships. **DO NOT** use xView/xView2/FAIR1M/DOTA — CC-BY-NC-SA, customer-facing trap.
5. **Trackers:** HANDOFF_01 clean-room ByteTrack + OC-SORT (MIT). 3-5 days.
6. **CesiumJS imagery layer:** click STAC item → render as Cesium ImageryLayer using titiler tiles.
7. **Tests:** Planetary Computer search returns >0 items, TerraMind classifies a fixture chip, samgeo "ships" prompt segments correctly, RemoteCLIP retrieval.
8. Update `infra/sources.yaml` for every imagery source (license + attribution).

---

## PHASE 10 — AGENT LAYER (NEW, post-v1.0, 3-4 weeks)

**Why:** Per research M10. Autonomous sentinel agents + investigator-on-demand built on the MCP servers from Phase 4.5.

**Steps (research M10):**
1. **LangGraph + deepagents** (both MIT) on top of existing LangGraph runtime (none exists today — fresh add).
2. **MetaMCP gateway** — verify license at install (research notes "verify"). All MCP servers connect through MetaMCP; agents see one unified tool catalog.
3. **Mount additional MCP servers (verify each license at install):**
   - planetary-computer-mcp (Apache-2.0 typical)
   - gis-mcp (Shapely/pyproj as tools)
   - Orbit-MCP (TLE generation, satellite orbital mechanics)
   - microsoft/playwright-mcp (Apache-2.0)
   - Glassbox-entities/events/investigation MCPs from Phase 4.5
4. **Agent personas in `21_GLASSBOX_AI/agents/`:**
   - **WatcherAgent:** patrols a configured AOI, raises alerts on cross-domain proximity / loitering / rendezvous / AIS gaps.
   - **CorrelationAgent:** when an alert fires, cross-references against news, social, sanctions, imagery (when Phase 9 ships).
   - **InvestigatorAgent:** human-in-the-loop, handles NL queries with extended tool budget (10 calls vs 5 for the brief LLM).
5. Each agent run: logs to Langfuse (MIT, self-hosted), max-cost ceiling per run, persists state in `langgraph-postgres-checkpointer`, outputs structured `AgentRunReport` to disk.
6. UI: panel showing active agents + alert inbox + click-an-alert → InvestigatorAgent session.

---

## CHANGELOG OF THIS PLAN

| Date | Change |
|---|---|
| 2026-05-09 | **Research integration shipped.** 8 research/handoff docs reviewed (`00_MASTER_DOCS/research_2026_05_09/`). Synthesis at `00_MASTER_DOCS/RESEARCH_INTEGRATION_PROPOSAL.md`. License register at `00_MASTER_DOCS/legal/LICENSE_RISK_REGISTER.md`. Plan additions: Phase 4.A (GDELT bulk re-enable), 4.B (outlines), 4.C (MobilityDB), 4.D (mlx-lm benchmark), 4.5 (MCP servers per HANDOFF_04), 9 (satellite imagery + EO foundation models per research M9), 10 (agent layer per research M10). ACLED ingester archived (no commercial license). Timeline: v1.0 launch slips ~1 week to wk 19-20 absorbing Phase 4 additions; Phase 9 + 10 are post-v1.0. |
| 2026-05-03 | Initial draft, post-Pass-1 audit, after Ethan locks "do it correctly" mandate. |
| 2026-05-04 | v2 of the plan. Adds 8 items from PROJECT_HANDOFF.md per Ethan's approval: Postgres+PostGIS+TimescaleDB, decoupling from Prediqt, deck.gl, statistical algorithms, entity ontology, vertical slice methodology, perf budgets, multi-user architecture. Timeline 10-12wk → 14-18wk. Rejects GraphQL/AGE/MinIO/Docker/MLX-LM. |
| 2026-05-04 evening | **Phase 0.7 + Phase 2 sprint shipped.** Backend SourcesRegistry license gate built + structurally enforced (sources_registry.py + base.py.source_id + glassbox_server.py wiring + GET /api/sources). 11 NEW backend ingesters: noaa_nws, nasa_eonet, emsc_fdsn, ofac_sdn, nasa_firms, waqi_aqi, nasa_neo, nasa_donki, ourairports, noaa_aviation_weather, sec_edgar, bluesky_jetstream. planes.py rewritten (adsb.lol primary, OpenSky removed). glassbox.html v147 (Cesium MEWR token, NASA real key 3 sites, WAQI real key 4 sites, frontend Open-Meteo license gate). Ops tools: smoke_test_ingesters.py + run_migrations.py + .env.glassbox.template. Net: 18 ingesters now PASS the gate (vs 6 at start), 3 compound ingesters cleanly REFUSE pending per-source audit. |

---

## RELATED DOCS

- `STATE_OF_GLASSBOX.md` — what exists today (catalog)
- `GLASSBOX_GAP_ANALYSIS.md` — defects found in audit Pass 1
- `GLASSBOX_API_CREDENTIALS_CHECKLIST.md` — every credential needed
- `GLASSBOX_DECOUPLING_PLAN.md` — Phase 0.5 detailed plan (next deliverable)
- `CONSUMER_API_CONTRACT.md` — platform/consumer separation contract (next deliverable)
- `infra/postgres/init.sql` — DB schema (next deliverable)
- `09_SETUP_GUIDES/POSTGRES_SETUP.md` — operator install guide (next deliverable)
- `21_GLASSBOX_AI/INGESTER_HEALTH.md` — per-ingester operations reference
- `_archive/planning_legacy/GLASSBOX_V2_ARCHITECTURE_vintage.md` — original V2 vision (archived 2026-05-20 per P3-J; vintage Phase 1 doc that referenced never-built MobilityDB/Redis-SQLite stack — preserved for historical context)
- `GLASSBOX_PERF_PLAN.md` — Cesium Primitive renderer plan (Phase 3 work)
- `GLASSBOX_SHIP_PLAN.md` — DEPRECATED demo-readiness plan (do not use)
- `GLASSBOX_DEMO_SCRIPT.md` — recordings (post-launch artifact)
- `LESSONS_FROM_PRIOR_ITERATIONS.md` (uploaded) — TEMPLATE, Ethan to fill in. Recommended before Phase 0 starts.
- `PROJECT_HANDOFF.md` (uploaded) — external Claude greenfield spec. Items 1-8 from this plan adopted from sections 5, 8, 9, 18.
