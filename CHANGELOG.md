# Glassbox CHANGELOG — post-v151 cockpit + backend

The legacy `16_GLASSBOX_VERSIONS/CHANGELOG.md` tracks the monolithic `glassbox.html` snapshots through v151 (2026-05-09). After v151 the system bifurcated:

- The cockpit became `21_GLASSBOX_AI/landing/atlas.js` (a much smaller, content-hashed file served by the daemon — no more per-version `.html` snapshots). Cache-bust is now automatic via P1-D's content-hash injection (`<script src="/atlas.js?h={hash}">`), so the operator never needs to bump a manual `?v=YYYYMMDD` placeholder.
- The backend rewrote into the V2 architecture (Postgres+PostGIS+Timescale+pgvector, 30 ingesters, 13 algorithms, 113 routes, 3 MCP servers / 14 tools). State of the world: `GLASSBOX_AUDIT_2026_05_18.md`. Action list: `GLASSBOX_BACKEND_BACKLOG.md`.

This file tracks **functional milestones** in the post-v151 era, NOT individual commits. For per-commit detail, run `git log --since="<date>" --oneline` against `21_GLASSBOX_AI/` or any specific file. Versioning is date-based to disambiguate from the legacy v1XX scheme.

---

## v2026-05-27 — Layers panel: category-grouped accordion

The cockpit layers panel (`landing/atlas.js`) reorganized its 28 flat globe-layer toggles into **6 collapsible accordion categories**: Live Traffic, Sanctions & Dark Activity, Geopolitics, Strategic Infrastructure, Environment & Climate, Overlays.

- Each category header has a `▾` chevron (collapse/expand), an `N/M` count badge, and a tri-state group swatch (all-on / mixed / all-off) that toggles the whole group on/off in a single globe repaint.
- New `LAYER_GROUPS` taxonomy constant drives the render; a defensive "Other" bucket guarantees any future unlisted layer still appears + toggles.
- Collapse/expand state persists across reloads via `localStorage` (`glassbox.atlas.layerGroups.collapsed`); layer on/off stays session-only, so reloads reset to the curated defaults exactly as before.
- Frontend-only (`landing/atlas.js` + the layers-panel CSS in `landing/index.html`). No backend or route changes. Design + plan: `docs/ATLAS_LAYER_GROUPS_DESIGN_2026_05_27.md`, `docs/ATLAS_LAYER_GROUPS_PLAN_2026_05_27.md`.

## v2026-05-20 — P1 + P3 cleanup batch

3 P1 items closed + 7 P3 cleanups + full pytest re-validation, all in parallel with a 12M-row P0-C proximity cleanup running on Postgres.

### P1 work
- **P1-D — Cloudflare cache override on `/atlas.js`** — verified the hash-injection fix (shipped 2026-05-13 in `6183a24`) is working end-to-end against `mewrcreate.com`. Added 3 regression tests at `21_GLASSBOX_AI/tests/test_root_landing_page.py` pinning the contract. ACTIVE BLOCKER #1 cleared.
- **P1-E — Cesium clock auto-tick** — documentation path. Cesium 1.120 source confirms `CesiumWidget.render()` always ticks the clock; 2026-05-14 symptom was likely a transient init-race. Kept the 10Hz `setInterval` workaround, added single-shot `[clock-tick] OK / REGRESSION` runtime detector at atlas.js boot. Delivered live via P1-D cache-bust mechanism (atlas.js hash `4f5fc6e37049` → `06640039b13e`).
- **P1-B — Vessel-writer deadlock** — `_sort_batch_for_upsert()` helper in `writers.py` applied to 4 entity-writers (aircraft/vessel/satellite/sanction_entities) eliminating cross-writer batch-ordering deadlocks. 10 new hermetic tests + 213 broader writer tests green. Production validation: previous daemon PID 33809 was SIGKILL'd by parallel-cleanup memory pressure; launchd auto-relaunched as PID 74203 1m38s AFTER the writers.py edit — new daemon imported the sort fix automatically. **0 deadlocks in 42m+ of new-daemon uptime** vs predicted ~0.9 at the pre-fix 1.3/hr rate (406 deadlocks across 12.7 days baseline).

### P3 cleanups
- **P3-D** Migrations README — explains the 003 / MobilityDB gap (reserved but never built; downstream algorithms shipped without it).
- **P3-E** Tauri `target/` already excluded in `.gitignore:25` — backlog tracking was stale.
- **P3-G** CLAUDE.md trimmed 60.6 KB → 19.2 KB (68% reduction); 16 detail entries from 2026-05-19/05-18 archived to `memory/SESSION_ARCHIVE.md`.
- **P3-I** `INGESTER_HEALTH.md` replaced with a thin pointer doc to `infra/sources.yaml` (config) + `/api/health` (runtime). Mirroring 30 ingesters in markdown will always rot.
- **P3-J** `GLASSBOX_V2_ARCHITECTURE.md` archived to `_archive/planning_legacy/GLASSBOX_V2_ARCHITECTURE_vintage.md` + 4 back-reference updates.
- **P3-O** AI-on-graph rule verified at all 5 LLM call sites; created `21_GLASSBOX_AI/docs/AI_DATA_GROUNDING_RULE.md` formal-rule + exemplars doc.
- **P3-P** `21_GLASSBOX_AI/docs/API_SURFACES.md` created — corrected the backlog's wrong "alias" framing; `/api/intel/*` is NOT a `/api/v1/*` alias.

### Test suite
- Full pytest re-run: **939 passed / 1 skipped / 0 failed in 36.06s** (+13 new tests from this session).

---

## v2026-05-19 — P0 sweep close-out — ~12.7M historical FPs withdrawn

The 2026-05-18 audit-pause mandate ("backend correctness gate; no new features until P0 is done") fulfilled in one massive session. All 8 P0 items closed.

### P0-C — 10-algorithm FP audit (the big rock)
- **sanctioned_port_arrival** — 115 test-leakage rows withdrawn; production FP=0%
- **sanctions_multijurisdictional** — PASS, 0% production FP (GROUP BY aggregates correctly)
- **shadow_fleet_cluster** — 81% FP fixed via DBSCAN diameter cap (Mercator-projected, ≤30 km cluster diameter); 2,225 rows withdrawn
- **dark_ship** — 99.6% FP fixed via cohort-size suppression (≥6 vessels going dark in the same one-second bucket = AIS receiver downtime, not real signal); 209,903 rows withdrawn
- **loitering** — 70% FP fixed via stale-pings-zero-bbox suppression; 28,238 rows withdrawn
- **port_call** — 7,413 historical FP-DUPE rows withdrawn (active algo already correct since `c4906ae` extended cooldown 24h → 168h)
- **proximity** — 53.3% FP fixed via deny-list expansion to 16 algorithm-derived event types; 12,072,548 row cleanup (largest single in batch — entire job took ~5h of batched UPDATE)
- **rendezvous** — 76.7% FP fixed via sustained-proximity (≥2 samples spanning ≥20 min) + no-recent-high-speed (no >50 m/s in past 30 min); 452,100 rows withdrawn
- **military_flights** — PASS, 0% FP (trusts authoritative adsb.lol dbflags + ICAO Annex 10 Vol III + curated callsign prefixes)
- **sanctioned_airspace** — 76.7% FP fixed via replacing 10 axis-aligned bboxes with concave-hull polygons hugging country borders; 6,118 rows withdrawn

**Net:** 706,012 confirmed-withdrawn + 12.07M proximity in flight = **~12.78M historical FPs withdrawn**. 6 algorithm fixes landed. 10+ regression tests added.

### Other P0 items
- **P0-B** — `infra/sources.yaml` reconciled: 30 enabled / 54 disabled (all with `disabled_reason`) / 0 duplicates. Doc: `21_GLASSBOX_AI/docs/SOURCES_RECONCILIATION_2026_05_19.md`.
- **P0-D** — Cockpit data-flow spot-check: 10 random entities all match across DB / `/api/v1/entity/{id}` / `position_track`. The `839a215` hover/click fix is intact.
- **P0-F** — Test suite isolation: separate `glassbox_test` DB; pass rate 73% → 97.8% in 69s vs baseline 6h56m21s (361× runtime improvement). P0-F.3 closeup got the remaining 20 failures green: 907/0 in 30.93s.
- **P0-G** — `CLAUDE_CODE_GLASSBOX.md` rewritten: 67 KB → 20 KB (71% smaller), kept §0/§7/§15 verbatim, dropped sections that had rotted.
- **P0-H** — `master` fast-forwarded to `glassbox-perf`; pre-V2 baseline preserved at tag `master-pre-2026-05-19-perf-merge`.

---

## v2026-05-18 — Glassbox full audit (pause + document)

Operator paused feature work because of accumulated drift across 2026-05-13/14 sessions. Two foundational artifacts produced:

- **`GLASSBOX_AUDIT_2026_05_18.md`** (47 KB / 773 lines): file inventory, backend state (30 registered ingesters, 13 algorithms, 113 routes, 3 MCP servers / 14 tools), frontend state, test state (81 files / 908 functions), algorithm correctness gap (10 algorithms NOT yet audited — addressed by P0-C above), git state, documentation drift inventory.
- **`GLASSBOX_BACKEND_BACKLOG.md`** (29 KB / 547 lines, now ~38 KB): 38 items across P0 (8, all closed by 2026-05-19) / P1 (5, 3 closed by 2026-05-20) / P2 (3) / P3 (~22 ish, mixed status).

Sign-off gate ("backend 100% perfect"): 8 P0 items in §17 of the audit. **No new feature work until all 8 are checked** — gate met 2026-05-19.

Also archived 4 superseded handoff docs to `_archive/2026_05_18_pre_audit/`. Added staleness banner to top of `CLAUDE_CODE_GLASSBOX.md` (rewritten 2026-05-19 per P0-G) preserving §0/§7/§15 as binding.

---

## v2026-05-14 — Real engineering pass

5 commits, all substantive.

- **Sanctions IMO-mismatch false positives** fixed across 3 algorithms (`f4dab9a`) — 2,245 historical FPs withdrawn. When upstream sanctions data has IMO present on the listing but live ADS-B/AIS reports a different IMO, the algorithms now correctly REJECT the match (previously the name-only path fired and produced false positives).
- **Viewport API limit raise** 5K → 15K (`abb842d`) — accommodates dense viewports without truncation.
- **Track-line / contrail rendering** per entity (`8b02585`) — fading polylines show recent movement.
- **Hover/click data mismatch fix** (`839a215`) — both `_hoverCardHTML` and `_renderEntityDetail` now receive `_glassbox_meta` from the same picked Cesium entity and prefer the same identifier (display_name → canonical_id). Pre-fix: hover card and detail panel could show different entities on dense clusters.

---

## v2026-05-13 — Major perf + UX wave (35 commits)

Highest-velocity day post-v151. Major wins:

- **Cache-bust at last** (`6183a24`) — content-hash atlas.js URL kills the "I see old behavior" tax. The fix that becomes P1-D in the backlog.
- **Motion fix** (`5c7a401`) — anchor SampledPositionProperty samples to `viewer.clock.currentTime` + force-tick loop. The reason entities visibly move.
- **Motion-column denormalization** (`4439383`) — added `current_velocity_ms` + `current_heading_deg` + `current_altitude_m` directly on `entity` (companion to the 2026-05-08 `current_geom` denormalization). Backfilled as tracked migration `006_entity_motion_denormalize.sql` per P0-E.
- **Imagery chain** (`36bece1`) — Sentinel-2 Cloudless 2024 (EOX::Maps) primary, NASA GIBS MODIS Terra fallback, OSM final fallback. Replaces the Bing-via-Ion default.
- **Viewport perf** (`c45e16f` + `dad1d65`) — skip LATERAL on hypertable + single-flight loadAll + cap time-from to 1h + fire-and-paint. Viewport p95 came down significantly.
- 3D Tiles tuning + bug fixes (multiple commits): hide above 80 km altitude, explicit Bing-via-Ion (asset 3), tile black-globe trap fixes, Venice webcam embed-blocked → Italy 600 cams aggregator.

---

## v2026-05-12 — Lane A: stability + onboarding

6 commits.

- **3D Tiles** (`187f603`/`2594b8b`) — depth bleed-through fix + DB pool + admin auth + LLM rate limit + 3D Tiles tweaks (Lane A bundled fix).
- **FD ulimit** (`256ac2e`) — raise file descriptor soft/hard limit 256 → 4096 in `com.mewr.glassbox-server.plist` (the daemon was hitting FD exhaustion at ~26 ingesters).
- **BRIN index** (`90db679`) — on `event.created_at` to recover from pool starvation under heavy proximity scans.
- **First-visit onboarding hero** (`f894632`) — 30-second tour over the cockpit for new visitors.
- **mewrcreate.com publicly live** (`049da47`) — via new Cloudflare tunnel on the right CF account (`mewrcreate@gmail.com`, not the old MEWR Slack account).

---

## v2026-05-11 — Public launch hardening (6 commits)

- **Satellites layer + 4× signal data + 2 missing layers** (`47bfe3e`) — the cockpit picked up additional layers.
- **Public launch hardening** (`7f07962`) — tunnel live, CORS lockdown, rate limit, SEO basics.
- **MEWR rebrand** (`ad95a5c`) — public URLs reverted to `mewrcreate.com` (had drifted to a placeholder during cockpit work).
- **Pricing + waitlist + analytics + operator dashboard** (`e65e099`) — `/pricing`, waitlist form, first-party analytics (`/track.js`), operator dashboard.
- 3D Tiles fixes: don't hide Bing imagery (`2f69b7c`), track.js added to all subpages (`c7f4068`).

---

## v2026-05-10 — Cockpit rewrite + multi-page launch (83 commits — biggest single day)

Major architectural shift: the legacy 2 MB `glassbox.html` cockpit was supplemented by a much smaller `atlas.js` cockpit served from `landing/index.html`, plus multiple new pages (`/monitor`, `/globe`, `/network`, `/signals`, `/status`). The old `glassbox.html` is still on disk and served at `/glassbox` but new development moved to atlas.js.

Highlights:
- **Atlas cockpit** — military-atlas treatment (graticule, range rings, hover card, tactical frame), draggable + resizable + min/max/close HUD panels, layout menu, persistence, Google 3D Tiles via Cesium Ion + vanilla drag/resize, default to 3D Terrain (drop the dark-military look per operator feedback).
- **New pages** — `/monitor` (country intel highlight overlay via Natural Earth + bbox classifier), `/globe` (3D conflict-spike extrusions), `/network` (cross-entity graph), `/signals` (algorithm-derived findings feed), `/status` (Atlassian-style health page).
- **Postmark daily-digest sender** (`52a9062`) + launchd schedule + tests. The empire's first scheduled outbound email pipeline.
- **`/api/intel` alias** (`8ea581a`) — the surface that later got documented in `API_SURFACES.md` (P3-P, 2026-05-20).
- **MapLibre 4.x bug fix** (`4a8a434`) — `filter:null` was breaking the `/monitor` map load entirely.

Lazy-load news + webcams (`a01c0db`) to neutralize 3rd-party SSO trigger that was confusing some visitors.

---

## v2026-05-09 — Phase 4 wave (end of v151 day, 22 commits)

The legacy CHANGELOG's v151 (Phase 3 SSE default-on for HTTP origins) was the morning of this day. The afternoon/evening shipped Phase 4 maritime work:

- **port_call detection** (`223fdfb`) — vessels at major ports. Phase 4 algorithm.
- **port_call v1.1** (`ea2b944`) — port_arrival + port_departure transition events.
- **AISStream.io global vessel firehose** (`6727f18`) — Phase 4 maritime expansion. The fourth AIS upstream alongside Digitraffic + BarentsWatch + DMA.
- **sanctions_match MMSI-flag safety filter** (`5be9470`) — for name-only matches, require a flag-state match. 217 historical false positives withdrawn.
- **sanctioned_port_arrival compound tier-1 alert** (`44d3120`) — Phase 4d-4.
- **Phase 6 SLA monitor** (`b5e611d`) — per-ingester staleness grading.
- **Phase 7 truthfulness pass** (`6a47ffe`) — fix 4 fabricated claims on the public site (numbers without sources).
- **eu_cfsp stale-cache fallback** (`996173a`) — for upstream 5xx outages.

---

## Versioning notes

- The legacy `16_GLASSBOX_VERSIONS/CHANGELOG.md` ends at v151 and remains the canonical reference for everything before 2026-05-09. It tracks `.html` snapshots that no longer apply.
- This file uses **date-based versions** (`v2026-MM-DD`) so there's no ambiguity with legacy v1XX, and the version directly tells you what shipped when.
- Each milestone summarizes ~5-30 commits. Per-commit detail is `git log --since=... --oneline`; per-file detail is `git log --follow -p <file>`.
- Backlog status is tracked in `GLASSBOX_BACKEND_BACKLOG.md`, not here. This file is for what shipped; that file is for what's planned.

## Adding new entries

When closing a major milestone (a P0/P1/P2 item, a phase rollup, a major bug-fix wave):
1. Add a new top-level `## v2026-MM-DD — <one-line summary>` block ABOVE the current top entry.
2. Keep it ≤30 lines per entry. Cite commits as `(`abc1234`)` so `git show` works directly.
3. If you ship multiple things on the same day, group them by sub-section (e.g., "### P1 work" / "### P3 cleanups").
4. Don't fabricate version numbers from the future. Date-stamp when it actually ships.
