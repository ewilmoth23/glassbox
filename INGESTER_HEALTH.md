# Glassbox Ingester Health Reference

**Refreshed 2026-05-20 (P3-I).** The previous version of this file (last meaningful update 2026-05-12) enumerated the original 5-to-8-ingester roster (planes via OpenSky, police_incidents, citizen_osint, traffic_cams) and went stale as the system grew to 30 active ingesters across 13 algorithms. Mirroring per-ingester config in markdown was always going to rot — so this is now a **pointer** to the live source-of-truth, not a mirror.

If you're looking for the OLD 8-class roster, the pre-refresh content is preserved in git history (`git log --follow 21_GLASSBOX_AI/INGESTER_HEALTH.md` → revision before 2026-05-20).

---

## Two sources of truth

| Question | Answer lives in |
|---|---|
| Which ingesters are **configured to run** (enabled / disabled / why)? | `infra/sources.yaml` — declarative config, 84 source ids total |
| Which ingesters are **actually running right now** (live health, last-tick time, error state)? | `GET /api/health` on the daemon — runtime telemetry |
| Which ingester CLASS handles a given source id? | The `class_name` field on each entry in `infra/sources.yaml`, paired with the matching `class` in `21_GLASSBOX_AI/ingesters/` |
| Why is `<source_id>` disabled? | The `disabled_reason` field next to `enabled: false` in `infra/sources.yaml` (reconciliation done in P0-B, 2026-05-19) |

## Live snapshot (2026-05-20)

```text
Total source ids:  84
Enabled:           30   (paired 1:1 with registered ingester classes)
Disabled:          54   (each with disabled_reason — typically "no ingester exists yet" or "non-commercial license")
Registered ingester classes:  37   (across 21_GLASSBOX_AI/ingesters/ — some classes serve multiple sources, e.g. ships aggregates digitraffic+barentswatch+DMA)
```

The 30-enabled / 54-disabled split was reconciled on 2026-05-19 (P0-B). See `21_GLASSBOX_AI/docs/SOURCES_RECONCILIATION_2026_05_19.md` for the full mapping including which sources got `enabled: false + disabled_reason: <text>` because no ingester exists for them.

## Useful commands

**Enumerate enabled ingesters with their layer/cadence (no daemon required):**
```bash
cd "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC"
grep -B1 -A4 "enabled: true" infra/sources.yaml | grep -E "^  - id:|layer:|poll_interval_sec:" | paste - - -
```

**Live health of the running daemon (one row per ingester, with status/last_tick/errors):**
```bash
curl -s http://127.0.0.1:8790/api/health | python3 -c '
import json, sys
data = json.load(sys.stdin)
for ing in data.get("ingesters", []):
    print(f"{ing[\"layer\"]:<24} {ing.get(\"health\", \"?\"):<8} {ing.get(\"running\", \"?\"):<6} {ing.get(\"last_tick_at\", \"-\")}")
'
```

**Why is `<source_id>` not emitting data?** The probe at `/glassbox-probe` (slash-command in `.claude/commands/`) is the canonical 10-step live-infra checklist. Run it on the Mac for a full diagnosis in ~30 seconds.

## Common failure modes (rules of thumb, not exhaustive)

These hold across the 30 active ingesters; specifics live in the ingester source under `21_GLASSBOX_AI/ingesters/<class>.py`.

| Symptom | Most likely cause |
|---|---|
| Ingester reports `health: down` for >1h | Upstream HTTP 5xx, auth expired, or upstream rate-limit ban. Check `mewr-logs/glassbox-server.log` for the latest stack from the ingester's logger. |
| `health: ok` but `last_tick_at` stale by hours | The ingester's poll loop is alive but its `fetch()` is returning empty payloads (often: upstream silently dropping records mid-feed). Run the source's normalize step manually to confirm. |
| Daemon `sla_breach: true` for a layer with daily cadence | False positive in the probe (P0-A.1 fix at 2026-05-19 made the probe trust the daemon's own `sla_breach` flag instead of a naive 60-min cutoff). If the daemon itself reports `sla_breach: false`, you can ignore the probe. |
| Vessel layer specifically shows zero rows over the last hour | Check whether the parallel proximity / dark_ship cleanup is running (multi-million-row UPDATE statements on the same Postgres instance can starve the ingester's write throughput temporarily — not a bug, just contention). |
| Single source within a multi-source ingester (e.g. AISStream within `ships`) drops | The ingester's docstring + `class.tick()` log statements tell you which upstream is the failure. AISStream specifically: websocket disconnect → 6h gap → dark_ship FPs (fixed 2026-05-19 with the cohort suppression in dark_ship.py). |

## See also

- `infra/sources.yaml` — source-of-truth declarative config
- `infra/sources.yaml` neighborhood comments document each source's licensing posture (`commercial_use_ok` field, `license_notes`)
- `21_GLASSBOX_AI/docs/SOURCES_RECONCILIATION_2026_05_19.md` — the P0-B reconciliation that brought sources.yaml to its current 30/54 state
- `00_MASTER_DOCS/legal/LICENSE_RISK_REGISTER.md` — sources that need commercial-use written authorization (OpenSky, etc.)
- `.claude/commands/glassbox-probe.md` — the 10-probe live-infra checklist
- `21_GLASSBOX_AI/ingesters/<class>.py` — each ingester's own docstring documents what payload it emits, its poll interval, its auth requirements, and its known failure modes
