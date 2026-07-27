# P2-B Unported Old-Glassbox Layers — Scoping Doc

**Generated:** 2026-05-27 NIGHT (post-P2-A Phase 1 MVP shipped, pre-P2-B work)
**Source backlog item:** `GLASSBOX_BACKEND_BACKLOG.md` § P2-B
**Mirrors:** `21_GLASSBOX_AI/docs/P2A_CYBER_LAYERS_SCOPING.md` — the doc P2-A used.
**Target:** Port the 10 layers that lived in `05_WEBSITE_AND_LANDING/glassbox_v2.html` but were never moved to the new cockpit (`landing/atlas.js`).

This doc gives the next session a runway. It surveys each of the 10
unported layers + its original data source (per recon of
`glassbox_v2.html`), codifies per-source license posture, groups the
layers into 3 implementation phases by gate complexity, and identifies
operator-side decisions needed before work begins.

## Why this matters

The 10 layers below were active in v1 glassbox. The new cockpit
(`atlas.js`) has shipped 5 infrastructure layers (military bases,
nuclear, cables, trafficking, pipelines) + 2 new cyber layers (CISA
KEV + Spamhaus DROP/EDROP) — proven the geojson-layer pattern at scale.
The 10 below slot into that same shape, but each has its own license +
sourcing posture that needs review before code lands.

**Operator ask (verbatim per backlog item P2-B):** "much better than the
original" — implying disciplined license posture + live-data freshness
where feasible, NOT just feature parity.

## Layer survey (10 layers, recon from `glassbox_v2.html`)

| Layer | Original source in v2 | Static or live? | License posture |
|---|---|---|---|
| `disputedZones` | Hand-curated polygons (~line 12017-12025) | Static | CC-BY-SA fork from Wikipedia / Natural Earth ok |
| `sanctionTargets` | Hand-curated points (~line 12057-12060) | Static | Likely overlaps existing OFAC/UK/EU data we already have |
| `propagandaCenters` | Hand-curated points (~line 12115-12118) | Static | OSINT-curated, attribution to original list |
| `terrorIncidents` | Hand-curated regions (line 10693) + GDELT fetch (line 10712) | Hybrid | GDELT is **already wired** in 21_GLASSBOX_AI; static curated regions need recreation |
| `diplomaticPosts` | Hand-curated points (~line 14879-14882) | Static | Wikidata SPARQL has this CC0; reproducible |
| `unMissions` | Hand-curated points (~line 14773-14776) | Static | UN OpenData CC-BY 4.0 |
| `whoOutbreaks` | Hand-curated countries (~line 12742-12807) | Static | WHO disease outbreak news — verify ToS |
| `noaaBuoys` | NDBC live (lines 7811-7839) — buoy positions hardcoded | Live | NDBC = US gov public domain ✓ gate-free |
| `climateForecast` | Open-Meteo Forecast API | Live | ✅ CC-BY 4.0, commercial OK with attribution (recon 2026-05-27 NIGHT LATE) |
| `reliefWebCrises` | ReliefWeb API (line 8450) | Live | **CC-BY-NC-SA per ReliefWeb ToS = NON-commercial → BLOCKED per `feedback_no_noncommercial_apis.md`** |

## License posture per source (decisions deferred to operator sign-off)

Each gate-needing source carries a non-trivial decision the next
session cannot clear on its own:

1. **ReliefWeb ToS re-verification.** Their public license is
   CC-BY-NC-SA which is non-commercial. Glassbox is a public commercial
   site → **blocked under feedback_no_noncommercial_apis.md**.
   Operator action: contact ReliefWeb for a commercial-use grant, save
   the response (yes or no) to `00_MASTER_DOCS/legal/license_evidence/`.
   If declined, drop `reliefWebCrises` from the port list permanently
   and document the rationale in `LICENSE_RISK_REGISTER.md`.

2. **WHO disease-outbreak-news ToS.** Free for academic/non-commercial.
   Need to verify their position on commercial-OSINT visualization.
   Operator action: re-read WHO open-data policy + their attribution
   requirements, save dated PDF to `license_evidence/`, set
   `commercial_use_ok` accordingly in `infra/sources.yaml`.

3. **Wikidata SPARQL for diplomaticPosts.** CC0 — gate-free. No
   operator action needed but bulk-query rate limits apply (Wikidata
   recommends max 5 req/sec, 30-sec timeout). Cache aggressively.

4. **GDELT for terrorIncidents.** We already have a GDELT ingester
   (`21_GLASSBOX_AI/ingesters/gdelt_topical.py`). Same source, new
   topical filter (`terrorism|insurgency|militant`) added to the
   query — no new gate needed.

**Recommendation:** Phase 1 MVP = the 5 fully-gate-free static layers
(disputedZones / sanctionTargets / propagandaCenters / diplomaticPosts
/ unMissions) PLUS the NOAA buoys live layer. Defer the WHO outbreaks
work + reliefWeb work until operator-side license clearance.

## Implementation phases

### Phase 1 (MVP — no operator gate): 5 static layers + NOAA buoys

**Estimated effort:** 12-16h across one focused session.

**Static layers** (same pattern as the existing 5 infrastructure
layers + the just-shipped cyber side-panel layers; choose globe-overlay
vs side-panel per layer):

| Layer | Geometry | Rendering | Effort |
|---|---|---|---|
| `disputedZones` | Polygon (LineString border outlines) | Globe overlay, orange | 2-3h |
| `diplomaticPosts` | Point (embassy locations, ~1000 features) | Globe overlay, faint dot | 2-3h |
| `unMissions` | Point (mission HQ) + region | Globe overlay, UN-blue | 2-3h |
| `propagandaCenters` | Point | Side panel (no useful single-point geo for some entries) | 2-3h |
| `sanctionTargets` | Point | **Check first if subsumed by existing sanctions data**; if novel, side panel | 2-3h |

Each static layer:
- `21_GLASSBOX_AI/data/<layer>.geojson` (committed snapshot)
- `21_GLASSBOX_AI/web/routes/infrastructure.py` → 1 new route
- `21_GLASSBOX_AI/landing/atlas.js` → render + LAYERS toggle entry
- Optional generator script if data has an upstream feed

**NOAA buoys live layer:**
- `21_GLASSBOX_AI/ingesters/noaa_ndbc.py` (new — 5-min poll, ~250 stations)
- `21_GLASSBOX_AI/writers/noaa_ndbc.py` (event_type='buoy_observation'; wave height + wind + SST in payload)
- `infra/sources.yaml` entry (ndbc, US gov PD)
- Atlas.js layer toggle — globe-overlay points with hover tooltip
- Effort: 4-6h (the only Phase 1 layer with a live ingester)

### Phase 2 (post-WHO-ToS-verification): whoOutbreaks

**Estimated effort:** 4-6h, blocked on WHO ToS gate.

WHO publishes disease outbreak news at https://www.who.int/emergencies/disease-outbreak-news.
Their public-domain stance for commercial-OSINT use needs re-verification.

Same pattern as Phase 1 — pulls the published JSON feed (if any) or
scrapes the index page, normalizes into event_type='who_outbreak'
rows. Render: globe-overlay points colored by alert severity.

### Phase 3 (post-ReliefWeb-clearance OR drop): reliefWebCrises

**Estimated effort:** 6-8h if commercial-use grant obtained;
otherwise drop and remove from this list permanently.

ReliefWeb is the UN OCHA humanitarian operations clearinghouse.
Their default license is CC-BY-NC-SA. **Currently blocked** under
the `feedback_no_noncommercial_apis.md` rule.

Operator path forward:
1. Email ReliefWeb (contact: reliefwebcontact@un.org) requesting
   commercial-use grant for Glassbox at mewrcreate.com.
2. If granted, save the written grant to `00_MASTER_DOCS/legal/
   license_evidence/reliefweb_commercial_grant_YYYY_MM_DD.pdf`.
3. Add source to `infra/sources.yaml` with `commercial_use_ok: true`.
4. Then port. Same pattern as Phase 1.
5. If denied or no response in 30 days: drop from the port list,
   document the rationale in `LICENSE_RISK_REGISTER.md` section
   "Sources we tried + declined for commercial use".

### Special-case: `terrorIncidents`

This layer in v2 was a HYBRID — hand-curated regions + GDELT
real-time fetch. **GDELT is already wired** in `ingesters/gdelt_topical.py`
with topical filtering. The right move is to NOT create a new ingester
— instead add a new topical filter to the existing one:

1. Edit `infra/sources.yaml`'s `gdelt_topical` entry (or
   `ingesters/gdelt_topical.py`) to add a `terrorism` filter family.
2. Surface filtered events as a new layer in atlas.js (filter
   `event_type='gdelt_topical' AND properties->>'topic'='terrorism'`).
3. Static curated regions (Sahel, Boko Haram, Al-Shabaab) ship as a
   small companion geojson file.

**Estimated effort:** 3-4h within Phase 1.

### Special-case: `climateForecast` — RECON COMPLETE (2026-05-27 NIGHT LATE)

**Upstream source (per v2 code at `glassbox_v2.html:18473-18475`):**
Open-Meteo Forecast API (https://api.open-meteo.com/v1/forecast).
Pulls daily temp_max + temp_min + precipitation_sum for ~15 major
world cities. v2 also has a hardcoded fallback dataset embedded
(lines 18504-18519) in case the live fetch fails.

**License posture: GATE-FREE.**
Open-Meteo is CC-BY 4.0 (commercial use permitted with attribution).
Their docs are explicit: "Open-Meteo provides free weather forecasts
for non-commercial AND commercial use; only requirement is
attribution." See https://open-meteo.com/en/license.

**Promotion: this layer moves from "Unknown" to Phase 1 (gate-free).**

Recommended implementation: same pattern as `noaa_buoys` shipped
2026-05-27 NIGHT LATE — start with a static-seed geojson of the 15
v2-curated cities + their typical climate ranges, then add a
follow-on `open_meteo_forecast.py` ingester refreshing once per 6h
(Open-Meteo's forecast cadence). The seed gives the cockpit
something to render immediately; the ingester provides live data
later without changing the route or frontend contract.

**Estimated effort for Phase 1 static slice:** 1-2 hours.
**Estimated effort for full live-ingester upgrade:** +3-4 hours.

## File-by-file change list (Phase 1 MVP — 5 static + NOAA buoys)

For Phase 1 only (the gate-free path):

| File | Action | Effort |
|---|---|---|
| `infra/sources.yaml` | Add `noaa_ndbc` entry; mark 5 static layers as `commercial_use_ok: true` with attribution metadata | 10 min |
| `21_GLASSBOX_AI/ingesters/noaa_ndbc.py` | New ~150 lines (HTTP poll + parse) | 90 min |
| `21_GLASSBOX_AI/writers/noaa_ndbc.py` | New ~110 lines (event-table cluster template) | 30 min |
| `21_GLASSBOX_AI/writers/__init__.py` | Add re-export line | 5 min |
| `21_GLASSBOX_AI/writers/_shared.py` | Add `noaa_buoys` to `_LAYER_TO_PLATFORM` | 5 min |
| `21_GLASSBOX_AI/web/routes/infrastructure.py` | Add 6 new geojson serving routes (5 static + 1 buoy snapshot) | 60 min |
| `21_GLASSBOX_AI/data/{disputed_zones,diplomatic_posts,un_missions,propaganda_centers,sanction_targets,noaa_buoys}.geojson` | 6 new seed snapshots | 4-6h (data sourcing + manual curation) |
| `21_GLASSBOX_AI/scripts/generate_unported_seed.py` | New regenerator (mirrors `generate_cyber_seed.py`) | 60 min |
| `21_GLASSBOX_AI/landing/atlas.js` | 5 new LAYERS toggles + render funcs (globe overlay for 3, side panel for 2) | 3-4h |
| `21_GLASSBOX_AI/glassbox_server.py` | Import + register the NOAA buoys ingester | 10 min |
| `21_GLASSBOX_AI/tests/test_noaa_ndbc_ingester.py` | New unit + writer-test (mirror cisa_kev tests) | 60 min |
| `21_GLASSBOX_AI/tests/test_routes_smoke.py` | Manifest +6 routes | 5 min |
| `21_GLASSBOX_AI/tests/test_writers_smoke.py` | Manifest +1 writer | 5 min |

**Total Phase 1 effort:** ~12-16 hours, including ~5-6 hours on the
manual data curation (the 5 static layers each need a quality seed
dataset, which can't be auto-generated).

## Risks + unknowns

1. **sanctionTargets may be redundant.** Glassbox already has OFAC SDN
   + UK OFSI + EU CFSP sanctions data flowing through entity tables.
   Need to verify if the v2 `sanctionTargets` layer adds NEW intel or
   just re-displays what's already in `entity` table. If redundant,
   drop from the port list and document instead.

2. **Static-data sourcing is manual-curation-heavy.** Unlike the cyber
   layers (CISA KEV + Spamhaus = live JSON / plain text feeds), the 5
   static layers need someone to curate the dataset from primary
   sources (Wikidata, UN OpenData, OSINT articles). The previous v2
   author did this; we need to either: (a) extract the v2's hardcoded
   arrays directly into geojson, OR (b) re-curate from primary sources
   (more rigorous but slower).
   **Recommend (a)** — extract the v2 arrays as the initial seed,
   then re-curate in a follow-on pass. Captures the v2 effort + gives
   the new cockpit visible content immediately.

3. **Some layers belong on a side panel, not the globe.** Per the
   P2-A scoping doc's reasoning (CVE entries don't have meaningful
   single-point geo): same may apply to `propagandaCenters` and
   `sanctionTargets`. Each layer's rendering decision should be
   evaluated separately — don't force everything onto the globe.

4. **Refresh cadence per layer.** Static layers don't need polling;
   live layers do. NOAA buoys: every 5-15 min (NDBC observation
   cadence). NOTE: NDBC has ~1000 buoys globally — pulling them all
   per cycle is wasteful; the v2 code hardcoded a subset. Decide:
   pull all + filter client-side, OR maintain a curated subset.

5. **Atlas.js layer-toggle proliferation.** The cockpit's LAYERS
   array already has 11+ entries. Adding 6 more without a category
   filter / dropdown grouping will make the toggle list unwieldy.
   Consider: adding a category hierarchy (e.g. "Politics ▾",
   "Humanitarian ▾", "Infrastructure ▾") before Phase 1 ships, OR
   accept the long list and group with section headers in the UI.

## Recommended execution order

1. **Open this doc + the backlog entry** at the start of the next
   P2-B session.
2. **Confirm operator has cleared the WHO + ReliefWeb gates** (or
   confirm Phase 1 only and defer 2+3).
3. **Start with `terrorIncidents`** because:
   (a) GDELT ingester already exists — just need a topical filter
       update + a layer-toggle in atlas.js
   (b) Lowest novel code volume
   (c) Most operationally interesting (real-time conflict events)
4. **Then `diplomaticPosts` + `unMissions`** — both Wikidata/UN-Open-
   Data sourced, both clean static layers. Same pattern as the 5
   shipped infrastructure layers.
5. **Then `disputedZones`** — polygon overlay, distinct from the
   point-based layers.
6. **Then `noaaBuoys`** — only Phase 1 layer needing a live ingester.
   Easier after the 4 above are done because the patterns are well-
   worn.
7. **Then `propagandaCenters` + `sanctionTargets`** — both need a
   research pass (sourcing for propaganda; redundancy-check for
   sanctions) before deciding to ship.
8. **DEFER Phase 2 + 3 until operator clears each source's gate** —
   don't write code against an unverified license.

**Order of file edits for Phase 1, per layer (mirrors P2-A discipline):**

1. `infra/sources.yaml` — add entries (license metadata is the gate)
2. Layer-specific:
   - Static: write `data/<layer>.geojson` (extract from v2 arrays
     initially); add `web/routes/infrastructure.py` route
   - Live (only NDBC): TDD write `tests/test_noaa_ndbc_ingester.py`
     first, then ingester + writer
3. `21_GLASSBOX_AI/landing/atlas.js` — add LAYERS entry + render code
4. Smoke-test extensions (`test_routes_smoke.py`, `test_writers_smoke.py`)
5. Full pytest → expect 1233 → 1245+
6. Daemon restart to activate any new ingesters in production
7. Document the deployment in `21_GLASSBOX_AI/CHANGELOG.md`

## Hand-off checklist for the next session

- [ ] Read this doc (`P2B_UNPORTED_LAYERS_SCOPING.md`) before starting
- [ ] Verify with the operator which of the 5 static layers are
      DEFINITELY in-scope vs which need a research pass first
      (especially `sanctionTargets` redundancy + `propagandaCenters`
      sourcing)
- [ ] Confirm Phase 1 only and defer Phase 2 (WHO) + Phase 3
      (ReliefWeb) unless operator has cleared those gates
- [ ] Verify daemon is up + pytest baseline is **1233** (post-P2-A
      Phase 1)
- [ ] Write the first regression test BEFORE the first source-file edit
      (TDD discipline per P2-A pattern)
- [ ] Pull each static layer's hardcoded array directly from
      `glassbox_v2.html` (lines ~10693, 12017-12118, 14773-14882) and
      convert to geojson — DO NOT manually re-key
