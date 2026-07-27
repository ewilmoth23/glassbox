# Atlas Layer Panel — Category-Grouped Accordion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group the 28 flat globe-layer toggles in the atlas cockpit into 6 collapsible accordion sections with per-group toggle-all + count badges and persisted collapse state.

**Architecture:** Frontend only. A new ordered `LAYER_GROUPS` taxonomy constant drives a rewritten `initLayersPanel()` that renders collapsible `.lyr-group` sections wrapping the existing `.lyr` rows verbatim. One delegated click handler branches three ways (toggle-all / collapse / per-layer). Collapse state persists to localStorage; layer on/off stays session-only. `refreshVisibility()` is untouched.

**Tech Stack:** Plain ES (browser global script — no modules, no build step), CSS custom properties, localStorage. No new dependencies. Served as a static asset by the running glassbox-server daemon (`:8790`).

**Spec:** `21_GLASSBOX_AI/docs/ATLAS_LAYER_GROUPS_DESIGN_2026_05_27.md`

---

## Testing note (read before starting)

This repo has **no JavaScript unit-test harness** — tests are pytest/backend, and `landing/atlas.js` is a browser-global `<script>` that depends on Cesium globals and DOM. Standing up a JS test framework (jsdom + Cesium mocking) for one panel would be over-engineering (YAGNI). Per established Glassbox frontend practice (P2-D, P2-A were browser-verified), **each task is gated by a concrete browser-verification checkpoint** using the `mcp__Claude_Preview` tools with exact expected observations, in place of red-green unit cycles.

**Serving / reload:** the glassbox-server daemon (PID 49684, `:8790`) serves `landing/` static assets **read-fresh from disk per request** (the `/atlas.js` handler re-reads the file and the content hash recomputes on mtime change). So **editing `atlas.js` / `index.html` needs NO daemon restart** — a hard reload picks up changes. Verify against `http://localhost:8790/`.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `21_GLASSBOX_AI/landing/atlas.js` | Layer taxonomy + panel render + interaction | Add `LAYER_GROUPS` + helpers near line 102; rewrite `initLayersPanel()` at line 2836 |
| `21_GLASSBOX_AI/landing/index.html` | Layers-panel styling + version marker | Add `.lyr-group*` CSS near line 497; bump version marker (Task 5) |
| `21_GLASSBOX_AI/CHANGELOG.md` | Glassbox version record | One new entry (Task 5) |

---

## Task 1: Taxonomy constant + data/persistence helpers

**Files:**
- Modify: `21_GLASSBOX_AI/landing/atlas.js` (insert after the `LAYERS` array closes at line 102)

- [ ] **Step 1: Add the `LAYER_GROUPS` constant + localStorage key**

Insert immediately after line 102 (the `];` closing `LAYERS`):

```js
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
```

- [ ] **Step 2: Add the model-builder + persistence helpers**

Insert directly below the constant from Step 1:

```js
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
```

- [ ] **Step 3: Verify the model is correct in the browser**

The old `initLayersPanel` is still active (untouched), so the page renders as before; this step only checks the new helpers parse and map correctly.

Using `mcp__Claude_Preview`: start/attach a preview on `http://localhost:8790/`, then `preview_eval`:

```js
(() => {
  const m = buildLayerGroupModel();
  const total = m.reduce((n, x) => n + x.layers.length, 0);
  const hasOther = m.some(x => x.group.id === 'other');
  return { groups: m.length, total, hasOther,
           perGroup: m.map(x => `${x.group.id}:${x.layers.length}`) };
})()
```

Expected: `{ groups: 6, total: 28, hasOther: false, perGroup: ["traffic:4","sanctions:7","geopolitics:8","infrastructure:4","environment:4","overlays:1"] }`

Also `preview_console_logs`: expect **no** `[layers] ungrouped` warning.

- [ ] **Step 4: Commit**

```bash
git add 21_GLASSBOX_AI/landing/atlas.js
git commit -m "feat(glassbox): add LAYER_GROUPS taxonomy + accordion data helpers"
```

---

## Task 2: Accordion CSS

**Files:**
- Modify: `21_GLASSBOX_AI/landing/index.html` (insert after the `.lyr .count {…}` rule that ends at line 497, before the `/* AI Brief */` block at line 499)

- [ ] **Step 1: Add the accordion CSS rules**

Insert after line 497:

```css
/* Layer category accordion */
.lyr-group { border-bottom: 1px solid var(--rule); }
.lyr-group:last-child { border-bottom: none; }
.lyr-group-head {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 14px;
  cursor: pointer; user-select: none;
  font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
  color: var(--text-soft);
}
.lyr-group-head:hover { background: var(--gold-soft); }
.lyr-group-head .chevron {
  font-size: 9px; color: var(--muted);
  width: 9px; text-align: center; flex-shrink: 0;
  transition: transform .15s ease;
}
.lyr-group.collapsed .lyr-group-head .chevron { transform: rotate(-90deg); }
.lyr-group-head .label { flex: 1; }
.lyr-group-head .swatch.group {
  width: 8px; height: 8px; border-radius: 50%;
  border: 1px solid var(--rule-hi); background: transparent;
  flex-shrink: 0; cursor: pointer;
  transition: background .2s, box-shadow .2s, opacity .2s;
}
.lyr-group-head .swatch.group.all-on {
  background: var(--gold); border-color: var(--gold); box-shadow: var(--gold-glow);
}
.lyr-group-head .swatch.group.mixed { background: var(--gold); opacity: .45; }
.lyr-group-head .gcount {
  font-family: var(--mono); font-size: 9px; color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.lyr-group.collapsed .lyr-group-body { display: none; }
```

- [ ] **Step 2: Verify the rules are served**

The DOM is still the flat list (rewrite is Task 3), so there is no visual change yet — only confirm the stylesheet now contains the rules and the page still renders without error.

`preview_eval` after hard reload:

```js
[...document.styleSheets].some(ss => {
  try { return [...ss.cssRules].some(r => r.selectorText === '.lyr-group-head'); }
  catch (e) { return false; }
})
```

Expected: `true`. Also `preview_console_logs`: no new errors.

- [ ] **Step 3: Commit**

```bash
git add 21_GLASSBOX_AI/landing/index.html
git commit -m "feat(glassbox): accordion CSS for grouped layers panel"
```

---

## Task 3: Grouped rendering (display only)

Render the accordion DOM with default collapse states. The click handler in this task still only handles per-layer toggles (group-head clicks are inert until Task 4), so nothing breaks and individual toggles keep working.

**Files:**
- Modify: `21_GLASSBOX_AI/landing/atlas.js` — add `renderLayerGroup()` and rewrite `initLayersPanel()` (lines 2836-2854)

- [ ] **Step 1: Add the `renderLayerGroup()` helper**

Insert directly above `function initLayersPanel()` (line 2836):

```js
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
```

- [ ] **Step 2: Add module-level state + rewrite `initLayersPanel()`**

Replace the entire existing `initLayersPanel` function (lines 2836-2854) with:

```js
let LAYER_GROUP_MODEL = [];
let COLLAPSED_GROUPS = new Set();

/* ─── Layers panel ───────────────────────────────────────────────── */
function initLayersPanel() {
  LAYER_GROUP_MODEL = buildLayerGroupModel();
  COLLAPSED_GROUPS = initialCollapsedSet(LAYER_GROUP_MODEL);
  $('layers-list').innerHTML =
    LAYER_GROUP_MODEL.map(m => renderLayerGroup(m, COLLAPSED_GROUPS)).join('');
  $('layers-count').textContent = String(LAYERS.length);

  $('layers-list').addEventListener('click', (ev) => {
    // Per-layer toggle (group-head interactions added in Task 4).
    const row = ev.target.closest('.lyr');
    if (!row) return;
    const L = LAYERS.find(x => x.id === row.dataset.id);
    if (!L) return;
    L.on = !L.on;
    row.classList.toggle('on',  L.on);
    row.classList.toggle('off', !L.on);
    refreshVisibility();
  });
}
```

Note: `.lyr-group-head` contains a `.swatch.group` but NOT a `.lyr`, so `ev.target.closest('.lyr')` returns null for header clicks → they are safely inert this task.

- [ ] **Step 3: Verify grouped rendering + default states + per-layer toggle**

`preview_eval` after hard reload — structure + defaults:

```js
(() => {
  const groups = [...document.querySelectorAll('.lyr-group')]
    .map(g => ({ id: g.dataset.group, collapsed: g.classList.contains('collapsed') }));
  return { count: groups.length, groups };
})()
```

Expected: 6 groups; `traffic`/`sanctions`/`overlays` collapsed=false, `geopolitics`/`infrastructure`/`environment` collapsed=true.

`preview_snapshot`: the panel shows 6 uppercase category headers; expanded groups show their `.lyr` rows; collapsed groups show only the header.

Per-layer toggle still works — `preview_click` on a visible `.lyr` row (e.g. the "All vessels (live AIS)" row), then `preview_eval`:

```js
LAYERS.find(L => L.id === 'vessels').on
```

Expected: flips `true` → `false` (row gains `.off`), and `preview_console_logs` shows no errors. Click again to restore.

- [ ] **Step 4: Commit**

```bash
git add 21_GLASSBOX_AI/landing/atlas.js
git commit -m "feat(glassbox): render layers panel as grouped accordion sections"
```

---

## Task 4: Group interactions (collapse/expand + toggle-all + counts + persistence)

**Files:**
- Modify: `21_GLASSBOX_AI/landing/atlas.js` — add `updateGroupCounts()` + `toggleGroupAll()`, extend the click handler, call `updateGroupCounts()` from init + toggles

- [ ] **Step 1: Add `updateGroupCounts()` and `toggleGroupAll()`**

Insert directly above `function initLayersPanel()` (just below `renderLayerGroup`):

```js
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
```

- [ ] **Step 2: Replace the click handler in `initLayersPanel()` with the 3-way branch + call `updateGroupCounts()` on init**

In `initLayersPanel()`, add `updateGroupCounts();` immediately after the `$('layers-count').textContent = …` line, and replace the existing `$('layers-list').addEventListener('click', …)` block with:

```js
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
```

- [ ] **Step 3: Verify collapse/expand + persistence**

`preview_eval` after hard reload — collapse the open `traffic` group via `preview_click` on its header (the "Live Traffic" text), then:

```js
(() => {
  const g = document.querySelector('.lyr-group[data-group="traffic"]');
  return { collapsed: g.classList.contains('collapsed'),
           stored: localStorage.getItem('glassbox.atlas.layerGroups.collapsed') };
})()
```

Expected: `collapsed: true`; `stored` JSON array includes `"traffic"` (plus the default-collapsed `geopolitics`/`infrastructure`/`environment`).

Reload the page (`preview_eval` `window.location.reload()`), then re-check: `traffic` group is still collapsed → persistence works. Expand it again to restore, confirm `stored` no longer contains `"traffic"`.

- [ ] **Step 4: Verify toggle-all + tri-state swatch + count badge**

Expand `geopolitics` (all layers off by default). `preview_click` its group swatch (the `.swatch.group` in the geopolitics header), then `preview_eval`:

```js
(() => {
  const entry = LAYER_GROUP_MODEL.find(m => m.group.id === 'geopolitics');
  const sw = document.querySelector('.lyr-group[data-group="geopolitics"] .swatch.group');
  return { allOn: entry.layers.every(L => L.on),
           swatchAllOn: sw.classList.contains('all-on'),
           badge: document.getElementById('gcnt-geopolitics').textContent };
})()
```

Expected: `allOn: true`, `swatchAllOn: true`, `badge: "8/8"`. `preview_screenshot` should show the geopolitics overlays now drawn on the globe.

Then toggle ONE layer in the group off via `preview_click` on its row, and `preview_eval`:

```js
(() => {
  const sw = document.querySelector('.lyr-group[data-group="geopolitics"] .swatch.group');
  return { mixed: sw.classList.contains('mixed'), badge: document.getElementById('gcnt-geopolitics').textContent };
})()
```

Expected: `mixed: true`, `badge: "7/8"`. Click the group swatch once more → all off (`badge: "0/8"`, swatch has neither `all-on` nor `mixed`). Restore to default (all off) before finishing. `preview_console_logs`: no errors throughout.

- [ ] **Step 5: Commit**

```bash
git add 21_GLASSBOX_AI/landing/atlas.js
git commit -m "feat(glassbox): group collapse/expand, toggle-all, count badges + persistence"
```

---

## Task 5: Glassbox versioning (Drift Prevention Rule 2.6)

**Files:**
- Modify: `21_GLASSBOX_AI/landing/index.html` (version marker, if present)
- Modify: `21_GLASSBOX_AI/CHANGELOG.md`

- [ ] **Step 1: Locate the cockpit's version marker**

```bash
cd "21_GLASSBOX_AI"
grep -nE "data-version|class=\"v\"|id=\"version|cockpit v|build [0-9]" landing/index.html | head
head -25 CHANGELOG.md
```

Expected: shows the topbar `.context .v` span content (the visible version label, if any) and the top of the CHANGELOG so you can match its entry format and find the latest version number.

- [ ] **Step 2: Bump the visible marker (only if one exists)**

If Step 1 found a version label in `index.html` (e.g. inside the topbar `.context`), increment it to the next number following the CHANGELOG's existing scheme. If `index.html` has **no** version label (the cockpit may track version only via CHANGELOG), skip the HTML edit — do not invent a marker.

- [ ] **Step 3: Add a CHANGELOG entry**

Prepend a new entry to `21_GLASSBOX_AI/CHANGELOG.md` matching the existing format (mirror the heading style shown in Step 1), e.g.:

```markdown
## v<next> — 2026-05-27
- Layers panel: 28 globe layers reorganized into 6 collapsible accordion
  categories (Live Traffic, Sanctions & Dark Activity, Geopolitics, Strategic
  Infrastructure, Environment & Climate, Overlays) with per-group toggle-all,
  N/M count badges, tri-state group swatches, and localStorage-persisted
  collapse state. Layer on/off defaults unchanged.
```

- [ ] **Step 4: Commit**

```bash
git add 21_GLASSBOX_AI/CHANGELOG.md 21_GLASSBOX_AI/landing/index.html
git commit -m "docs(glassbox): version bump + CHANGELOG for grouped layers panel"
```

(If only the CHANGELOG changed, stage just that file.)

---

## Task 6: Final verification pass + proof

**Files:** none (verification only)

- [ ] **Step 1: Full golden-path walkthrough**

Hard reload `http://localhost:8790/`. Confirm in one pass:
1. 6 category headers render; `traffic`/`sanctions`/`overlays` expanded, others collapsed.
2. Each header shows an `N/M` badge with correct counts (e.g. `traffic 3/4` — vessels+aircraft+satellites on, military_air off; `sanctions 5/7`; `overlays 1/1`).
3. Collapse/expand toggles + chevron rotates.
4. Group-swatch toggle-all flips the group on the globe + updates badge + tri-state.
5. Per-layer toggle still flips a single layer + updates its group badge.
6. Reload persists collapse state; layer on/off resets to defaults.
7. `preview_console_logs`: zero errors.

- [ ] **Step 2: Capture proof**

`preview_screenshot` of the panel with at least one expanded and one collapsed group visible. Share it with the user.

- [ ] **Step 3: Confirm clean tree**

```bash
git status --short
git log --oneline -6
```

Expected: working tree clean of this feature's files; 5 feature commits present (Tasks 1-5).

---

## Self-review (completed at write time)

**Spec coverage:** §3 decisions → Tasks 1/3/4; §4 taxonomy + drift guard → Task 1; §5 rendering → Task 3; §6 interaction (3-way) → Task 4; §7 persistence → Tasks 1+4; §8 styling → Task 2; §9 edge cases (Other bucket, localStorage try/catch, single repaint) → Tasks 1+4; §10 versioning → Task 5; §11 verification → every task + Task 6. No gaps.

**Placeholder scan:** No TBD/TODO; every code step shows complete code; Task 5 Step 2 is conditional (bump only if a marker exists) with explicit "don't invent one" guidance — concrete, not a placeholder.

**Type/name consistency:** `LAYER_GROUPS`, `LAYER_GROUPS_LS_KEY`, `buildLayerGroupModel`, `loadCollapsedGroups`, `saveCollapsedGroups`, `initialCollapsedSet`, `renderLayerGroup`, `updateGroupCounts`, `toggleGroupAll`, module-level `LAYER_GROUP_MODEL` / `COLLAPSED_GROUPS`, element ids `gcnt-<id>` / `cnt-<id>`, classes `.lyr-group` / `.lyr-group-head` / `.lyr-group-body` / `.chevron` / `.swatch.group` / `.all-on` / `.mixed` / `.collapsed` — all consistent across CSS (Task 2), render (Task 3), and interaction (Task 4).
