/* GLASSBOX // COMMAND — front-end controller for the unified dashboard.
 *
 * Wires the MapLibre globe, layer toggles, news/webcam tabs, stat strip,
 * clock, DEFCON gauge, search, time-window picker, and SSE live updates.
 * No external code copied — written fresh against common MapLibre patterns.
 */

const $ = (id) => document.getElementById(id);

// ─── Layer registry (mirrors /monitor's structure with cockpit branding) ─
const LAYERS = [
  { id: 'heatmap',           ic: '🟧', name: 'Severity heatmap',  on: true,
    src: 'heatmap' },
  { id: 'critical_findings', ic: '🚨', name: 'Critical findings', on: true,
    src: 'signals', filter: 'critical', color: '#ff4d6a' },
  { id: 'high_findings',     ic: '⚠️', name: 'High findings',     on: true,
    src: 'signals', filter: 'high',     color: '#ffa657' },
  { id: 'sanctioned_dark',   ic: '⚫', name: 'Sanctioned dark',   on: true,
    src: 'signals', filter: 'sanctioned_dark', color: '#ff4d6a' },
  { id: 'sanctioned_rendezvous', ic: '🚨', name: 'Sanctions rendezvous', on: true,
    src: 'signals', filter: 'sanctioned_rendezvous', color: '#ff4d6a' },
  { id: 'shadow_fleet',      ic: '🛢️', name: 'Shadow-fleet',      on: true,
    src: 'signals', filter: 'shadow_fleet', color: '#ff4d6a' },
  { id: 'sanctioned_underway', ic: '📡', name: 'Sanctioned AIS',  on: false,
    src: 'signals', filter: 'sanctioned_underway', color: '#ffa657' },
  { id: 'sanctioned_airspace', ic: '✈️', name: 'Sanctioned airspace', on: false,
    src: 'signals', filter: 'sanctioned_airspace', color: '#ffa657' },
  { id: 'military_air',      ic: '🪖', name: 'Military aircraft', on: false,
    src: 'signals', filter: 'military_air', color: '#d4c43a' },
  { id: 'dark_vessel',       ic: '🌑', name: 'Dark vessels',      on: false,
    src: 'signals', filter: 'dark_vessel', color: '#d4c43a' },
  { id: 'wildfires',         ic: '🔥', name: 'Wildfires',         on: false,
    src: 'signals', filter: 'wildfires', color: '#ff7b3a' },
  { id: 'quakes',            ic: '🌐', name: 'Earthquakes',       on: false,
    src: 'signals', filter: 'quakes',  color: '#d4c43a' },
  { id: 'vessels',           ic: '🚢', name: 'All vessels',       on: false,
    src: 'viewport', kind: 'vessel',   color: '#58a6ff' },
  { id: 'aircraft',          ic: '✈️', name: 'All aircraft',      on: false,
    src: 'viewport', kind: 'aircraft', color: '#c08bff' },
];
const SEV_WEIGHT = { critical: 4, high: 3, medium: 2, low: 1 };

let MAP = null, SSE = null, SSE_RETRY = 1000, REFRESH_TIMER = null;
let WINDOW_H = 24;

// ─── Webcam sets (region toggles in the bottom-right pane) ────────────
const WEBCAM_SETS = {
  global: [
    { label: 'Times Square, NYC', src: 'https://www.youtube.com/embed/wHRNmmuP7q4?autoplay=1&mute=1&controls=0' },
    { label: 'Tokyo Shibuya',     src: 'https://www.youtube.com/embed/3kPH7kTphnE?autoplay=1&mute=1&controls=0' },
    { label: 'Tel Aviv',          src: 'https://www.youtube.com/embed/PlsrxX8cqBE?autoplay=1&mute=1&controls=0' },
    { label: 'Moscow Red Square', src: 'https://www.youtube.com/embed/h1wly909BYw?autoplay=1&mute=1&controls=0' },
  ],
  mideast: [
    { label: 'Tel Aviv',  src: 'https://www.youtube.com/embed/PlsrxX8cqBE?autoplay=1&mute=1&controls=0' },
    { label: 'Jerusalem', src: 'https://www.youtube.com/embed/u1FeGmA5_qs?autoplay=1&mute=1&controls=0' },
    { label: 'Dubai',     src: 'https://www.youtube.com/embed/qe0KlyhrO50?autoplay=1&mute=1&controls=0' },
    { label: 'Beirut',    src: 'https://www.youtube.com/embed/qFKD-vsNqaw?autoplay=1&mute=1&controls=0' },
  ],
  europe: [
    { label: 'Moscow',     src: 'https://www.youtube.com/embed/h1wly909BYw?autoplay=1&mute=1&controls=0' },
    { label: 'London',     src: 'https://www.youtube.com/embed/9Auq9mYxFEE?autoplay=1&mute=1&controls=0' },
    { label: 'Paris',      src: 'https://www.youtube.com/embed/Hh7zrJ1otU8?autoplay=1&mute=1&controls=0' },
    { label: 'Kyiv',       src: 'https://www.youtube.com/embed/MEsKBnRKnmw?autoplay=1&mute=1&controls=0' },
  ],
  asia: [
    { label: 'Tokyo Shibuya', src: 'https://www.youtube.com/embed/3kPH7kTphnE?autoplay=1&mute=1&controls=0' },
    { label: 'Seoul',         src: 'https://www.youtube.com/embed/F-Z7XUuPgSQ?autoplay=1&mute=1&controls=0' },
    { label: 'Hong Kong',     src: 'https://www.youtube.com/embed/Cnt0wsJ4FxE?autoplay=1&mute=1&controls=0' },
    { label: 'Bangkok',       src: 'https://www.youtube.com/embed/zXkS1HqJBSk?autoplay=1&mute=1&controls=0' },
  ],
};

// ─── Layer list render ────────────────────────────────────────────────
function renderLayerList() {
  $('layer-list').innerHTML = LAYERS.map(L => `
    <div class="layer-row${L.on ? ' on' : ''}" data-id="${L.id}">
      <div class="ck"></div>
      <div class="ic">${L.ic}</div>
      <div class="layer-name">${L.name}</div>
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

function setLayerVisibility(L) {
  const v = L.on ? 'visible' : 'none';
  const ids = [L.id];
  if (L.src === 'viewport') ids.push(L.id + '-clusters', L.id + '-cluster-count');
  for (const id of ids) {
    if (MAP.getLayer(id)) MAP.setLayoutProperty(id, 'visibility', v);
  }
}

// ─── Map init ─────────────────────────────────────────────────────────
function initMap() {
  const style = {
    version: 8,
    sources: {
      'osm-dark': {
        type: 'raster',
        tiles: [
          'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
          'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
          'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',
        ],
        tileSize: 256,
      },
    },
    layers: [
      { id: 'background', type: 'background', paint: { 'background-color': '#04060c' } },
      { id: 'osm-dark',   type: 'raster',     source: 'osm-dark',
        paint: { 'raster-opacity': 0.78, 'raster-saturation': -0.3 } },
    ],
  };
  MAP = new maplibregl.Map({
    container: 'map', style,
    center: [10, 25], zoom: 1.8, minZoom: 1, maxZoom: 18,
    attributionControl: false,
  });
  MAP.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

  MAP.on('load', () => {
    for (const L of LAYERS) {
      if (L.src === 'heatmap') {
        MAP.addSource(L.id, { type: 'geojson', data: emptyFC() });
        MAP.addLayer({
          id: L.id, type: 'heatmap', source: L.id,
          paint: {
            'heatmap-weight': ['interpolate', ['linear'], ['get', 'sevWeight'], 0, 0, 4, 1],
            'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 0, 1, 9, 3],
            'heatmap-color': [
              'interpolate', ['linear'], ['heatmap-density'],
              0,    'rgba(0,0,0,0)',
              0.15, 'rgba(63,185,80,0.4)',
              0.35, 'rgba(212,196,58,0.6)',
              0.6,  'rgba(255,166,87,0.75)',
              0.85, 'rgba(255,77,106,0.85)',
              1.0,  'rgba(255,40,80,0.95)',
            ],
            'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 0, 18, 4, 28, 9, 44],
            'heatmap-opacity': ['interpolate', ['linear'], ['zoom'], 7, 0.85, 10, 0.0],
          },
          layout: { visibility: L.on ? 'visible' : 'none' },
        });
        continue;
      }

      const isViewport = L.src === 'viewport';
      MAP.addSource(L.id, {
        type: 'geojson', data: emptyFC(),
        cluster: isViewport, clusterMaxZoom: 9, clusterRadius: 36,
      });
      if (isViewport) {
        MAP.addLayer({
          id: L.id + '-clusters', type: 'circle', source: L.id,
          filter: ['has', 'point_count'],
          paint: {
            'circle-color': L.color, 'circle-opacity': 0.45,
            'circle-radius': ['step', ['get', 'point_count'], 10, 10, 14, 100, 20, 500, 28],
            'circle-stroke-width': 1, 'circle-stroke-color': L.color,
          },
          layout: { visibility: L.on ? 'visible' : 'none' },
        });
        MAP.addLayer({
          id: L.id + '-cluster-count', type: 'symbol', source: L.id,
          filter: ['has', 'point_count'],
          layout: { 'text-field': ['get', 'point_count_abbreviated'],
                    'text-size': 11, 'text-allow-overlap': true,
                    visibility: L.on ? 'visible' : 'none' },
          paint: { 'text-color': '#04060c' },
        });
        MAP.on('click', L.id + '-clusters', (e) => {
          const features = MAP.queryRenderedFeatures(e.point,
            { layers: [L.id + '-clusters'] });
          const cid = features[0].properties.cluster_id;
          MAP.getSource(L.id).getClusterExpansionZoom(cid, (err, z) => {
            if (err) return;
            MAP.easeTo({ center: features[0].geometry.coordinates, zoom: z });
          });
        });
      }
      MAP.addLayer({
        id: L.id, type: 'circle', source: L.id,
        filter: isViewport ? ['!', ['has', 'point_count']] : null,
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 1, 3, 5, 5, 10, 8],
          'circle-color': L.color || '#b042ff',
          'circle-opacity': 0.85,
          'circle-stroke-width': 0.6, 'circle-stroke-color': '#04060c',
        },
        layout: { visibility: L.on ? 'visible' : 'none' },
      });
      MAP.on('click', L.id, (e) => onPinClick(L, e.features[0]));
      MAP.on('mouseenter', L.id, () => MAP.getCanvas().style.cursor = 'pointer');
      MAP.on('mouseleave', L.id, () => MAP.getCanvas().style.cursor = '');
    }
    load();
    startSSE();
  });
}

function emptyFC() { return { type: 'FeatureCollection', features: [] }; }

// ─── Data load ────────────────────────────────────────────────────────
async function load() {
  try {
    const [sig, vessels, aircraft, sumr, health] = await Promise.all([
      fetch(`/api/v1/signals/today?window_hours=${WINDOW_H}&per_category=50`).then(r => r.ok ? r.json() : null),
      fetchViewport('vessel', 1500),
      fetchViewport('aircraft', 1500),
      fetch(`/api/v1/dashboard/summary?window_hours=${WINDOW_H}`).then(r => r.ok ? r.json() : null),
      fetch('/api/v1/health/full').then(r => r.ok ? r.json() : null),
    ]);
    if (sig) paintSignals(sig);
    paintViewport('vessels', vessels);
    paintViewport('aircraft', aircraft);
    if (sumr) updateStats(sumr);
    if (health) updateFooter(health);
    updateOverlay(sig, vessels, aircraft);
  } catch (e) {
    console.error('load failed', e);
  }
}

async function fetchViewport(kind, limit) {
  const since = new Date(Date.now() - WINDOW_H * 3600 * 1000).toISOString();
  const url = `/api/v1/viewport?bbox=-180,-90,180,90&time_from=${encodeURIComponent(since)}` +
              `&types=${kind}&limit=${limit}`;
  try {
    const r = await fetch(url);
    if (!r.ok) return [];
    return (await r.json()).entities || [];
  } catch (e) { return []; }
}

function paintSignals(sig) {
  const buckets = {};
  for (const L of LAYERS) buckets[L.id] = [];
  const heatmap = [];
  for (const cat of sig.categories || []) {
    for (const it of cat.items || []) {
      if (it.lat == null || it.lng == null) continue;
      const f = {
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [it.lng, it.lat] },
        properties: {
          id: it.id,
          title: stripPrefix(it.title || '(untitled)'),
          ts: it.ts, severity: cat.severity,
          category: cat.label, category_id: cat.id,
          entity_id: it.entity_id || null,
          entity_url: (it.links && it.links.entity) || null,
          authority: (it.authority && it.authority.name) || null,
          sevWeight: SEV_WEIGHT[cat.severity] || 1,
        },
      };
      heatmap.push(f);
      for (const L of LAYERS) {
        if (L.src !== 'signals') continue;
        if (L.filter === 'critical' && cat.severity === 'critical') buckets[L.id].push(f);
        else if (L.filter === 'high' && cat.severity === 'high')    buckets[L.id].push(f);
        else if (L.filter === cat.id)                                buckets[L.id].push(f);
      }
    }
  }
  if (MAP.getSource('heatmap'))
    MAP.getSource('heatmap').setData({ type: 'FeatureCollection', features: heatmap });
  $('cnt-heatmap').textContent = heatmap.length;
  for (const L of LAYERS) {
    if (L.src !== 'signals') continue;
    const fc = { type: 'FeatureCollection', features: buckets[L.id] };
    if (MAP.getSource(L.id)) MAP.getSource(L.id).setData(fc);
    const cnt = $(`cnt-${L.id}`);
    if (cnt) cnt.textContent = buckets[L.id].length;
  }
}

function paintViewport(layerId, entities) {
  const features = [];
  for (const e of entities || []) {
    const p = e.position || {};
    if (p.lat == null || p.lng == null) continue;
    features.push({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [p.lng, p.lat] },
      properties: {
        id: e.id, entity_id: e.id, entity_url: `/entity/${e.id}`,
        title: e.display_name || `${e.canonical_id_type}:${e.canonical_id}`,
        kind: e.entity_type,
      },
    });
  }
  if (MAP.getSource(layerId))
    MAP.getSource(layerId).setData({ type: 'FeatureCollection', features });
  const cnt = $(`cnt-${layerId}`);
  if (cnt) cnt.textContent = features.length;
}

function onPinClick(L, feature) {
  const p = feature.properties || {};
  if (p.entity_url) {
    window.location.href = p.entity_url;
  }
}

// ─── Stat strip + overlay + DEFCON ────────────────────────────────────
function updateStats(sum) {
  $('s-signals').textContent  = (sum.signals || 0).toLocaleString();
  $('s-critical').textContent = (sum.critical || 0).toLocaleString();
  $('s-cases').textContent    = (sum.open_cases || 0).toLocaleString();
  $('s-sources').textContent  = sum.sources != null ? sum.sources.toLocaleString() : '—';
  $('s-geo').textContent      = (sum.geolocated || 0).toLocaleString();
  $('s-subs').textContent     = (sum.subscribers || 0).toLocaleString();

  const c = sum.critical || 0;
  const dc = c >= 500 ? 1 : c >= 50 ? 2 : c >= 10 ? 3 : c >= 1 ? 4 : 5;
  const dv = $('defcon-value');
  dv.textContent = String(dc);
  dv.style.color = ({1: 'var(--critical)', 2: 'var(--critical)',
                     3: 'var(--high)', 4: 'var(--medium)', 5: 'var(--low)'})[dc];
}

function updateOverlay(sig, vessels, aircraft) {
  const ts = sig && sig.summary;
  if (ts) $('overlay-findings').textContent = (ts.total_findings || 0).toLocaleString();
  $('overlay-vessels').textContent  = (vessels?.length || 0).toLocaleString();
  $('overlay-aircraft').textContent = (aircraft?.length || 0).toLocaleString();
  $('overlay-window').textContent   = WINDOW_H + 'H';
}

function updateFooter(h) {
  const ing = h.ingesters || {};
  $('ing-count').textContent = `${ing.ok || 0}/${ing.total || 0}`;
  if (h.db && h.db.ok) {
    $('db-latency').textContent = (h.db.latency_ms || 0) + 'ms';
    $('link-status').textContent = 'ACTIVE';
  } else {
    $('db-latency').textContent = 'ERR';
    $('link-status').textContent = 'DEGRADED';
  }
}

// ─── Clock ────────────────────────────────────────────────────────────
function tickClock() {
  const d = new Date();
  const z = (n) => String(n).padStart(2, '0');
  $('clock').textContent =
    `${d.getUTCFullYear()}-${z(d.getUTCMonth()+1)}-${z(d.getUTCDate())} ` +
    `${z(d.getUTCHours())}:${z(d.getUTCMinutes())}:${z(d.getUTCSeconds())} UTC`;
}

// ─── News tab switcher ───────────────────────────────────────────────
$('news-tabs').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-ch]');
  if (!btn) return;
  $('news-tabs').querySelectorAll('button')
    .forEach(b => b.classList.toggle('active', b === btn));
  $('news-frame').src =
    `https://www.youtube.com/embed/live_stream?channel=${btn.dataset.ch}&autoplay=1&mute=1`;
});

// ─── Webcam set switcher ─────────────────────────────────────────────
function renderWebcams(setKey) {
  const tiles = WEBCAM_SETS[setKey] || WEBCAM_SETS.global;
  $('webcam-grid').innerHTML = tiles.map(c => `
    <div class="webcam-tile">
      <div class="label">${c.label}</div>
      <iframe src="${c.src}" allow="autoplay; encrypted-media" allowfullscreen></iframe>
    </div>
  `).join('');
}
$('cam-tabs').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-set]');
  if (!btn) return;
  $('cam-tabs').querySelectorAll('button')
    .forEach(b => b.classList.toggle('active', b === btn));
  renderWebcams(btn.dataset.set);
});

// ─── Time-window picker (top-right of map) ───────────────────────────
document.querySelector('.map-overlay-tr').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-h]');
  if (!btn) return;
  document.querySelector('.map-overlay-tr').querySelectorAll('button')
    .forEach(b => b.classList.toggle('active', b === btn));
  WINDOW_H = parseInt(btn.dataset.h, 10);
  load();
});

// ─── Search → /signals?q=... ─────────────────────────────────────────
$('search').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') {
    const q = ev.target.value.trim();
    if (q) window.location.href = '/signals#q=' + encodeURIComponent(q);
  }
});

// ─── SSE: live pin pulses ────────────────────────────────────────────
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
      id: alert.id, title: stripPrefix(alert.title || ''),
      ts: alert.event_time, severity: sev, category_id: catId,
      entity_id: alert.entity_id || null,
      entity_url: alert.entity_id ? `/entity/${alert.entity_id}` : null,
      sevWeight: SEV_WEIGHT[sev] || 1,
    },
  };
  // Add to heatmap union
  const heatSrc = MAP.getSource('heatmap');
  if (heatSrc) {
    const d = heatSrc._data || { type: 'FeatureCollection', features: [] };
    if (!d.features.some(x => x.properties.id === alert.id)) {
      d.features.unshift(f);
      if (d.features.length > 2000) d.features.pop();
      heatSrc.setData(d);
    }
  }
  for (const L of LAYERS) {
    if (L.src !== 'signals') continue;
    let match = false;
    if (L.filter === 'critical' && sev === 'critical') match = true;
    else if (L.filter === 'high' && sev === 'high')    match = true;
    else if (L.filter === catId)                        match = true;
    if (!match) continue;
    const src = MAP.getSource(L.id);
    if (!src) continue;
    const d = src._data || { type: 'FeatureCollection', features: [] };
    if (d.features.some(x => x.properties.id === alert.id)) continue;
    d.features.unshift(f);
    if (d.features.length > 500) d.features.pop();
    src.setData(d);
    const cnt = $(`cnt-${L.id}`);
    if (cnt) cnt.textContent = d.features.length;
  }
}

// ─── Util ─────────────────────────────────────────────────────────────
function stripPrefix(t) {
  return String(t || '').replace(/^CRITICAL — /, '').replace(/^ALERT — /, '');
}

// ─── Boot ─────────────────────────────────────────────────────────────
renderLayerList();
renderWebcams('global');
tickClock(); setInterval(tickClock, 1000);
initMap();
setInterval(load, 60000);   // refresh stat strip + signals every 60s
