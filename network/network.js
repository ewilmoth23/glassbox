/* Glassbox Network — entity-relationship graph controller.
 *
 * Renders a force-directed graph (vis-network, Apache-2) of multi-
 * entity findings:
 *   nodes = entities (vessels, sanctioned vessels, alias matches)
 *   edges = the findings that connect them (cross-domain, rendezvous,
 *           shadow-fleet cluster pairs)
 *
 * Boot data flow:
 *   1. fetch /api/v1/signals/today (window 7d, per_category=50)
 *   2. extract entity_ids from the multi-entity findings
 *      (sanctioned_vessel_rendezvous, shadow_fleet_cluster, ...)
 *   3. for the first ~10 findings, fan out to
 *      /api/v1/entities/{id}/cross_domain to harvest partner pairs
 *      (5 in parallel; capped at 200 nodes / 400 edges)
 *   4. hydrate node display_name / entity_type via /api/v1/entity/{id}
 *      (5 in parallel; capped at 50 hydrations)
 *
 * Optional live updates: /api/v1/alerts/stream SSE → patch new
 * rendezvous_detected / sanctioned_* alerts as edges into the graph.
 *
 * No backend endpoints added; no copied code. */

const $ = (id) => document.getElementById(id);

// ─── Config ──────────────────────────────────────────────────────────
const MAX_NODES         = 200;
const MAX_EDGES         = 400;
const SEED_FINDING_CAP  = 12;   // most-recent multi-entity findings to seed from
const HYDRATE_CAP       = 50;   // max /entity/{id} calls
const PARALLEL_FANOUT   = 5;    // concurrency for both /cross_domain and /entity

// Multi-entity event types we render edges for.
// Mapped to the visual edge style below.
const RENDEZVOUS_TYPES = new Set([
  'sanctioned_vessel_rendezvous',
  'rendezvous_detected',
]);
const SHADOW_TYPES = new Set([
  'shadow_fleet_cluster',
]);

// Node-class color palette (mirrors index.html CSS variables — kept
// in JS so the vis-network draw layer can read them without DOM peek).
const NODE_COLORS = {
  sanctioned: { bg: '#ff4d6a', border: '#b81f3a' },
  alias:      { bg: '#ffa657', border: '#b96d2a' },
  other:      { bg: '#79c0ff', border: '#3b82c4' },
  unknown:    { bg: '#6b7280', border: '#3f4248' },
};

// Layer registry — left-sidebar toggles control visibility of these
// classes on the graph. Edge layers control edge style classes.
const NODE_LAYERS = [
  { id: 'sanctioned', cls: 'sanctioned', label: 'Sanctioned vessels',          on: true,
    color: '#ff4d6a' },
  { id: 'alias',      cls: 'alias',      label: 'Live vessels matched (alias)', on: true,
    color: '#ffa657' },
  { id: 'other',      cls: 'other',      label: 'Cross-domain partners',        on: true,
    color: '#79c0ff' },
];
const EDGE_LAYERS = [
  { id: 'cross',       cls: 'cross',       label: 'Cross-domain edges',     on: true,
    color: 'rgba(122,133,151,0.55)' },
  { id: 'rendezvous',  cls: 'rendezvous',  label: 'Rendezvous edges',       on: true,
    color: '#ff4d6a' },
  { id: 'shadow',      cls: 'shadow',      label: 'Shadow-fleet edges',     on: true,
    color: '#c08bff' },
];

// ─── State ───────────────────────────────────────────────────────────
let WINDOW_H = 168;
let NETWORK   = null;
let NODES_DS  = null;     // vis.DataSet — node rows
let EDGES_DS  = null;     // vis.DataSet — edge rows
let NODE_INFO = new Map();// node_id → { class, partners:[], events:[], display_name, entity_type, canonical_id }
let SSE       = null;
let SSE_RETRY = 1000;

// P2-D click-to-expand state:
//   EXPANDED_NODES — set of node_ids whose /cross_domain has been
//   fetched + merged into the graph by a user click. Used to drive
//   the "expanded vs collapsed" visual border treatment and to
//   prevent the same node from being expanded twice.
//   EXPANDING_NODES — transient set: a node is in here from the moment
//   the click fires to the moment fetch+ingest+hydrate finishes. Lets
//   us short-circuit duplicate clicks while a fetch is in flight.
let EXPANDED_NODES  = new Set();
let EXPANDING_NODES = new Set();

// ─── Layer-toggle UI ─────────────────────────────────────────────────
function renderLayerLists() {
  const renderRow = (L, kind) => `
    <div class="layer-row${L.on ? ' on' : ''}" data-id="${L.id}" data-kind="${kind}">
      <div class="ck"></div>
      <div class="swatch${kind === 'edge' ? ' edge' : ''}" style="background:${L.color}"></div>
      <div class="layer-name">${L.label}</div>
      <div class="count" id="cnt-${kind}-${L.id}">0</div>
    </div>`;
  $('node-layer-list').innerHTML = NODE_LAYERS.map(L => renderRow(L, 'node')).join('');
  $('edge-layer-list').innerHTML = EDGE_LAYERS.map(L => renderRow(L, 'edge')).join('');

  const onClick = (ev) => {
    const row = ev.target.closest('.layer-row');
    if (!row) return;
    const kind = row.dataset.kind;
    const list = kind === 'edge' ? EDGE_LAYERS : NODE_LAYERS;
    const L = list.find(x => x.id === row.dataset.id);
    if (!L) return;
    L.on = !L.on;
    row.classList.toggle('on', L.on);
    applyLayerVisibility();
  };
  $('node-layer-list').addEventListener('click', onClick);
  $('edge-layer-list').addEventListener('click', onClick);
}

function applyLayerVisibility() {
  if (!NODES_DS || !EDGES_DS) return;
  const nodeOn = Object.fromEntries(NODE_LAYERS.map(L => [L.cls, L.on]));
  const edgeOn = Object.fromEntries(EDGE_LAYERS.map(L => [L.cls, L.on]));
  NODES_DS.forEach((n) => {
    const visible = nodeOn[n._cls] !== false;
    if (n.hidden !== !visible) NODES_DS.update({ id: n.id, hidden: !visible });
  });
  EDGES_DS.forEach((e) => {
    const visible = edgeOn[e._cls] !== false;
    if (e.hidden !== !visible) EDGES_DS.update({ id: e.id, hidden: !visible });
  });
  refreshOverlay();
}

// ─── Network init ────────────────────────────────────────────────────
function initNetwork() {
  NODES_DS = new vis.DataSet([]);
  EDGES_DS = new vis.DataSet([]);
  const container = $('graph');
  const data = { nodes: NODES_DS, edges: EDGES_DS };
  const options = {
    nodes: {
      shape: 'dot',
      size: 14,
      borderWidth: 2,
      font: {
        color: '#e6edf3',
        face: 'ui-monospace, "SF Mono", Menlo, monospace',
        size: 11,
        strokeWidth: 3,
        strokeColor: '#0b0e14',
      },
    },
    edges: {
      width: 1,
      smooth: { type: 'continuous', roundness: 0.4 },
      arrows: { to: { enabled: false } },
      color: { color: 'rgba(122,133,151,0.55)', highlight: '#79c0ff' },
    },
    physics: {
      enabled: true,
      barnesHut: {
        gravitationalConstant: -3500,
        centralGravity: 0.25,
        springLength: 110,
        springConstant: 0.04,
        damping: 0.5,
        avoidOverlap: 0.6,
      },
      stabilization: { iterations: 180 },
    },
    interaction: {
      hover: true,
      tooltipDelay: 120,
      navigationButtons: false,
      keyboard: false,
    },
  };
  NETWORK = new vis.Network(container, data, options);
  NETWORK.on('click', (params) => {
    if (params.nodes && params.nodes.length) {
      onNodeClick(String(params.nodes[0]));
    }
  });
}

// ─── Data loading ────────────────────────────────────────────────────
async function load() {
  $('graph-loading').style.display = '';
  $('graph-empty').style.display = 'none';
  // Reset state
  NODE_INFO = new Map();
  if (NODES_DS) NODES_DS.clear();
  if (EDGES_DS) EDGES_DS.clear();

  let seedEntityIds = [];
  let summary = {};
  try {
    const r = await fetch(`/api/v1/signals/today?window_hours=${WINDOW_H}&per_category=50`);
    if (!r.ok) throw new Error(`signals HTTP ${r.status}`);
    const sig = await r.json();
    summary = sig.summary || {};
    seedEntityIds = extractSeedEntityIds(sig);
  } catch (e) {
    console.error('signals load failed', e);
    $('graph-loading').textContent = 'Failed to load signals.';
    return;
  }

  if (!seedEntityIds.length) {
    $('graph-loading').style.display = 'none';
    $('graph-empty').style.display = '';
    updateStatusbar(summary);
    return;
  }

  // Cap the seed set to keep the first /cross_domain fan-out bounded.
  const seeds = seedEntityIds.slice(0, SEED_FINDING_CAP);
  const cdResults = await mapPool(seeds, PARALLEL_FANOUT, fetchCrossDomain);
  for (let i = 0; i < seeds.length; i += 1) {
    const ent  = seeds[i];
    const data = cdResults[i];
    if (!data) continue;
    ingestCrossDomain(ent, data);
    if (NODES_DS.length >= MAX_NODES || EDGES_DS.length >= MAX_EDGES) break;
  }

  // Hydrate display_name / entity_type for the node set we ended up with.
  const toHydrate = [];
  NODE_INFO.forEach((info, id) => {
    if (!info.display_name) toHydrate.push(id);
  });
  const hyd = toHydrate.slice(0, HYDRATE_CAP);
  const hydResults = await mapPool(hyd, PARALLEL_FANOUT, fetchEntityDetail);
  for (let i = 0; i < hyd.length; i += 1) {
    if (hydResults[i]) applyHydration(hyd[i], hydResults[i]);
  }

  $('graph-loading').style.display = 'none';
  if (NODES_DS.length === 0) $('graph-empty').style.display = '';

  applyLayerVisibility();
  updateStatusbar(summary);
}

// Walk the /signals/today response and pull entity_ids from the
// multi-entity categories. The response only carries the primary
// entity_id per item, but the same entity_id is what /cross_domain
// keys off — calling /cross_domain on it returns each event's
// `partners` array which IS the edge set we want.
function extractSeedEntityIds(sig) {
  const seen = new Set();
  const out  = [];
  // Severity priority — critical multi-entity findings first.
  const orderedCategories = (sig.categories || []).slice().sort((a, b) => {
    const sevRank = { critical: 0, high: 1, medium: 2, low: 3 };
    return (sevRank[a.severity] ?? 9) - (sevRank[b.severity] ?? 9);
  });
  for (const cat of orderedCategories) {
    // Only multi-entity-flavored categories have edges to draw.
    // (Single-entity categories like "wildfires" don't produce a graph.)
    if (!isMultiEntityCategory(cat.id)) continue;
    for (const it of cat.items || []) {
      if (!it.entity_id || seen.has(it.entity_id)) continue;
      seen.add(it.entity_id);
      out.push(it.entity_id);
    }
  }
  return out;
}

function isMultiEntityCategory(catId) {
  // Whitelist of category-ids known to map to multi-entity event_types.
  // Source of truth: api_v1.py _SIGNALS_CATEGORY_ORDER.
  return catId === 'sanctioned_rendezvous'
      || catId === 'shadow_fleet'
      || catId === 'sanctioned_dark'        // alias-cluster + co-occurrence
      || catId === 'sanctioned_underway'
      || catId === 'sanctioned_port';
}

async function fetchCrossDomain(entityId) {
  try {
    const url = `/api/v1/entities/${entityId}/cross_domain`
              + `?within_hours=${WINDOW_H}&limit=20`;
    const r = await fetch(url);
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    return null;
  }
}

async function fetchEntityDetail(entityId) {
  try {
    const r = await fetch(`/api/v1/entity/${entityId}`);
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    return null;
  }
}

// Bounded-concurrency map. Lets us throttle parallel fan-out without
// pulling in p-limit — about 25 lines vs another CDN dependency.
async function mapPool(items, concurrency, fn) {
  const out = new Array(items.length);
  let cursor = 0;
  const workers = new Array(Math.max(1, Math.min(concurrency, items.length)))
    .fill(0).map(async () => {
      while (true) {
        const i = cursor; cursor += 1;
        if (i >= items.length) return;
        out[i] = await fn(items[i]);
      }
    });
  await Promise.all(workers);
  return out;
}

// ─── Ingest one /cross_domain payload into the graph ─────────────────
function ingestCrossDomain(centerEntityId, payload) {
  if (!payload || !Array.isArray(payload.events)) return;
  ensureNode(centerEntityId, classifyFromEvents(payload.events, centerEntityId));
  for (const ev of payload.events) {
    const evType = ev.event_type;
    const partners = ev.partners || [];
    for (const p of partners) {
      if (!p.entity_id) continue;
      if (NODES_DS.length >= MAX_NODES || EDGES_DS.length >= MAX_EDGES) return;
      const partnerCls = classifyPartner(p);
      ensureNode(p.entity_id, partnerCls, {
        display_name: p.display_name,
        canonical_id: p.canonical_id,
        canonical_id_type: p.canonical_id_type,
        entity_type: p.entity_type,
      });
      // Draw an edge between the center and each partner.
      addEdge(centerEntityId, p.entity_id, ev);
      // Track per-node partner+event for the deep-dive sidebar.
      addPartnerLinkage(centerEntityId, p, ev);
      addPartnerLinkage(p.entity_id, {
        entity_id: centerEntityId,
        display_name: NODE_INFO.get(centerEntityId)?.display_name,
        canonical_id: NODE_INFO.get(centerEntityId)?.canonical_id,
        entity_type: NODE_INFO.get(centerEntityId)?.entity_type,
      }, ev);
    }
  }
}

// Pick a node class. Sanctioned-flavored event types make the center
// node 'sanctioned'; otherwise fall back to 'other'.
function classifyFromEvents(events, _id) {
  for (const ev of events) {
    if (typeof ev.event_type === 'string'
        && ev.event_type.startsWith('sanctioned_')) {
      return 'sanctioned';
    }
  }
  return 'other';
}

function classifyPartner(p) {
  // Heuristic: when the entity_type carries 'sanction' we treat it
  // as sanctioned. Otherwise default to 'other'. The hydration pass
  // can re-classify into 'alias' downstream.
  const t = (p.entity_type || '').toLowerCase();
  if (t.indexOf('sanction') !== -1) return 'sanctioned';
  return 'other';
}

function ensureNode(id, cls, fields = {}) {
  if (NODE_INFO.has(id)) {
    const info = NODE_INFO.get(id);
    if (fields.display_name && !info.display_name) info.display_name = fields.display_name;
    if (fields.canonical_id && !info.canonical_id) info.canonical_id = fields.canonical_id;
    if (fields.canonical_id_type && !info.canonical_id_type) info.canonical_id_type = fields.canonical_id_type;
    if (fields.entity_type && !info.entity_type) info.entity_type = fields.entity_type;
    // Promote sanctioned class — it's the most informative.
    if (cls === 'sanctioned' && info._cls !== 'sanctioned') {
      info._cls = 'sanctioned';
      NODES_DS.update(buildNodeRow(id, info));
    }
    return;
  }
  const info = {
    _cls:        cls || 'unknown',
    display_name: fields.display_name || null,
    canonical_id: fields.canonical_id || null,
    canonical_id_type: fields.canonical_id_type || null,
    entity_type:  fields.entity_type || null,
    partners:     [],
    events:       [],
  };
  NODE_INFO.set(id, info);
  NODES_DS.add(buildNodeRow(id, info));
}

function buildNodeRow(id, info) {
  const palette = NODE_COLORS[info._cls] || NODE_COLORS.unknown;
  const label   = nodeLabel(info);
  const expanded  = EXPANDED_NODES.has(id);
  const expanding = EXPANDING_NODES.has(id);
  // Visual affordance (P2-D): expanded nodes get a bright white border to
  // distinguish them from "click to expand" candidates. Un-clicked nodes
  // keep their muted class-color border. While a fetch is in flight, the
  // border is yellow so the user sees a momentary "loading" pulse.
  const borderColor = expanding ? '#ffd166'
                    : expanded  ? '#ffffff'
                                : palette.border;
  const borderWidth = (expanded || expanding) ? 3 : 2;
  const titleParts = [
    info.display_name || '(unnamed)',
    info.entity_type ? `type: ${info.entity_type}` : null,
    info.canonical_id ? `${info.canonical_id_type || 'id'}: ${info.canonical_id}` : null,
  ].filter(Boolean);
  if (!expanded && !expanding) {
    titleParts.push('Click to expand connections');
  }
  return {
    id: id,
    label: label,
    title: titleParts.join('\n'),
    borderWidth: borderWidth,
    color: { background: palette.bg, border: borderColor,
             highlight: { background: palette.bg, border: '#ffffff' } },
    _cls: info._cls,
  };
}

function nodeLabel(info) {
  if (info.display_name && info.display_name.length <= 22) return info.display_name;
  if (info.display_name) return info.display_name.slice(0, 21) + '…';
  if (info.canonical_id) return info.canonical_id;
  return '';
}

// Edges are de-duped by (a, b) pair sorted alphabetically — undirected.
function addEdge(a, b, ev) {
  if (a === b) return;
  const lo = a < b ? a : b;
  const hi = a < b ? b : a;
  const id = `e:${lo}|${hi}`;
  const cls = edgeClass(ev.event_type);
  const palette = EDGE_LAYERS.find(L => L.cls === cls);
  const color = palette ? palette.color : 'rgba(122,133,151,0.55)';
  const isStrong = cls !== 'cross';
  const existing = EDGES_DS.get(id);
  if (existing) {
    // Promote a weaker class to a stronger one (rendezvous/shadow win
    // over generic cross-domain).
    if (existing._cls === 'cross' && isStrong) {
      EDGES_DS.update({ id, _cls: cls,
        color: { color, highlight: '#ffffff' }, width: 2 });
    }
    return;
  }
  EDGES_DS.add({
    id, from: lo, to: hi,
    color: { color, highlight: '#ffffff' },
    width: isStrong ? 2 : 1,
    title: ev.title || ev.event_type || 'cross-domain',
    _cls: cls,
  });
}

function edgeClass(evType) {
  if (RENDEZVOUS_TYPES.has(evType)) return 'rendezvous';
  if (SHADOW_TYPES.has(evType))     return 'shadow';
  return 'cross';
}

function addPartnerLinkage(nodeId, partner, ev) {
  const info = NODE_INFO.get(nodeId);
  if (!info) return;
  if (partner.entity_id && partner.entity_id !== nodeId) {
    if (!info.partners.some(p => p.entity_id === partner.entity_id)) {
      info.partners.push({
        entity_id:   partner.entity_id,
        display_name: partner.display_name || null,
        canonical_id: partner.canonical_id || null,
        entity_type:  partner.entity_type || null,
      });
    }
  }
  if (!info.events.some(e => e.id === ev.id)) {
    info.events.push({
      id:         ev.id,
      event_type: ev.event_type,
      severity:   ev.severity,
      title:      ev.title,
      ts:         ev.event_time,
    });
  }
}

// Promote a node from 'other' → 'alias' when /entity says it's a live
// vessel that has matched a sanctioned alias cluster (sentinel
// property carried in the entity properties bag by the Splink
// pipeline). Falls back to filling in display_name when not.
function applyHydration(id, detail) {
  const info = NODE_INFO.get(id);
  if (!info) return;
  const ent = (detail && (detail.entity || detail)) || {};
  const props = ent.properties || {};
  if (!info.display_name && ent.display_name) info.display_name = ent.display_name;
  if (!info.entity_type   && ent.entity_type)   info.entity_type = ent.entity_type;
  if (!info.canonical_id  && ent.canonical_id)  info.canonical_id = ent.canonical_id;
  if (!info.canonical_id_type && ent.canonical_id_type) info.canonical_id_type = ent.canonical_id_type;

  // Splink alias-cluster sentinels — carry over from sanctioned matches.
  const aliasMatch = props.sanctioned_alias_match
                  || props.alias_cluster_id
                  || props.splink_match_score;
  if (aliasMatch && info._cls !== 'sanctioned') info._cls = 'alias';

  NODES_DS.update(buildNodeRow(id, info));
}

// ─── Status bar / overlay ────────────────────────────────────────────
function refreshOverlay() {
  const visibleNodes = NODES_DS.get({ filter: n => !n.hidden }).length;
  const visibleEdges = EDGES_DS.get({ filter: e => !e.hidden }).length;
  $('overlay-nodes').textContent = visibleNodes.toLocaleString();
  $('overlay-edges').textContent = visibleEdges.toLocaleString();
  $('overlay-window').textContent = humanWindow(WINDOW_H);

  $('status-nodes').textContent = NODES_DS.length.toLocaleString();
  $('status-edges').textContent = EDGES_DS.length.toLocaleString();
  let sanctioned = 0, aliases = 0;
  NODE_INFO.forEach((info) => {
    if (info._cls === 'sanctioned') sanctioned += 1;
    if (info._cls === 'alias')      aliases    += 1;
  });
  $('status-sanctioned').textContent = sanctioned.toLocaleString();
  $('status-aliases').textContent    = aliases.toLocaleString();

  // Per-layer counts in the left sidebar
  const nodeCounts = { sanctioned: 0, alias: 0, other: 0 };
  NODE_INFO.forEach((info) => {
    if (nodeCounts[info._cls] != null) nodeCounts[info._cls] += 1;
  });
  for (const [cls, n] of Object.entries(nodeCounts)) {
    const el = $(`cnt-node-${cls}`);
    if (el) el.textContent = n;
  }
  const edgeCounts = { cross: 0, rendezvous: 0, shadow: 0 };
  EDGES_DS.forEach((e) => { if (edgeCounts[e._cls] != null) edgeCounts[e._cls] += 1; });
  for (const [cls, n] of Object.entries(edgeCounts)) {
    const el = $(`cnt-edge-${cls}`);
    if (el) el.textContent = n;
  }
}

function updateStatusbar(summary) {
  refreshOverlay();
  $('status-updated').textContent = new Date()
    .toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  // DEFCON: same mapping as /monitor — keyed off critical_count.
  const c = summary.critical_count || 0;
  const dc = c >= 500 ? 1 : c >= 50 ? 2 : c >= 10 ? 3 : c >= 1 ? 4 : 5;
  const dv = $('defcon-value');
  dv.textContent = String(dc);
  const colors = { 1: 'var(--critical)', 2: 'var(--critical)', 3: 'var(--high)',
                   4: 'var(--medium)',   5: 'var(--low)' };
  dv.style.color = colors[dc];
}

function humanWindow(h) {
  if (h >= 168) return Math.round(h / 24) + 'D';
  return h + 'H';
}

// ─── Click → deep-dive in right sidebar + expand graph (P2-D) ────────
function onNodeClick(id) {
  const info = NODE_INFO.get(id);
  if (!info) return;
  // Kick off graph expansion in parallel with rendering the sidebar.
  // The sidebar shows whatever partners we already know about; once
  // expandNode finishes, we re-render the sidebar so newly-discovered
  // partners + events appear.
  expandNode(id);
  $('dd-title').textContent = info.display_name || info.canonical_id || 'Entity';

  const lines = [];
  lines.push(line('Display name', info.display_name || '(unnamed)'));
  if (info.entity_type)  lines.push(line('Type', info.entity_type));
  if (info.canonical_id) lines.push(line(info.canonical_id_type || 'Canonical ID', info.canonical_id));
  lines.push(line('UUID', id));
  lines.push(line('Class', classLabel(info._cls)));

  let partnersHtml = '';
  if (info.partners.length) {
    const items = info.partners.slice(0, 30).map(p => `
      <li>
        <a href="/entity/${escapeAttr(p.entity_id)}">
          ${escapeHtml(p.display_name || p.canonical_id || p.entity_id)}
        </a>
        <div class="meta">${escapeHtml((p.entity_type || '') + (p.canonical_id ? ' · ' + p.canonical_id : ''))}</div>
      </li>`).join('');
    partnersHtml = `<div class="deep-section">
      <div class="label">Partners (${info.partners.length})</div>
      <ul class="partners">${items}</ul>
    </div>`;
  }

  let eventsHtml = '';
  if (info.events.length) {
    const items = info.events.slice(0, 20).map(ev => {
      const sevCls = ev.severity === 'critical' ? 'crit'
                   : ev.severity === 'high'     ? 'high' : '';
      return `
      <li>
        <span class="ev-type ${sevCls}">${escapeHtml(ev.event_type || 'event')}</span>
        ${escapeHtml((ev.title || '').replace(/^CRITICAL — /, '').replace(/^ALERT — /, ''))}
        <div class="meta">${escapeHtml(ev.ts ? new Date(ev.ts).toLocaleString() : '')}</div>
      </li>`;
    }).join('');
    eventsHtml = `<div class="deep-section">
      <div class="label">Connecting events (${info.events.length})</div>
      <ul class="events">${items}</ul>
    </div>`;
  }

  const profileLink = `<div class="deep-section">
    <a href="/entity/${escapeAttr(id)}" style="display:inline-block;
        padding:8px 14px;background:var(--accent);color:#000;border-radius:4px;
        font-weight:700;text-transform:uppercase;letter-spacing:0.06em;font-size:11px">
        Open entity profile →</a></div>`;

  $('dd-body').innerHTML = lines.join('') + partnersHtml + eventsHtml + profileLink;
  $('app').classList.remove('no-right');
  $('right').classList.remove('collapsed');
}

// P2-D — click-to-expand. Fetch this node's cross-domain partners,
// merge into the existing graph, hydrate display names, and update
// the visual border to mark the node as "expanded".
//
// Bounded by the existing MAX_NODES / MAX_EDGES caps in
// ingestCrossDomain — once those are hit, additional partners are
// silently dropped. This keeps the layout readable.
//
// Idempotent: clicking an already-expanded node is a no-op (the
// sidebar still re-renders, but no second fetch fires).
async function expandNode(id) {
  if (EXPANDED_NODES.has(id) || EXPANDING_NODES.has(id)) return;
  const info = NODE_INFO.get(id);
  if (!info) return;
  EXPANDING_NODES.add(id);
  // Flip border to "loading" color immediately so the user gets feedback
  // before the (potentially 200-500ms) fetch resolves.
  NODES_DS.update(buildNodeRow(id, info));

  const nodesBefore = NODES_DS.length;
  const edgesBefore = EDGES_DS.length;
  let payload = null;
  try {
    payload = await fetchCrossDomain(id);
  } catch (e) {
    console.error('expandNode fetch failed', id, e);
  }

  EXPANDING_NODES.delete(id);
  EXPANDED_NODES.add(id);

  if (payload && Array.isArray(payload.events)) {
    ingestCrossDomain(id, payload);
    // Hydrate any brand-new partner nodes that still lack a display name.
    const toHydrate = [];
    NODE_INFO.forEach((nInfo, nid) => {
      if (!nInfo.display_name && nid !== id) toHydrate.push(nid);
    });
    const cap = Math.max(0, MAX_NODES - NODES_DS.length + toHydrate.length);
    const slice = toHydrate.slice(0, Math.min(cap, 20));
    if (slice.length) {
      const hyd = await mapPool(slice, PARALLEL_FANOUT, fetchEntityDetail);
      for (let i = 0; i < slice.length; i += 1) {
        if (hyd[i]) applyHydration(slice[i], hyd[i]);
      }
    }
  }

  // Flip the border to "expanded" (overrides "loading").
  NODES_DS.update(buildNodeRow(id, info));

  const nodesAdded = NODES_DS.length - nodesBefore;
  const edgesAdded = EDGES_DS.length - edgesBefore;
  if (nodesAdded || edgesAdded) {
    applyLayerVisibility(); // re-applies hidden flags + refreshes overlay
    // Sidebar may have been opened against this node — re-render so any
    // newly-discovered partners + events appear.
    onNodeClickRerender(id);
  } else {
    // No new data — refresh the title so it shows "no additional
    // connections found" rather than the old "click to expand" hint.
    refreshOverlay();
  }
}

// Re-render the right sidebar for the currently-displayed node, without
// re-kicking the expansion. Called after expandNode adds new partners.
function onNodeClickRerender(id) {
  // Skip if the sidebar is closed.
  if ($('right').classList.contains('collapsed')) return;
  const info = NODE_INFO.get(id);
  if (!info) return;
  // Only re-render if the displayed title still matches this node — if
  // the user has clicked elsewhere meanwhile, leave the sidebar alone.
  const displayed = $('dd-title').textContent;
  const expectedTitle = info.display_name || info.canonical_id || 'Entity';
  if (displayed !== expectedTitle) return;
  // Cheap path: invoke onNodeClick with a marker that suppresses the
  // re-fetch. We accomplish that by EXPANDED_NODES already containing
  // `id`, so the expandNode() call short-circuits.
  onNodeClick(id);
}

function classLabel(cls) {
  if (cls === 'sanctioned') return 'Sanctioned';
  if (cls === 'alias')      return 'Alias-matched (live vessel)';
  if (cls === 'other')      return 'Cross-domain partner';
  return 'Unknown';
}

function line(label, value) {
  return `<div class="deep-section">
    <div class="label">${escapeHtml(label)}</div>
    <div class="value">${escapeHtml(value)}</div>
  </div>`;
}

$('dd-close').addEventListener('click', () => {
  $('right').classList.add('collapsed');
  $('app').classList.add('no-right');
});

// ─── Time-window picker ──────────────────────────────────────────────
$('time-picker').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-h]');
  if (!btn) return;
  $('time-picker').querySelectorAll('button')
    .forEach(b => b.classList.toggle('active', b === btn));
  WINDOW_H = parseInt(btn.dataset.h, 10);
  load();
});

// ─── SSE: live patches ───────────────────────────────────────────────
// When a new alert arrives that names an entity we already render,
// we add (or strengthen) an edge from that entity to whatever
// counterparties the alert names. Cheap and additive — no full
// reload. Mirrors the SSE pattern in /monitor + /globe.
function startSSE() {
  if (typeof EventSource === 'undefined') { $('status-sse').textContent = 'n/a'; return; }
  if (SSE) SSE.close();
  SSE = new EventSource('/api/v1/alerts/stream?poll_sec=5');
  $('status-sse').textContent = 'connecting';
  SSE.addEventListener('hello', () => {
    SSE_RETRY = 1000; $('status-sse').textContent = 'live';
  });
  SSE.addEventListener('alert', (ev) => {
    try { onSSEAlert(JSON.parse(ev.data)); } catch (e) { /* swallow */ }
  });
  SSE.onerror = () => {
    SSE.close();
    $('status-sse').textContent = 'reconnecting';
    SSE_RETRY = Math.min(SSE_RETRY * 2, 30000);
    setTimeout(startSSE, SSE_RETRY);
  };
}

function onSSEAlert(alert) {
  if (!NODES_DS) return;
  // Only act on multi-entity event types that yield edges.
  if (!RENDEZVOUS_TYPES.has(alert.event_type)
      && !SHADOW_TYPES.has(alert.event_type)) return;
  const center = alert.entity_id;
  if (!center) return;
  const partners = Array.isArray(alert.partner_entity_ids)
    ? alert.partner_entity_ids : [];
  if (!partners.length) return;
  // Only add edges between nodes already in the graph — we don't
  // explode the graph on every live alert. The next full reload will
  // pick up brand-new entities.
  if (!NODE_INFO.has(center)) return;
  const evShim = {
    id: alert.id || `sse:${Date.now()}`,
    event_type: alert.event_type,
    severity: alert.severity || 'high',
    title: alert.title || alert.event_type,
    event_time: alert.event_time || new Date().toISOString(),
  };
  for (const pid of partners) {
    if (pid === center) continue;
    if (!NODE_INFO.has(pid)) continue;
    addEdge(center, pid, evShim);
  }
  refreshOverlay();
}

// ─── Util ────────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, m => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[m]));
}
function escapeAttr(s) { return escapeHtml(s); }

// ─── Boot ────────────────────────────────────────────────────────────
renderLayerLists();
initNetwork();
load();
startSSE();
