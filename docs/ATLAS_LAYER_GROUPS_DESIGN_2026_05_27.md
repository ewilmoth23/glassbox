# Atlas Layer Panel — Category-Grouped Accordion

- **Date:** 2026-05-27
- **Status:** Design approved; implementation plan pending
- **Scope:** Frontend only — `21_GLASSBOX_AI/landing/atlas.js` + the layers-panel CSS in `21_GLASSBOX_AI/landing/index.html`
- **Backlog item:** Free-to-execute follow-on B ("atlas.js LAYERS toggle hierarchy") from the 2026-05-27 NIGHT FINAL session close.

---

## 1. Problem

The layers panel renders all 28 globe layers as one flat scrolling list
(`initLayersPanel()` at `atlas.js:2836`). After P2-A + P2-B shipped 10 new
layers this session, the flat list is long enough that related layers are
hard to find and the panel requires scrolling past unrelated entries. The
list has implicit comment-based groupings in the source already
(`atlas.js:26-101`) that are invisible to the user.

Goal: group the layers into labelled, collapsible categories so a user can
scan, collapse what they don't need, and flip whole groups at once — without
changing how any individual layer toggle or the globe rendering works.

## 2. Non-goals

- **No "Cyber" group.** The resume sketch suggested `Geopolitics / Infrastructure / Cyber`, but CISA KEV and Spamhaus are **side-panels** (`?kev=1` / `?spamhaus=1`), not positioned globe overlays. There are no cyber entries in `LAYERS`, so there is no Cyber section.
- **No change to layer on/off defaults or `refreshVisibility()` semantics.** The curated default-on set is preserved exactly.
- **No persistence of layer on/off state.** Only the collapse/expand state of groups persists (see §6).
- **No floating dropdown menus.** Rejected in favor of an inline accordion (see §3).
- **No new build step, framework, or dependency.** Plain JS + CSS, consistent with the existing static-file landing page.

## 3. Confirmed design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Interaction pattern | **Inline accordion** | A layers panel's value is seeing/flipping several groups at once. Floating dropdowns hide all but one group and overlap the globe + panel chrome. Accordion keeps the existing `.lyr` rows + click-to-toggle untouched and just wraps them in collapsible sections. |
| Group controls | **Toggle-all + count badge** | With 28 layers, a per-group on/off and an `N/M on` badge are worth the small extra JS. |
| Persistence | **Collapse state only** | Remember which groups are expanded/collapsed. Layer on/off stays session-only, preserving today's reset-to-defaults-on-reload behavior for the actual data toggles. |

## 4. Data model — taxonomy as a separate ordered constant

Add `LAYER_GROUPS` alongside `LAYERS`. All 28 `LAYERS` rows stay **unchanged**;
the taxonomy lives in one readable place with explicit ordering and per-group
metadata (label, defaultOpen).

```js
const LAYER_GROUPS = [
  { id: 'traffic',        label: 'Live Traffic',             defaultOpen: true,
    layerIds: ['vessels', 'aircraft', 'military_air', 'satellites'] },
  { id: 'sanctions',      label: 'Sanctions & Dark Activity', defaultOpen: true,
    layerIds: ['sanctioned_dark', 'sanctioned_rendezvous', 'shadow_fleet',
               'sanctioned_underway', 'sanctioned_port', 'dark_vessel', 'loitering'] },
  { id: 'geopolitics',    label: 'Geopolitics',              defaultOpen: false,
    layerIds: ['sanctioned_airspace', 'conflict_zones', 'disputed_zones',
               'diplomatic_posts', 'un_missions', 'state_media',
               'sanction_targets', 'trafficking'] },
  { id: 'infrastructure', label: 'Strategic Infrastructure', defaultOpen: false,
    layerIds: ['pipelines', 'cables', 'mil_bases', 'nuclear'] },
  { id: 'environment',    label: 'Environment & Climate',    defaultOpen: false,
    layerIds: ['wildfires', 'quakes', 'noaa_buoys', 'climate_forecast'] },
  { id: 'overlays',       label: 'Overlays',                 defaultOpen: true,
    layerIds: ['tracks'] },
];
```

**Default-open rule of thumb:** groups whose layers include any default-on
layer start expanded (Live Traffic, Sanctions, Overlays); all-off groups start
collapsed (Geopolitics, Infrastructure, Environment).

**Drift guard:** any `LAYERS` id not referenced by exactly one group renders
under a fallback **"Other"** section so a future layer added to `LAYERS` can
never silently disappear from the panel. A `console.warn` lists ungrouped ids
to flag the omission during development.

## 5. Rendering — `initLayersPanel()` rewrite

Replace the flat `LAYERS.map(...)` with a per-group structure. Iterate
`LAYER_GROUPS` in order, then emit the "Other" bucket if non-empty.

```
.lyr-group[data-group=ID][.collapsed?]
  .lyr-group-head                      ← click target = collapse/expand
    span.chevron        "▾"            (rotates -90° when .collapsed)
    span.swatch.group                  ← click (stopPropagation) = toggle-all; tri-state
    span.label          "Geopolitics"
    span.gcount         "3/8"          ← N on / M total
  .lyr-group-body                      ← display:none when .collapsed
    .lyr ...                           (EXISTING row markup, verbatim)
      span.swatch
      span.name
      span.count#cnt-<id>              (KEPT — external code updates this)
```

The existing `.lyr` row markup and `id="cnt-<id>"` per-layer count spans are
preserved verbatim, so whatever code refreshes per-layer counts keeps working
unchanged. `#layers-count` in the panel head continues to show
`LAYERS.length` (grand total).

## 6. Interaction — one delegated click handler

Keep a single delegated listener on `#layers-list`; branch by
`ev.target.closest(...)` in priority order:

1. **`.swatch.group`** → toggle-all. Compute the group's target state
   (`off` if any layer in the group is on, else `on`), set every `L.on` in the
   group to it, then call `refreshVisibility()` once and `updateGroupCounts()`.
   `stopPropagation` so the header doesn't also collapse.
2. **`.lyr-group-head`** (anywhere but the group swatch) → collapse/expand.
   Toggle the `.collapsed` class on the `.lyr-group`, persist the new set
   (§7).
3. **`.lyr`** → existing per-layer toggle, unchanged (`L.on = !L.on`, toggle
   `.on`/`.off`, `refreshVisibility()`), then `updateGroupCounts()` for that
   row's group.

**Tri-state group swatch:** `all-on` = filled, `all-off` = empty,
`mixed` = half/dim. `updateGroupCounts()` recomputes both the `N/M` badge and
the swatch state for every group after any toggle.

## 7. Persistence — collapse state only

- localStorage key: `glassbox.atlas.layerGroups.collapsed` → JSON array of
  collapsed group ids.
- **On init:** read the array; for each group, collapsed iff its id is in the
  array; if the key is absent entirely, fall back to each group's
  `defaultOpen`.
- **On collapse/expand:** rewrite the array.
- Layer `on/off` is **not** persisted — reloads reset to curated defaults
  exactly as today.
- All localStorage reads/writes wrapped in `try/catch`. If localStorage is
  unavailable (private mode, disabled), silently fall back to `defaultOpen`
  and never break the panel.

## 8. Styling — additive CSS in index.html

New rules (additive; no change to existing `.lyr` rules, panel width 280px, or
the scroll `.body`):

- `.lyr-group` — wrapper.
- `.lyr-group-head` — flex row, `cursor:pointer`, hover `background: var(--gold-soft)`, subtle bottom rule.
- `.chevron` — `transition: transform .15s`; `.lyr-group.collapsed .chevron { transform: rotate(-90deg) }`.
- `.swatch.group` — reuse the 8px dot language; tri-state via modifier classes (`.all-on` / `.mixed` / default empty).
- `.gcount` — `font-family: var(--mono)`, `font-size: 9px`, `color: var(--muted)`, tabular-nums.
- `.lyr-group.collapsed .lyr-group-body { display: none }`.

`display:none` collapse (no `max-height`/`scrollHeight` animation math) for
robustness — the chevron rotation supplies the motion cue. Reuses existing CSS
vars (`--gold`, `--gold-soft`, `--text`, `--text-soft`, `--muted`, `--rule-hi`,
`--mono`, severity colors).

## 9. Edge cases

- Ungrouped layer → "Other" bucket (+ `console.warn`).
- localStorage disabled/throwing → defaults, no throw, panel still works.
- Empty "Other" bucket → not rendered.
- `refreshVisibility()` is untouched; toggle-all flips multiple `L.on` then
  calls it once (one render, not N).
- Per-layer count spans (`#cnt-<id>`) remain present so external count-refresh
  code is unaffected.

## 10. Glassbox versioning (Drift Prevention Rule 2.6)

- Bump the visible version string in `landing/index.html` and add a CHANGELOG
  entry describing the grouped-accordion layers panel.
- The content-addressed `/atlas.js?h=<hash>` cache-bust (P1-D, commit
  `6183a24`) triggers automatically because atlas.js content changes — no
  manual cache action needed.

## 11. Testing / verification

No JS unit-test harness exists in the repo (tests are pytest/backend; atlas.js
is a served static file). Validation is **browser verification** via the
preview tools:

1. Panel renders 6 grouped sections (+ "Other" only if a layer is ungrouped).
2. Default open/collapsed state matches §4 on first load (no stored state).
3. Click a header → collapses, chevron rotates; click again → expands.
4. Click a group swatch → flips the whole group on the globe, updates the
   `N/M` badge and tri-state swatch; does not collapse the section.
5. Click an individual `.lyr` → toggles that layer (as today) and updates its
   group's count badge.
6. Reload → collapse/expand state persists; layer on/off resets to defaults.
7. Zero console errors throughout.

Screenshot of the grouped panel (expanded + one collapsed group) as proof.

## 12. File-by-file change list

| File | Change |
|---|---|
| `landing/atlas.js` | Add `LAYER_GROUPS` constant near `LAYERS` (~line 102). Rewrite `initLayersPanel()` (~line 2836) to render grouped accordion + delegated handler with the 3-way branch. Add `updateGroupCounts()` helper + localStorage collapse-state read/write helpers. |
| `landing/index.html` | Add `.lyr-group*` / `.chevron` / `.swatch.group` / `.gcount` CSS rules in the layers-panel block (~line 458). Bump version string (~the vNNN marker). |
| `CHANGELOG` (Glassbox) | One entry: grouped-accordion layers panel. |

## 13. Resolved questions

- Spec location: project convention `21_GLASSBOX_AI/docs/` (not the generic
  superpowers default) for discoverability alongside other Glassbox design docs.
- Taxonomy, interaction pattern, group controls, persistence scope: all
  confirmed with the operator before this doc was written (§3).
