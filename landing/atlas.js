/* GLASSBOX // SPATIAL INTELLIGENCE — front-end controller.
 *
 * Cesium-based 3D globe with planes/ships in motion via
 * SampledPositionProperty interpolation, layered over a dark space
 * canvas with floating glassmorphic HUD panels (KPI bento, layers,
 * AI brief, news video, chronicle, webcams, alert ticker).
 *
 * NOT copied from any reference — written fresh against Cesium 1.120
 * patterns and the Glassbox /api/v1/* endpoints already shipped.
 */

const $ = (id) => document.getElementById(id);

/* ─── Cesium Ion token: fetched from backend so no JWT in committed JS */
async function _initCesiumToken() {
  try {
    const r = await fetch('/api/v1/cesium-token');
    if (r.ok) {
      const j = await r.json();
      if (j.token) Cesium.Ion.defaultAccessToken = j.token;
    }
  } catch (e) { /* fall through */ }
}

/* ─── Layer registry ─────────────────────────────────────────────── */
const LAYERS = [
  // Tier-1 — critical findings, default ON
  { id: 'sanctioned_dark',         name: 'Sanctioned · gone dark',     cls: 'crit',   on: true },
  { id: 'sanctioned_rendezvous',   name: 'Sanctioned · rendezvous',    cls: 'crit',   on: true },
  { id: 'shadow_fleet',            name: 'Shadow-fleet clusters',      cls: 'crit',   on: true },
  // Tier-2 — high severity, default ON
  { id: 'sanctioned_underway',     name: 'Sanctioned · live AIS',      cls: 'high',   on: true },
  { id: 'sanctioned_port',         name: 'Sanctioned · port arrival',  cls: 'high',   on: true },
  { id: 'sanctioned_airspace',     name: 'Restricted airspace',        cls: 'high',   on: true },
  // Tier-3 — medium / large-volume, default OFF (heavy)
  { id: 'military_air',            name: 'Military aircraft',          cls: 'high',   on: false },
  { id: 'dark_vessel',             name: 'Vessels gone dark · 21K',    cls: 'on',     on: false },
  { id: 'loitering',               name: 'Loitering detected · 17K',   cls: 'on',     on: false },
  // Environmental
  { id: 'wildfires',               name: 'Active wildfires · 27K',     cls: 'low',    on: false },
  { id: 'quakes',                  name: 'Earthquakes',                cls: 'on',     on: false },
  // Live entity feeds (separate API, viewport)
  { id: 'vessels',                 name: 'All vessels (live AIS)',     cls: 'cyan',   on: true,
    src: 'viewport', kind: 'vessel' },
  { id: 'aircraft',                name: 'All aircraft (live ADS-B)',  cls: 'purple', on: true,
    src: 'viewport', kind: 'aircraft' },
  // Orbital — Web Worker propagates SGP4 every 30s
  { id: 'satellites',              name: 'Satellites (live SGP4)',     cls: 'low',    on: true,
    src: 'satellites' },
  // Infrastructure — strategic oil/gas pipeline routes (static geojson)
  { id: 'pipelines',               name: 'Pipelines (oil + gas)',      cls: 'on',     on: false,
    src: 'pipelines' },
  // Trafficking corridors — drugs / humans / arms (UNODC + SIPRI sources)
  { id: 'trafficking',             name: 'Trafficking corridors',      cls: 'crit',   on: false,
    src: 'trafficking' },
  // Undersea telecom cables — chokepoint + cut-event intel (TeleGeography)
  { id: 'cables',                  name: 'Undersea cables',            cls: 'on',     on: false,
    src: 'cables' },
  // Military bases — publicly-disclosed strategic installations
  { id: 'mil_bases',               name: 'Military bases',             cls: 'high',   on: false,
    src: 'mil_bases' },
  // Nuclear infrastructure — reactors, enrichment, weapons labs
  { id: 'nuclear',                 name: 'Nuclear sites',              cls: 'crit',   on: false,
    src: 'nuclear' },
  // Conflict zones — ongoing insurgency / terror / armed group / civil war
  // theaters (curated overlay; live GDELT terror events stream in parallel
  // via the gdelt_topical ingester)
  { id: 'conflict_zones',          name: 'Conflict zones',             cls: 'crit',   on: false,
    src: 'conflict_zones' },
  // Diplomatic posts — major capitals + UN-organization hubs where embassies
  // + missions concentrate (15 regional aggregates, not individual buildings)
  { id: 'diplomatic_posts',        name: 'Diplomatic posts',           cls: 'low',    on: false,
    src: 'diplomatic_posts' },
  // UN missions — active peacekeeping + political + observer missions + system HQ
  { id: 'un_missions',             name: 'UN missions',                cls: 'on',     on: false,
    src: 'un_missions' },
  // Disputed zones — sovereignty contests, occupied regions, strategic flashpoints
  { id: 'disputed_zones',          name: 'Disputed zones',             cls: 'high',   on: false,
    src: 'disputed_zones' },
  // State media + disinfo ops — state-owned/funded broadcasters + documented
  // coordinated-inauthentic-behavior operations
  { id: 'state_media',             name: 'State media + disinfo ops',  cls: 'high',   on: false,
    src: 'state_media' },
  // Sanction targets — country-level summary of who's sanctioned by whom
  // (distinct from the entity-level OFAC/UK/EU asset data we already store)
  { id: 'sanction_targets',        name: 'Sanction targets (regimes)', cls: 'crit',   on: false,
    src: 'sanction_targets' },
  // NOAA buoys — curated NDBC ocean monitoring stations (location-only;
  // live observation data is a future enhancement)
  { id: 'noaa_buoys',              name: 'NOAA buoys',                 cls: 'low',    on: false,
    src: 'noaa_buoys' },
  // Climate forecast — 15 major world cities (static seed; live Open-Meteo
  // refresh is a future enhancement)
  { id: 'climate_forecast',        name: 'Climate forecast',           cls: 'low',    on: false,
    src: 'climate_forecast' },
  // Tracks: per-entity position history rendered as polylines. Off by
  // default — toggled on in the entity detail panel for selected
  // entities. Master toggle here hides/shows ALL active tracks at once
  // without losing the entity-level selections.
  { id: 'tracks',                  name: 'Track lines (selected)',     cls: 'on',     on: true,
    src: 'tracks' },
];

/* Category grouping for the layers panel. Ordered; each group renders as a
   collapsible accordion section. Groups whose layers are default-on start
   expanded. Any LAYERS id not listed here falls into a defensive "Other"
   bucket (see buildLayerGroupModel) so a new layer never silently vanishes
   from the panel. */
const LAYER_GROUPS = [
  { id: 'traffic',        label: 'Live Traffic',              defaultOpen: true,
    layerIds: ['vessels', 'aircraft', 'military_air', 'satellites'] },
  { id: 'sanctions',      label: 'Sanctions & Dark Activity', defaultOpen: true,
    layerIds: ['sanctioned_dark', 'sanctioned_rendezvous', 'shadow_fleet',
               'sanctioned_underway', 'sanctioned_port', 'dark_vessel', 'loitering'] },
  { id: 'geopolitics',    label: 'Geopolitics',               defaultOpen: false,
    layerIds: ['sanctioned_airspace', 'conflict_zones', 'disputed_zones',
               'diplomatic_posts', 'un_missions', 'state_media',
               'sanction_targets', 'trafficking'] },
  { id: 'infrastructure', label: 'Strategic Infrastructure',  defaultOpen: false,
    layerIds: ['pipelines', 'cables', 'mil_bases', 'nuclear'] },
  { id: 'environment',    label: 'Environment & Climate',     defaultOpen: false,
    layerIds: ['wildfires', 'quakes', 'noaa_buoys', 'climate_forecast'] },
  { id: 'overlays',       label: 'Overlays',                  defaultOpen: true,
    layerIds: ['tracks'] },
];
const LAYER_GROUPS_LS_KEY = 'glassbox.atlas.layerGroups.collapsed';

/* Build the ordered [{group, layers}] render model from LAYER_GROUPS, looking
   up each layer object by id. Any LAYERS entry not claimed by a group is
   collected into a synthetic "Other" group so it stays visible + toggleable. */
function buildLayerGroupModel() {
  const claimed = new Set();
  const model = LAYER_GROUPS.map(g => {
    const layers = g.layerIds
      .map(id => LAYERS.find(L => L.id === id))
      .filter(Boolean);
    layers.forEach(L => claimed.add(L.id));
    return { group: g, layers };
  });
  const orphans = LAYERS.filter(L => !claimed.has(L.id));
  if (orphans.length) {
    console.warn('[layers] ungrouped layers fell into "Other":',
                 orphans.map(L => L.id).join(', '));
    model.push({
      group: { id: 'other', label: 'Other', defaultOpen: true },
      layers: orphans,
    });
  }
  return model;
}

/* Returns a Set of collapsed group ids from localStorage, or null when no
   preference is stored (caller falls back to each group's defaultOpen).
   Never throws — localStorage may be disabled (private mode). */
function loadCollapsedGroups() {
  try {
    const raw = localStorage.getItem(LAYER_GROUPS_LS_KEY);
    if (!raw) return null;
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : null;
  } catch (e) { return null; }
}

function saveCollapsedGroups(collapsedSet) {
  try {
    localStorage.setItem(LAYER_GROUPS_LS_KEY, JSON.stringify([...collapsedSet]));
  } catch (e) { /* localStorage unavailable — collapse state is non-critical */ }
}

/* Initial collapsed Set for a freshly-built model: stored preference wins,
   else each group's defaultOpen (defaultOpen:false => starts collapsed). */
function initialCollapsedSet(model) {
  const stored = loadCollapsedGroups();
  const collapsed = new Set();
  for (const { group } of model) {
    const isCollapsed = stored ? stored.has(group.id) : !group.defaultOpen;
    if (isCollapsed) collapsed.add(group.id);
  }
  return collapsed;
}

const SEV_COLOR = {
  critical: Cesium.Color.fromCssColorString('#ff5b5b'),
  high:     Cesium.Color.fromCssColorString('#ff9f3a'),
  medium:   Cesium.Color.fromCssColorString('#ffd166'),
  low:      Cesium.Color.fromCssColorString('#54e29c'),
};
const VESSEL_COLOR   = Cesium.Color.fromCssColorString('#7fd0ff').withAlpha(0.92);
const AIRCRAFT_COLOR = Cesium.Color.fromCssColorString('#c8a6ff').withAlpha(0.92);

/* Directional sprite icons for moving entities — rendered once as
   data-URIs, reused for every entity. Vessels are wedge-shaped (boat
   prow points along heading); aircraft are arrowhead silhouettes. The
   color is baked in so we don't have to multiply at draw time. */
function _spriteDataURI(svg) {
  return 'data:image/svg+xml;utf8,' + encodeURIComponent(svg);
}
/* Top-down vessel silhouette: pointed bow at top, parallel hull sides,
   flat stern at bottom, dark superstructure rectangle near the bow.
   Reads as "boat" at glance instead of generic arrowhead.
   ViewBox is tall (24 high, 18 wide) to look ship-like. */
const VESSEL_SPRITE = _spriteDataURI(`
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 24" width="18" height="24">
  <path d="M9 1 L14 7 L14 22 L4 22 L4 7 Z"
        fill="#7fd0ff" stroke="#02040a" stroke-width="1.1" stroke-linejoin="round"
        filter="drop-shadow(0 0 3px rgba(127,208,255,0.55))"/>
  <rect x="6.5" y="9" width="5" height="6" fill="#02040a" opacity="0.55" rx="0.6"/>
</svg>`);
const AIRCRAFT_SPRITE = _spriteDataURI(`
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
  <path d="M12 2 L13 10 L22 13 L22 15 L13 14 L13 19 L16 21 L16 22 L12 21 L8 22 L8 21 L11 19 L11 14 L2 15 L2 13 L11 10 Z"
        fill="#c8a6ff" stroke="#02040a" stroke-width="0.8" stroke-linejoin="round"/>
</svg>`);
const VESSEL_SANCTIONED_SPRITE = _spriteDataURI(`
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 18 24" width="18" height="24">
  <path d="M9 1 L14 7 L14 22 L4 22 L4 7 Z"
        fill="#ff5b5b" stroke="#02040a" stroke-width="1.1" stroke-linejoin="round"
        filter="drop-shadow(0 0 4px rgba(255,91,91,0.85))"/>
  <rect x="6.5" y="9" width="5" height="6" fill="#02040a" opacity="0.65" rx="0.6"/>
</svg>`);
const MILITARY_AIR_SPRITE = _spriteDataURI(`
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">
  <path d="M12 2 L13 10 L22 13 L22 15 L13 14 L13 19 L16 21 L16 22 L12 21 L8 22 L8 21 L11 19 L11 14 L2 15 L2 13 L11 10 Z"
        fill="#ff9f3a" stroke="#02040a" stroke-width="0.8" stroke-linejoin="round"/>
</svg>`);

/* ─── Webcam regional sets ──────────────────────────────────────────
 * Each tile carries both an embed `src` (video) and a `watch` URL
 * (always opens the live stream directly on YouTube). YouTube live
 * streams die regularly (channels stop streaming, videos go private,
 * embeds get region-blocked) — the watch link is the always-working
 * escape hatch so a dead tile is never a dead end.
 *
 * Where possible we use the channel-live-stream form
 * (`embed/live_stream?channel=…`) which auto-resolves to whichever
 * stream that channel currently has live, instead of hardcoded
 * video IDs. */
/* All 16 below verified currently-live as of 2026-05-14. Audit
   2026-05-13 NIGHT discovered that nearly all of the original cam
   IDs had been deleted (404 from oembed). These are sourced from
   the two most-reliable always-streaming channels:
     EarthCam — UC6qrG3W8SMK0jior2olka3g  (US-heavy)
     Skyline Webcams — UC2WMV4vCYurHdHPd9pCqYSg  (EU-heavy, Mediterranean)
   Both channels stream individual cams as their own YouTube videos
   with stable IDs. When a cam goes down, swap with another from the
   same channel — scrape /streams page for current live IDs. */
const WEBCAM_SETS = {
  world: [
    { lbl: 'World Trade Center · NYC',
      src: 'https://www.youtube.com/embed/5C9oM7C2Q9k?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=5C9oM7C2Q9k' },
    { lbl: 'Italy · 600 cams',
      src: 'https://www.youtube.com/embed/wMT2aNcP4Wg?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=wMT2aNcP4Wg' },
    { lbl: 'Sint Maarten',
      src: 'https://www.youtube.com/embed/gXdwMR7Wyrw?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=gXdwMR7Wyrw' },
    { lbl: 'Africa Watering Hole',
      src: 'https://www.youtube.com/embed/FEdZ87fxV2w?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=FEdZ87fxV2w' },
  ],
  americas: [
    { lbl: 'Statue of Liberty',
      src: 'https://www.youtube.com/embed/cWR8KGKftUw?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=cWR8KGKftUw' },
    { lbl: 'Washington Monument',
      src: 'https://www.youtube.com/embed/oDCAAfOSqvA?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=oDCAAfOSqvA' },
    { lbl: 'New Orleans Balcony',
      src: 'https://www.youtube.com/embed/C32EiZiQPkQ?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=C32EiZiQPkQ' },
    { lbl: 'Chicago Skydeck',
      src: 'https://www.youtube.com/embed/O0UGT7AT3aw?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=O0UGT7AT3aw' },
  ],
  europe: [
    { lbl: 'Italy · 600 cams',
      src: 'https://www.youtube.com/embed/wMT2aNcP4Wg?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=wMT2aNcP4Wg' },
    { lbl: 'Spain · 200 cams',
      src: 'https://www.youtube.com/embed/NHRDdaH4LpU?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=NHRDdaH4LpU' },
    { lbl: 'Greece · 200 cams',
      src: 'https://www.youtube.com/embed/5p-s-1453Us?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=5p-s-1453Us' },
    { lbl: 'USA · UK · Canada',
      src: 'https://www.youtube.com/embed/uGaxXTlz_f8?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=uGaxXTlz_f8' },
  ],
  explore: [
    { lbl: 'Coney Island Boardwalk',
      src: 'https://www.youtube.com/embed/H67j7H-7QD0?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=H67j7H-7QD0' },
    { lbl: 'Marco Island Marina',
      src: 'https://www.youtube.com/embed/AEtwttyRljo?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=AEtwttyRljo' },
    { lbl: 'Midway Airport · Chicago',
      src: 'https://www.youtube.com/embed/0ZzPWoeZzuI?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=0ZzPWoeZzuI' },
    { lbl: 'Wildwoods · NJ',
      src: 'https://www.youtube.com/embed/58Lx2eLT1ig?autoplay=1&mute=1&controls=0',
      watch: 'https://www.youtube.com/watch?v=58Lx2eLT1ig' },
  ],
};

/* News: always open-on-YouTube link per channel so a dead embed is
   never a dead-end. Watch URL points at the channel's /live page
   which YouTube auto-redirects to whatever's currently streaming. */
const NEWS_WATCH_URL = (chId) => `https://www.youtube.com/channel/${chId}/live`;

/* ─── State ──────────────────────────────────────────────────────── */
let viewer = null;
let WINDOW_H = 24;
const ENT_BY_KEY = new Map();
let LAST_PAYLOAD = null;
let SSE = null, SSE_RETRY = 1000;
const REFRESH_SEC = 30;
let countdownTimer = null;

/* ─── Onboarding overlay (first-visit only) ──────────────────────── */
/* Show once per browser. Dismissed state lives in localStorage so
   returning visitors skip it. URL `?welcome=1` forces re-show
   (useful for screen recordings + sales demos). */
const ONBOARD_KEY = 'glassbox_onboarded_v1';

function maybeShowOnboarding() {
  const el = $('onboard');
  if (!el) return;
  const forced = new URLSearchParams(location.search).get('welcome') === '1';
  let seen = false;
  try { seen = localStorage.getItem(ONBOARD_KEY) === '1'; } catch (_) {}
  if (seen && !forced) return;
  // Defer until the loader fades so the welcome card lands over the
  // cockpit, not a blank screen.
  setTimeout(() => {
    el.classList.add('show');
    if (window.gbtrack) window.gbtrack('onboard_shown');
  }, 1500);
  const dismiss = () => {
    el.classList.remove('show');
    try { localStorage.setItem(ONBOARD_KEY, '1'); } catch (_) {}
    if (window.gbtrack) window.gbtrack('onboard_dismissed');
  };
  const btn = $('ob-dismiss');
  if (btn) btn.addEventListener('click', dismiss);
  // Escape key also dismisses
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && el.classList.contains('show')) dismiss();
  });
  // Click the backdrop (not the card) dismisses
  el.addEventListener('click', (ev) => { if (ev.target === el) dismiss(); });
  // "See pricing" doesn't need dismiss — it navigates away
}

/* ─── Pipelines layer: strategic oil + gas routes ───────────────── */
/* Fetches /api/v1/infrastructure/pipelines (hand-curated GeoJSON of
   ~25 globally-strategic pipelines + chokepoints) and renders each
   LineString as a Cesium polyline color-coded by status:
     operational     → amber
     damaged_*       → red (Nord Stream et al)
     suspended_*     → muted gray
     proposed        → dashed cyan
     under_construction → dashed amber
   Click a pipeline → entity-detail panel shows name + operator +
   length + commodity + notes. */
const PIPELINE_ENT_BY_NAME = new Map();
const PIPELINE_LAYER_ID = 'pipelines';
const PIPELINE_COLOR = {
  operational:        '#ff9f3a',  // amber — flowing crude/gas
  damaged_2022:       '#ff5b5b',  // red — Nord Stream
  suspended_2022:     '#807a6c',  // muted — Yamal
  suspended_2021:     '#807a6c',  // muted — Maghreb-Europe
  completed_never_operational: '#ff5b5b',
  operational_partial_sanctions: '#ffd166',
  operational_carveout: '#ffd166',
  operational_houthi_threat: '#ff5b5b',
  proposed:           '#7fd0ff',  // cyan dashed
  under_construction: '#ffd166',  // yellow dashed
  proposed_sanctions_blocked: '#807a6c',
};

// ─── Optional density-grid layer (P3-M, 2026-05-20) ──────────────────────
// Opt-in via the `?heat=1` URL flag. Buckets the last 24h of /signals/today
// findings into a 5°×5° lat/lng grid and renders each non-zero cell as a
// Cesium ellipse colored by event density (cool blue → hot red). Stock
// primitives only — no new CDN dependencies, no third-party heatmap
// library. The trade-off: this is a density GRID, not a smoothed gradient
// heatmap; if you later want true gradient heatmaps the upgrade path is
// to swap this function's body for a HeatmapImageryProvider polyfill
// (see https://github.com/MendigoBordo/Cesium-Heatmap, MIT license).
// Fails silent: any fetch error / empty payload simply doesn't render
// and a console.info logs the reason — no exceptions propagate.
async function _loadDensityHeatmap() {
  const params = new URLSearchParams(location.search);
  if (!params.has('heat')) {
    return;  // not requested; quiet exit
  }
  try {
    const resp = await fetch('/api/v1/signals/today?window_hours=24&per_category=50');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const cells = new Map();  // "latBucket,lngBucket" → {count, sumSev, lat, lng}
    for (const cat of (data.categories || [])) {
      for (const item of (cat.items || [])) {
        if (typeof item.lat !== 'number' || typeof item.lng !== 'number') continue;
        const latBucket = Math.floor(item.lat / 5) * 5;
        const lngBucket = Math.floor(item.lng / 5) * 5;
        const key = `${latBucket},${lngBucket}`;
        if (!cells.has(key)) {
          cells.set(key, {
            count: 0, sumSev: 0,
            lat: latBucket + 2.5,  // cell center
            lng: lngBucket + 2.5,
          });
        }
        const c = cells.get(key);
        c.count++;
        c.sumSev += Number(item.severity || 0);
      }
    }
    if (cells.size === 0) {
      console.info('[heatmap] No events in last 24h to render');
      return;
    }
    const counts = Array.from(cells.values()).map(c => c.count);
    const maxCount = Math.max(...counts);
    for (const c of cells.values()) {
      const norm = c.count / maxCount;  // 0..1
      const r = norm;
      const g = 0.2;
      const b = 1 - norm;
      const alpha = 0.25 + 0.50 * norm;  // more opaque for hotter cells
      const css = `rgba(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)},${alpha})`;
      viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(c.lng, c.lat),
        ellipse: {
          // ~2.5° in meters at the equator; mapped to our 5° bucket scale.
          // Cesium ellipse semiAxes are in meters along the surface, so this
          // is approximate at high latitudes (cells visibly shrink near the
          // poles, which is geometrically correct).
          semiMinorAxis: 250000.0,
          semiMajorAxis: 250000.0,
          material: Cesium.Color.fromCssColorString(css),
          outline: false,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
        properties: {
          _glassbox_meta: {
            kind: 'heatmap_cell',
            count: c.count,
            avg_severity: c.sumSev / c.count,
            lat_bucket: Math.floor(c.lat / 5) * 5,
            lng_bucket: Math.floor(c.lng / 5) * 5,
          },
        },
      });
    }
    console.info(`[heatmap] OK — ${cells.size} non-zero cells rendered (max count: ${maxCount}); pass ?heat=0 or remove the flag to disable`);
  } catch (e) {
    console.warn(`[heatmap] FAILED: ${e.message} — layer not rendered`);
  }
}

// ─── Cyber-attack data layers (P2-A Phase 1 MVP, 2026-05-27) ─────────────
//
// CISA KEV + Spamhaus DROP/EDROP are NOT geographically positioned, so
// these layers render as SIDE-PANEL LIST views rather than globe overlays.
// Activated via URL flags ?kev=1 and ?spamhaus=1 — opt-in to keep the
// default cockpit uncluttered.
//
// Endpoints:
//   /api/v1/infrastructure/cyber-kev            — CISA Known Exploited Vulns
//   /api/v1/infrastructure/cyber-spamhaus-drop  — Spamhaus DROP+EDROP blocks
//
// The panel is a fixed-position div appended to <body> at init time. It
// includes a close button + license attribution in the footer per the
// scoping doc's "license attribution rendered in footer" requirement.
// Defensive: any fetch error logs a warning and renders nothing —
// failure must NOT block the cockpit from booting.

function _buildCyberPanel(opts) {
  // opts: { id, title, attribution, headerStyle, anchor }
  // anchor: 'bottom-right' (default) | 'top-right' — lets caller stack
  // KEV + Spamhaus on opposite corners so they don't overlap when both
  // toggles are active.
  const panel = document.createElement('div');
  panel.id = opts.id;
  const vertical = opts.anchor === 'top-right'
    ? 'top: 16px'
    : 'bottom: 16px';
  panel.style.cssText = [
    'position: fixed',
    'right: 16px',
    vertical,
    'width: 380px',
    'max-height: calc(50vh - 24px)',
    'background: rgba(2, 4, 10, 0.94)',
    'border: 1px solid ' + (opts.headerStyle || '#7fd0ff'),
    'border-radius: 6px',
    'box-shadow: 0 0 28px rgba(0,0,0,0.55)',
    'color: #e8edf5',
    'font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace',
    'font-size: 11.5px',
    'z-index: 9001',
    'display: flex',
    'flex-direction: column',
  ].join(';');

  const header = document.createElement('div');
  header.style.cssText = [
    'display: flex',
    'align-items: center',
    'justify-content: space-between',
    'padding: 8px 12px',
    'border-bottom: 1px solid rgba(255,255,255,0.12)',
    'color: ' + (opts.headerStyle || '#7fd0ff'),
    'font-weight: 600',
    'letter-spacing: 0.04em',
    'text-transform: uppercase',
  ].join(';');
  header.innerHTML = `<span>${opts.title}</span>`;
  const closeBtn = document.createElement('button');
  closeBtn.textContent = '×';
  closeBtn.style.cssText = [
    'background: none',
    'border: 1px solid rgba(255,255,255,0.25)',
    'color: inherit',
    'cursor: pointer',
    'font-size: 14px',
    'line-height: 1',
    'padding: 2px 8px',
    'border-radius: 3px',
  ].join(';');
  closeBtn.onclick = () => panel.remove();
  header.appendChild(closeBtn);

  const list = document.createElement('div');
  list.style.cssText = [
    'overflow-y: auto',
    'flex: 1 1 auto',
    'padding: 6px 12px',
  ].join(';');

  const footer = document.createElement('div');
  footer.style.cssText = [
    'padding: 6px 12px',
    'border-top: 1px solid rgba(255,255,255,0.12)',
    'color: rgba(232,237,245,0.55)',
    'font-size: 10px',
  ].join(';');
  footer.textContent = opts.attribution;

  panel.appendChild(header);
  panel.appendChild(list);
  panel.appendChild(footer);
  document.body.appendChild(panel);
  return list;
}

function _escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function _loadCyberKev() {
  const params = new URLSearchParams(location.search);
  if (!params.has('kev')) return;
  try {
    const resp = await fetch('/api/v1/infrastructure/cyber-kev');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const fc = await resp.json();
    const features = (fc.features || []);
    if (features.length === 0) {
      console.info('[kev] No KEV features to render');
      return;
    }
    const meta = fc.metadata || {};
    const list = _buildCyberPanel({
      id: 'cyber-kev-panel',
      title: `CISA KEV · ${features.length}`,
      attribution: meta.attribution || 'Known-exploited vulnerabilities: CISA KEV Catalog (CC0)',
      headerStyle: '#ff9f3a',
      anchor: 'bottom-right',
    });

    // Sort by date_added desc so most-recent CVEs render first.
    features.sort((a, b) => {
      const da = (a.properties && a.properties.date_added) || '';
      const db = (b.properties && b.properties.date_added) || '';
      return db.localeCompare(da);
    });

    for (const feat of features.slice(0, 200)) {  // cap initial render at 200
      const p = feat.properties || {};
      const ransomware = (p.known_ransomware_campaign_use === 'Known')
        ? '<span style="color:#ff5b5b;font-weight:600">RANSOMWARE</span> '
        : '';
      const row = document.createElement('div');
      row.style.cssText = 'padding:5px 0;border-bottom:1px dotted rgba(255,255,255,0.07);';
      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;gap:8px;">
          <a href="${_escapeHtml(p.link || '#')}" target="_blank" rel="noopener"
             style="color:#7fd0ff;text-decoration:none;font-weight:600;">
            ${_escapeHtml(p.cve_id)}
          </a>
          <span style="color:rgba(232,237,245,0.55)">${_escapeHtml(p.date_added || '')}</span>
        </div>
        <div style="color:#e8edf5;line-height:1.35;">
          ${ransomware}${_escapeHtml(p.vendor_project || '')} ${_escapeHtml(p.product || '')}
          ${p.vulnerability_name ? '— ' + _escapeHtml(p.vulnerability_name) : ''}
        </div>`;
      list.appendChild(row);
    }
    console.info(`[kev] OK — ${features.length} entries; panel-rendered (top 200). Remove ?kev=1 to hide.`);
  } catch (e) {
    console.warn(`[kev] FAILED: ${e.message} — layer not rendered`);
  }
}

async function _loadCyberSpamhaus() {
  const params = new URLSearchParams(location.search);
  if (!params.has('spamhaus')) return;
  try {
    const resp = await fetch('/api/v1/infrastructure/cyber-spamhaus-drop');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const fc = await resp.json();
    const features = (fc.features || []);
    if (features.length === 0) {
      console.info('[spamhaus] No Spamhaus DROP/EDROP features to render');
      return;
    }
    const meta = fc.metadata || {};
    // Sort: DROP first (higher confidence), then by sbl_id ascending for stability
    features.sort((a, b) => {
      const la = (a.properties && a.properties.list_name) || '';
      const lb = (b.properties && b.properties.list_name) || '';
      if (la !== lb) return la === 'DROP' ? -1 : 1;
      const sa = (a.properties && a.properties.sbl_id) || '';
      const sb = (b.properties && b.properties.sbl_id) || '';
      return sa.localeCompare(sb);
    });

    const list = _buildCyberPanel({
      id: 'cyber-spamhaus-panel',
      title: `Spamhaus DROP/EDROP · ${features.length}`,
      attribution: meta.attribution || 'Block lists: Spamhaus',
      headerStyle: '#ff5b5b',
      anchor: 'top-right',
    });
    for (const feat of features.slice(0, 200)) {
      const p = feat.properties || {};
      const tag = p.list_name === 'DROP'
        ? '<span style="color:#ff5b5b;font-weight:600">DROP</span>'
        : '<span style="color:#ff9f3a;font-weight:600">EDROP</span>';
      const row = document.createElement('div');
      row.style.cssText = 'padding:5px 0;border-bottom:1px dotted rgba(255,255,255,0.07);';
      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;gap:8px;">
          <span style="color:#e8edf5;font-weight:600;">${_escapeHtml(p.cidr)}</span>
          ${tag}
        </div>
        <div>
          <a href="${_escapeHtml(p.link || '#')}" target="_blank" rel="noopener"
             style="color:#7fd0ff;text-decoration:none;font-size:10.5px;">
            ${_escapeHtml(p.sbl_id)}
          </a>
        </div>`;
      list.appendChild(row);
    }
    console.info(`[spamhaus] OK — ${features.length} entries; panel-rendered (top 200). Remove ?spamhaus=1 to hide.`);
  } catch (e) {
    console.warn(`[spamhaus] FAILED: ${e.message} — layer not rendered`);
  }
}


async function loadPipelines() {
  if (PIPELINE_ENT_BY_NAME.size > 0) return;  // load once
  try {
    const r = await fetch('/api/v1/infrastructure/pipelines');
    if (!r.ok) {
      console.warn('Pipelines endpoint returned', r.status);
      return;
    }
    const fc = await r.json();
    const layer = LAYERS.find(L => L.id === PIPELINE_LAYER_ID) || {};
    for (const feat of fc.features || []) {
      const props = feat.properties || {};
      const coords = feat.geometry && feat.geometry.coordinates;
      if (!coords || coords.length < 2) continue;
      // Flatten [[lng,lat],...] into [lng,lat,lng,lat,...] for Cesium
      const flat = [];
      for (const c of coords) {
        flat.push(c[0], c[1]);
      }
      const color = Cesium.Color.fromCssColorString(
        PIPELINE_COLOR[props.status] || '#7fd0ff').withAlpha(0.85);
      const isDashed = (props.status || '').startsWith('proposed') ||
                       (props.status || '').startsWith('under_construction');
      const material = isDashed
        ? new Cesium.PolylineDashMaterialProperty({
            color: color,
            dashLength: 16,
          })
        : new Cesium.PolylineGlowMaterialProperty({
            color: color,
            glowPower: 0.18,
          });
      const ent = viewer.entities.add({
        polyline: {
          positions: Cesium.Cartesian3.fromDegreesArray(flat),
          width: 2.6,
          material: material,
          clampToGround: true,
        },
        show: !!layer.on,
      });
      ent._glassbox_layer = PIPELINE_LAYER_ID;
      ent._glassbox_meta = {
        entity_type: 'pipeline',
        display_name: props.name,
        title: props.name,
        description:
          `${props.commodity || 'pipeline'} · ${props.length_km || '?'} km · ` +
          `operator: ${props.operator || 'unknown'} · status: ${props.status || 'unknown'}` +
          (props.notes ? `\n\n${props.notes}` : ''),
        properties: props,
      };
      PIPELINE_ENT_BY_NAME.set(props.name, ent);
    }
    const cnt = $('cnt-' + PIPELINE_LAYER_ID);
    if (cnt) cnt.textContent = PIPELINE_ENT_BY_NAME.size.toLocaleString();
  } catch (e) {
    console.warn('Pipelines layer load failed:', e);
  }
}

function setPipelineLayerVisible(visible) {
  for (const ent of PIPELINE_ENT_BY_NAME.values()) {
    ent.show = visible;
  }
  if (viewer) viewer.scene.requestRender();
}

/* ─── Trafficking corridors layer ──────────────────────────────── */
/* Same render pattern as pipelines — fetch curated GeoJSON, render
   each LineString as a polyline. Color-coded by trafficking category:
     cocaine        → white-grey   (US-centered routes)
     heroin         → light brown  (Afghan/Myanmar routes)
     methamphetamine→ pale blue    (Mekong)
     fentanyl       → red          (US overdose driver)
     humans         → magenta      (high-priority intel)
     arms           → orange       (state + black-market)
   Click → entity-detail panel shows source / destination / volume /
   primary actors. */
const TRAFFICKING_ENT_BY_NAME = new Map();
const TRAFFICKING_LAYER_ID = 'trafficking';
const TRAFFICKING_COLOR = {
  cocaine:        '#e0d7c8',
  heroin:         '#c89878',
  methamphetamine:'#a0c8e0',
  fentanyl:       '#ff5b5b',
  humans:         '#d770ff',
  arms:           '#ffa53a',
};

async function loadTrafficking() {
  if (TRAFFICKING_ENT_BY_NAME.size > 0) return;
  try {
    const r = await fetch('/api/v1/infrastructure/trafficking');
    if (!r.ok) {
      console.warn('Trafficking endpoint returned', r.status);
      return;
    }
    const fc = await r.json();
    const layer = LAYERS.find(L => L.id === TRAFFICKING_LAYER_ID) || {};
    for (const feat of fc.features || []) {
      const props = feat.properties || {};
      const coords = feat.geometry && feat.geometry.coordinates;
      if (!coords || coords.length < 2) continue;
      const flat = [];
      for (const c of coords) flat.push(c[0], c[1]);
      const color = Cesium.Color.fromCssColorString(
        TRAFFICKING_COLOR[props.category] || '#a59a82').withAlpha(0.78);
      // Use a dashed line for visual distinction from pipelines.
      const material = new Cesium.PolylineDashMaterialProperty({
        color: color,
        dashLength: 14,
      });
      const ent = viewer.entities.add({
        polyline: {
          positions: Cesium.Cartesian3.fromDegreesArray(flat),
          width: 2.2,
          material: material,
          clampToGround: true,
        },
        show: !!layer.on,
      });
      ent._glassbox_layer = TRAFFICKING_LAYER_ID;
      ent._glassbox_meta = {
        entity_type: 'trafficking_corridor',
        display_name: props.name,
        title: props.name,
        description:
          `${props.category} · source: ${props.source || '?'} → ` +
          `dest: ${props.destination || '?'} · ` +
          `${props.volume_estimate || ''}` +
          (props.primary_actors ? `\nActors: ${props.primary_actors}` : '') +
          (props.notes ? `\n\n${props.notes}` : ''),
        properties: props,
      };
      TRAFFICKING_ENT_BY_NAME.set(props.name, ent);
    }
    const cnt = $('cnt-' + TRAFFICKING_LAYER_ID);
    if (cnt) cnt.textContent = TRAFFICKING_ENT_BY_NAME.size.toLocaleString();
  } catch (e) {
    console.warn('Trafficking layer load failed:', e);
  }
}

/* ─── Military bases + Nuclear sites layers ──────────────────────── */
/* Point-based overlays (unlike pipelines/trafficking/cables which are
   lines). Each Feature is a single facility with a hot-colored pin.
   Click → entity-detail shows category, country, type, command, notes. */
const MIL_ENT_BY_NAME = new Map();
const NUC_ENT_BY_NAME = new Map();
const MIL_LAYER_ID = 'mil_bases';
const NUC_LAYER_ID = 'nuclear';
const MIL_COLOR = {
  us_overseas:    '#7fd0ff',
  nato:           '#7fd0ff',
  russia:         '#ff5b5b',
  china:          '#ffd166',
  iran:           '#ff9f3a',
  north_korea:    '#ff5b5b',
  israel:         '#54e29c',
  india:          '#a0e0c8',
  pakistan:       '#c8a6ff',
};
const NUC_COLOR = {
  reactor_active:        '#54e29c',
  reactor_decommissioned:'#807a6c',
  enrichment:            '#ff9f3a',
  weapons_lab:           '#ff5b5b',
  storage:               '#7fd0ff',
  disaster_site:         '#ff5b5b',
};

// P2-B Phase 1 (2026-05-27 NIGHT): conflict-zones layer — curated overlay
// of ongoing armed-conflict / insurgency / terror / civil-war theaters.
// Ported from v1 glassbox_v2.html:10692-10700. Live GDELT terrorism events
// flow in parallel via the existing gdelt_topical ingester.
const CONFLICT_ENT_BY_NAME = new Map();
const CONFLICT_LAYER_ID = 'conflict_zones';
const CONFLICT_COLOR = {
  Insurgency:   '#ff9f3a',    // amber — sustained armed insurgency
  Terror:       '#ff5b5b',    // red — designated terror group activity
  'Armed Group':'#ffd166',    // yellow — non-state armed group
  'Civil War':  '#ff5b5b',    // red — multi-party state-level conflict
};

// P2-B Phase 1 (2026-05-27 NIGHT): diplomatic-posts layer — major
// diplomatic clusters worldwide. 15 regional aggregates from v1
// glassbox_v2.html:14856-14885. Distinguished by category: capital_hub
// (bilateral diplomacy), un_org_hub (multilateral UN agencies),
// regional_hub (secondary aggregators).
const DIPLOMATIC_ENT_BY_NAME = new Map();
const DIPLOMATIC_LAYER_ID = 'diplomatic_posts';
const DIPLOMATIC_COLOR = {
  capital_hub:  '#ffdd44',    // yellow — bilateral diplomacy primary
  un_org_hub:   '#54e29c',    // green — multilateral UN-agency cluster
  regional_hub: '#7fd0ff',    // cyan — regional secondary hub
};

// P2-B Phase 1 (2026-05-27 NIGHT): un_missions layer — active UN
// peacekeeping + political + observer missions + system HQ. 14 entries
// from v1 glassbox_v2.html:14747-14778 (ended missions stripped).
const UN_ENT_BY_NAME = new Map();
const UN_LAYER_ID = 'un_missions';
const UN_COLOR = {
  peacekeeping: '#00aaff',    // bright blue — Chapter VII troop deployments
  political:    '#7fd0ff',    // light blue — Chapter VI political/support
  observers:    '#a0e0c8',    // pale green — truce-supervision observers
  hq:           '#ffffff',    // white — UN-system headquarters
};

// P2-B Phase 1 (2026-05-27 NIGHT): disputed_zones layer — sovereignty
// contests, occupied regions, strategic flashpoints. 20 entries from v1
// glassbox_v2.html:11993-12013. Categorized into 3 intensity tiers.
const DISPUTED_ENT_BY_NAME = new Map();
const DISPUTED_LAYER_ID = 'disputed_zones';
const DISPUTED_COLOR = {
  active:     '#ff5b5b',      // red — currently kinetic
  flashpoint: '#ff9f3a',      // amber — major strategic risk
  frozen:     '#ffd166',      // yellow — unresolved but not active fighting
};

// P2-B Phase 1 (2026-05-27 NIGHT): state_media layer — state-owned
// broadcasters + state-funded media + documented coordinated-inauthentic
// -behavior operations. 15 entries from v1 glassbox_v2.html:12093-12120
// (the v1 layer name `propagandaCenters` conflated 3 distinct governance
// models — this port separates them via category sub-tags).
const STATE_MEDIA_ENT_BY_NAME = new Map();
const STATE_MEDIA_LAYER_ID = 'state_media';
const STATE_MEDIA_COLOR = {
  state_owned:  '#c8a6ff',    // light purple — direct gov control
  state_funded: '#ff88dd',    // pink — gov-funded with editorial independence claimed
  disinfo_ops:  '#ff5b5b',    // red — documented coordinated-inauthentic-behavior op
};

// P2-B Phase 1 (2026-05-27 NIGHT): sanction_targets layer — country-
// level / regime-level summary of who is sanctioned by whom. 19
// entries from v1 glassbox_v2.html:12030-12063 (with 2 v1 errors fixed
// at port time: Myanmar location + Mogadishu dedupe).
const SANCTION_ENT_BY_NAME = new Map();
const SANCTION_LAYER_ID = 'sanction_targets';
const SANCTION_COLOR = {
  comprehensive: '#ff0044',   // bright red — whole-economy regime
  terrorism:     '#ff5b5b',   // red — CT designations (FTO etc.)
  arms_embargo:  '#ff9f3a',   // amber — arms-only restriction
  targeted:      '#ffd166',   // yellow — individual/sector measures
  monitoring:    '#a0e0c8',   // pale green — sanctions-evasion watchlist
  legacy:        '#807a6c',   // muted gray — residual / mostly-lifted
};

// P2-B Phase 1 (2026-05-27 NIGHT): noaa_buoys layer — curated NDBC
// ocean monitoring stations. 14 stations from v1 glassbox_v2.html:7813
// -7829 (v1 dupe fixed). Static layer; live observation data is a
// future enhancement.
const NDBC_ENT_BY_NAME = new Map();
const NDBC_LAYER_ID = 'noaa_buoys';
const NDBC_COLOR = {
  pacific_nw: '#22d3ee',     // light cyan — Pacific NW
  pacific:    '#7fd0ff',     // cyan — Pacific (general)
  gulf:       '#54e29c',     // green — Gulf of Mexico
  atlantic:   '#a0e0c8',     // pale green — US Atlantic
  alaska:     '#c8a6ff',     // light purple — Alaska / Bering
  hawaii:     '#ffd166',     // yellow — Hawaii
};

// P2-B Phase 1 (2026-05-27 NIGHT LATE): climate_forecast layer — daily
// climate snapshot for 15 major world cities. Static seed from v1
// fallback dataset at glassbox_v2.html:18504-18519. Live Open-Meteo
// refresh is a follow-on enhancement.
const CLIMATE_ENT_BY_NAME = new Map();
const CLIMATE_LAYER_ID = 'climate_forecast';
const CLIMATE_COLOR = {
  cold:      '#4466ff',      // blue — sub-10°C max
  temperate: '#88cc00',      // yellow-green — 10-20°C
  warm:      '#ffcc00',      // yellow — 20-30°C
  hot:       '#ff6600',      // orange — 30°C+
};

async function loadInfrastructurePoints(url, layerId, colorMap, entMap,
                                          markerSym='⬛') {
  if (entMap.size > 0) return;
  try {
    const r = await fetch(url);
    if (!r.ok) {
      console.warn(`${url} returned ${r.status}`);
      return;
    }
    const fc = await r.json();
    const layer = LAYERS.find(L => L.id === layerId) || {};
    for (const feat of fc.features || []) {
      const props = feat.properties || {};
      const c = feat.geometry && feat.geometry.coordinates;
      if (!c || c.length < 2) continue;
      const [lng, lat] = c;
      const color = Cesium.Color.fromCssColorString(
        colorMap[props.category] || '#a59a82').withAlpha(0.92);
      const ent = viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(lng, lat),
        point: {
          pixelSize: 8,
          color: color,
          outlineColor: Cesium.Color.fromCssColorString('#02040a'),
          outlineWidth: 1.5,
          disableDepthTestDistance: 3e6,
        },
        label: {
          text: props.name,
          font: '500 9px "Geist Mono", monospace',
          fillColor: color,
          outlineColor: Cesium.Color.fromCssColorString('#02040a'),
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(8, 0),
          horizontalOrigin: Cesium.HorizontalOrigin.LEFT,
          verticalOrigin: Cesium.VerticalOrigin.CENTER,
          scale: 0.9,
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 6_000_000),
          disableDepthTestDistance: 3e6,
          showBackground: true,
          backgroundColor: new Cesium.Color(0.01, 0.02, 0.04, 0.6),
          backgroundPadding: new Cesium.Cartesian2(4, 2),
        },
        show: !!layer.on,
      });
      ent._glassbox_layer = layerId;
      ent._glassbox_meta = {
        entity_type: layerId,
        display_name: props.name,
        title: props.name,
        description:
          `${props.category} · ${props.country || '?'} · ` +
          `type: ${props.type || '?'}` +
          (props.command ? ` · command: ${props.command}` : '') +
          (props.reactors ? ` · reactors: ${props.reactors}` : '') +
          (props.capacity_mw ? ` · ${props.capacity_mw} MW` : '') +
          (props.notes ? `\n\n${props.notes}` : ''),
        properties: props,
      };
      entMap.set(props.name, ent);
    }
    const cnt = $('cnt-' + layerId);
    if (cnt) cnt.textContent = entMap.size.toLocaleString();
  } catch (e) {
    console.warn(`Failed to load ${layerId}:`, e);
  }
}

function loadMilitaryBases() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/military-bases',
    MIL_LAYER_ID, MIL_COLOR, MIL_ENT_BY_NAME);
}
function loadNuclearSites() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/nuclear',
    NUC_LAYER_ID, NUC_COLOR, NUC_ENT_BY_NAME);
}
function loadConflictZones() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/conflict-zones',
    CONFLICT_LAYER_ID, CONFLICT_COLOR, CONFLICT_ENT_BY_NAME);
}
function loadDiplomaticPosts() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/diplomatic-posts',
    DIPLOMATIC_LAYER_ID, DIPLOMATIC_COLOR, DIPLOMATIC_ENT_BY_NAME);
}
function loadUNMissions() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/un-missions',
    UN_LAYER_ID, UN_COLOR, UN_ENT_BY_NAME);
}
function loadDisputedZones() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/disputed-zones',
    DISPUTED_LAYER_ID, DISPUTED_COLOR, DISPUTED_ENT_BY_NAME);
}
function loadStateMedia() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/state-media',
    STATE_MEDIA_LAYER_ID, STATE_MEDIA_COLOR, STATE_MEDIA_ENT_BY_NAME);
}
function loadSanctionTargets() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/sanction-targets',
    SANCTION_LAYER_ID, SANCTION_COLOR, SANCTION_ENT_BY_NAME);
}
function loadNoaaBuoys() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/noaa-buoys',
    NDBC_LAYER_ID, NDBC_COLOR, NDBC_ENT_BY_NAME);
}
function loadClimateForecast() {
  return loadInfrastructurePoints(
    '/api/v1/infrastructure/climate-forecast',
    CLIMATE_LAYER_ID, CLIMATE_COLOR, CLIMATE_ENT_BY_NAME);
}

/* ─── Geofence drawing — interactive watchlist creator ──────────── */
/* Click GEOFENCE button → cursor turns to crosshair.
   First click on globe: drops center point.
   Move mouse: preview circle expands.
   Second click: locks radius, opens save modal.
   Saving: POSTs to /api/glassbox/watchlist (existing API) +
   shows the saved fence as a persistent ring on the globe.
   Escape cancels at any stage. */
const _geofenceState = {
  active: false,
  step: 0,           // 0=idle, 1=center-placed, 2=saved
  center: null,      // {lat, lng}
  radiusM: 0,
  preview: [],       // entities: center pin + ring
  handler: null,     // ScreenSpaceEventHandler
  movePollMs: 50,
  _lastMove: 0,
};

function toggleGeofence() {
  _geofenceState.active = !_geofenceState.active;
  const btn = $('geofence-btn');
  if (btn) {
    btn.classList.toggle('on', _geofenceState.active);
    btn.style.background = _geofenceState.active
      ? 'rgba(255, 91, 91, 0.18)' : 'rgba(127, 208, 255, 0.08)';
    btn.style.borderColor = _geofenceState.active
      ? 'var(--crit)' : 'rgba(127, 208, 255, 0.30)';
    btn.style.color = _geofenceState.active ? 'var(--crit)' : 'var(--cyan)';
  }
  if (!_geofenceState.active) {
    _geofenceClear();
  } else {
    _geofenceState.step = 0;
    _geofenceShowToast('Click globe to drop geofence center.');
  }
}

function _geofenceShowToast(msg) {
  const t = $('geofence-toast');
  if (t) { t.textContent = msg; t.style.display = 'inline-block'; }
}
function _geofenceHideToast() {
  const t = $('geofence-toast');
  if (t) t.style.display = 'none';
}

function _geofenceClear() {
  if (!viewer) return;
  for (const ent of _geofenceState.preview) {
    try { viewer.entities.remove(ent); } catch (_) {}
  }
  _geofenceState.preview = [];
  _geofenceState.center = null;
  _geofenceState.radiusM = 0;
  _geofenceState.step = 0;
  _geofenceHideToast();
}

function _setupGeofenceDrawing() {
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  _geofenceState.handler = handler;

  handler.setInputAction((evt) => {
    if (!_geofenceState.active) return;
    const ray = viewer.camera.getPickRay(evt.position);
    const cart = viewer.scene.globe.pick(ray, viewer.scene);
    if (!cart) return;
    const c = Cesium.Cartographic.fromCartesian(cart);
    const lat = Cesium.Math.toDegrees(c.latitude);
    const lng = Cesium.Math.toDegrees(c.longitude);

    if (_geofenceState.step === 0) {
      // Place center
      _geofenceState.center = { lat, lng };
      _geofenceState.step = 1;
      const centerEnt = viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(lng, lat),
        point: {
          pixelSize: 9,
          color: Cesium.Color.fromCssColorString('#ff5b5b'),
          outlineColor: Cesium.Color.fromCssColorString('#02040a'),
          outlineWidth: 2,
          disableDepthTestDistance: 3e6,
        },
      });
      _geofenceState.preview.push(centerEnt);
      _geofenceShowToast('Move cursor to set radius. Click again to lock.');
    } else if (_geofenceState.step === 1) {
      // Lock radius
      _geofenceState.step = 2;
      _geofenceHideToast();
      _openGeofenceModal();
    }
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // Live radius preview while moving
  handler.setInputAction((evt) => {
    if (!_geofenceState.active) return;
    if (_geofenceState.step !== 1) return;
    const now = Date.now();
    if (now - _geofenceState._lastMove < _geofenceState.movePollMs) return;
    _geofenceState._lastMove = now;
    const ray = viewer.camera.getPickRay(evt.endPosition);
    const cart = viewer.scene.globe.pick(ray, viewer.scene);
    if (!cart) return;
    const c = Cesium.Cartographic.fromCartesian(cart);
    const lat = Cesium.Math.toDegrees(c.latitude);
    const lng = Cesium.Math.toDegrees(c.longitude);
    const r = _haversineMeters(
      _geofenceState.center.lat, _geofenceState.center.lng, lat, lng);
    _geofenceState.radiusM = r;
    // Remove + redraw ring (preview[0] is center pin; preview[1+] is ring)
    while (_geofenceState.preview.length > 1) {
      viewer.entities.remove(_geofenceState.preview.pop());
    }
    const ringEnt = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(
        _geofenceState.center.lng, _geofenceState.center.lat),
      ellipse: {
        semiMajorAxis: r,
        semiMinorAxis: r,
        material: Cesium.Color.fromCssColorString('#ff5b5b').withAlpha(0.10),
        outline: true,
        outlineColor: Cesium.Color.fromCssColorString('#ff5b5b').withAlpha(0.85),
        outlineWidth: 1.6,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      },
    });
    _geofenceState.preview.push(ringEnt);
    _geofenceShowToast(`Radius ${_fmtDist(r)} · click to lock`);
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

  // Wire button + Esc + 'G' shortcut
  const btn = $('geofence-btn');
  if (btn) btn.addEventListener('click', toggleGeofence);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && _geofenceState.active) {
      toggleGeofence();
      return;
    }
    if (ev.key !== 'g' && ev.key !== 'G') return;
    const t = ev.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
              t.isContentEditable)) return;
    toggleGeofence();
  });
}

function _openGeofenceModal() {
  const modal = $('geofence-modal');
  if (!modal) return;
  // Pre-fill suggested label from approximate location
  const lat = _geofenceState.center.lat.toFixed(2);
  const lng = _geofenceState.center.lng.toFixed(2);
  $('gf-label').value = `Watch @ ${lat},${lng}`;
  $('gf-coords').textContent = `${lat}, ${lng}`;
  $('gf-radius').textContent = _fmtDist(_geofenceState.radiusM);
  modal.classList.add('show');
}

function _closeGeofenceModal(save) {
  const modal = $('geofence-modal');
  if (!modal) return;
  modal.classList.remove('show');
  if (!save) {
    _geofenceClear();
    if (_geofenceState.active) toggleGeofence();
    return;
  }
  const email = $('gf-email').value.trim().toLowerCase();
  const label = $('gf-label').value.trim() || 'Untitled fence';
  const layers = Array.from($('gf-layers').querySelectorAll('input:checked'))
                      .map(i => i.value);
  const minSev = parseInt($('gf-severity').value, 10) || 5;
  if (!email || !email.includes('@')) {
    alert('Email required for notifications.');
    return;
  }
  const payload = {
    email, label, layers,
    center_lat: _geofenceState.center.lat,
    center_lng: _geofenceState.center.lng,
    radius_km: _geofenceState.radiusM / 1000.0,
    min_severity: minSev,
  };
  fetch('/api/glassbox/watchlist', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      _geofenceShowToast(`✓ Geofence saved · ${label}`);
      setTimeout(_geofenceHideToast, 4000);
    } else {
      alert('Save failed: ' + (data.error || 'unknown'));
    }
  }).catch(e => alert('Network error: ' + e.message));
  if (_geofenceState.active) toggleGeofence();
}

/* ─── Undersea cables layer ─────────────────────────────────────── */
/* Same render pattern as pipelines/trafficking — submarine telecom
   cables color-coded by category:
     trans-atlantic  → cyan
     trans-pacific   → magenta
     asia-europe     → amber (Red Sea risk zone)
     regional        → soft cyan
   Click → entity-detail shows operator, capacity, landing points,
   status (Houthi-cut cables flagged in notes). */
const CABLE_ENT_BY_NAME = new Map();
const CABLE_LAYER_ID = 'cables';
const CABLE_COLOR = {
  'trans-atlantic':  '#7fd0ff',
  'trans-pacific':   '#d770ff',
  'asia-europe':     '#ffd166',
  'regional':        '#a0e0c8',
};

async function loadCables() {
  if (CABLE_ENT_BY_NAME.size > 0) return;
  try {
    const r = await fetch('/api/v1/infrastructure/cables');
    if (!r.ok) {
      console.warn('Cables endpoint returned', r.status);
      return;
    }
    const fc = await r.json();
    const layer = LAYERS.find(L => L.id === CABLE_LAYER_ID) || {};
    for (const feat of fc.features || []) {
      const props = feat.properties || {};
      const coords = feat.geometry && feat.geometry.coordinates;
      if (!coords || coords.length < 2) continue;
      const flat = [];
      for (const c of coords) flat.push(c[0], c[1]);
      const color = Cesium.Color.fromCssColorString(
        CABLE_COLOR[props.category] || '#7fd0ff').withAlpha(0.70);
      const ent = viewer.entities.add({
        polyline: {
          positions: Cesium.Cartesian3.fromDegreesArray(flat),
          width: 1.8,
          material: color,
          clampToGround: true,
        },
        show: !!layer.on,
      });
      ent._glassbox_layer = CABLE_LAYER_ID;
      ent._glassbox_meta = {
        entity_type: 'submarine_cable',
        display_name: props.name,
        title: props.name,
        description:
          `${props.category} · ${props.length_km || '?'} km · ` +
          `${props.capacity_tbps || '?'} Tbps · ` +
          `${props.landing_a || '?'} ↔ ${props.landing_b || '?'} · ` +
          `operator: ${props.operator || '?'} · ${props.status || ''}` +
          (props.notes ? `\n\n${props.notes}` : ''),
        properties: props,
      };
      CABLE_ENT_BY_NAME.set(props.name, ent);
    }
    const cnt = $('cnt-' + CABLE_LAYER_ID);
    if (cnt) cnt.textContent = CABLE_ENT_BY_NAME.size.toLocaleString();
  } catch (e) {
    console.warn('Cables layer load failed:', e);
  }
}

/* ─── Tactical HUD: heading compass + cardinal strip ────────────── */
/* Top-center overlay (above the cockpit, below the topbar) showing:
   - Current camera heading in degrees + nearest cardinal direction
   - A horizontal compass strip with N/NE/E/SE/S/SW/W/NW tick marks
     scrolling under a fixed center indicator as the camera rotates
   Old glassbox had a similar HUD; this is the modernized version.
   Updates via the camera.changed listener (already wired). */
function _setupTacticalHud() {
  const hudEl = $('tactical-hud');
  if (!hudEl) return;
  const headingEl = $('thud-heading');
  const cardEl    = $('thud-cardinal');
  const stripEl   = $('thud-strip');

  function updateHud() {
    if (!viewer || !viewer.camera) return;
    let headingRad = viewer.camera.heading;
    let headingDeg = Cesium.Math.toDegrees(headingRad);
    // Cesium 0° = north; this is what we want.
    headingDeg = ((headingDeg % 360) + 360) % 360;
    const intDeg = Math.round(headingDeg);
    if (headingEl) headingEl.textContent = String(intDeg).padStart(3, '0') + '°';
    // Nearest cardinal
    const cards = ['N','NE','E','SE','S','SW','W','NW'];
    const cIdx = Math.round(headingDeg / 45) % 8;
    if (cardEl) cardEl.textContent = cards[cIdx];
    // Strip: pan in -heading so the rotating ticks slide under the
    // fixed center indicator. Strip is 720px wide showing -180 to +180.
    if (stripEl) stripEl.style.transform = `translateX(${-headingDeg * 2}px)`;
  }
  // Cesium fires camera.changed at the configured cadence (default 0.5).
  // We already set 0.05 elsewhere so this gets called often.
  viewer.camera.changed.addEventListener(updateHud, 0.05);
  updateHud();
}

/* ─── Measure tool: distance + bearing ──────────────────────────── */
/* Click points on the globe → each segment shows distance (km) +
   initial bearing (°). Total path distance shown in the top-bar
   status badge while active. Toggle the MEASURE button (or press 'M')
   to clear and exit. */
const _measureState = {
  active:    false,
  points:    [],   // {lat, lng}
  entities: [],   // Cesium entities created
  totalM:   0,
};

function _haversineMeters(lat1, lng1, lat2, lng2) {
  const R = 6371000;
  const toRad = (d) => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLng = toRad(lng2 - lng1);
  const a = Math.sin(dLat/2) ** 2 +
            Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
            Math.sin(dLng/2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function _initialBearingDeg(lat1, lng1, lat2, lng2) {
  const toRad = (d) => d * Math.PI / 180;
  const toDeg = (r) => r * 180 / Math.PI;
  const φ1 = toRad(lat1), φ2 = toRad(lat2);
  const Δλ = toRad(lng2 - lng1);
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2)
          - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  return (toDeg(Math.atan2(y, x)) + 360) % 360;
}

function _fmtDist(meters) {
  return meters >= 1000
    ? (meters / 1000).toFixed(meters > 100000 ? 0 : 1) + ' km'
    : Math.round(meters) + ' m';
}

function toggleMeasure() {
  _measureState.active = !_measureState.active;
  const btn = $('measure-btn');
  if (btn) {
    btn.classList.toggle('on', _measureState.active);
    btn.style.background = _measureState.active
      ? 'rgba(255, 159, 58, 0.20)'
      : 'rgba(127, 208, 255, 0.08)';
    btn.style.borderColor = _measureState.active
      ? 'var(--warn)'
      : 'rgba(127, 208, 255, 0.30)';
    btn.style.color = _measureState.active ? 'var(--warn)' : 'var(--cyan)';
  }
  if (!_measureState.active) {
    _clearMeasure();
  } else {
    _measureState.points = [];
    _measureState.totalM = 0;
    _updateMeasureStatus();
  }
}

function _clearMeasure() {
  if (!viewer) return;
  for (const ent of _measureState.entities) {
    try { viewer.entities.remove(ent); } catch (_) {}
  }
  _measureState.entities = [];
  _measureState.points   = [];
  _measureState.totalM   = 0;
  _updateMeasureStatus();
}

function _updateMeasureStatus() {
  const badge = $('measure-status');
  if (!badge) return;
  if (!_measureState.active || _measureState.points.length === 0) {
    badge.style.display = 'none';
    badge.textContent = '';
    return;
  }
  badge.style.display = 'inline-block';
  badge.textContent = `${_measureState.points.length} pt · ${_fmtDist(_measureState.totalM)}`;
}

function _setupMeasureTool() {
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction((evt) => {
    if (!_measureState.active) return;
    const ray = viewer.camera.getPickRay(evt.position);
    const cart = viewer.scene.globe.pick(ray, viewer.scene);
    if (!cart) return;
    const c = Cesium.Cartographic.fromCartesian(cart);
    const lat = Cesium.Math.toDegrees(c.latitude);
    const lng = Cesium.Math.toDegrees(c.longitude);
    const ptNum = _measureState.points.length + 1;

    // Point marker
    const ptEnt = viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(lng, lat),
      point: {
        pixelSize: 7,
        color: Cesium.Color.fromCssColorString('#ff9f3a'),
        outlineColor: Cesium.Color.fromCssColorString('#02040a'),
        outlineWidth: 1.5,
        disableDepthTestDistance: 3e6,
      },
      label: {
        text: `P${ptNum}`,
        font: '600 10px "Geist Mono", monospace',
        fillColor: Cesium.Color.fromCssColorString('#ff9f3a'),
        outlineColor: Cesium.Color.fromCssColorString('#02040a'),
        outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        pixelOffset: new Cesium.Cartesian2(0, -12),
        disableDepthTestDistance: 3e6,
      },
    });
    _measureState.entities.push(ptEnt);

    // Segment line + distance/bearing label
    if (_measureState.points.length > 0) {
      const prev = _measureState.points[_measureState.points.length - 1];
      const segM = _haversineMeters(prev.lat, prev.lng, lat, lng);
      const bearing = _initialBearingDeg(prev.lat, prev.lng, lat, lng);
      _measureState.totalM += segM;

      const lineEnt = viewer.entities.add({
        polyline: {
          positions: Cesium.Cartesian3.fromDegreesArray(
            [prev.lng, prev.lat, lng, lat]),
          width: 2,
          material: new Cesium.PolylineGlowMaterialProperty({
            glowPower: 0.18,
            color: Cesium.Color.fromCssColorString('#ff9f3a'),
          }),
          clampToGround: true,
        },
      });
      _measureState.entities.push(lineEnt);

      const midLat = (prev.lat + lat) / 2;
      const midLng = (prev.lng + lng) / 2;
      const segText = `${_fmtDist(segM)} · ${bearing.toFixed(0)}°`;
      const segLabel = viewer.entities.add({
        position: Cesium.Cartesian3.fromDegrees(midLng, midLat, 1000),
        label: {
          text: segText,
          font: '600 10px "Geist Mono", monospace',
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.fromCssColorString('#02040a'),
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          showBackground: true,
          backgroundColor: Cesium.Color.fromCssColorString('rgba(6,10,20,0.88)'),
          backgroundPadding: new Cesium.Cartesian2(4, 2),
          disableDepthTestDistance: 3e6,
        },
      });
      _measureState.entities.push(segLabel);
    }

    _measureState.points.push({ lat, lng });
    _updateMeasureStatus();
    viewer.scene.requestRender();
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // Wire button + 'M' shortcut
  const btn = $('measure-btn');
  if (btn) btn.addEventListener('click', toggleMeasure);
  document.addEventListener('keydown', (ev) => {
    if (ev.key !== 'm' && ev.key !== 'M') return;
    const t = ev.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
              t.isContentEditable)) return;
    toggleMeasure();
  });
}

/* ─── Hotspots: 10 preset regional flyto bookmarks ──────────────── */
/* Click a hotspot → camera flies to that geo over 2s. Same set as the
   original glassbox; chosen for being the highest-density intel
   regions worldwide. Altitudes tuned so the destination view shows
   relevant context (city-scale for capitals, region-scale for
   theaters of interest). */
const HOTSPOTS = [
  { name: 'Washington DC',     lat: 38.90,  lng: -77.04, alt:   600000 },
  { name: 'Moscow',            lat: 55.75,  lng:  37.62, alt:   600000 },
  { name: 'Beijing',           lat: 39.91,  lng: 116.40, alt:   600000 },
  { name: 'Pyongyang',         lat: 39.02,  lng: 125.74, alt:   500000 },
  { name: 'Middle East',       lat: 30.00,  lng:  44.00, alt:  3000000 },
  { name: 'Persian Gulf',      lat: 27.00,  lng:  52.00, alt:  1500000 },
  { name: 'Strait of Hormuz',  lat: 26.55,  lng:  56.25, alt:   600000 },
  { name: 'Ukraine / Black Sea', lat: 47.00, lng:  34.00, alt:  1800000 },
  { name: 'Baltic Sea',        lat: 58.50,  lng:  20.00, alt:  1800000 },
  { name: 'Taiwan Strait',     lat: 24.00,  lng: 120.00, alt:  1000000 },
  { name: 'South China Sea',   lat: 12.00,  lng: 115.00, alt:  3000000 },
  { name: 'Strait of Malacca', lat:  2.50,  lng: 102.50, alt:   900000 },
  { name: 'Horn of Africa',    lat:  5.00,  lng:  45.00, alt:  3000000 },
  { name: 'Red Sea',           lat: 22.00,  lng:  38.00, alt:  2200000 },
  { name: 'Mediterranean',     lat: 36.00,  lng:  18.00, alt:  3000000 },
  { name: 'Arctic',            lat: 85.00,  lng:   0.00, alt:  5000000 },
];

function initHotspots() {
  const body = $('hotspots-body');
  if (!body) return;
  body.innerHTML = HOTSPOTS.map((h, i) => `
    <button class="hs-item" data-i="${i}">
      <span class="hs-name">${h.name}</span>
      <span class="hs-coord">${h.lat.toFixed(2)}, ${h.lng.toFixed(2)}</span>
    </button>`).join('');
  body.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.hs-item');
    if (!btn || !viewer) return;
    const idx = parseInt(btn.getAttribute('data-i'), 10);
    const h = HOTSPOTS[idx];
    if (!h) return;
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(h.lng, h.lat, h.alt),
      duration: 2.0,
      easingFunction: Cesium.EasingFunction.CUBIC_IN_OUT,
    });
    if (window.gbtrack) window.gbtrack('hotspot_flyto', {name: h.name});
  });
}

/* ─── View modes: NVG / FLIR / CRT / Tactical ──────────────────────
 * Ported from the original glassbox_v2 (pre-cockpit-rewrite). Pure CSS
 * filter pipeline applied to #cesiumContainer — reliable everywhere,
 * GPU-accelerated, zero runtime cost when in 'normal'. Cycle with the
 * top-bar button or press 'V'. Choice persists across reloads. */
const VIEW_MODES = [
  { id: 'normal',   label: 'Normal',  filter: 'none',
    badge: '#7fd0ff', badgeBg: 'rgba(127,208,255,0.15)' },
  { id: 'nvg',      label: 'NVG',     // green night-vision
    filter: 'saturate(0.2) brightness(1.3) sepia(1) hue-rotate(70deg) contrast(1.2)',
    badge: '#54e29c', badgeBg: 'rgba(84,226,156,0.20)' },
  { id: 'flir',     label: 'FLIR',    // orange thermal
    filter: 'saturate(0) brightness(1.2) contrast(1.5) sepia(1) hue-rotate(-15deg)',
    badge: '#ff9f3a', badgeBg: 'rgba(255,159,58,0.22)' },
  { id: 'crt',      label: 'CRT',     // amber retro
    filter: 'saturate(0.6) brightness(0.9) contrast(1.15) sepia(0.25) hue-rotate(10deg)',
    badge: '#ffd166', badgeBg: 'rgba(255,209,102,0.18)' },
  { id: 'tactical', label: 'Tactical',// hard-edge high-contrast
    filter: 'contrast(1.45) saturate(1.6) brightness(0.92)',
    badge: '#ff5b5b', badgeBg: 'rgba(255,91,91,0.20)' },
];
const VIEW_MODE_KEY = 'glassbox.viewmode.v1';
let _viewModeIdx = 0;

function applyViewMode() {
  const mode = VIEW_MODES[_viewModeIdx];
  const c = document.getElementById('cesiumContainer');
  if (c) c.style.filter = mode.filter;
  const badge = $('view-mode-badge');
  if (badge) {
    badge.textContent = mode.label.toUpperCase();
    badge.style.color  = mode.badge;
    badge.style.background = mode.badgeBg;
    badge.style.borderColor = mode.badge;
    badge.style.display = mode.id === 'normal' ? 'none' : 'inline-block';
  }
  try { localStorage.setItem(VIEW_MODE_KEY, mode.id); } catch (_) {}
  if (viewer && viewer.scene) viewer.scene.requestRender();
}

function cycleViewMode() {
  _viewModeIdx = (_viewModeIdx + 1) % VIEW_MODES.length;
  applyViewMode();
  if (window.gbtrack) window.gbtrack('view_mode_cycle',
    {mode: VIEW_MODES[_viewModeIdx].id});
}

function initViewModes() {
  // Restore persisted choice
  try {
    const saved = localStorage.getItem(VIEW_MODE_KEY);
    if (saved) {
      const idx = VIEW_MODES.findIndex(m => m.id === saved);
      if (idx >= 0) _viewModeIdx = idx;
    }
  } catch (_) {}
  applyViewMode();
  // Wire button (if rendered by index.html)
  const btn = $('view-mode-btn');
  if (btn) btn.addEventListener('click', cycleViewMode);
  // Keyboard: V cycles
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'v' || ev.key === 'V') {
      // Ignore if typing in an input
      const t = ev.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
                t.isContentEditable)) return;
      cycleViewMode();
    }
  });
}

/* ─── Boot ──────────────────────────────────────────────────────── */
async function boot() {
  maybeShowOnboarding();
  initLayersPanel();
  renderWebcams('world');
  await _initCesiumToken();
  await initCesium();
  bootClocks();
  wirePicker();
  wireNewsTabs();
  wireWebcamTabs();
  wireSearch();
  initPanelWindowing();      // drag + resize + min/max/close + persist
  wire3DTilesToggle();       // Google Photorealistic 3D Tiles via Cesium Ion
  initSatellites();          // SGP4-propagated orbital layer
  initViewModes();           // NVG / FLIR / CRT / Tactical cycle
  _setupEntityClick();       // click-to-inspect on any pin/billboard
  initHotspots();            // 16 preset regional flyto bookmarks
  _setupMeasureTool();       // distance + bearing measurement (M to toggle)
  _setupTacticalHud();       // heading compass strip top-center
  loadPipelines();           // strategic oil + gas pipelines (one-shot fetch)
  loadTrafficking();         // drug / human / arms trafficking corridors
  loadCables();              // submarine telecom cables
  loadMilitaryBases();       // strategic military installations
  loadNuclearSites();        // reactors + enrichment + weapons labs
  loadConflictZones();       // P2-B: ongoing armed-conflict / insurgency / terror zones
  loadDiplomaticPosts();     // P2-B: major diplomatic clusters worldwide
  loadUNMissions();          // P2-B: active UN peacekeeping + political + observer missions + HQ
  loadDisputedZones();       // P2-B: sovereignty contests, occupied regions, strategic flashpoints
  loadStateMedia();          // P2-B: state-owned/funded media + documented disinfo ops
  loadSanctionTargets();     // P2-B: country-level sanctioned regimes (strategic overlay)
  loadNoaaBuoys();           // P2-B: NDBC ocean monitoring buoy locations (curated subset)
  loadClimateForecast();     // P2-B: 15-city climate snapshot (static seed; Open-Meteo live ingester is a follow-on)
  _loadDensityHeatmap();     // optional 5°-grid event density (opt-in via ?heat=1)
  _loadCyberKev();           // P2-A cyber layer: CISA KEV (opt-in via ?kev=1)
  _loadCyberSpamhaus();      // P2-A cyber layer: Spamhaus DROP/EDROP (opt-in via ?spamhaus=1)
  _setupGeofenceDrawing();   // operator-drawn watchlist circles (G to toggle)
  // Hide the loader IMMEDIATELY — Cesium is ready (init succeeded
  // above), the globe + tileset are visible, all interactive features
  // are wired. Data fetched via loadAll() can populate panels as it
  // arrives without blocking the user. Previously `await loadAll()`
  // before hideLoader() left the cockpit stuck on the splash for 20+s
  // (cold-cache /signals/today + viewport calls); operator sees a
  // blank screen and assumes "nothing works."
  hideLoader();
  // Fire-and-forget data load; SSE keeps everything fresh after.
  loadAll().catch(e => console.warn('loadAll partial failure:', e));
  startSSE();
  loadBrief(); setInterval(loadBrief, 5 * 60 * 1000);
}

/* ─── Panel windowing: drag / resize / min / max / close / persist ──
 * Each `.panel[data-panel-id]` becomes a draggable + resizable window.
 * Position + size + minimized/maximized/hidden state persist to
 * localStorage under "glassbox.layout.v1". interact.js (loaded by
 * index.html) does the pointer math. */
const LAYOUT_KEY = 'glassbox.layout.v1';

function _readLayout() {
  try { return JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{}'); }
  catch (_) { return {}; }
}

function _writeLayout(state) {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(state)); }
  catch (_) { /* quota or private mode — silent */ }
}

function _persistPanel(id, patch) {
  const s = _readLayout();
  s[id] = Object.assign({}, s[id] || {}, patch);
  _writeLayout(s);
}

function initPanelWindowing() {
  const saved = _readLayout();

  // Restore persisted positions/sizes/states first
  document.querySelectorAll('.panel[data-panel-id]').forEach(panel => {
    const id = panel.dataset.panelId;
    const s = saved[id] || {};
    if (s.x != null && s.y != null) {
      panel.style.left = s.x + 'px';
      panel.style.top  = s.y + 'px';
      panel.style.right = 'auto'; panel.style.bottom = 'auto';
      panel.dataset.x = s.x; panel.dataset.y = s.y;
    }
    if (s.w) panel.style.width  = s.w + 'px';
    if (s.h) panel.style.height = s.h + 'px';
    if (s.minimized) panel.classList.add('minimized');
    if (s.maximized) panel.classList.add('maximized');
    if (s.hidden)    panel.classList.add('hidden');
  });

  // Wire window-control buttons (min / max / close)
  document.querySelectorAll('.panel[data-panel-id] .head .ctl button')
    .forEach(btn => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const panel = ev.target.closest('.panel');
        const id = panel.dataset.panelId;
        if (btn.classList.contains('min')) {
          panel.classList.toggle('minimized');
          panel.classList.remove('maximized');
          _persistPanel(id, {
            minimized: panel.classList.contains('minimized'),
            maximized: false,
          });
        } else if (btn.classList.contains('max')) {
          panel.classList.toggle('maximized');
          panel.classList.remove('minimized');
          _persistPanel(id, {
            maximized: panel.classList.contains('maximized'),
            minimized: false,
          });
        } else if (btn.classList.contains('close')) {
          panel.classList.add('hidden');
          _persistPanel(id, { hidden: true });
          _refreshLayoutChecks();
        }
      });
    });

  // Vanilla drag + resize (no third-party CDN dependency).
  // Pointer events handle mouse + touch + pen uniformly.
  document.querySelectorAll('.panel[data-panel-id]').forEach(panel => {
    _makePanelDraggable(panel);
    _makePanelResizable(panel);
  });

  // Layout menu: toggle visibility per panel + reset
  const menu    = $('layout-menu');
  const trigger = $('layout-trigger');
  const pop     = $('layout-pop');
  if (trigger && menu && pop) {
    trigger.addEventListener('click', (ev) => {
      ev.stopPropagation();
      menu.classList.toggle('open');
    });
    document.addEventListener('click', (ev) => {
      if (!menu.contains(ev.target)) menu.classList.remove('open');
    });
    pop.addEventListener('click', (ev) => {
      const item = ev.target.closest('[data-toggle], [data-action]');
      if (!item) return;
      if (item.dataset.action === 'reset') {
        localStorage.removeItem(LAYOUT_KEY);
        location.reload();
        return;
      }
      if (item.dataset.action === 'toggle-tactical') {
        document.body.classList.toggle('show-tactical');
        const on = document.body.classList.contains('show-tactical');
        const s = _readLayout(); s._tactical = on; _writeLayout(s);
        _refreshLayoutChecks();
        return;
      }
      const id = item.dataset.toggle;
      const panel = document.querySelector(`.panel[data-panel-id="${id}"]`);
      if (!panel) return;
      panel.classList.toggle('hidden');
      _persistPanel(id, { hidden: panel.classList.contains('hidden') });
      _refreshLayoutChecks();
    });
    // Restore tactical-overlay preference if previously toggled on
    if (saved._tactical) document.body.classList.add('show-tactical');
    _refreshLayoutChecks();
  }
}

function _refreshLayoutChecks() {
  document.querySelectorAll('#layout-pop .check').forEach(c => {
    if (c.hasAttribute('data-tactical')) {
      c.style.visibility = document.body.classList.contains('show-tactical')
        ? 'visible' : 'hidden';
      return;
    }
    const panel = document.querySelector(`.panel[data-panel-id="${c.dataset.id}"]`);
    if (!panel) return;
    c.style.visibility = panel.classList.contains('hidden') ? 'hidden' : 'visible';
  });
}

/* ─── Vanilla drag implementation ─────────────────────────────────── */
/* Pointer-events drag from the panel head. Skips clicks that originate
   on the window-controls strip, buttons, links, selects — those are
   handled by their own listeners. Cancels maximized state on drag.
   Persists final {x,y} to localStorage on pointer-up. */
function _makePanelDraggable(panel) {
  const head = panel.querySelector('.head');
  if (!head) return;
  let startX = 0, startY = 0, startLeft = 0, startTop = 0, dragging = false;

  function onDown(ev) {
    if (ev.button !== 0 && ev.pointerType === 'mouse') return;
    if (ev.target.closest('.ctl, button, a, select, input, textarea')) return;
    panel.classList.remove('maximized');
    _persistPanel(panel.dataset.panelId, { maximized: false });
    const rect = panel.getBoundingClientRect();
    startX = ev.clientX; startY = ev.clientY;
    startLeft = rect.left; startTop = rect.top;
    panel.style.left = startLeft + 'px';
    panel.style.top  = startTop + 'px';
    panel.style.right = 'auto'; panel.style.bottom = 'auto';
    panel.style.zIndex = '40';   // raise above sibling panels while dragging
    dragging = true;
    head.setPointerCapture && head.setPointerCapture(ev.pointerId);
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
    document.addEventListener('pointercancel', onUp);
    ev.preventDefault();
  }

  function onMove(ev) {
    if (!dragging) return;
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    // Constrain so the panel header stays at least 20px on-screen.
    const w = panel.offsetWidth;
    const h = panel.offsetHeight;
    const x = Math.max(20 - w, Math.min(window.innerWidth - 20, startLeft + dx));
    const y = Math.max(0, Math.min(window.innerHeight - 28, startTop + dy));
    panel.style.left = x + 'px';
    panel.style.top  = y + 'px';
  }

  function onUp(ev) {
    if (!dragging) return;
    dragging = false;
    panel.style.zIndex = '';
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    document.removeEventListener('pointercancel', onUp);
    _persistPanel(panel.dataset.panelId, {
      x: parseFloat(panel.style.left) || 0,
      y: parseFloat(panel.style.top) || 0,
    });
  }

  head.addEventListener('pointerdown', onDown);
}

/* ─── Vanilla resize implementation ─────────────────────────────────
 * Eight invisible handles per panel: 4 edges (n/e/s/w) + 4 corners
 * (ne/nw/se/sw). Each handle drags only the dimensions it controls.
 * Min size is 180x60 so panels never collapse to nothing. */
const _RESIZE_HANDLES = ['n','e','s','w','ne','nw','se','sw'];

function _makePanelResizable(panel) {
  for (const dir of _RESIZE_HANDLES) {
    const h = document.createElement('div');
    h.className = 'rh ' + dir;
    panel.appendChild(h);
    h.addEventListener('pointerdown', (ev) => _onResizeDown(ev, panel, dir, h));
  }
}

function _onResizeDown(ev, panel, dir, handle) {
  if (ev.button !== 0 && ev.pointerType === 'mouse') return;
  ev.stopPropagation();
  ev.preventDefault();
  panel.classList.remove('maximized');
  _persistPanel(panel.dataset.panelId, { maximized: false });
  const rect = panel.getBoundingClientRect();
  const startX = ev.clientX, startY = ev.clientY;
  const startLeft = rect.left, startTop = rect.top;
  const startW = rect.width, startH = rect.height;
  panel.style.left = startLeft + 'px';
  panel.style.top  = startTop + 'px';
  panel.style.right = 'auto'; panel.style.bottom = 'auto';
  panel.style.width  = startW + 'px';
  panel.style.height = startH + 'px';
  handle.setPointerCapture && handle.setPointerCapture(ev.pointerId);

  function onMove(e) {
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    let nL = startLeft, nT = startTop, nW = startW, nH = startH;
    if (dir.indexOf('e') >= 0) nW = Math.max(180, startW + dx);
    if (dir.indexOf('s') >= 0) nH = Math.max(60,  startH + dy);
    if (dir.indexOf('w') >= 0) {
      const cap = Math.max(180, startW - dx);
      nL = startLeft + (startW - cap);
      nW = cap;
    }
    if (dir.indexOf('n') >= 0) {
      const cap = Math.max(60, startH - dy);
      nT = startTop + (startH - cap);
      nH = cap;
    }
    panel.style.left = nL + 'px';
    panel.style.top  = nT + 'px';
    panel.style.width  = nW + 'px';
    panel.style.height = nH + 'px';
  }
  function onUp() {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    document.removeEventListener('pointercancel', onUp);
    _persistPanel(panel.dataset.panelId, {
      x: parseFloat(panel.style.left)   || 0,
      y: parseFloat(panel.style.top)    || 0,
      w: parseFloat(panel.style.width)  || 0,
      h: parseFloat(panel.style.height) || 0,
    });
  }
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', onUp);
  document.addEventListener('pointercancel', onUp);
}

/* ─── Google Photorealistic 3D Tiles toggle ──────────────────────────
 * Uses Cesium Ion's bundled Google 3D Tiles (asset 2275207) — no
 * separate Google Cloud key, no separate billing. Cost is bundled
 * into the Cesium Ion plan we already have. Same approach the user
 * is using in their other Cesium project. When ON, the standard
 * Bing Maps imagery layer is hidden because Google's tileset
 * includes its own ground texture and they fight visually. */
let GOOGLE_3D_TILESET = null;

async function toggle3DTiles() {
  const btn = $('toggle-3d');
  if (!viewer) return;
  if (!GOOGLE_3D_TILESET) {
    btn.textContent = '🌐 loading…';
    try {
      GOOGLE_3D_TILESET = await Cesium.createGooglePhotorealistic3DTileset();
      viewer.scene.primitives.add(GOOGLE_3D_TILESET);
      // Keep base imagery visible — see _autoEnable3DTiles comment for why.
      viewer.scene.globe.depthTestAgainstTerrain = true;
      btn.classList.add('on');
      btn.textContent = '🌐 3D Tiles';
      btn.title = '3D Tiles ON — zoom below ~3000km to see buildings';
      // Auto-fly to a city-scale view so the tiles are immediately visible
      // (without this, user is at 18000km altitude where 0 tiles render
      // and it looks like the toggle did nothing).
      viewer.camera.flyTo({
        destination: Cesium.Cartesian3.fromDegrees(20, 28, 2_000_000),
        duration: 2.0,
        easingFunction: Cesium.EasingFunction.CUBIC_IN_OUT,
      });
    } catch (e) {
      console.error('Google 3D Tiles via Cesium Ion failed:', e);
      GOOGLE_3D_TILESET = null;
      btn.textContent = '🌐 unavail';
      btn.title = 'Google 3D Tiles unavailable — check Cesium Ion token quota';
      setTimeout(() => { btn.textContent = '🌐 3D Tiles'; }, 4000);
    }
  } else {
    viewer.scene.primitives.remove(GOOGLE_3D_TILESET);
    GOOGLE_3D_TILESET = null;
    // Keep depthTestAgainstTerrain=true even when 3D Tiles are off — the
    // globe still needs to occlude back-side pins. (Was flipping false
    // here, which re-introduced the "transparent globe" bleed-through.)
    viewer.scene.globe.depthTestAgainstTerrain = true;
    btn.classList.remove('on');
    btn.textContent = '🌐 3D Tiles';
    btn.title = 'Switch to Google Photorealistic 3D Tiles via Cesium Ion';
  }
  viewer.scene.requestRender();
}

function wire3DTilesToggle() {
  const btn = $('toggle-3d');
  if (btn) btn.addEventListener('click', toggle3DTiles);
}

/* ─── Satellites layer (SGP4 via Web Worker) ─────────────────────────
 * Spawns satellites_worker.js which loads satellite.js (MIT, self-
 * hosted), parses TLE from /api/v1/satellites/tle (server-cached
 * AMSAT proxy with celestrak fallback), and propagates positions
 * every 30 seconds. Renders each satellite as a small cyan point
 * clamped at its altitude_km — orbits visibly drift across the globe
 * over ~minutes. */
let SAT_WORKER = null;
const SAT_ENT_BY_NAME = new Map();
const SAT_LAYER_ID = 'satellites';

function initSatellites() {
  const layer = LAYERS.find(L => L.id === SAT_LAYER_ID);
  if (!layer) return;
  try {
    SAT_WORKER = new Worker('/satellites_worker.js');
    SAT_WORKER.onmessage = (ev) => {
      const m = ev.data || {};
      if (m.type === 'ready') {
        const cnt = $('cnt-' + SAT_LAYER_ID);
        if (cnt) cnt.textContent = m.count.toLocaleString();
      } else if (m.type === 'positions') {
        _paintSatellites(m.sats);
      } else if (m.type === 'error') {
        console.warn('Satellite worker error:', m.msg);
      }
    };
    SAT_WORKER.postMessage({ cmd: 'init', tleUrl: '/api/v1/satellites/tle' });
  } catch (e) {
    console.warn('Satellite worker unavailable:', e);
  }
}

function _paintSatellites(sats) {
  if (!viewer) return;
  const enabled = (LAYERS.find(L => L.id === SAT_LAYER_ID) || {}).on;
  const seen = new Set();
  for (const s of sats) {
    seen.add(s.name);
    const cart = Cesium.Cartesian3.fromDegrees(s.lng, s.lat, s.alt_km * 1000);
    let ent = SAT_ENT_BY_NAME.get(s.name);
    if (!ent) {
      ent = viewer.entities.add({
        position: cart,
        point: {
          pixelSize: 4,
          color: Cesium.Color.fromCssColorString('#7fd0ff').withAlpha(0.85),
          outlineColor: Cesium.Color.fromCssColorString('#02040a'),
          outlineWidth: 1,
          /* 3e6m (~3000km from camera) keeps near pins crisp + lets the
   globe properly occlude back-side pins. Higher values cause the
   classic bleed-through bug where pins from the far side of the
   planet show up on the near side when the camera is tilted. */
disableDepthTestDistance: 3e6,
        },
        label: {
          text: s.name,
          font: '600 9px "Geist Mono", ui-monospace, monospace',
          fillColor: Cesium.Color.fromCssColorString('#7fd0ff'),
          outlineColor: Cesium.Color.fromCssColorString('#02040a'),
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(8, 0),
          horizontalOrigin: Cesium.HorizontalOrigin.LEFT,
          verticalOrigin: Cesium.VerticalOrigin.CENTER,
          scale: 0.9,
          // Hide label until camera zooms in (>1500km altitude shows count only)
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 5_000_000),
          showBackground: true,
          backgroundColor: new Cesium.Color(0.01, 0.02, 0.04, 0.55),
          backgroundPadding: new Cesium.Cartesian2(4, 2),
        },
      });
      ent._glassbox_layer = SAT_LAYER_ID;
      ent._glassbox_meta  = { entity_type: 'satellite', name: s.name, ...s };
      SAT_ENT_BY_NAME.set(s.name, ent);
    } else {
      // Smooth from previous → current over the 30s tick
      const oldPos = ent.position.getValue(Cesium.JulianDate.now());
      if (oldPos && !Cesium.Cartesian3.equalsEpsilon(oldPos, cart, 1e-3)) {
        ent.position = animateBetween(oldPos, cart, 30.0);
      } else {
        ent.position = cart;
      }
      ent._glassbox_meta.lat = s.lat;
      ent._glassbox_meta.lng = s.lng;
      ent._glassbox_meta.alt_km = s.alt_km;
    }
    ent.show = enabled;
  }
  // Drop any sats that disappeared from the propagation set
  for (const [name, ent] of SAT_ENT_BY_NAME.entries()) {
    if (!seen.has(name)) {
      viewer.entities.remove(ent);
      SAT_ENT_BY_NAME.delete(name);
    }
  }
  viewer.scene.requestRender();
}

/* ─── Cesium init ────────────────────────────────────────────────── */
/* Mirrors the Meridian project's working pattern (the user's other
   Cesium project where Google 3D Tiles render perfectly). Differences
   from prior Glassbox attempts:
     1. Token is awaited BEFORE Viewer construction (race fix)
     2. NO requestRenderMode — Photoreal tiles stream async; that flag
        causes them to never paint until the next camera move
     3. NO requestVertexNormals/requestWaterMask — they inflate every
        terrain tile and double-light the Photoreal geometry
     4. globe.enableLighting OFF — Photoreal tiles ship pre-lit
     5. depthTestAgainstTerrain OFF — Photoreal carries its own ground;
        z-fighting at tile edges is the symptom of leaving this on */
async function initCesium() {
  // Token must be live before constructing the viewer (race fix —
  // if Cesium.Ion.defaultAccessToken is empty when Viewer() runs,
  // every Ion request including the tileset will 401).
  await _initCesiumToken();

  let terrainProvider;
  try {
    terrainProvider = await Cesium.createWorldTerrainAsync();
  } catch (e) {
    console.warn('Cesium World Terrain unavailable, falling back to ellipsoid:', e);
    terrainProvider = new Cesium.EllipsoidTerrainProvider();
  }

  viewer = new Cesium.Viewer('cesiumContainer', {
    animation: false, timeline: false, geocoder: false,
    homeButton: false, sceneModePicker: false, navigationHelpButton: false,
    fullscreenButton: false, baseLayerPicker: false, infoBox: false,
    selectionIndicator: false,
    contextOptions: { webgl: { alpha: true } },
    terrainProvider,
  });
  // **Base imagery: Sentinel-2 Cloudless 2024 from EOX::Maps**, with
  // NASA GIBS MODIS true-color as today's daily-updated overlay, and
  // OSM as final fallback. Replaces Bing-via-Ion-asset-3 which was a
  // lazy default — both Sentinel-2 Cloudless and NASA GIBS are free
  // for commercial use under attribution, and Sentinel-2's 10m-
  // resolution cloudless mosaic looks visibly better than Bing's
  // photo-stitched aerial. Plus the GIBS daily overlay is a
  // differentiator the old glassbox lacked entirely.
  try {
    const sentinel2 = new Cesium.WebMapTileServiceImageryProvider({
      url: 'https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{TileMatrix}/{TileRow}/{TileCol}.jpg',
      layer: 's2cloudless-2024_3857',
      style: 'default',
      format: 'image/jpeg',
      tileMatrixSetID: 'g',
      maximumLevel: 17,
      credit: new Cesium.Credit(
        'Sentinel-2 cloudless 2024 by <a href="https://s2maps.eu">EOX::Maps</a> '
        + '(Contains modified Copernicus Sentinel data 2024)',
        true,
      ),
    });
    viewer.scene.imageryLayers.removeAll();
    viewer.scene.imageryLayers.addImageryProvider(sentinel2);
    console.info('[imagery] Sentinel-2 Cloudless 2024 (EOX::Maps) loaded');
  } catch (e) {
    console.warn('Sentinel-2 cloudless failed; trying NASA GIBS', e);
    try {
      // NASA GIBS — MODIS Terra true-color, refreshed daily, public
      // domain. Yesterday's date avoids the gap when today's tiles
      // haven't been processed yet.
      const yest = new Date(Date.now() - 24*3600*1000).toISOString().slice(0,10);
      const gibs = new Cesium.WebMapTileServiceImageryProvider({
        url: `https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/MODIS_Terra_CorrectedReflectance_TrueColor/default/${yest}/250m/{TileMatrix}/{TileRow}/{TileCol}.jpg`,
        layer: 'MODIS_Terra_CorrectedReflectance_TrueColor',
        style: 'default',
        format: 'image/jpeg',
        tileMatrixSetID: '250m',
        maximumLevel: 8,
        credit: new Cesium.Credit(
          'NASA EOSDIS GIBS — MODIS Terra Corrected Reflectance', true,
        ),
      });
      viewer.scene.imageryLayers.removeAll();
      viewer.scene.imageryLayers.addImageryProvider(gibs);
      console.info('[imagery] NASA GIBS MODIS Terra loaded');
    } catch (e2) {
      console.warn('NASA GIBS failed; trying OSM fallback', e2);
      try {
        const osm = new Cesium.OpenStreetMapImageryProvider({
          url: 'https://tile.openstreetmap.org/',
        });
        viewer.scene.imageryLayers.removeAll();
        viewer.scene.imageryLayers.addImageryProvider(osm);
      } catch (e3) {
        console.warn('OSM fallback also failed:', e3);
      }
    }
  }
  // Debug handle — lets devtools + the Chrome MCP probe entity state,
  // depth-test values, animation state, etc. without instrumenting the
  // code each time. Read-only by convention; do not mutate from here.
  window.__atlas = {
    get viewer() { return viewer; },
    get entByKey() { return ENT_BY_KEY; },
    get satByName() { return SAT_ENT_BY_NAME; },
    get tileset() { return GOOGLE_3D_TILESET; },
    showDetail: (e) => showEntityDetail(e),
    hideDetail: () => hideEntityDetail(),
  };
  viewer.scene.globe.enableLighting       = false;  // Photoreal is pre-lit
  // depthTestAgainstTerrain = TRUE so the globe + terrain occlude billboards
  // on the far side of the planet. With this off, every entity pin renders
  // through the globe (the "transparent globe / opposite-side bleed-through"
  // bug). At Photoreal-altitude there can be minor z-fighting at tile edges
  // but that's far better than seeing back-side pins on the front.
  // Combined with each entity's disableDepthTestDistance=3e6, close-up pins
  // are still always visible while far ones get occluded.
  viewer.scene.globe.depthTestAgainstTerrain = true;
  // **CRITICAL for entity motion**: Cesium's clock defaults to
  // shouldAnimate=false in viewer constructor when `animation: false`.
  // Without this, SampledPositionProperty entities never interpolate —
  // planes/ships sit motionless even when their position has 2+ samples
  // with EXTRAPOLATE forward. Verified by Chrome MCP audit 2026-05-14:
  // aircraft positions identical after 6s wait. Setting shouldAnimate=true
  // here is the missing piece that makes the "moving entities" promise
  // actually deliver.
  viewer.clock.shouldAnimate = true;
  viewer.clock.multiplier = 1.0;
  // CRITICAL FIX 2026-05-14: even with shouldAnimate=true + useDefault
  // RenderLoop=true, Cesium 1.120 was NOT auto-ticking the clock from
  // its render loop on this page. Verified empirically: manual
  // viewer.clock.tick() advanced currentTime by the full elapsed wall
  // time of the page (4+ minutes accumulated in a single tick) even
  // when the page had been alive and rendering for that whole period.
  // Force a 10Hz manual tick so SampledPositionProperty interpolation
  // actually progresses. requestRender() pairs with it to ensure the
  // visual updates.
  //
  // P1-E (2026-05-20) investigation: Cesium 1.120's
  // CesiumWidget.render() source DOES call this._clock.tick() on
  // every frame regardless of _canRender state, so the source-of-truth
  // contract says it should auto-tick. The 2026-05-14 symptom (4-min
  // accumulated delta on manual tick) most likely came from a transient
  // race during init — possibly imagery provider promise resolution,
  // or a frame skipped between Viewer construction at line 1611 and
  // the shouldAnimate=true assignment 95 lines later (during which
  // shouldAnimate could briefly be false and _lastSystemTime not
  // updated). The empirically-reproduced "stuck planes" symptom was
  // the actionable evidence; the workaround pinned the fix. Per backlog
  // P1-E: documenting the workaround + adding a regression detector is
  // the chosen disposition. Removal protocol: launch the page with
  // window.__atlas_test_no_tick=true in console BEFORE init, observe
  // entity motion for ≥30s, and only then delete the setInterval.
  const _atlasManualTick = setInterval(() => {
    viewer.clock.tick();
    viewer.scene.requestRender();
  }, 100);
  // Boot-time regression detector: if the manual tick stops advancing
  // the clock (e.g., a future refactor breaks `viewer` reference, or
  // Cesium changes Clock.tick semantics), we'd silently regress to
  // motionless entities — exactly the bug the workaround was added to
  // fix. Sample currentTime at boot + 5s, again at boot + 10s; warn
  // loudly if the delta is <3s (expected ~5s at multiplier=1.0).
  // Single-shot — only meaningful at startup, not worth a recurring cost.
  setTimeout(() => {
    const t0 = Cesium.JulianDate.clone(viewer.clock.currentTime);
    setTimeout(() => {
      const dt = Cesium.JulianDate.secondsDifference(
        viewer.clock.currentTime, t0);
      if (dt < 3.0) {
        console.warn(
          `[clock-tick] REGRESSION: viewer.clock advanced only ${dt.toFixed(2)}s `
          + `in 5s wall-clock — the P1-E manual-tick workaround appears `
          + `broken. Entities will not interpolate. Check that the setInterval `
          + `at atlas.js:~1718 is still firing and viewer/scene are valid.`);
      } else {
        console.info(
          `[clock-tick] OK: viewer.clock advanced ${dt.toFixed(2)}s in 5s `
          + `(workaround active, see P1-E in GLASSBOX_BACKEND_BACKLOG.md).`);
      }
    }, 5000);
  }, 5000);
  viewer.scene.fog.enabled  = true;
  viewer.scene.fog.density  = 0.00006;
  viewer.scene.backgroundColor = Cesium.Color.fromCssColorString('#02040a');
  /* No imagery brightness/saturation/contrast tweak — the user
     explicitly rejected the dark-military-look treatment. Stock
     Bing Maps imagery + 3D terrain reads as a real-world atlas. */

  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(20, 28, 18000000),
    duration: 0,
  });

  viewer.camera.changed.addEventListener(updateCamPos, 0.05);
  viewer.camera.changed.addEventListener(updateScaleBar, 0.05);
  updateCamPos();
  updateScaleBar();

  /* Subtle graticule + hover card stay (analyst utility, not military
     cosplay). Range rings stay (left-click to drop). The corner
     brackets + center reticle in the HTML have CSS opacity 0 by
     default now — turn on via the layout menu if anyone wants them. */
  _drawGraticule();
  _setupRangeRings();
  _setupHoverCard();

  /* Auto-enable Google Photorealistic 3D Tiles on boot. The user has
     this working in another Cesium project under the same Ion plan.
     If the token doesn't have asset access for 2275207, fall back
     silently to the standard Bing imagery. */
  await _autoEnable3DTiles();
}

async function _autoEnable3DTiles() {
  try {
    /* Ion-asset path (Meridian's working pattern). Asset 2275207 is
       Cesium Ion's bundled Google Photorealistic 3D Tiles.
       The createGooglePhotorealistic3DTileset() helper hits Google's
       Map Tiles API directly and was failing silently in production. */
    GOOGLE_3D_TILESET = await Cesium.Cesium3DTileset.fromIonAssetId(2275207);

    /* Tune quality knobs BEFORE adding to the scene. Without these,
       the tileset uses stock Cesium defaults (SSE 16, 512MB cache, no
       preload) which gives the "white box LOD0 skyscraper" effect at
       marketing-grade zooms. These match Meridian's "balanced" preset. */
    GOOGLE_3D_TILESET.maximumScreenSpaceError    = 12;
    GOOGLE_3D_TILESET.cacheBytes                 = 1024 * 1024 * 1024;
    GOOGLE_3D_TILESET.maximumCacheOverflowBytes  = 512  * 1024 * 1024;
    GOOGLE_3D_TILESET.preloadFlightDestinations  = true;
    GOOGLE_3D_TILESET.preloadWhenHidden          = true;
    GOOGLE_3D_TILESET.foveatedScreenSpaceError   = true;

    viewer.scene.primitives.add(GOOGLE_3D_TILESET);
    // Keep tileset visible at all altitudes — when hidden at high
    // altitude the underlying Bing imagery turns out to be a black
    // void (Cesium's default imagery provider isn't getting tiles
    // through Ion at v1.120 for unclear reasons). The Photoreal
    // patches at high altitude look "patchy" but are still better
    // than a black ball. Reverted altitude-gating commit 990b29a.
    viewer.scene.requestRender();

    const btn = $('toggle-3d');
    if (btn) {
      btn.classList.add('on');
      btn.title = '3D Tiles ON via Cesium Ion · auto-render at low altitude';
    }
    console.info('[3D Tiles] Google Photoreal loaded via Ion asset 2275207');
  } catch (e) {
    console.warn('Google 3D Tiles failed to load (Ion asset 2275207).' +
                 ' Falling back to Bing imagery.', e);
    GOOGLE_3D_TILESET = null;
  }
}


/* ─── Lat/lng graticule overlay ─────────────────────────────────── */
/* Subtle white grid on the globe — every 30° major, every 10° minor.
   Pure visual atlas-grade reference; doesn't change behavior. */
function _drawGraticule() {
  const major = Cesium.Color.WHITE.withAlpha(0.10);
  const minor = Cesium.Color.WHITE.withAlpha(0.045);
  // Latitude lines (-80 to +80, every 10°)
  for (let lat = -80; lat <= 80; lat += 10) {
    if (lat === 0) continue;            // equator drawn separately
    const isMajor = lat % 30 === 0;
    const positions = [];
    for (let lng = -180; lng <= 180; lng += 5) positions.push(lng, lat);
    viewer.entities.add({
      polyline: {
        positions: Cesium.Cartesian3.fromDegreesArray(positions),
        width: isMajor ? 1.1 : 0.6,
        material: isMajor ? major : minor,
        clampToGround: true,
      },
    });
  }
  // Equator — a touch warmer
  const equatorPositions = [];
  for (let lng = -180; lng <= 180; lng += 5) equatorPositions.push(lng, 0);
  viewer.entities.add({
    polyline: {
      positions: Cesium.Cartesian3.fromDegreesArray(equatorPositions),
      width: 1.4,
      material: Cesium.Color.fromCssColorString('#ffb547').withAlpha(0.18),
      clampToGround: true,
    },
  });
  // Longitude lines (-180 to +170, every 10°)
  for (let lng = -180; lng < 180; lng += 10) {
    const isMajor = lng % 30 === 0;
    const positions = [];
    for (let lat = -85; lat <= 85; lat += 5) positions.push(lng, lat);
    viewer.entities.add({
      polyline: {
        positions: Cesium.Cartesian3.fromDegreesArray(positions),
        width: isMajor ? 1.1 : 0.6,
        material: isMajor ? major : minor,
        clampToGround: true,
      },
    });
  }
  // Prime meridian — also warmer
  const pmPositions = [];
  for (let lat = -85; lat <= 85; lat += 5) pmPositions.push(0, lat);
  viewer.entities.add({
    polyline: {
      positions: Cesium.Cartesian3.fromDegreesArray(pmPositions),
      width: 1.4,
      material: Cesium.Color.fromCssColorString('#ffb547').withAlpha(0.18),
      clampToGround: true,
    },
  });
}

/* ─── Range rings on left-click (empty globe only) ──────────────── */
/* Click anywhere on empty globe → drop a range-ring set centered
   there. Click on an entity → entity detail panel (separate handler
   below at _setupEntityClick). The two handlers cooperate via the
   `picked.id` check: range rings short-circuit when an entity is
   under the click, detail panel short-circuits when one isn't. */
function _setupRangeRings() {
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction((evt) => {
    if (_measureState.active) return;  // measure mode owns the click
    const picked = viewer.scene.pick(evt.position);
    if (picked && picked.id) return;   // entity click — leave it alone
    const cart = viewer.camera.pickEllipsoid(
      evt.position, viewer.scene.globe.ellipsoid,
    );
    if (!cart) return;
    const c = Cesium.Cartographic.fromCartesian(cart);
    const lat = Cesium.Math.toDegrees(c.latitude);
    const lng = Cesium.Math.toDegrees(c.longitude);
    _drawRangeRingsAt(lat, lng);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
}

/* ─── Entity click → detail panel ───────────────────────────────── */
/* Click an entity (vessel / aircraft / satellite / algorithm finding)
   → slide the right-side detail panel in with the full _glassbox_meta
   formatted as labeled rows + status badges + a deep-link to the
   entity profile page. Esc or the × button closes. */
function _setupEntityClick() {
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction((evt) => {
    if (_measureState.active) return;  // measure mode owns the click
    const picked = viewer.scene.pick(evt.position);
    if (!picked || !picked.id) return;
    showEntityDetail(picked.id);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // Esc closes the panel
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') hideEntityDetail();
  });
  // × button + clicking outside the panel
  const closeBtn = $('ed-close');
  if (closeBtn) closeBtn.addEventListener('click', hideEntityDetail);
}

function showEntityDetail(ent) {
  const panel = $('entity-detail-panel');
  const body  = $('ed-body');
  const kind  = $('ed-kind');
  if (!panel || !body) return;
  const meta = ent._glassbox_meta || {};
  const layer = ent._glassbox_layer || 'entity';
  // Layer name → human label
  const layerLabel = ({
    vessels: 'VESSEL', aircraft: 'AIRCRAFT', satellites: 'SATELLITE',
    signals: 'FINDING', signal: 'FINDING',
  })[layer] || layer.toUpperCase();
  if (kind) kind.textContent = layerLabel;
  // Track which entity is currently shown so external selections work
  panel._currentEntity = ent;
  body.innerHTML = _renderEntityDetail(meta, layer, ent);
  panel.classList.add('show');
  if (window.gbtrack) window.gbtrack('entity_clicked',
    {layer, kind: meta.entity_type || layer});
  // Highlight selected entity in Cesium by tracking the camera onto it
  // (Cesium's built-in selection box would be intrusive; just pulse
  // a temporary outline by toggling pixel size up briefly).
  if (ent.billboard) {
    const orig = ent.billboard.width;
    ent.billboard.width  = (typeof orig === 'number' ? orig : 14) * 1.7;
    ent.billboard.height = ent.billboard.height && typeof ent.billboard.height === 'number'
      ? ent.billboard.height * 1.7 : 22;
    setTimeout(() => {
      if (ent.billboard) {
        ent.billboard.width  = orig;
        ent.billboard.height = orig;
      }
    }, 1500);
  }
}

function hideEntityDetail() {
  const panel = $('entity-detail-panel');
  if (panel) panel.classList.remove('show');
}

function _renderEntityDetail(meta, layer, ent) {
  // Pull common fields out of meta with safe defaults. Aircraft + vessel
  // schemas overlap heavily (position, properties, canonical_id) so we
  // can mostly render uniformly. Satellites + algorithm findings have
  // slightly different shapes — handled below.
  const name = meta.display_name || meta.name || meta.title || '(unnamed)';
  const eid  = meta.canonical_id || meta.mmsi || meta.icao24 || meta.norad_id || '';
  const eidType = (meta.canonical_id_type || '').toUpperCase();
  const props = meta.properties || {};
  const pos   = meta.position || {};
  const lat   = pos.lat != null ? pos.lat : meta.lat;
  const lng   = pos.lng != null ? pos.lng : meta.lng;
  const headingDeg = pos.heading_deg != null ? pos.heading_deg : props.heading_deg;
  const altitudeM  = pos.altitude_m  != null ? pos.altitude_m  : props.altitude_m;
  const velocityMs = pos.velocity_ms != null ? pos.velocity_ms : props.velocity_ms;
  const lastSeen   = meta.last_seen || meta.ts || pos.time;

  const badges = [];
  if (props.sanctioned || meta.sanctioned) badges.push(`<span class="ed-badge sanctioned">Sanctioned</span>`);
  if (props.military)                       badges.push(`<span class="ed-badge military">Military</span>`);
  if (props.emergency)                      badges.push(`<span class="ed-badge emergency">Emergency</span>`);

  const rows = [];
  function row(lbl, val) {
    if (val == null || val === '') return;
    rows.push(`<div class="ed-row"><span class="ed-lbl">${lbl}</span><span class="ed-val">${val}</span></div>`);
  }

  if (eid) row(eidType || 'ID', `<code>${eid}</code>`);
  if (props.callsign) row('CALLSIGN', `<code>${props.callsign}</code>`);
  if (props.flag || props.country_code) row('FLAG', props.flag || props.country_code);
  if (props.vessel_type || props.aircraft_type || meta.entity_type)
    row('TYPE', props.vessel_type || props.aircraft_type || meta.entity_type);
  if (lat != null && lng != null) row('POSITION', `${(+lat).toFixed(4)}, ${(+lng).toFixed(4)}`);
  if (altitudeM != null && altitudeM > 0)
    row('ALTITUDE', `${Math.round(altitudeM).toLocaleString()} m`);
  if (velocityMs != null) {
    const kn = (+velocityMs) * 1.94384;
    row('SPEED', `${kn.toFixed(1)} kn`);
  }
  if (headingDeg != null) row('HEADING', `${Math.round(+headingDeg)}°`);
  if (lastSeen) {
    const t = new Date(lastSeen);
    if (!isNaN(t.getTime())) {
      const ago = Math.max(0, Math.round((Date.now() - t.getTime()) / 1000));
      const agoStr = ago < 60 ? `${ago}s ago` :
                     ago < 3600 ? `${Math.round(ago/60)}m ago` :
                     `${Math.round(ago/3600)}h ago`;
      row('LAST SEEN', `${t.toISOString().substr(11,8)} UTC · ${agoStr}`);
    }
  }
  // Algorithm findings carry description text
  if (meta.description) {
    row('DETAILS', `<span style="text-align:left;display:inline-block">${meta.description.substring(0,260)}${meta.description.length>260?'…':''}</span>`);
  }
  if (meta.severity) row('SEVERITY', String(meta.severity));

  const profileUrl = meta.id ? `/entity/${meta.id}` :
                     (meta.entity_id ? `/entity/${meta.entity_id}` : null);
  const cta = profileUrl
    ? `<a class="ed-cta" href="${profileUrl}" target="_blank">FULL PROFILE ↗</a>`
    : '';

  // Track-line toggle button. Per-entity — clicking adds a polyline of
  // the entity's position history (24h, up to 5000 points) to the globe;
  // clicking again removes it. Tracks survive panel close so the user
  // can compare multiple entities' paths simultaneously. The master
  // `tracks` layer toggle hides ALL active tracks at once via the layer
  // panel without losing the per-entity selection.
  const eidForTrack = meta.id || meta.entity_id;
  const trackBtn = (eidForTrack && (layer === 'aircraft' || layer === 'vessels'))
    ? `<button class="ed-track-btn" data-eid="${eidForTrack}"
              onclick="toggleEntityTrack('${eidForTrack}', this)">
         ${TRACKS_BY_ENTITY_ID.has(eidForTrack) ? 'HIDE TRACK' : 'SHOW TRACK (24h)'}
       </button>`
    : '';

  return `
    <div class="ed-id-line">${name}</div>
    <div class="ed-sub">${layer.toUpperCase()}${eidType ? ' · ' + eidType : ''}</div>
    ${badges.length ? `<div style="margin-bottom:8px">${badges.join('')}</div>` : ''}
    ${rows.length ? rows.join('') : '<div class="ed-empty">No metadata available.</div>'}
    ${trackBtn}
    ${cta}
  `;
}

/* ─── Track lines (position history polylines) ──────────────────────
   Renders an entity's recent position history as a colored polyline on
   the globe. Sourced from /api/v1/entity/{id}.track (up to 24h / 5000
   points). Each tracked entity gets a Cesium Entity with PolylineGraphics
   keyed by entity-id in TRACKS_BY_ENTITY_ID so we can toggle / refresh
   / clear them. Color: cyan for aircraft, gold for vessels (matches the
   sprite palette). */
const TRACKS_BY_ENTITY_ID = new Map();

async function toggleEntityTrack(entityId, btn) {
  // Off → on: fetch + render
  if (!TRACKS_BY_ENTITY_ID.has(entityId)) {
    try {
      const r = await fetch(`/api/v1/entity/${entityId}?track_window_hours=24`);
      if (!r.ok) {
        if (btn) btn.textContent = 'TRACK UNAVAILABLE';
        return;
      }
      const data = await r.json();
      const track = (data.track || []).filter(p => p.lat != null && p.lng != null);
      if (track.length < 2) {
        if (btn) btn.textContent = 'NO RECENT TRACK';
        return;
      }
      const kind = data.entity?.entity_type;
      const isAircraft = kind === 'aircraft';
      // Position list — track returns DESC; reverse so oldest first
      // for natural left-to-right line growth.
      const positions = [];
      for (let i = track.length - 1; i >= 0; i--) {
        const p = track[i];
        const altM = isAircraft && p.altitude_m != null ? p.altitude_m : 0;
        positions.push(Cesium.Cartesian3.fromDegrees(p.lng, p.lat, altM));
      }
      const color = Cesium.Color.fromCssColorString(
        isAircraft ? '#c8a6ff' : '#7fd0ff'
      ).withAlpha(0.85);
      const lineEnt = viewer.entities.add({
        polyline: {
          positions,
          width: 1.8,
          material: color,
          arcType: Cesium.ArcType.GEODESIC,
          // 3e6m so the line is occluded by the globe on the far side
          // (consistent with sprite behavior) and remains crisp up close.
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 6e7),
        },
      });
      lineEnt._glassbox_layer = 'tracks';
      lineEnt._glassbox_meta  = {entity_id: entityId, point_count: track.length};
      // Respect master `tracks` toggle on insert
      const tracksOn = (LAYERS.find(L => L.id === 'tracks') || {on: true}).on;
      lineEnt.show = tracksOn;
      TRACKS_BY_ENTITY_ID.set(entityId, lineEnt);
      if (btn) btn.textContent = 'HIDE TRACK';
      viewer.scene.requestRender();
    } catch (e) {
      console.warn('toggleEntityTrack failed:', e);
      if (btn) btn.textContent = 'TRACK ERROR';
    }
    return;
  }
  // On → off: remove
  const lineEnt = TRACKS_BY_ENTITY_ID.get(entityId);
  if (lineEnt) viewer.entities.remove(lineEnt);
  TRACKS_BY_ENTITY_ID.delete(entityId);
  if (btn) btn.textContent = 'SHOW TRACK (24h)';
  viewer.scene.requestRender();
}

function clearAllTracks() {
  for (const [eid, lineEnt] of TRACKS_BY_ENTITY_ID) {
    viewer.entities.remove(lineEnt);
  }
  TRACKS_BY_ENTITY_ID.clear();
  viewer.scene.requestRender();
}

// Expose for the detail panel onclick — needs to be globally callable
window.toggleEntityTrack = toggleEntityTrack;
window.clearAllTracks    = clearAllTracks;

const RANGE_RING_RADII_KM = [100, 250, 500];

function _drawRangeRingsAt(lat, lng) {
  const ringColor = Cesium.Color.fromCssColorString('#ffb547').withAlpha(0.62);
  const center = Cesium.Cartesian3.fromDegrees(lng, lat);
  const ents = [];

  // Center reticle dot
  ents.push(viewer.entities.add({
    position: center,
    point: {
      pixelSize: 5,
      color: Cesium.Color.fromCssColorString('#ffb547'),
      outlineColor: Cesium.Color.fromCssColorString('#02040a'),
      outlineWidth: 1.5,
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      /* finite depth-test distance keeps near pins crisp but lets the
   globe occlude far-side pins (no see-through). Earth radius is
   ~6.37M m; 1.5e7 hides anything past the planet. */
/* 3e6m (~3000km from camera) keeps near pins crisp + lets the
   globe properly occlude back-side pins. Higher values cause the
   classic bleed-through bug where pins from the far side of the
   planet show up on the near side when the camera is tilted. */
disableDepthTestDistance: 3e6,
    },
  }));

  // Rings
  for (const km of RANGE_RING_RADII_KM) {
    const r = km * 1000;
    ents.push(viewer.entities.add({
      position: center,
      ellipse: {
        semiMajorAxis: r,
        semiMinorAxis: r,
        material: Cesium.Color.TRANSPARENT,
        outline: true,
        outlineColor: ringColor,
        outlineWidth: 1.4,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      },
    }));
    // Label at northern edge of ring
    const labelLat = lat + (r / 111000);
    ents.push(viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(lng, labelLat),
      label: {
        text: `${km}km`,
        font: '600 9px "Geist Mono", monospace',
        fillColor: Cesium.Color.fromCssColorString('#ffb547'),
        outlineColor: Cesium.Color.fromCssColorString('#02040a'),
        outlineWidth: 3,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
        horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
        scale: 0.95,
        showBackground: true,
        backgroundColor: new Cesium.Color(0.01, 0.02, 0.04, 0.75),
        backgroundPadding: new Cesium.Cartesian2(5, 2),
        /* finite depth-test distance keeps near pins crisp but lets the
   globe occlude far-side pins (no see-through). Earth radius is
   ~6.37M m; 1.5e7 hides anything past the planet. */
/* 3e6m (~3000km from camera) keeps near pins crisp + lets the
   globe properly occlude back-side pins. Higher values cause the
   classic bleed-through bug where pins from the far side of the
   planet show up on the near side when the camera is tilted. */
disableDepthTestDistance: 3e6,
      },
    }));
  }

  viewer.scene.requestRender();
  // Fade out via removal after 8s
  setTimeout(() => {
    ents.forEach((e) => viewer.entities.remove(e));
    viewer.scene.requestRender();
  }, 8000);
}

/* ─── Hover card (Apple-Maps style cursor-following tooltip) ─────── */
/* Mouse-move over a vessel/aircraft/signal pin shows a contextual
   card with name + identifier + flag/callsign + sanctioned/military
   warning + last-seen relative time. Card disappears when not over
   anything pickable. Pure read-only — no click required. */
function _setupHoverCard() {
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  const card = $('hover-card');
  if (!card) return;
  handler.setInputAction((movement) => {
    const picked = viewer.scene.pick(movement.endPosition);
    if (picked && picked.id && picked.id._glassbox_meta) {
      const meta = picked.id._glassbox_meta;
      const layer = picked.id._glassbox_layer || '';
      card.innerHTML = _hoverCardHTML(meta, layer);
      // Position with edge-detection so the card never escapes the viewport
      const x = movement.endPosition.x;
      const y = movement.endPosition.y;
      const cw = card.offsetWidth || 220;
      const ch = card.offsetHeight || 80;
      const winW = window.innerWidth;
      const winH = window.innerHeight;
      const left = (x + cw + 24 > winW) ? (x - cw - 14) : (x + 14);
      const top  = (y + ch + 24 > winH) ? (y - ch - 14) : (y + 14);
      card.style.left = left + 'px';
      card.style.top  = top + 'px';
      card.style.opacity = '1';
    } else {
      card.style.opacity = '0';
    }
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
}

function _hoverCardHTML(m, layer) {
  const props = m.properties || {};
  const ts = m.last_seen || m.event_time || m.ts;
  // Read fields the SAME way showEntityDetail does — otherwise hover and
  // click show different identifiers for the same entity. Prior code used
  // `m.name` (returns undefined; API field is `display_name`) and `m.id`
  // (returns the entity UUID, not the canonical MMSI/ICAO). Result: hover
  // labeled the UUID as "MMSI" while click showed the real MMSI — same
  // ship, two different "MMSI" values. Fixed below.
  if (m.entity_type === 'vessel') {
    const name = m.display_name || m.name || 'Unknown vessel';
    const mmsi = m.canonical_id || m.mmsi || '—';
    return `
      <div class="hc-name">${esc(name)}</div>
      <div class="hc-row"><span class="lbl">MMSI</span> ${esc(mmsi)}</div>
      ${props.imo ? `<div class="hc-row"><span class="lbl">IMO</span> ${esc(props.imo)}</div>` : ''}
      ${props.flag ? `<div class="hc-row"><span class="lbl">Flag</span> ${esc(props.flag)}</div>` : ''}
      ${props.sanctioned ? `<div class="hc-row hc-crit">⚠ Sanctioned</div>` : ''}
      <div class="hc-row hc-when">${fmtRel(ts)} ago</div>
    `;
  }
  if (m.entity_type === 'aircraft') {
    const name = m.display_name || m.name || props.callsign || 'Unknown aircraft';
    const icao = m.canonical_id || m.icao24 || '—';
    // Altitude lives on position.altitude_m for aircraft (from the entity
    // viewport response), not properties.altitude_m. Read both.
    const altM = (m.position?.altitude_m != null) ? m.position.altitude_m : props.altitude_m;
    return `
      <div class="hc-name">${esc(name)}</div>
      <div class="hc-row"><span class="lbl">ICAO</span> ${esc(icao)}</div>
      ${props.callsign && props.callsign !== name ? `<div class="hc-row"><span class="lbl">Sign</span> ${esc(props.callsign)}</div>` : ''}
      ${altM != null ? `<div class="hc-row"><span class="lbl">Alt</span> ${Math.round(altM / 1000 * 3.28084)}kft</div>` : ''}
      ${props.military ? `<div class="hc-row hc-crit">⚠ Military</div>` : ''}
      <div class="hc-row hc-when">${fmtRel(ts)} ago</div>
    `;
  }
  // Signal pin (algorithm finding)
  // P3-N step 3 (2026-05-20): show confidence_label when present. Pulled
  // from event.properties.confidence_label via the API pass-through.
  // Pre-P3-N events + layers without a PLATFORM_BASELINE mapping have
  // no value; we silently omit the row rather than render a placeholder
  // (keeps the card tight when the field is genuinely missing).
  const conf = props.confidence_label;
  const confScore = props.confidence_score;
  return `
    <div class="hc-name">${esc((m.title || '(untitled)').replace(/^(CRITICAL|ALERT) — /, ''))}</div>
    ${m.description ? `<div class="hc-row hc-desc">${esc(m.description)}</div>` : ''}
    ${conf ? `<div class="hc-row"><span class="lbl">Conf</span> ${esc(conf)}${typeof confScore === 'number' ? ` (${confScore.toFixed(2)})` : ''}</div>` : ''}
    <div class="hc-row hc-when">${fmtRel(ts)} ago</div>
  `;
}

/* ─── Scale bar (km/cm at current view altitude) ─────────────────── */
/* Approximate on-screen scale calculated from camera height. Shown
   in the bottom-bar — analyst-grade UX cue. */
function updateScaleBar() {
  const el = $('scale-bar-text');
  if (!el || !viewer || !viewer.camera || !viewer.camera.positionCartographic) return;
  const heightKm = viewer.camera.positionCartographic.height / 1000;
  // Heuristic: at h km altitude, ~h/40 km per pixel near scene center.
  // Pick a "nice" round km-per-100px value.
  const kmPer100px = Math.max(1, heightKm / 40);
  const nice = _niceRoundKm(kmPer100px);
  el.textContent = `100px ≈ ${nice}km`;
}

function _niceRoundKm(v) {
  if (v < 5)    return Math.round(v);
  if (v < 50)   return Math.round(v / 5) * 5;
  if (v < 500)  return Math.round(v / 50) * 50;
  if (v < 5000) return Math.round(v / 500) * 500;
  return Math.round(v / 1000) * 1000;
}

function updateCamPos() {
  const c = viewer.camera.positionCartographic;
  if (!c) return;
  const lat = c.latitude  * 180 / Math.PI;
  const lng = c.longitude * 180 / Math.PI;
  const h   = c.height / 1000;
  $('cam-pos').textContent =
    `${Math.abs(lat).toFixed(1)}°${lat >= 0 ? 'N' : 'S'} ` +
    `${Math.abs(lng).toFixed(1)}°${lng >= 0 ? 'E' : 'W'} · ` +
    `${h.toFixed(0)}km`;
}

/* Render one accordion section. Keeps the existing .lyr row markup verbatim
   (incl. id="cnt-<id>" spans that external count-refresh code updates). */
function renderLayerGroup({ group, layers }, collapsed) {
  const isCollapsed = collapsed.has(group.id);
  const rows = layers.map(L => `
      <div class="lyr ${L.on ? 'on' : 'off'} ${L.cls}" data-id="${L.id}">
        <span class="swatch"></span>
        <span class="name">${L.name}</span>
        <span class="count" id="cnt-${L.id}">—</span>
      </div>`).join('');
  return `
    <div class="lyr-group ${isCollapsed ? 'collapsed' : ''}" data-group="${group.id}">
      <div class="lyr-group-head">
        <span class="chevron">▾</span>
        <span class="swatch group" title="Toggle all in group"></span>
        <span class="label">${group.label}</span>
        <span class="gcount" id="gcnt-${group.id}">—</span>
      </div>
      <div class="lyr-group-body">${rows}</div>
    </div>`;
}

let LAYER_GROUP_MODEL = [];
let COLLAPSED_GROUPS = new Set();

/* Recompute every group's "N/M" badge and tri-state group swatch
   (all-on / mixed / all-off) from current L.on values. */
function updateGroupCounts() {
  for (const { group, layers } of LAYER_GROUP_MODEL) {
    const total = layers.length;
    const on = layers.filter(L => L.on).length;
    const badge = $('gcnt-' + group.id);
    if (badge) badge.textContent = `${on}/${total}`;
    const groupEl = document.querySelector(`.lyr-group[data-group="${group.id}"]`);
    if (!groupEl) continue;
    const sw = groupEl.querySelector('.swatch.group');
    if (!sw) continue;
    sw.classList.toggle('all-on', total > 0 && on === total);
    sw.classList.toggle('mixed',  on > 0 && on < total);
    // neither class = all-off
  }
}

/* Flip every layer in a group: if any are on, turn all off; else turn all on.
   Updates each row's classes, repaints the globe once, refreshes counts. */
function toggleGroupAll(groupId) {
  const entry = LAYER_GROUP_MODEL.find(m => m.group.id === groupId);
  if (!entry) return;
  const target = !entry.layers.some(L => L.on);
  for (const L of entry.layers) {
    L.on = target;
    const row = document.querySelector(`.lyr[data-id="${L.id}"]`);
    if (row) {
      row.classList.toggle('on',  L.on);
      row.classList.toggle('off', !L.on);
    }
  }
  refreshVisibility();
  updateGroupCounts();
}

/* ─── Layers panel ───────────────────────────────────────────────── */
function initLayersPanel() {
  LAYER_GROUP_MODEL = buildLayerGroupModel();
  COLLAPSED_GROUPS = initialCollapsedSet(LAYER_GROUP_MODEL);
  $('layers-list').innerHTML =
    LAYER_GROUP_MODEL.map(m => renderLayerGroup(m, COLLAPSED_GROUPS)).join('');
  $('layers-count').textContent = String(LAYERS.length);
  updateGroupCounts();

  $('layers-list').addEventListener('click', (ev) => {
    // 1) Toggle-all — group swatch (checked before head + before .lyr).
    const gSwatch = ev.target.closest('.swatch.group');
    if (gSwatch) {
      ev.stopPropagation();
      toggleGroupAll(gSwatch.closest('.lyr-group').dataset.group);
      return;
    }
    // 2) Collapse / expand — group header.
    const head = ev.target.closest('.lyr-group-head');
    if (head) {
      const groupEl = head.closest('.lyr-group');
      const gid = groupEl.dataset.group;
      const nowCollapsed = groupEl.classList.toggle('collapsed');
      if (nowCollapsed) COLLAPSED_GROUPS.add(gid);
      else COLLAPSED_GROUPS.delete(gid);
      saveCollapsedGroups(COLLAPSED_GROUPS);
      return;
    }
    // 3) Per-layer toggle.
    const row = ev.target.closest('.lyr');
    if (!row) return;
    const L = LAYERS.find(x => x.id === row.dataset.id);
    if (!L) return;
    L.on = !L.on;
    row.classList.toggle('on',  L.on);
    row.classList.toggle('off', !L.on);
    refreshVisibility();
    updateGroupCounts();
  });
}

function refreshVisibility() {
  const enabled = new Set(LAYERS.filter(L => L.on).map(L => L.id));
  // Sub-layer toggles applied on top of parent-layer visibility. Read
  // once per refresh so the loop body is cheap.
  const militaryOn = enabled.has('military_air');
  const tracksOn   = enabled.has('tracks');
  for (const ent of viewer.entities.values) {
    const lid = ent._glassbox_layer;
    if (!lid) continue;
    let show = enabled.has(lid);
    // Military aircraft hidden when their sub-toggle is off, even when
    // the parent `aircraft` layer is on.
    if (show && lid === 'aircraft' && ent._glassbox_meta?.properties?.military && !militaryOn) {
      show = false;
    }
    // Track polylines: master `tracks` toggle hides all active tracks
    // at once without losing the per-entity track selections (re-toggling
    // brings them back instantly).
    if (lid === 'tracks') {
      show = tracksOn;
    }
    ent.show = show;
  }
  viewer.scene.requestRender();
}

/* ─── Data load ──────────────────────────────────────────────────── */
// Single-flight guard: countdown ticks every 1s and triggers loadAll
// when remaining<0. Under DB load, loadAll can take 30-120s — meanwhile
// the countdown keeps firing fresh loadAll calls, stacking up concurrent
// requests that compound the load (avalanche). This flag drops any
// re-entry while a load is already running.
let LOAD_IN_FLIGHT = false;
let LATEST_VESSELS = [];
let LATEST_AIRCRAFT = [];
async function loadAll() {
  if (LOAD_IN_FLIGHT) return;
  LOAD_IN_FLIGHT = true;
  // Fire each endpoint independently and paint AS IT RETURNS. Previously,
  // the visible map was gated on Promise.all completing — meaning if ANY
  // endpoint was slow (dashboard/summary hits a 17M-row event scan and
  // can take 120s), planes and ships wouldn't render until the slow one
  // finished. With fire-and-paint, viewport entities show the moment
  // they arrive (1-10s), while the slow analytical panes update in the
  // background.
  const fetchOk = (url) => fetch(url).then(r => r.ok ? r.json() : null).catch(() => null);
  const pendingPromises = [
    // Vessels: bumped to 10K with 4h time window. Class B AIS reports
    // every 3 minutes, Class A every 2-10 seconds underway / 3 min at
    // anchor. 1h window missed ~25K vessels that broadcast 1-4h ago
    // (truth: 37K active in 4h vs 12K in 1h). 10K visible at global
    // zoom is heavy without clustering — Cesium's EntityCluster groups
    // them into cluster markers at low zoom, individual sprites at
    // high zoom. See setupEntityClustering() further down.
    fetchViewport('vessel', 10000, 4).then(v => {
      LATEST_VESSELS = v || [];
      paintViewport('vessels', LATEST_VESSELS);
    }),
    // Aircraft: bumped to 5K with 1h window. Aircraft transmit ADS-B
    // every 0.1-2 seconds, so 1h is plenty — anything older is on the
    // ground. Truth: ~8.7K active in 1h.
    fetchViewport('aircraft', 5000, 1).then(a => {
      LATEST_AIRCRAFT = a || [];
      paintViewport('aircraft', LATEST_AIRCRAFT);
    }),
    fetchOk(`/api/v1/signals/today?window_hours=${WINDOW_H}&per_category=250`).then(sig => {
      if (sig) {
        LAST_PAYLOAD = sig;
        paintSignals(sig);
        paintChronicle(sig);
        paintTicker(sig);
      }
    }),
    fetchOk(`/api/v1/dashboard/summary?window_hours=${WINDOW_H}`).then(sumr => {
      if (sumr) paintKpis(sumr, LATEST_VESSELS, LATEST_AIRCRAFT);
    }),
    fetchOk('/api/v1/health/full').then(health => {
      if (health) paintHealth(health);
    }),
  ];
  // Don't await all 5 — just kick off the countdown immediately after
  // the fast endpoints (viewport) typically complete. Use Promise.race
  // against a 2s floor to start the countdown promptly, but only release
  // the in-flight guard after ALL endpoints settle so we don't double-fire.
  Promise.allSettled(pendingPromises).finally(() => {
    LOAD_IN_FLIGHT = false;
  });
  startCountdown();
}

async function fetchViewport(kind, limit, hoursBack) {
  // hoursBack: caller-controlled per-type window. Aircraft (continuous
  // ADS-B every 1-2s) → 1h. Vessels (Class B AIS every 3 min; at-anchor
  // every 3-6 min) → 4h to catch slow / anchored vessels that miss the
  // tighter window. Decoupled from the user-visible WINDOW_H (signals
  // analytical view) because entity positions and event lookbacks have
  // different optimal windows.
  const h = hoursBack || 1;
  const since = new Date(Date.now() - h * 3600 * 1000).toISOString();
  try {
    const r = await fetch(`/api/v1/viewport?bbox=-180,-90,180,90&time_from=${encodeURIComponent(since)}&types=${kind}&limit=${limit}`);
    if (!r.ok) return [];
    return (await r.json()).entities || [];
  } catch (e) { return []; }
}

/* ─── Paint: signals → Cesium pins ───────────────────────────────── */
function paintSignals(sig) {
  const enabled = new Set(LAYERS.filter(L => L.on).map(L => L.id));
  for (const cat of sig.categories || []) {
    for (const it of cat.items || []) {
      if (it.lat == null || it.lng == null) continue;
      const key = 'sig:' + it.id;
      const sevColor = SEV_COLOR[cat.severity] || SEV_COLOR.medium;
      const radius = (cat.severity === 'critical' ? 7
                   :  cat.severity === 'high'     ? 6
                   :  cat.severity === 'medium'   ? 5 : 4);
      let ent = ENT_BY_KEY.get(key);
      if (!ent) {
        ent = viewer.entities.add({
          position: Cesium.Cartesian3.fromDegrees(it.lng, it.lat),
          point: {
            pixelSize: radius,
            color: sevColor,
            outlineColor: Cesium.Color.fromCssColorString('#02040a'),
            outlineWidth: 1.5,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            /* finite depth-test distance keeps near pins crisp but lets the
   globe occlude far-side pins (no see-through). Earth radius is
   ~6.37M m; 1.5e7 hides anything past the planet. */
/* 3e6m (~3000km from camera) keeps near pins crisp + lets the
   globe properly occlude back-side pins. Higher values cause the
   classic bleed-through bug where pins from the far side of the
   planet show up on the near side when the camera is tilted. */
disableDepthTestDistance: 3e6,
          },
        });
        ent._glassbox_layer = cat.id;
        ent._glassbox_meta = it;
        ENT_BY_KEY.set(key, ent);
      }
      ent.show = enabled.has(cat.id);
    }
    const cnt = $(`cnt-${cat.id}`);
    if (cnt) cnt.textContent = (cat.count || 0).toLocaleString();
  }
  viewer.scene.requestRender();
}

/* ─── Paint: viewport entities (smooth motion + rotated sprites) ─── */
function paintViewport(layerId, entities) {
  const L = LAYERS.find(x => x.id === layerId);
  const enabled = L ? L.on : true;
  const isAircraft = layerId === 'aircraft';
  // Sub-layer toggles: military aircraft + sanctioned vessels can be
  // hidden independently of their parent layer. Read each fresh on
  // every paint so layer-panel checkboxes take effect immediately.
  const militaryOn   = LAYERS.find(L => L.id === 'military_air')?.on ?? true;
  const sanctionedOn = !!(LAYERS.find(L => L.id === 'sanctioned_underway')?.on
                        || LAYERS.find(L => L.id === 'sanctioned_dark')?.on);
  const seenKeys = new Set();

  for (const e of entities || []) {
    const p = e.position || {};
    if (p.lat == null || p.lng == null) continue;
    const key = layerId + ':' + e.id;
    seenKeys.add(key);
    // Aircraft: real altitude from ADS-B. Vessels: lift sprite 200m above
    // sea level so it doesn't z-fight with the rendered globe / 3D Tiles
    // surface at close camera ranges (the bug where ships visible at
    // zoom-out disappeared on zoom-in). The vessel is logically at sea
    // level but the visible globe mesh is at +/-tens of meters from the
    // ellipsoid depending on terrain LOD, so a small lift keeps the
    // sprite always above the surface. 200m is invisible at altitudes
    // where ships are normally viewed (>5km).
    const altitudeM = isAircraft ? (p.altitude_m || 0) : 200;
    // Pick sprite by entity flavor — sanctioned vessels and military
    // aircraft get distinct icons so they pop visually amongst the
    // ambient traffic.
    const props = e.properties || {};
    let sprite;
    if (isAircraft) {
      sprite = props.military ? MILITARY_AIR_SPRITE : AIRCRAFT_SPRITE;
    } else {
      sprite = props.sanctioned ? VESSEL_SANCTIONED_SPRITE : VESSEL_SPRITE;
    }
    // Per-entity visibility: respect both the parent layer toggle AND
    // the sub-layer flavor toggle. Military aircraft are hidden when
    // `military_air` is off even though `aircraft` is on. Sanctioned
    // vessels follow the sanctioned_* layer toggles.
    let entityShow = enabled;
    if (isAircraft && props.military && !militaryOn) entityShow = false;
    // Heading (degrees, CW from north). Our sprites (aircraft + vessel)
    // are drawn pointing UP (north) in their SVG — so heading 0 (north)
    // = rotation 0; heading 90 (east) = rotation -90° (Cesium rotation
    // is CCW positive in the alignedAxis=Z frame, so CW heading needs
    // negative rotation). Fix: `-headingDeg` not `-headingDeg + 90`.
    // The old formula assumed sprites drawn pointing east — wrong.
    const headingDeg = (p.heading_deg != null) ? p.heading_deg : null;
    const rotation = headingDeg != null
      ? Cesium.Math.toRadians(-headingDeg)
      : 0;

    // Build the entity's position — for moving entities (vessels +
    // aircraft) with known velocity + heading, project forward in
    // time so they animate continuously from the moment they appear.
    // Without this, the FIRST creation uses a static Cartesian3 and
    // the entity sits motionless until a fresh data update arrives
    // (which can be 30s+). With SampledPositionProperty + EXTRAPOLATE,
    // motion is visible immediately and persists between updates.
    const velocityMs = (p.velocity_ms != null) ? p.velocity_ms : null;
    let initialPosition;
    if (velocityMs != null && velocityMs > 0.5 && headingDeg != null) {
      initialPosition = _projectMovingPosition(
        p.lat, p.lng, altitudeM, headingDeg, velocityMs);
    } else {
      initialPosition = Cesium.Cartesian3.fromDegrees(
        p.lng, p.lat, altitudeM);
    }
    // Keep `cart` for the existing update-path equality check below.
    const cart = Cesium.Cartesian3.fromDegrees(p.lng, p.lat, altitudeM);

    let ent = ENT_BY_KEY.get(key);
    if (!ent) {
      ent = viewer.entities.add({
        position: initialPosition,
        billboard: {
          image: sprite,
          width: isAircraft ? 14 : 12,
          height: isAircraft ? 14 : 12,
          // Rotate around the sprite's center anchor.
          rotation: rotation,
          alignedAxis: Cesium.Cartesian3.UNIT_Z,
          // Scale a touch up at higher zoom so distant pins don't vanish.
          scaleByDistance: new Cesium.NearFarScalar(
            1.5e6, 1.0,
            6.0e7, 0.55,
          ),
          /* finite depth-test distance keeps near pins crisp but lets the
   globe occlude far-side pins (no see-through). Earth radius is
   ~6.37M m; 1.5e7 hides anything past the planet. */
/* 3e6m (~3000km from camera) keeps near pins crisp + lets the
   globe properly occlude back-side pins. Higher values cause the
   classic bleed-through bug where pins from the far side of the
   planet show up on the near side when the camera is tilted. */
disableDepthTestDistance: 3e6,
        },
      });
      ent._glassbox_layer = layerId;
      ent._glassbox_kind  = e.entity_type;
      ent._glassbox_meta  = e;
      ENT_BY_KEY.set(key, ent);
    } else {
      // Update: rebuild a moving-position property anchored at viewer
      // clock so motion continues at the REAL velocity (not compressed).
      // Old code used animateBetween(oldPos, cart, 4.0) which forced the
      // entity to traverse the actual ground-distance between updates in
      // 4 seconds, then EXTRAPOLATE forward at that fictitious rate — net
      // effect 5-15x too fast. The new path uses the entity's real
      // velocity_ms + heading_deg to project a sample 60s out, so
      // EXTRAPOLATE forward continues at the actual airspeed/seaspeed.
      //
      // Sanity: huge jumps (>200km) probably indicate data error or
      // callsign reuse — fall back to a static snap rather than animate.
      const oldPos = ent.position.getValue(viewer.clock.currentTime);
      const deltaMeters = oldPos ? Cesium.Cartesian3.distance(oldPos, cart) : 0;
      if (deltaMeters > 200000) {
        ent.position = cart;
      } else if (velocityMs != null && velocityMs > 0.5 && headingDeg != null) {
        ent.position = _projectMovingPosition(
          p.lat, p.lng, altitudeM, headingDeg, velocityMs);
      } else {
        ent.position = cart;
      }
      // Update rotation if heading changed
      if (ent.billboard) {
        ent.billboard.rotation = rotation;
      }
    }
    ent.show = entityShow;
  }
  for (const [k, ent] of ENT_BY_KEY.entries()) {
    if (!k.startsWith(layerId + ':')) continue;
    if (!seenKeys.has(k)) {
      viewer.entities.remove(ent);
      ENT_BY_KEY.delete(k);
    }
  }
  const cnt = $(`cnt-${layerId}`);
  if (cnt) cnt.textContent = entities ? entities.length.toLocaleString() : '0';
  viewer.scene.requestRender();
}

/* Build a SampledPositionProperty that extrapolates forward at the
   entity's known velocity + heading. Two samples 60 seconds apart;
   EXTRAPOLATE forward means the planar continues moving smoothly past
   the second sample using the slope of the two. This is the missing
   piece that makes vessels + aircraft actually animate on screen the
   moment they appear, instead of sitting motionless until the next
   data update overwrites their position. */
function _projectMovingPosition(lat, lng, altM, headingDeg, velocityMs) {
  // Anchor samples to viewer.clock.currentTime, NOT JulianDate.now(). The
  // Cesium viewer's clock can lag wall-clock by tens of seconds (especially
  // after a heavy initial load) — anchoring to wall time means the entity's
  // SampledPositionProperty has samples 30+ seconds in the FUTURE relative
  // to viewer.clock.currentTime, so getValue returns the HOLD-backward sample
  // forever (no visible motion). Anchoring to viewer.clock.currentTime makes
  // sample 1 = the moment Cesium thinks "now" is, so EXTRAPOLATE forward
  // kicks in immediately and the entity glides.
  const start = viewer.clock.currentTime.clone();
  const end = Cesium.JulianDate.addSeconds(start, 60.0, new Cesium.JulianDate());
  // Forward-project 60 seconds along great-circle from (lat,lng) at
  // (headingDeg, velocityMs). Equirectangular approximation is fine at
  // 60-second scale: a 600 m/s jet covers 36 km in 60s, well within
  // the equirectangular accuracy at any latitude not near the poles.
  const R = 6371000;
  const δ = (velocityMs * 60.0) / R;
  const θ = headingDeg * Math.PI / 180;
  const φ1 = lat * Math.PI / 180;
  const λ1 = lng * Math.PI / 180;
  const φ2 = Math.asin(Math.sin(φ1) * Math.cos(δ)
                     + Math.cos(φ1) * Math.sin(δ) * Math.cos(θ));
  const λ2 = λ1 + Math.atan2(
    Math.sin(θ) * Math.sin(δ) * Math.cos(φ1),
    Math.cos(δ) - Math.sin(φ1) * Math.sin(φ2),
  );
  const lat2 = φ2 * 180 / Math.PI;
  const lng2 = λ2 * 180 / Math.PI;
  const sp = new Cesium.SampledPositionProperty();
  sp.addSample(start, Cesium.Cartesian3.fromDegrees(lng,  lat,  altM));
  sp.addSample(end,   Cesium.Cartesian3.fromDegrees(lng2, lat2, altM));
  sp.setInterpolationOptions({
    interpolationDegree: 1,
    interpolationAlgorithm: Cesium.LinearApproximation,
  });
  sp.forwardExtrapolationType  = Cesium.ExtrapolationType.EXTRAPOLATE;
  sp.backwardExtrapolationType = Cesium.ExtrapolationType.HOLD;
  return sp;
}

function animateBetween(from, to, secs) {
  // Anchor to viewer clock, not JulianDate.now() — see _projectMovingPosition
  // for the full rationale. Same bug applies here on entity-position updates.
  const start = viewer.clock.currentTime.clone();
  const end   = Cesium.JulianDate.addSeconds(start, secs, new Cesium.JulianDate());
  const sp = new Cesium.SampledPositionProperty();
  sp.addSample(start, from);
  sp.addSample(end,   to);
  sp.setInterpolationOptions({
    interpolationDegree: 1,
    interpolationAlgorithm: Cesium.LinearApproximation,
  });
  // EXTRAPOLATE forward (not HOLD): past the end sample, keep moving in
  // the same direction at the same rate using last-known velocity. So
  // vessels and aircraft continue gliding between SSE updates rather
  // than freezing the moment a single 4s interp window completes. This
  // is what makes motion visible at a global zoom (a ship moving 20kt
  // covers ~10m/s; visible only if motion is continuous, not 4s bursts).
  sp.forwardExtrapolationType  = Cesium.ExtrapolationType.EXTRAPOLATE;
  sp.backwardExtrapolationType = Cesium.ExtrapolationType.HOLD;
  return sp;
}

/* ─── KPI bento ──────────────────────────────────────────────────── */
function paintKpis(sumr, vessels, aircraft) {
  $('kpi-critical').textContent = (sumr.critical || 0).toLocaleString();
  $('kpi-cases').textContent    = (sumr.open_cases || 0).toLocaleString();
  $('kpi-signals').textContent  = (sumr.signals || 0).toLocaleString();
  const tracked = (vessels?.length || 0) + (aircraft?.length || 0);
  $('kpi-tracked').textContent  = tracked.toLocaleString();
  $('kpi-tracked-delta').textContent =
    `${(vessels?.length || 0).toLocaleString()} vessels · ${(aircraft?.length || 0).toLocaleString()} aircraft`;
  $('kpi-critical-delta').textContent = `last ${WINDOW_H}h · ${sumr.geolocated || 0} geolocated`;
}

function paintHealth(h) {
  const ing = h.ingesters || {};
  $('ing-count').textContent = `${ing.ok || 0}/${ing.total || 0}`;
  if (h.db && h.db.ok) {
    $('db-latency').textContent = (h.db.latency_ms || 0) + 'ms';
  } else {
    $('db-latency').textContent = 'ERR';
  }
}

/* ─── Chronicle ──────────────────────────────────────────────────── */
function paintChronicle(sig) {
  const items = [];
  for (const cat of sig.categories || []) {
    for (const it of cat.items || []) {
      items.push({ ...it, category: cat.label, severity: cat.severity, category_id: cat.id });
    }
  }
  items.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));
  const top = items.slice(0, 30);
  $('chronicle').innerHTML = top.map(it => {
    const sevCls = it.severity === 'critical' ? 'crit'
                 : it.severity === 'high'     ? 'high'
                 : it.severity === 'medium'   ? 'med' : 'low';
    const title = (it.title || '(untitled)')
      .replace(/^CRITICAL — /, '').replace(/^ALERT — /, '');
    const desc = it.description ? `<p class="desc">${esc(it.description)}</p>` : '';
    return `
      <div class="entry" data-href="${esc((it.links && it.links.entity) || '')}"
                          data-lat="${it.lat || ''}" data-lng="${it.lng || ''}">
        <div class="meta">
          <span class="sev ${sevCls}">${it.severity || 'unknown'}</span>
          <span>${esc(it.category || '')}</span>
          <span class="when">${fmtRel(it.ts)}</span>
        </div>
        <p class="title">${esc(title)}</p>
        ${desc}
      </div>`;
  }).join('');
  $('chronicle-count').textContent = items.length.toLocaleString();
}

document.addEventListener('click', (ev) => {
  const entry = ev.target.closest('.chronicle .entry');
  if (!entry) return;
  const lat = parseFloat(entry.dataset.lat);
  const lng = parseFloat(entry.dataset.lng);
  if (!isNaN(lat) && !isNaN(lng)) {
    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(lng, lat, 1500000),
      duration: 1.6,
      easingFunction: Cesium.EasingFunction.CUBIC_IN_OUT,
    });
  }
  if ((ev.metaKey || ev.ctrlKey) && entry.dataset.href) {
    window.open(entry.dataset.href, '_blank', 'noopener');
  }
});

/* ─── Critical alert ticker ──────────────────────────────────────── */
function paintTicker(sig) {
  const crits = [];
  for (const cat of sig.categories || []) {
    if (cat.severity !== 'critical') continue;
    for (const it of (cat.items || []).slice(0, 5)) {
      const t = (it.title || '').replace(/^CRITICAL — /, '').replace(/^ALERT — /, '');
      crits.push(`<span><span class="gold">${esc(cat.label)}</span> · ${esc(t)} · ${fmtRel(it.ts)}</span>`);
    }
  }
  if (!crits.length) {
    $('ticker-stream').innerHTML =
      '<span style="color:var(--low)">No critical findings in the past hour. System nominal.</span>';
    return;
  }
  // Duplicate the run so the marquee loops seamlessly
  const inner = crits.join('') + crits.join('');
  $('ticker-stream').innerHTML = inner;
}

/* ─── AI Brief ───────────────────────────────────────────────────── */
async function loadBrief() {
  try {
    const r = await fetch('/api/v1/brief/latest');
    if (!r.ok) return;
    const d = await r.json();
    const md = d.markdown || '';
    const body = md
      .replace(/^#[^\n]*\n/, '')
      .replace(/^_[^_]*_\n/m, '')
      .replace(/^---\n+/m, '')
      .trim();
    const html = esc(body)
      .replace(/\*\*\*\s*CRITICAL\s*\*\*\*/g, '<span class="em-crit">CRITICAL</span>')
      .replace(/\bALERT\b/g, '<span class="em-crit">ALERT</span>');
    $('brief-quote').innerHTML = html || '<em>No brief yet — Ollama warming up.</em>';
    if (d.published_at) {
      const t = new Date(d.published_at);
      const z = (n) => String(n).padStart(2, '0');
      $('brief-time').textContent =
        `${z(t.getUTCHours())}:${z(t.getUTCMinutes())} UTC`;
    }
  } catch (e) {
    $('brief-quote').innerHTML = '<em>Brief unavailable (Ollama may be warming).</em>';
  }
}

/* ─── Loader ─────────────────────────────────────────────────────── */
function hideLoader() {
  setTimeout(() => $('loader').classList.add('gone'), 200);
  setTimeout(() => $('loader').remove(), 1200);
}

/* ─── Header clocks ──────────────────────────────────────────────── */
function bootClocks() {
  const tick = () => {
    const d = new Date();
    const z = (n) => String(n).padStart(2, '0');
    const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
    $('utc-date').textContent =
      `${z(d.getUTCDate())} ${months[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
    $('utc-time').textContent =
      `${z(d.getUTCHours())}:${z(d.getUTCMinutes())}:${z(d.getUTCSeconds())}`;
  };
  tick(); setInterval(tick, 1000);
}

/* ─── Time-window picker ─────────────────────────────────────────── */
function wirePicker() {
  $('picker').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-h]');
    if (!btn) return;
    $('picker').querySelectorAll('button')
      .forEach(b => b.classList.toggle('active', b === btn));
    WINDOW_H = parseInt(btn.dataset.h, 10);
    for (const [k, ent] of ENT_BY_KEY.entries()) {
      if (k.startsWith('sig:')) {
        viewer.entities.remove(ent);
        ENT_BY_KEY.delete(k);
      }
    }
    loadAll();
  });
}

function startCountdown() {
  if (countdownTimer) clearInterval(countdownTimer);
  let remaining = REFRESH_SEC;
  const tick = () => {
    $('countdown').textContent = remaining + 's';
    remaining -= 1;
    if (remaining < 0) loadAll();
  };
  tick();
  countdownTimer = setInterval(tick, 1000);
}

/* ─── News tab switcher (lazy-load: no iframe until user opts in) ── */
let NEWS_ACTIVATED = false;

function _newsEmbedUrl(channelId) {
  // youtube-nocookie.com is YouTube's privacy-enhanced mode — fewer
  // cross-origin trackers fire, less likely to trigger any third-party
  // proxy/auth interception (e.g. nimbleway.com OAuth flows).
  return `https://www.youtube-nocookie.com/embed/live_stream?channel=${channelId}&autoplay=1&mute=1`;
}

function activateNews(channelId) {
  const slot = $('news-frame');
  if (!slot) return;
  // Replace the placeholder div with a real iframe on first activation
  if (!NEWS_ACTIVATED || slot.tagName !== 'IFRAME') {
    const iframe = document.createElement('iframe');
    iframe.id = 'news-frame';
    iframe.allow = 'autoplay; encrypted-media';
    iframe.allowFullscreen = true;
    iframe.src = _newsEmbedUrl(channelId);
    slot.replaceWith(iframe);
    NEWS_ACTIVATED = true;
  } else {
    slot.src = _newsEmbedUrl(channelId);
  }
}

function _setNewsWatchLink(chId) {
  const a = $('news-open-yt');
  if (a) a.href = NEWS_WATCH_URL(chId);
}

function wireNewsTabs() {
  // Initialize the YouTube ↗ link to whatever's currently active
  const activeBtn = $('news-tabs').querySelector('button.active');
  _setNewsWatchLink((activeBtn && activeBtn.dataset.ch) || 'UCNye-wNBqNL5ZzHSJj3l8Bg');

  $('news-tabs').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-ch]');
    if (!btn) return;
    $('news-tabs').querySelectorAll('button')
      .forEach(b => b.classList.toggle('active', b === btn));
    activateNews(btn.dataset.ch);
    _setNewsWatchLink(btn.dataset.ch);
  });
  // Click on the placeholder activates with the currently-active tab's channel
  document.addEventListener('click', (ev) => {
    const slot = ev.target.closest('#news-frame.placeholder');
    if (!slot) return;
    const active = $('news-tabs').querySelector('button.active');
    activateNews((active && active.dataset.ch) || 'UCNye-wNBqNL5ZzHSJj3l8Bg');
  });
}

/* ─── Webcam tab switcher (lazy-load placeholders) ───────────────── */
function _camNoCookieSrc(originalSrc) {
  // Swap youtube.com → youtube-nocookie.com to reduce 3rd-party tracker
  // requests + trip rate of any browser-extension proxy (nimbleway etc).
  return originalSrc.replace('https://www.youtube.com/',
                              'https://www.youtube-nocookie.com/');
}

function renderWebcams(setKey) {
  const tiles = WEBCAM_SETS[setKey] || WEBCAM_SETS.global;
  // Render iframes directly so cams auto-load on page open. Previously
  // shipped a "tap to load" placeholder pattern that required a click;
  // operator feedback was that cams "never worked" because the
  // implicit-click expectation wasn't obvious from the UI. Cesium +
  // iframes share GPU memory but the load cost is acceptable — 4
  // muted 1080p streams use <5% of a modern GPU.
  $('webcam-grid').innerHTML = tiles.map((c, i) => `
    <div class="tile">
      <div class="lbl">${esc(c.lbl)}</div>
      <a class="open-yt" href="${esc(c.watch || '#')}" target="_blank"
         rel="noopener" title="Open in new tab on YouTube (works even if embed is blocked)">↗</a>
      <iframe src="${esc(_camNoCookieSrc(c.src))}"
              allow="autoplay; encrypted-media; picture-in-picture"
              allowfullscreen
              loading="lazy"
              referrerpolicy="no-referrer-when-downgrade"></iframe>
    </div>
  `).join('');
}
function wireWebcamTabs() {
  $('cam-tabs').addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-set]');
    if (!btn) return;
    $('cam-tabs').querySelectorAll('button')
      .forEach(b => b.classList.toggle('active', b === btn));
    renderWebcams(btn.dataset.set);
  });
}

/* ─── Search ─────────────────────────────────────────────────────── */
function wireSearch() {
  $('search').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      const q = ev.target.value.trim();
      if (q) window.location.href = '/signals#q=' + encodeURIComponent(q);
    }
  });
}

/* ─── SSE: live pin pulses ───────────────────────────────────────── */
const TYPE_TO_CAT_ID = {
  sanctioned_vessel_went_dark:      'sanctioned_dark',
  sanctioned_vessel_rendezvous:     'sanctioned_rendezvous',
  shadow_fleet_cluster:             'shadow_fleet',
  sanctioned_vessel_underway:       'sanctioned_underway',
  sanctioned_port_arrival:          'sanctioned_port',
  aircraft_in_sanctioned_airspace:  'sanctioned_airspace',
  military_aircraft_underway:       'military_air',
  dark_vessel_detected:             'dark_vessel',
  loitering_detected:               'loitering',
};
const CAT_TO_SEV = {
  sanctioned_dark: 'critical', sanctioned_rendezvous: 'critical', shadow_fleet: 'critical',
  sanctioned_underway: 'high', sanctioned_port: 'high', sanctioned_airspace: 'high',
  military_air: 'medium', dark_vessel: 'medium', loitering: 'medium',
};

function startSSE() {
  if (typeof EventSource === 'undefined') return;
  if (SSE) SSE.close();
  SSE = new EventSource('/api/v1/alerts/stream?poll_sec=5');
  SSE.addEventListener('hello', () => { SSE_RETRY = 1000; });
  SSE.addEventListener('alert', (ev) => {
    try { onSSEAlert(JSON.parse(ev.data)); } catch (e) { /* swallow */ }
  });
  SSE.onerror = () => {
    SSE.close();
    SSE_RETRY = Math.min(SSE_RETRY * 2, 30000);
    setTimeout(startSSE, SSE_RETRY);
  };
}

function onSSEAlert(alert) {
  if (!viewer || alert.lat == null || alert.lng == null) return;
  const catId = TYPE_TO_CAT_ID[alert.event_type];
  if (!catId) return;
  const sev = CAT_TO_SEV[catId] || 'low';
  const key = 'sig:' + alert.id;
  if (ENT_BY_KEY.has(key)) return;
  const sevColor = SEV_COLOR[sev] || SEV_COLOR.medium;
  const ent = viewer.entities.add({
    position: Cesium.Cartesian3.fromDegrees(alert.lng, alert.lat),
    point: {
      pixelSize: sev === 'critical' ? 8 : 6,
      color: sevColor,
      outlineColor: Cesium.Color.fromCssColorString('#02040a'),
      outlineWidth: 1.5,
      heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      /* finite depth-test distance keeps near pins crisp but lets the
   globe occlude far-side pins (no see-through). Earth radius is
   ~6.37M m; 1.5e7 hides anything past the planet. */
/* 3e6m (~3000km from camera) keeps near pins crisp + lets the
   globe properly occlude back-side pins. Higher values cause the
   classic bleed-through bug where pins from the far side of the
   planet show up on the near side when the camera is tilted. */
disableDepthTestDistance: 3e6,
    },
  });
  ent._glassbox_layer = catId;
  ent._glassbox_meta = alert;
  ENT_BY_KEY.set(key, ent);

  // Outline-pulse "drop" animation
  const t0 = Date.now();
  function pulse() {
    const elapsed = (Date.now() - t0) / 1500;
    if (elapsed >= 1.0) {
      ent.point.outlineWidth = 1.5;
      viewer.scene.requestRender();
      return;
    }
    ent.point.outlineWidth = 1.5 + (1 - elapsed) * 8.0;
    viewer.scene.requestRender();
    requestAnimationFrame(pulse);
  }
  pulse();

  // Prepend to chronicle if relevant
  if (LAST_PAYLOAD) {
    const cat = (LAST_PAYLOAD.categories || []).find(c => c.id === catId);
    if (cat) {
      cat.items.unshift({
        id: alert.id, title: alert.title, description: alert.description,
        ts: alert.event_time,
        lat: alert.lat, lng: alert.lng,
        entity_id: alert.entity_id || null,
        links: alert.entity_id ? { entity: '/entity/' + alert.entity_id } : null,
      });
      cat.count = (cat.count || 0) + 1;
      paintChronicle(LAST_PAYLOAD);
      paintTicker(LAST_PAYLOAD);
      setTimeout(() => {
        const first = $('chronicle').querySelector('.entry');
        if (first) first.classList.add('fresh');
      }, 50);
    }
  }
}

/* ─── Idle guard against third-party SSO interception ────────────── */
(function idleGuard() {
  const IDLE_MS = 30 * 60 * 1000;
  let timer = null;
  const reset = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      document.querySelectorAll('iframe[src*="youtube.com"], iframe[src*="youtube-nocookie.com"]')
        .forEach(f => { f.dataset.parkedSrc = f.src; f.src = 'about:blank'; });
    }, IDLE_MS);
  };
  ['mousemove', 'keydown', 'scroll', 'touchstart'].forEach(ev =>
    window.addEventListener(ev, reset, { passive: true })
  );
  reset();
})();

/* ─── Util ───────────────────────────────────────────────────────── */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[m]));
}
function fmtRel(iso) {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1)  return 'now';
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h';
  return Math.floor(h / 24) + 'd';
}

boot();
