/* Glassbox Globe — front-end controller.
 *
 * 3D globe via globe.gl (MIT) + three.js (MIT) — both freely usable
 * under Glassbox's commercial license. NOT copied from any AGPL source.
 *
 * What this renders:
 *  - Earth sphere with night-side basemap
 *  - Vertical conflict spikes per ~grid cell, height = finding count,
 *    color = max severity in cell
 *  - Live entities (vessels/aircraft) as small dots near surface
 *  - Auto-rotate, atmosphere glow, hover tooltips, click → deep-dive
 *  - SSE real-time push: when a new tier-1 alert fires, a new spike
 *    pulses up at that lat/lng without polling
 */

const $ = (id) => document.getElementById(id);

// ─── Layer registry ───────────────────────────────────────────────────
const LAYERS = [
  { id: 'sanctioned_dark',         icon: '⚫', label: 'Dark sanctioned vessels', on: true },
  { id: 'sanctioned_rendezvous',   icon: '🚨', label: 'Sanctioned rendezvous', on: true },
  { id: 'shadow_fleet',            icon: '🛢️', label: 'Shadow-fleet clusters', on: true },
  { id: 'sanctioned_underway',     icon: '📡', label: 'Sanctioned underway',   on: true },
  { id: 'sanctioned_port',         icon: '⚓', label: 'Sanctioned port arr.',  on: true },
  { id: 'sanctioned_airspace',     icon: '✈️', label: 'Sanctioned airspace',   on: true },
  { id: 'military_air',            icon: '🪖', label: 'Military aircraft',     on: false },
  { id: 'dark_vessel',             icon: '🌑', label: 'Dark vessels',          on: false },
];

let WINDOW_H = 24;
const CELL_DEG = 4.0;          // grid cell size in degrees for spike binning
const REFRESH_SEC = 60;
const SEV_RANK = { critical: 0, high: 1, medium: 2, low: 3 };
const SEV_COLOR = {
  critical: '#ff4d6a',
  high:     '#ffa657',
  medium:   '#d4c43a',
  low:      '#3fb950',
};
const SEV_HEIGHT = { critical: 0.18, high: 0.12, medium: 0.08, low: 0.05 };

let GLOBE = null;
let SSE  = null;
let SSE_RETRY = 1000;
let COUNTDOWN_TIMER = null;
let CURRENT_PAYLOAD = null;
let AUTO_ROTATE = true;
let GLOW = true;

// ─── Boot ─────────────────────────────────────────────────────────────
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
    rebuildSpikes();
  });
}

function initGlobe() {
  if (typeof Globe === 'undefined') {
    $('globe-loading').textContent = 'Failed to load globe.gl from CDN.';
    return;
  }
  GLOBE = Globe()(document.getElementById('globe-container'))
    .globeImageUrl('https://unpkg.com/three-globe/example/img/earth-night.jpg')
    .bumpImageUrl('https://unpkg.com/three-globe/example/img/earth-topology.png')
    .backgroundColor('rgba(0,0,0,0)')
    .showAtmosphere(true)
    .atmosphereColor('#58a6ff')
    .atmosphereAltitude(0.18)
    .pointOfView({ lat: 25, lng: 10, altitude: 2.4 }, 0)
    // Spikes use the points layer with custom altitude per row
    .pointsData([])
    .pointLat('lat').pointLng('lng')
    .pointAltitude(d => d.altitude)
    .pointColor(d => d.color)
    .pointRadius(d => d.radius || 0.3)
    .pointResolution(8)
    .pointsMerge(false)
    .pointsTransitionDuration(800)
    .onPointHover((d) => onSpikeHover(d))
    .onPointClick((d) => onSpikeClick(d))
    // Polylines for connections (e.g. rendezvous pairs) — built later
    .arcsData([])
    .arcStartLat('startLat').arcStartLng('startLng')
    .arcEndLat('endLat').arcEndLng('endLng')
    .arcColor(d => d.color)
    .arcAltitude(0.2)
    .arcStroke(0.4)
    .arcDashLength(0.4).arcDashGap(0.6).arcDashAnimateTime(2000);

  // Auto-rotate + responsive resize
  GLOBE.controls().autoRotate = AUTO_ROTATE;
  GLOBE.controls().autoRotateSpeed = 0.4;
  window.addEventListener('resize', () => {
    GLOBE.width(window.innerWidth - 280 - 360);
    GLOBE.height(window.innerHeight - 44 - 28);
  });
  GLOBE.width(window.innerWidth - 280 - 360);
  GLOBE.height(window.innerHeight - 44 - 28);

  $('globe-loading').remove();
  load();
  startSSE();
}

// ─── Data loading ─────────────────────────────────────────────────────
async function load() {
  try {
    const r = await fetch(`/api/v1/signals/today?window_hours=${WINDOW_H}&per_category=50`);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    CURRENT_PAYLOAD = await r.json();
    rebuildSpikes();
    updateStatus();
    startCountdown();
  } catch (e) {
    console.error('load failed', e);
    $('status-updated').textContent = 'error';
  }
}

// ─── Spike binning (group findings into grid cells, build extrusions) ─
function rebuildSpikes() {
  if (!GLOBE || !CURRENT_PAYLOAD) return;
  const enabledIds = new Set(LAYERS.filter(L => L.on).map(L => L.id));
  // cell key (lat/lng rounded) → { count, maxSev, lat, lng, items[] }
  const cells = new Map();
  let totalForLayer = {};

  for (const cat of CURRENT_PAYLOAD.categories || []) {
    if (!enabledIds.has(cat.id)) continue;
    totalForLayer[cat.id] = (cat.count || 0);
    for (const it of cat.items || []) {
      if (it.lat == null || it.lng == null) continue;
      const lat = Math.floor(it.lat / CELL_DEG) * CELL_DEG + CELL_DEG / 2;
      const lng = Math.floor(it.lng / CELL_DEG) * CELL_DEG + CELL_DEG / 2;
      const key = lat.toFixed(2) + ',' + lng.toFixed(2);
      let cell = cells.get(key);
      if (!cell) {
        cell = { lat, lng, count: 0, maxSev: 'low', items: [], categories: new Set() };
        cells.set(key, cell);
      }
      cell.count += 1;
      cell.items.push({
        id: it.id, title: it.title, ts: it.ts,
        category: cat.label, severity: cat.severity,
        entity_id: it.entity_id || null,
        entity_url: (it.links && it.links.entity) || null,
      });
      cell.categories.add(cat.label);
      if (SEV_RANK[cat.severity] < SEV_RANK[cell.maxSev]) {
        cell.maxSev = cat.severity;
      }
    }
  }

  // Build pointsData for globe.gl. Altitude scaled by count + max severity.
  const points = [];
  for (const cell of cells.values()) {
    const baseHeight = SEV_HEIGHT[cell.maxSev] || 0.05;
    // Log-scale the count so a cell with 100 events doesn't dwarf one with 5
    const heightMultiplier = 1 + Math.log10(cell.count) * 0.6;
    points.push({
      lat: cell.lat,
      lng: cell.lng,
      altitude: Math.min(0.6, baseHeight * heightMultiplier),
      color: SEV_COLOR[cell.maxSev],
      radius: 0.25 + Math.min(0.5, Math.log10(cell.count) * 0.2),
      cell,
    });
  }

  GLOBE.pointsData(points);

  // Build arcs for rendezvous pairs (b_name + a_name in props of a
  // sanctioned_rendezvous category — we only have the centroid lat/lng
  // per event right now; for v1, draw arcs between the 5 highest-sev
  // sanctioned_rendezvous events to demonstrate the visual).
  const arcs = [];
  const rendezvous = (CURRENT_PAYLOAD.categories || [])
    .find(c => c.id === 'sanctioned_rendezvous');
  if (rendezvous && enabledIds.has('sanctioned_rendezvous') && rendezvous.items) {
    for (let i = 0; i < Math.min(5, rendezvous.items.length - 1); i++) {
      const a = rendezvous.items[i];
      const b = rendezvous.items[i + 1];
      if (a.lat == null || b.lat == null) continue;
      arcs.push({
        startLat: a.lat, startLng: a.lng,
        endLat:   b.lat, endLng:   b.lng,
        color: ['#ff4d6a', '#ff4d6a'],
      });
    }
  }
  GLOBE.arcsData(arcs);

  // Update layer-row counts
  for (const L of LAYERS) {
    const el = $(`cnt-${L.id}`);
    if (el) el.textContent = (totalForLayer[L.id] || 0).toLocaleString();
  }

  $('status-spikes').textContent = points.length.toLocaleString();
  $('status-cells').textContent  = cells.size.toLocaleString();
  $('overlay-spikes').textContent = points.length.toLocaleString();
}

// ─── Hover + click ────────────────────────────────────────────────────
function onSpikeHover(d) {
  document.body.style.cursor = d ? 'pointer' : '';
}

function onSpikeClick(d) {
  if (!d || !d.cell) return;
  const c = d.cell;
  $('dd-title').textContent =
    `${c.lat.toFixed(1)}°, ${c.lng.toFixed(1)}° · ${c.count} event${c.count === 1 ? '' : 's'}`;

  const cats = [...c.categories].slice(0, 6).join(' · ');
  const items = c.items.slice(0, 8).map(it => {
    const t = (it.title || '').replace(/^CRITICAL — /, '').replace(/^ALERT — /, '');
    const ago = it.ts ? fmtRel(it.ts) : '';
    const link = it.entity_url
      ? `<a href="${escapeHtml(it.entity_url)}" target="_top">${escapeHtml(t)}</a>`
      : escapeHtml(t);
    return `<li><span class="sev-dot" style="background:${SEV_COLOR[it.severity]}"></span>
      ${link}<div style="color:var(--muted);font-size:10px;margin-top:2px">
      ${escapeHtml(it.category)} &middot; ${ago}</div></li>`;
  }).join('');

  $('dd-body').innerHTML = `
    <div class="deep-section">
      <div class="label">Max severity</div>
      <div class="value ${c.maxSev === 'critical' ? 'crit' : c.maxSev === 'high' ? 'high' : c.maxSev === 'medium' ? 'med' : 'low'}">${c.maxSev.toUpperCase()}</div>
    </div>
    <div class="deep-section">
      <div class="label">Categories in this cell</div>
      <div style="font-size:12px;color:var(--text);margin-top:4px">${escapeHtml(cats)}</div>
    </div>
    <div class="deep-section">
      <div class="label">Findings (latest 8)</div>
      <ul style="list-style:none;margin:8px 0 0;padding:0;font-size:12px">${items}</ul>
    </div>
  `;
  // Inject the sev-dot CSS once — easier than touching the page CSS for
  // a single class.
  if (!document.getElementById('sev-dot-style')) {
    const s = document.createElement('style');
    s.id = 'sev-dot-style';
    s.textContent = `.sev-dot { display:inline-block; width:6px; height:6px;
      border-radius:50%; margin-right:6px; vertical-align:1px; }`;
    document.head.appendChild(s);
  }

  $('app').classList.remove('no-right');
  $('right').classList.remove('collapsed');

  // Pan globe to the clicked cell
  GLOBE.pointOfView({ lat: c.lat, lng: c.lng, altitude: 1.6 }, 1200);
}

$('dd-close').addEventListener('click', () => {
  $('right').classList.add('collapsed');
  $('app').classList.add('no-right');
});

// ─── Controls (auto-rotate, glow) ────────────────────────────────────
$('ctrl-rotate').addEventListener('click', () => {
  AUTO_ROTATE = !AUTO_ROTATE;
  $('ctrl-rotate').querySelector('.ck').classList.toggle('on', AUTO_ROTATE);
  if (GLOBE) GLOBE.controls().autoRotate = AUTO_ROTATE;
});
$('ctrl-glow').addEventListener('click', () => {
  GLOW = !GLOW;
  $('ctrl-glow').querySelector('.ck').classList.toggle('on', GLOW);
  if (GLOBE) GLOBE.showAtmosphere(GLOW);
});

// ─── Status + DEFCON ──────────────────────────────────────────────────
function updateStatus() {
  if (!CURRENT_PAYLOAD) return;
  const sum = CURRENT_PAYLOAD.summary || {};
  $('status-critical').textContent = (sum.critical_count || 0).toLocaleString();
  $('status-updated').textContent = new Date(CURRENT_PAYLOAD.generated_at)
    .toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const c = sum.critical_count || 0;
  const dc = c >= 500 ? 1 : c >= 50 ? 2 : c >= 10 ? 3 : c >= 1 ? 4 : 5;
  const dv = $('defcon-value');
  dv.textContent = String(dc);
  const colors = { 1: 'var(--critical)', 2: 'var(--critical)', 3: 'var(--high)',
                   4: 'var(--medium)',   5: 'var(--low)' };
  dv.style.color = colors[dc];

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

// ─── Time picker ─────────────────────────────────────────────────────
$('time-picker').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-h]');
  if (!btn) return;
  $('time-picker').querySelectorAll('button')
    .forEach(b => b.classList.toggle('active', b === btn));
  WINDOW_H = parseInt(btn.dataset.h, 10);
  load();
});

// ─── SSE: real-time spike pulses ──────────────────────────────────────
const TYPE_TO_CAT = {
  sanctioned_vessel_went_dark:      'sanctioned_dark',
  sanctioned_vessel_rendezvous:     'sanctioned_rendezvous',
  shadow_fleet_cluster:             'shadow_fleet',
  sanctioned_vessel_underway:       'sanctioned_underway',
  sanctioned_port_arrival:          'sanctioned_port',
  aircraft_in_sanctioned_airspace:  'sanctioned_airspace',
  military_aircraft_underway:       'military_air',
  dark_vessel_detected:             'dark_vessel',
};

function startSSE() {
  if (typeof EventSource === 'undefined') return;
  if (SSE) SSE.close();
  SSE = new EventSource('/api/v1/alerts/stream?poll_sec=5');
  SSE.addEventListener('hello', () => { SSE_RETRY = 1000; });
  SSE.addEventListener('alert', (ev) => {
    try {
      const a = JSON.parse(ev.data);
      patchSpike(a);
    } catch (e) { /* swallow */ }
  });
  SSE.onerror = () => {
    SSE.close();
    SSE_RETRY = Math.min(SSE_RETRY * 2, 30000);
    setTimeout(startSSE, SSE_RETRY);
  };
}

function patchSpike(alert) {
  if (!CURRENT_PAYLOAD || !GLOBE) return;
  if (alert.lat == null || alert.lng == null) return;
  const catId = TYPE_TO_CAT[alert.event_type];
  if (!catId) return;
  // Find the matching category in CURRENT_PAYLOAD; if missing, skip.
  const cat = (CURRENT_PAYLOAD.categories || []).find(c => c.id === catId);
  if (!cat) return;
  // Don't double-add
  if ((cat.items || []).some(it => it.id === alert.id)) return;
  cat.items.unshift({
    id: alert.id, title: alert.title, ts: alert.event_time,
    severity: cat.severity,
    lat: alert.lat, lng: alert.lng,
    entity_id: alert.entity_id || null,
    links: alert.entity_id ? { entity: '/entity/' + alert.entity_id } : null,
  });
  if (cat.items.length > 50) cat.items.pop();
  cat.count = (cat.count || 0) + 1;
  rebuildSpikes();
}

// ─── Util ─────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[m]));
}
function fmtRel(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 1)  return 'just now';
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}

renderLayerList();
initGlobe();
