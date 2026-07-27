# Glassbox operator console

Single-page operator dashboard at `http://127.0.0.1:8790/console`.
Surfaces every endpoint shipped in the 2026-05-10 continuation arc.

## What it is

`index.html` — vanilla HTML + JavaScript, ~10 KB, no framework, no
build step. Five tabs:

| Tab | Endpoint(s) | What you see |
|---|---|---|
| Health | `/api/v1/health/full` | 26-ingester roster with color-coded pills, DB pool gauge, findings counters |
| Prefilter | `/api/v1/metrics/prefilter` | pass/drop counters, drops-by-rule, A/B confusion matrix when active |
| Multi-entity findings | `/api/v1/entities/{id}/cross_domain` | interactive explorer — type a UUID or auto-pick "random" to walk recent multi-entity findings with resolved partner metadata |
| Event lookup | `/api/v1/event/{id}` | interactive explorer — type a UUID or auto-pick "latest" |
| Raw scrape | `/api/v1/metrics/prefilter` | verbatim Prometheus text |

Auto-refreshes every 30 s on Health + Prefilter + Raw. Findings + Event
are user-driven. Press `r` to refresh the active tab now.

## Why it's here, not in `05_WEBSITE_AND_LANDING/`

- This file IS in git (no embedded secrets).
- It's operator-tier, not customer-facing — belongs next to the daemon
  it talks to.
- Same-origin (port 8790 = the daemon's own port) means the JS can
  hit `/api/v1/*` without CORS plumbing.
- A direct symlink would survive a clean `git clone` better than a copy
  in the public-marketing tree.

## Wire-up

`glassbox_server.py` adds a `@app.get("/console")` route that reads
this file at request time and returns it as `HTMLResponse`. No
StaticFiles mount needed (one file, refresh-on-edit free).

## Edit + reload flow

1. Edit `index.html`.
2. Refresh the browser tab — no daemon restart needed (the route reads
   the file on every request).
3. Verify the change rendered. Hit `r` if a tab is showing stale data.

## Adding a new tab

Same pattern as the existing five — add a `<button data-tab="X">` to
the `<nav>`, a `<section class="tab" id="tab-X">` to the layout, a
`load_X()` function in the `<script>`, and a clause in `refreshActive()`
if you want it on the auto-refresh path.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/console` returns 404 | Daemon hasn't been reloaded since `433f1cf` | `bash 09_SETUP_GUIDES/scripts/glassbox/reload_daemon_and_verify.sh` |
| All panels show "loading…" forever | Browser blocked CSP / network | Open devtools → Network tab; look for the `/api/v1/*` calls |
| Prefilter tab says "disabled" | `gdelt_bulk` ingester not registered, OR `prometheus-client` not installed in daemon venv | Check `/api/v1/health/full` for the ingester; if missing, see `21_GLASSBOX_AI/ops/prometheus/README.md` for the disabled-shim flow |
| Multi-entity findings: "no events in last 24h" | Truly no multi-entity events (rendezvous, shadow_fleet) lately | Wait or pick a different entity manually |
