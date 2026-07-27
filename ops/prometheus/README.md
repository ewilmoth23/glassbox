# Glassbox observability stack — turnkey

Docker-compose pair (Prometheus + Grafana) wired to scrape the live
glassbox-server's `/api/v1/metrics{,/prefilter}` endpoints, with the
Glassbox prefilter dashboard auto-loaded at first start.

## Bring up

```bash
cd 21_GLASSBOX_AI/ops/prometheus
docker compose up -d
```

(Requires Docker Desktop running on the Mac.)

Two containers come up:

| Container | Port | URL |
|---|---|---|
| `glassbox-prometheus` | 9090 | http://localhost:9090 |
| `glassbox-grafana`    | 3000 | http://localhost:3000 (admin / admin) |

Within a minute you should see:

- Prometheus targets all green: http://localhost:9090/targets
- Grafana auto-provisioned with a Prometheus datasource
- The "Glassbox Prefilter — GDELT Bulk" dashboard available under
  the **Glassbox** folder in Grafana, pre-pointed at the right
  datasource.

## What's in this directory

```
21_GLASSBOX_AI/ops/prometheus/
├── README.md                   ← you are here
├── docker-compose.yml          ← two-container stack
├── prometheus.yml              ← scrape config (host.docker.internal:8790)
└── grafana_provisioning/       ← bind-mounted into the grafana container
    ├── datasources/prometheus.yml
    └── dashboards/glassbox.yml ← provider that auto-loads JSON files

# The dashboard JSON is mounted directly from the canonical path,
# 21_GLASSBOX_AI/ops/grafana/prefilter_dashboard.json, so editing
# the canonical updates Grafana on the next dashboard refresh.
```

## Verify the scrape

```bash
# 1. Confirm Prometheus is up + scraping
curl -s http://localhost:9090/api/v1/targets \
  | python3 -c "
import json, sys
for t in json.load(sys.stdin)['data']['activeTargets']:
    print(f\"  {t['labels']['job']:>22}  {t['health']:>10}  {t.get('lastError','')}\")"

# Expect three lines, all 'up':
#   glassbox-prefilter  up
#      glassbox-server  up
#           prometheus  up

# 2. Confirm metrics are being recorded
curl -s 'http://localhost:9090/api/v1/query?query=glassbox_prefilter_pass_total' \
  | python3 -m json.tool

# 3. Open Grafana
open http://localhost:3000
# Sign in: admin / admin (you'll be prompted to change it on first sign-in)
# Dashboards → Glassbox → "Glassbox Prefilter — GDELT Bulk"
```

## Tear down

```bash
docker compose down            # stop containers, keep data
docker compose down -v         # stop AND wipe Prometheus + Grafana volumes
```

## Common issues

**"context deadline exceeded" on prefilter scrape**

The daemon hasn't been reloaded since the metrics shim shipped (commit
`87f44f9`). Run:

```bash
bash 09_SETUP_GUIDES/scripts/glassbox/reload_daemon_and_verify.sh
```

Then wait one full scrape interval (30s).

**"# prefilter metrics disabled" in the scrape body**

Either:
1. The gdelt_bulk ingester isn't registered (check `/api/v1/health/full`).
2. `prometheus-client` isn't installed in the daemon's venv. Re-run:
   ```bash
   21_GLASSBOX_AI/.venv/bin/pip install -r 21_GLASSBOX_AI/requirements.txt
   bash 09_SETUP_GUIDES/scripts/glassbox/reload_daemon_and_verify.sh
   ```

**Grafana dashboard panels show "No data"**

- Make sure the daemon is reloaded (so the `/api/v1/metrics/prefilter`
  endpoint is live).
- Wait for the first GDELT bulk cycle to fire (every 5 minutes; default
  config passes only ~0.1-0.5% of events, so empty panels can be
  legitimate for low-traffic windows — flip the dashboard's time range
  to "Last 24 hours" to see broader activity).
- Confirm Prometheus is hitting the right host: by default the
  `prometheus.yml` uses `host.docker.internal:8790`. On non-Mac hosts
  this needs to be the actual IP.

**Container won't start: "address already in use"**

Some other service on the Mac is on 9090 or 3000. Edit
`docker-compose.yml` to map a different host port (e.g. `9091:9090`).
