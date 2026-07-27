/* Glassbox Monitor — front-end controller.
 *
 * MapLibre GL JS (BSD-3) renders a dark basemap with circle layers per
 * data source. No external code copied — written fresh against patterns
 * common to deck.gl / globe.gl / maplibre dashboards.
 */

const $ = (id) => document.getElementById(id);

// ─── Config: layer registry ──────────────────────────────────────────
// Each layer has: id, label, icon, on-by-default, source kind, color.
const LAYERS = [
  { id: 'country_intel',     icon: '🌐', label: 'Country intel heat',  on: true,
    src: 'country_intel', color: '#ff4d6a' },
  { id: 'heatmap',           icon: '🟧', label: 'Severity heatmap',   on: false,
    src: 'heatmap', color: '#ff4d6a' },
  { id: 'critical_findings', icon: '🚨', label: 'Critical findings',  on: true,
    src: 'signals', filter: 'critical', color: '#ff4d6a' },
  { id: 'high_findings',     icon: '⚠️', label: 'High findings',      on: true,
    src: 'signals', filter: 'high',     color: '#ffa657' },
  { id: 'sanctioned_vessels',icon: '⛔', label: 'Sanctioned vessels', on: true,
    src: 'signals', filter: 'sanctioned_underway', color: '#ff4d6a' },
  { id: 'dark_vessels',      icon: '🌑', label: 'Dark vessels',       on: true,
    src: 'signals', filter: 'dark_vessel', color: '#d4c43a' },
  { id: 'wildfires',         icon: '🔥', label: 'Wildfires (FIRMS)',  on: false,
    src: 'signals', filter: 'wildfires', color: '#ff7b3a' },
  { id: 'quakes',            icon: '🌐', label: 'Earthquakes (USGS)', on: false,
    src: 'signals', filter: 'quakes',  color: '#d4c43a' },
  { id: 'vessels',           icon: '🚢', label: 'All vessels',        on: false,
    src: 'viewport', kind: 'vessel',   color: '#58a6ff' },
  { id: 'aircraft',          icon: '✈️', label: 'All aircraft',       on: false,
    src: 'viewport', kind: 'aircraft', color: '#c08bff' },
];

// ─── SSE config ───────────────────────────────────────────────────────
let SSE = null;
let SSE_RETRY = 1000;

// Window in hours — bound to the time picker
let WINDOW_H = 24;

// ─── State ────────────────────────────────────────────────────────────
let MAP = null;
let LAYER_COUNTS = {};
let LATEST_SIGNALS = null;
let REFRESH_TIMER = null;
let COUNTDOWN_TIMER = null;
const REFRESH_SEC = 60;

// ─── Map init ────────────────────────────────────────────────────────
function initMap() {
  // Free dark basemap from CARTO via OSM tiles. No API key required.
  // Style is built inline so we don't depend on a remote style.json
  // (which would add a CORS hop + a single point of failure).
  const style = {
    version: 8,
    glyphs: 'https://fonts.openmaptiles.org/{fontstack}/{range}.pbf',
    sources: {
      'osm-dark': {
        type: 'raster',
        tiles: [
          'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
          'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
          'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
        ],
        tileSize: 256,
        attribution: '© OpenStreetMap contributors © CARTO',
      },
    },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': '#0b0e14' } },
      { id: 'osm-dark',   type: 'raster',     source: 'osm-dark',
        paint: { 'raster-opacity': 0.85 } },
    ],
  };

  MAP = new maplibregl.Map({
    container: 'map',
    style,
    center: [10, 30],
    zoom: 1.6,
    minZoom: 1,
    maxZoom: 18,
    attributionControl: false,
  });
  MAP.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

  MAP.on('load', () => {
    $('map-loading').remove();
    // Kick off country GeoJSON load in parallel; layer is added when ready
    _loadCountriesLayer();
    for (const L of LAYERS) {
      // Country intel — handled by _loadCountriesLayer below; data
      // attaches asynchronously when /monitor/countries.geojson lands.
      if (L.src === 'country_intel') continue;
      // Heatmap layer is special: it consumes the union of all signal
      // points across categories and renders as a heatmap, not circles.
      if (L.src === 'heatmap') {
        MAP.addSource(L.id, { type: 'geojson', data: emptyFC() });
        MAP.addLayer({
          id: L.id,
          type: 'heatmap',
          source: L.id,
          paint: {
            // Per-point weight scaled by severity (critical=4 high=3 ...).
            'heatmap-weight': [
              'interpolate', ['linear'], ['get', 'sevWeight'],
              0, 0,
              4, 1,
            ],
            'heatmap-intensity': [
              'interpolate', ['linear'], ['zoom'],
              0, 1, 9, 3,
            ],
            // Severity-tinted gradient: cool empty → yellow → orange → red.
            'heatmap-color': [
              'interpolate', ['linear'], ['heatmap-density'],
              0,    'rgba(0,0,0,0)',
              0.15, 'rgba(63,185,80,0.4)',
              0.35, 'rgba(212,196,58,0.6)',
              0.6,  'rgba(255,166,87,0.75)',
              0.85, 'rgba(255,77,106,0.85)',
              1.0,  'rgba(255,40,80,0.95)',
            ],
            'heatmap-radius': [
              'interpolate', ['linear'], ['zoom'],
              0,  18,
              4,  28,
              9,  44,
            ],
            // Fade out the heatmap above zoom 9 — at street-level the
            // individual circle pins tell the story better.
            'heatmap-opacity': [
              'interpolate', ['linear'], ['zoom'],
              7,  0.85,
              10, 0.0,
            ],
          },
          layout: { visibility: L.on ? 'visible' : 'none' },
        });
        continue;
      }

      // Vessels + aircraft get clustering (1000s of points → bubbles).
      const isViewport = L.src === 'viewport';
      MAP.addSource(L.id, {
        type: 'geojson',
        data: emptyFC(),
        cluster: isViewport,
        clusterMaxZoom: 9,
        clusterRadius: 36,
      });
      if (isViewport) {
        // Cluster bubbles
        MAP.addLayer({
          id: L.id + '-clusters',
          type: 'circle',
          source: L.id,
          filter: ['has', 'point_count'],
          paint: {
            'circle-color':  L.color,
            'circle-opacity': 0.45,
            'circle-radius': [
              'step', ['get', 'point_count'],
              10,  10,   // <10 points → r=10
              14,  100,  // <100      → r=14
              20,  500,  // <500      → r=20
              28,
            ],
            'circle-stroke-width': 1,
            'circle-stroke-color': L.color,
          },
          layout: { visibility: L.on ? 'visible' : 'none' },
        });
        MAP.addLayer({
          id: L.id + '-cluster-count',
          type: 'symbol',
          source: L.id,
          filter: ['has', 'point_count'],
          layout: {
            'text-field': ['get', 'point_count_abbreviated'],
            'text-size': 11,
            'text-allow-overlap': true,
            visibility: L.on ? 'visible' : 'none',
          },
          paint: { 'text-color': '#0b0e14' },
        });
        // Click cluster → zoom in
        MAP.on('click', L.id + '-clusters', (e) => {
          const features = MAP.queryRenderedFeatures(e.point,
            { layers: [L.id + '-clusters'] });
          const clusterId = features[0].properties.cluster_id;
          MAP.getSource(L.id).getClusterExpansionZoom(clusterId, (err, z) => {
            if (err) return;
            MAP.easeTo({ center: features[0].geometry.coordinates, zoom: z });
          });
        });
      }
      // Unclustered point layer (the actual pins).
      // MapLibre 4.x rejects `filter: null` (must be a valid expression
      // or the key omitted entirely). Only add the filter when we
      // actually need to exclude clustered points from this layer.
      const layerSpec = {
        id: L.id,
        type: 'circle',
        source: L.id,
        paint: {
          'circle-radius': [
            'interpolate', ['linear'], ['zoom'],
            1, 3,
            5, 5,
            10, 8,
          ],
          'circle-color': L.color,
          'circle-opacity': 0.85,
          'circle-stroke-width': 0.6,
          'circle-stroke-color': '#0b0e14',
        },
        layout: { visibility: L.on ? 'visible' : 'none' },
      };
      if (isViewport) layerSpec.filter = ['!', ['has', 'point_count']];
      MAP.addLayer(layerSpec);
      MAP.on('click', L.id, (e) => onPinClick(L, e.features[0]));
      MAP.on('mouseenter', L.id, () => MAP.getCanvas().style.cursor = 'pointer');
      MAP.on('mouseleave', L.id, () => MAP.getCanvas().style.cursor = '');
    }
    load();
    startSSE();
  });
}

// ─── Layer visibility toggle: handle multi-layer cases (clusters) ────
function setLayerVisibility(L) {
  const v = L.on ? 'visible' : 'none';
  const ids = [L.id];
  if (L.src === 'viewport')      ids.push(L.id + '-clusters', L.id + '-cluster-count');
  if (L.src === 'country_intel') ids.push(L.id + '-line');
  for (const id of ids) {
    if (MAP.getLayer(id)) MAP.setLayoutProperty(id, 'visibility', v);
  }
}

// ─── Country intel highlight overlay ─────────────────────────────────
// Loads Natural Earth 110m countries (public domain, fetched by
// 09_SETUP_GUIDES/scripts/glassbox/fetch_countries_geojson.sh) and
// colors each country fill by recent algorithm-fire intensity:
//   red    = >= 8 critical/high fires in window
//   orange = 4-7
//   yellow = 1-3
//   green  = 0
// Uses bbox-only point-in-polygon (fast, accurate enough at country
// scale; misses on Russia/USA edges are tolerable for highlight).
const COUNTRIES_LAYER_ID = 'country_intel';
let COUNTRIES_FC = null;     // loaded GeoJSON
const COUNTRY_BBOX = [];     // [{idx, w, s, e, n, name, iso}, …]

async function _loadCountriesLayer() {
  try {
    const r = await fetch('/monitor/countries.geojson');
    if (!r.ok) {
      console.warn('Country highlight unavailable — run ' +
        'bash 09_SETUP_GUIDES/scripts/glassbox/fetch_countries_geojson.sh');
      return;
    }
    COUNTRIES_FC = await r.json();
    // Pre-compute bbox per feature so per-refresh classification is O(N)
    COUNTRIES_FC.features.forEach((f, idx) => {
      const bb = _featureBBox(f);
      if (!bb) return;
      const props = f.properties || {};
      f.properties = Object.assign(props, { _idx: idx, _intensity: 0 });
      COUNTRY_BBOX.push({
        idx,
        w: bb[0], s: bb[1], e: bb[2], n: bb[3],
        name: props.ADMIN || props.NAME || '?',
        iso: props.ADM0_A3 || props.ISO_A3 || '',
      });
    });
    if (!MAP) return;
    MAP.addSource(COUNTRIES_LAYER_ID, { type: 'geojson', data: COUNTRIES_FC });
    // Fill — color by per-feature _intensity property
    MAP.addLayer({
      id: COUNTRIES_LAYER_ID,
      type: 'fill',
      source: COUNTRIES_LAYER_ID,
      paint: {
        'fill-color': [
          'step', ['get', '_intensity'],
          'rgba(63,185,80,0.0)',          // 0 — invisible (clean)
          1,  'rgba(212,196,58,0.18)',    // 1-3   — yellow tint
          4,  'rgba(255,166,87,0.28)',    // 4-7   — orange
          8,  'rgba(255,77,106,0.38)',    // 8-15  — red
          16, 'rgba(255,40,80,0.48)',     // 16+   — heavy red
        ],
        'fill-outline-color': 'rgba(0,0,0,0)',
      },
      layout: {
        visibility: (LAYERS.find(L => L.id === COUNTRIES_LAYER_ID) || { on: true }).on
          ? 'visible' : 'none',
      },
    }, MAP.getStyle().layers[1].id);   // insert above background, below pins
    // Subtle outline so quiet countries are still visible at the border
    MAP.addLayer({
      id: COUNTRIES_LAYER_ID + '-line',
      type: 'line',
      source: COUNTRIES_LAYER_ID,
      paint: {
        'line-color': 'rgba(255,255,255,0.10)',
        'line-width': 0.4,
      },
      layout: {
        visibility: (LAYERS.find(L => L.id === COUNTRIES_LAYER_ID) || { on: true }).on
          ? 'visible' : 'none',
      },
    }, MAP.getStyle().layers[1].id);
    // Hover popup — country name + intensity
    MAP.on('mousemove', COUNTRIES_LAYER_ID, _onCountryHover);
    MAP.on('mouseleave', COUNTRIES_LAYER_ID, _onCountryHoverOut);
  } catch (e) {
    console.error('Country layer load failed:', e);
  }
}

function _featureBBox(f) {
  let w = 180, s = 90, e = -180, n = -90;
  function visit(coords) {
    if (typeof coords[0] === 'number') {
      const [x, y] = coords;
      if (x < w) w = x; if (x > e) e = x;
      if (y < s) s = y; if (y > n) n = y;
    } else for (const c of coords) visit(c);
  }
  if (!f.geometry || !f.geometry.coordinates) return null;
  visit(f.geometry.coordinates);
  return [w, s, e, n];
}

// Recompute per-country intensity based on current critical+high signals.
// Called from paintSignals after the SSE/poll updates the categories.
function _recomputeCountryIntensity(sig) {
  if (!COUNTRIES_FC || !MAP || !MAP.getSource(COUNTRIES_LAYER_ID)) return;
  // Reset all to zero
  COUNTRIES_FC.features.forEach(f => { f.properties._intensity = 0; });
  // Walk every point; bump every country whose bbox contains it
  const cats = (sig && sig.categories) || [];
  for (const cat of cats) {
    const sevWeight = cat.severity === 'critical' ? 2
                    : cat.severity === 'high'     ? 1
                    : 0;
    if (!sevWeight) continue;
    for (const it of (cat.items || [])) {
      const lat = it.lat, lng = it.lng;
      if (lat == null || lng == null) continue;
      for (const bb of COUNTRY_BBOX) {
        if (lng >= bb.w && lng <= bb.e && lat >= bb.s && lat <= bb.n) {
          COUNTRIES_FC.features[bb.idx].properties._intensity += sevWeight;
        }
      }
    }
  }
  MAP.getSource(COUNTRIES_LAYER_ID).setData(COUNTRIES_FC);
}

function _onCountryHover(ev) {
  const f = ev.features && ev.features[0];
  if (!f) return;
  MAP.getCanvas().style.cursor = 'pointer';
  const name = f.properties.ADMIN || f.properties.NAME || '?';
  const intensity = f.properties._intensity || 0;
  const tier = intensity === 0 ? 'quiet'
             : intensity < 4   ? 'monitoring'
             : intensity < 8   ? 'elevated'
             : intensity < 16  ? 'high alert'
             : 'critical hotspot';
  let tip = document.getElementById('country-hover-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'country-hover-tip';
    tip.style.cssText = 'position:absolute;z-index:50;background:rgba(0,0,0,0.85);' +
      'border:1px solid rgba(255,255,255,0.15);border-radius:4px;padding:6px 10px;' +
      'font-family:"Geist Mono",ui-monospace,monospace;font-size:11px;color:#fff;' +
      'pointer-events:none;letter-spacing:0.04em';
    document.body.appendChild(tip);
  }
  tip.style.display = 'block';
  tip.style.left = (ev.point.x + 12) + 'px';
  tip.style.top  = (ev.point.y + 12) + 'px';
  tip.innerHTML = `<strong>${name}</strong><br>${intensity} weighted fires · ${tier}`;
}

function _onCountryHoverOut() {
  MAP.getCanvas().style.cursor = '';
  const tip = document.getElementById('country-hover-tip');
  if (tip) tip.style.display = 'none';
}

function emptyFC() {
  return { type: 'FeatureCollection', features: [] };
}

// ─── Layer toggle UI ──────────────────────────────────────────────────
function renderLayerList() {
  $('layer-list').innerHTML = LAYERS.map(L => `
    <div class="layer-row${L.on ? ' on' : ''}" data-id="${L.id}">
      <div class="ck"></div>
      <div class="icon">${L.icon}</div>
      <div class="layer-name">${L.label}</div>
      <div class="count" id="cnt-${L.id}">0</div>
    </div>
  `).join('');
  $('layer-list').addEventListener('click', (ev) => {
    const row = ev.target.closest('.layer-row');
    if (!row) return;
    const L = LAYERS.find(x => x.id === row.dataset.id);
    if (!L) return;
    L.on = !L.on;
    row.classList.toggle('on', L.on);
    if (MAP) setLayerVisibility(L);
  });
}

// ─── Data loading ─────────────────────────────────────────────────────
async function load() {
  try {
    const sigPromise = fetch(`/api/v1/signals/today?window_hours=${WINDOW_H}&per_category=50`)
      .then(r => r.ok ? r.json() : Promise.reject(`signals HTTP ${r.status}`));
    const vesPromise = fetchViewport('vessel', 2500);
    const aircPromise = fetchViewport('aircraft', 2500);

    const [sig, vessels, aircraft] = await Promise.all([sigPromise, vesPromise, aircPromise]);
    LATEST_SIGNALS = sig;
    paintSignals(sig);
    paintViewport('vessels', vessels);
    paintViewport('aircraft', aircraft);
    updateStatus(sig, vessels, aircraft);
    loadScrubber();   // hourly buckets — refreshed alongside main payload
    startCountdown();
  } catch (e) {
    console.error('load failed', e);
    $('status-updated').textContent = 'error';
  }
}

async function fetchViewport(kind, limit) {
  const since = new Date(Date.now() - WINDOW_H * 3600 * 1000).toISOString();
  const url = `/api/v1/viewport?bbox=-180,-90,180,90&time_from=${encodeURIComponent(since)}` +
              `&types=${kind}&limit=${limit}`;
  try {
    const r = await fetch(url);
    if (!r.ok) return [];
    const d = await r.json();
    return d.entities || [];
  } catch (e) {
    return [];
  }
}

// ─── Paint: signals (algorithm-derived findings) ──────────────────────
const SEV_WEIGHT = { critical: 4, high: 3, medium: 2, low: 1 };
function paintSignals(sig) {
  // Recompute country intensity from the refreshed signals payload.
  // No-op if the GeoJSON layer hasn't loaded yet.
  _recomputeCountryIntensity(sig);
  const buckets = {};
  for (const L of LAYERS) buckets[L.id] = [];
  const heatmapFeatures = [];

  for (const cat of sig.categories || []) {
    for (const it of cat.items || []) {
      if (it.lat == null || it.lng == null) continue;
      const f = {
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [it.lng, it.lat] },
        properties: {
          src: 'signal',
          id: it.id,
          title: stripPrefix(it.title || '(untitled)'),
          ts: it.ts,
          severity: cat.severity,
          category: cat.label,
          category_id: cat.id,
          entity_id: it.entity_id || null,
          entity_url: (it.links && it.links.entity) || null,
          authority: (it.authority && it.authority.name) || null,
          sevWeight: SEV_WEIGHT[cat.severity] || 1,
        },
      };
      // Heatmap gets EVERY signal point, weighted by severity.
      heatmapFeatures.push(f);
      // Route to per-layer buckets for the circle layers.
      for (const L of LAYERS) {
        if (L.src !== 'signals') continue;
        if (L.filter === 'critical' && cat.severity === 'critical') buckets[L.id].push(f);
        else if (L.filter === 'high' && cat.severity === 'high')    buckets[L.id].push(f);
        else if (L.filter === cat.id)                                buckets[L.id].push(f);
      }
    }
  }

  // Push heatmap union
  if (MAP && MAP.getSource('heatmap')) {
    MAP.getSource('heatmap').setData({
      type: 'FeatureCollection', features: heatmapFeatures,
    });
  }
  LAYER_COUNTS['heatmap'] = heatmapFeatures.length;
  const heatCnt = $('cnt-heatmap');
  if (heatCnt) heatCnt.textContent = heatmapFeatures.length;

  for (const L of LAYERS) {
    if (L.src !== 'signals') continue;
    const fc = { type: 'FeatureCollection', features: buckets[L.id] };
    if (MAP && MAP.getSource(L.id)) MAP.getSource(L.id).setData(fc);
    LAYER_COUNTS[L.id] = buckets[L.id].length;
    const cntEl = $(`cnt-${L.id}`);
    if (cntEl) cntEl.textContent = buckets[L.id].length;
  }
}

// ─── Paint: viewport entities (vessels/aircraft) ──────────────────────
function paintViewport(layerId, entities) {
  const features = [];
  for (const e of entities || []) {
    const p = e.position || {};
    if (p.lat == null || p.lng == null) continue;
    features.push({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [p.lng, p.lat] },
      properties: {
        src: 'viewport',
        id: e.id,
        entity_id: e.id,
        entity_url: `/entity/${e.id}`,
        title: e.display_name || `${e.canonical_id_type}:${e.canonical_id}`,
        canonical_id: e.canonical_id,
        kind: e.entity_type,
        velocity: p.velocity_ms,
        altitude: p.altitude_m,
      },
    });
  }
  if (MAP && MAP.getSource(layerId)) {
    MAP.getSource(layerId).setData({ type: 'FeatureCollection', features });
  }
  LAYER_COUNTS[layerId] = features.length;
  const cntEl = $(`cnt-${layerId}`);
  if (cntEl) cntEl.textContent = features.length;
}

// ─── Click → deep-dive in right sidebar ───────────────────────────────
function onPinClick(L, feature) {
  const p = feature.properties || {};
  const right = $('right');
  $('dd-title').textContent = p.title || 'Item';
  const lines = [];
  if (p.category) lines.push(line('Category', p.category, p.severity));
  if (p.severity) lines.push(line('Severity', p.severity.toUpperCase(), p.severity));
  if (p.kind)     lines.push(line('Type', p.kind));
  if (p.canonical_id) lines.push(line('ID', p.canonical_id));
  if (p.authority)    lines.push(line('Authority', p.authority));
  if (p.ts)           lines.push(line('Time', new Date(p.ts).toLocaleString()));
  if (p.altitude != null) lines.push(line('Altitude', Math.round(+p.altitude) + ' m'));
  if (p.velocity != null) lines.push(line('Velocity', (+p.velocity).toFixed(1) + ' m/s'));

  let actions = '';
  if (p.entity_url) {
    actions = `<div class="deep-section"><a href="${p.entity_url}" style="display:inline-block;
              padding:8px 14px;background:var(--accent);color:#000;border-radius:4px;
              font-weight:700;text-transform:uppercase;letter-spacing:0.06em;font-size:11px">
              Open entity profile →</a></div>`;
  }

  $('dd-body').innerHTML = lines.join('') + actions;
  $('app').classList.remove('no-right');
  right.classList.remove('collapsed');
}

function line(label, value, sev) {
  const cls = sev ? ` ${sev === 'critical' ? 'crit' :
                       sev === 'high'     ? 'high' :
                       sev === 'medium'   ? 'med' :
                       sev === 'low'      ? 'low' : ''}` : '';
  return `<div class="deep-section">
    <div class="label">${label}</div>
    <div class="value${cls}">${escapeHtml(value)}</div>
  </div>`;
}

$('dd-close').addEventListener('click', () => {
  $('right').classList.add('collapsed');
  $('app').classList.add('no-right');
});

// ─── Status bar + DEFCON ──────────────────────────────────────────────
function updateStatus(sig, vessels, aircraft) {
  const sum = sig.summary || {};
  $('status-findings').textContent = (sum.total_findings || 0).toLocaleString();
  $('status-critical').textContent = (sum.critical_count || 0).toLocaleString();
  $('status-vessels').textContent  = (vessels?.length || 0).toLocaleString();
  $('status-aircraft').textContent = (aircraft?.length || 0).toLocaleString();
  $('status-updated').textContent  = new Date(sig.generated_at)
    .toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  // DEFCON: rough mapping from critical_count.
  // 0 critical → 5 (lowest readiness), 1-9 → 4, 10-49 → 3, 50-499 → 2, 500+ → 1.
  const c = sum.critical_count || 0;
  const dc = c >= 500 ? 1 : c >= 50 ? 2 : c >= 10 ? 3 : c >= 1 ? 4 : 5;
  const dv = $('defcon-value');
  dv.textContent = String(dc);
  const colors = { 1: 'var(--critical)', 2: 'var(--critical)', 3: 'var(--high)',
                   4: 'var(--medium)',   5: 'var(--low)' };
  dv.style.color = colors[dc];

  $('overlay-count').textContent  = (sum.total_findings || 0).toLocaleString();
  $('overlay-window').textContent = WINDOW_H + 'H';
}

function startCountdown() {
  if (COUNTDOWN_TIMER) clearInterval(COUNTDOWN_TIMER);
  let remaining = REFRESH_SEC;
  const tick = () => {
    $('status-countdown').textContent = remaining + 's';
    remaining -= 1;
    if (remaining < 0) load();
  };
  tick();
  COUNTDOWN_TIMER = setInterval(tick, 1000);
}

// ─── Time-window picker ───────────────────────────────────────────────
$('time-picker').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-h]');
  if (!btn) return;
  $('time-picker').querySelectorAll('button')
    .forEach(b => b.classList.toggle('active', b === btn));
  WINDOW_H = parseInt(btn.dataset.h, 10);
  load();
});

// ─── SSE: real-time pin push (sub-second latency) ─────────────────────
// Mirror of the same pattern shipped in /signals + /globe — when a
// tier-1 algorithm event fires, we patch a fresh feature into the
// matching layer source without re-fetching.
const TYPE_TO_CAT_ID = {
  sanctioned_vessel_went_dark:      'sanctioned_dark',
  sanctioned_vessel_rendezvous:     'sanctioned_rendezvous',
  shadow_fleet_cluster:             'shadow_fleet',
  sanctioned_vessel_underway:       'sanctioned_underway',
  sanctioned_port_arrival:          'sanctioned_port',
  aircraft_in_sanctioned_airspace:  'sanctioned_airspace',
  military_aircraft_underway:       'military_air',
  dark_vessel_detected:             'dark_vessel',
};
const CAT_TO_SEV = {
  sanctioned_dark: 'critical', sanctioned_rendezvous: 'critical', shadow_fleet: 'critical',
  sanctioned_underway: 'high', sanctioned_port: 'high', sanctioned_airspace: 'high',
  military_air: 'medium', dark_vessel: 'medium',
  loitering: 'low', wildfires: 'low', quakes: 'low',
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
  if (!MAP || alert.lat == null || alert.lng == null) return;
  const catId = TYPE_TO_CAT_ID[alert.event_type];
  if (!catId) return;
  const sev = CAT_TO_SEV[catId] || 'low';
  const f = {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [alert.lng, alert.lat] },
    properties: {
      src: 'signal',
      id: alert.id,
      title: stripPrefix(alert.title || '(untitled)'),
      ts: alert.event_time,
      severity: sev,
      category_id: catId,
      entity_id: alert.entity_id || null,
      entity_url: alert.entity_id ? `/entity/${alert.entity_id}` : null,
      sevWeight: SEV_WEIGHT[sev] || 1,
    },
  };

  // Prepend to heatmap source
  if (MAP.getSource('heatmap')) {
    const src = MAP.getSource('heatmap');
    const data = src._data || { type: 'FeatureCollection', features: [] };
    if (!data.features.some(x => x.properties.id === alert.id)) {
      data.features.unshift(f);
      if (data.features.length > 2000) data.features.pop();
      src.setData(data);
    }
  }

  // Route to matching circle layer too
  for (const L of LAYERS) {
    if (L.src !== 'signals') continue;
    let match = false;
    if (L.filter === 'critical' && sev === 'critical') match = true;
    else if (L.filter === 'high' && sev === 'high')    match = true;
    else if (L.filter === catId)                        match = true;
    if (!match) continue;
    const src = MAP.getSource(L.id);
    if (!src) continue;
    const data = src._data || { type: 'FeatureCollection', features: [] };
    if (data.features.some(x => x.properties.id === alert.id)) continue;
    data.features.unshift(f);
    if (data.features.length > 500) data.features.pop();
    src.setData(data);
    LAYER_COUNTS[L.id] = data.features.length;
    const cntEl = $(`cnt-${L.id}`);
    if (cntEl) cntEl.textContent = data.features.length;
  }
}

// ─── Util ─────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[m]));
}
function stripPrefix(t) {
  return String(t || '').replace(/^CRITICAL — /, '').replace(/^ALERT — /, '');
}

// ─── Time-scrubber histogram ─────────────────────────────────────────
async function loadScrubber() {
  try {
    const r = await fetch('/api/v1/signals/timeline?window_hours=168&bucket_min=60');
    if (!r.ok) return;
    const d = await r.json();
    paintScrubber(d.buckets || []);
  } catch (e) { /* swallow */ }
}

function paintScrubber(buckets) {
  const wrap = $('scrubber');
  if (!wrap) return;
  // Build a 168-bucket dense array (one per hour for 7 days), zero-fill
  // missing buckets so the bars aren't time-distorted.
  const now = Date.now();
  const dense = [];
  for (let h = 167; h >= 0; h--) {
    const ts = new Date(now - h * 3600 * 1000);
    ts.setMinutes(0, 0, 0);
    dense.push({ ts: ts.toISOString(), total: 0,
                 by_severity: { critical: 0, high: 0, medium: 0, low: 0 } });
  }
  // Index dense by hour-truncated ISO string
  const idx = new Map();
  for (let i = 0; i < dense.length; i++) {
    idx.set(dense[i].ts.slice(0, 13), i);
  }
  for (const b of buckets) {
    const k = (b.ts || '').slice(0, 13);
    if (idx.has(k)) {
      dense[idx.get(k)].total = b.total;
      dense[idx.get(k)].by_severity = b.by_severity;
    }
  }

  // Compute max for height scale (skip outliers — use 95th percentile)
  const sorted = dense.map(b => b.total).sort((a, b) => a - b);
  const p95 = sorted[Math.floor(sorted.length * 0.95)] || 1;

  // Render bars
  const marker = wrap.querySelector('.marker-now');
  wrap.innerHTML = '';
  if (marker) wrap.appendChild(marker);
  for (const b of dense) {
    const bar = document.createElement('div');
    bar.className = 'bar';
    if (b.total === 0) {
      bar.classList.add('zero');
      bar.style.height = '1px';
    } else {
      const pct = Math.min(1.0, b.total / p95);
      bar.style.height = (4 + pct * 28) + 'px';
      // Color = max severity present in bucket
      const sev = b.by_severity || {};
      const cls = sev.critical > 0 ? 'crit' :
                   sev.high     > 0 ? 'high' :
                   sev.medium   > 0 ? 'med'  : 'low';
      bar.classList.add(cls);
    }
    // Tooltip
    const tip = document.createElement('div');
    tip.className = 'tip';
    const t = new Date(b.ts);
    const tStr = t.toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
    const sv = b.by_severity || {};
    tip.innerHTML = `<strong>${tStr}</strong><br>` +
      `${b.total.toLocaleString()} findings` +
      (sv.critical ? `<br>${sv.critical} critical` : '');
    bar.appendChild(tip);
    wrap.appendChild(bar);
  }
  if (marker) wrap.appendChild(marker);
}

// ─── Live news 3-up + Live webcams ──────────────────────────────────
// Each pane has its own dropdown to select a channel. ↗ link opens
// the channel's /live URL on YouTube — always works even when the
// embed is region-blocked or the stream just ended.

const NEWS_CHANNELS = [
  { id: 'UCNye-wNBqNL5ZzHSJj3l8Bg', label: 'Al Jazeera English' },
  { id: 'UCQfwfsi5VrQ8yKZ-UWmAEFg', label: 'France 24 English' },
  { id: 'UCknLrEdhRCp1aegoMqRaCZg', label: 'DW News' },
  { id: 'UCoMdktPbSTixAyNGwb-UYkQ', label: 'Sky News' },
  { id: 'UCeY0bbntWzzVIaj2z3QigXg', label: 'NBC News Now' },
  { id: 'UCBi2mrWuNuyYy4gbM6fU18Q', label: 'ABC News Live' },
  { id: 'UCIALMKvObZNtJ6AmdCLP7Lg', label: 'Bloomberg' },
  { id: 'UC16niRr50-MSBwiO3YDb3RA', label: 'BBC News' },
  { id: 'UC8p1vwvWtl6T73JiExfWs1g', label: 'CBS News' },
  { id: 'UCXIJgqnII2ZOINSWNOGFThA', label: 'Fox News' },
];

// Defaults: pane 0 = AJ, pane 1 = France 24, pane 2 = Bloomberg
const NEWS_DEFAULTS = [0, 1, 6];

const WEBCAMS = [
  { lbl: 'Times Square, NYC', id: 'wHRNmmuP7q4' },
  { lbl: 'Tokyo Shibuya',     id: '3kPH7kTphnE' },
  { lbl: 'Tel Aviv',          id: 'PlsrxX8cqBE' },
  { lbl: 'Moscow Red Square', id: 'h1wly909BYw' },
  { lbl: 'London',            id: '9Auq9mYxFEE' },
  { lbl: 'Niagara Falls',     id: 'dyMEtSqfu_M' },
];

function _newsEmbedUrl(chId) {
  return `https://www.youtube-nocookie.com/embed/live_stream?channel=${chId}&autoplay=1&mute=1`;
}
function _newsWatchUrl(chId) {
  return `https://www.youtube.com/channel/${chId}/live`;
}

function _renderNewsCells() {
  const host = $('news-3up');
  if (!host) return;
  host.innerHTML = NEWS_DEFAULTS.map((defaultIdx, paneIdx) => {
    const opts = NEWS_CHANNELS.map((c, i) =>
      `<option value="${c.id}" ${i === defaultIdx ? 'selected' : ''}>${c.label}</option>`
    ).join('');
    const ch = NEWS_CHANNELS[defaultIdx];
    return `
      <div class="news-cell" data-pane="${paneIdx}">
        <div class="news-cell-head">
          <select class="news-pick" data-pane="${paneIdx}">${opts}</select>
          <a class="open-yt" href="${_newsWatchUrl(ch.id)}" target="_blank" rel="noopener" title="Open on YouTube">↗</a>
        </div>
        <iframe id="news-frame-${paneIdx}"
                src="${_newsEmbedUrl(ch.id)}"
                allow="autoplay; encrypted-media" allowfullscreen></iframe>
      </div>
    `;
  }).join('');
  host.querySelectorAll('.news-pick').forEach(sel => {
    sel.addEventListener('change', (ev) => {
      const cell = ev.target.closest('.news-cell');
      const ch = ev.target.value;
      cell.querySelector('iframe').src = _newsEmbedUrl(ch);
      cell.querySelector('.open-yt').href = _newsWatchUrl(ch);
    });
  });
}

function _renderWebcams() {
  const host = $('webcam-grid');
  if (!host) return;
  host.innerHTML = WEBCAMS.map(c => `
    <div class="webcam-tile">
      <div class="label">${c.lbl}</div>
      <a class="open-yt" href="https://www.youtube.com/watch?v=${c.id}" target="_blank" rel="noopener" title="Open on YouTube">↗</a>
      <iframe src="https://www.youtube-nocookie.com/embed/${c.id}?autoplay=1&mute=1&controls=0"
              allow="autoplay; encrypted-media" allowfullscreen></iframe>
    </div>
  `).join('');
}

_renderNewsCells();
_renderWebcams();

// ─── Strip show/hide toggle ──────────────────────────────────────────
$('strip-toggle')?.addEventListener('click', () => {
  const app = document.getElementById('app');
  const t = $('strip-toggle');
  const isOn = !app.classList.contains('no-strip');
  if (isOn) {
    app.classList.add('no-strip');
    t.classList.remove('on');
  } else {
    app.classList.remove('no-strip');
    t.classList.add('on');
  }
  // Map needs a resize after the grid row changes — MapLibre keeps
  // its canvas size cached.
  setTimeout(() => MAP && MAP.resize(), 320);
});

// ─── Boot ─────────────────────────────────────────────────────────────
renderLayerList();
initMap();
