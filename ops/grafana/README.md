# Glassbox Grafana dashboards

## prefilter_dashboard.json

12-panel dashboard for the GDELT-bulk prefilter rule chain. Pairs with
the metrics shim shipped in commit `87f44f9` and the A/B shadow routing
shipped in `1532c40` + `9157add`.

### Panel layout

Top row (4 stat tiles):
- **Total events processed** — raw lifetime counter (pass + drop).
- **Pass rate (last hour)** — `pass / (pass + drop)` over a 1h window.
  Thresholded green at 0.5%-10%, yellow outside (rule misconfiguration).
- **Queue depth (% full)** — `queue_depth / queue_max_depth`. Yellow at
  70%, red at 90%.
- **Throughput (events/min)** — combined pass + drop rate × 60.

Drop breakdowns (2 stacked bar charts):
- **Drops per second by rule** — `category` / `severity` / `geography`
  / `source_quality` / `recency` / `dedup`. A spike on one rule
  indicates upstream feed shift or config drift.
- **Drops per second by structured reason** — same data sliced by
  `category_not_allowed` / `severity_below_floor` / `geography_outside_allowed`
  / `source_below_quality_floor` / `stale` / `duplicate`.

Throughput timeseries (2 line charts):
- **Pass / drop rate (events/sec)** — independent series for pass
  and aggregated drop.
- **Queue depth + overflow rate** — depth + max_depth + tail_drop +
  new_event_drop on one chart so capacity-pressure events are
  visible against the headroom.

Distribution + A/B (3 panels):
- **Priority score histogram** — heatmap of the priority-score bucket
  distribution for passing events. Shifts indicate scoring config drift
  or upstream-mix change.
- **A/B shadow confusion matrix** — only meaningful when the daemon
  is running with `GLASSBOX_PREFILTER_SHADOW_CONFIG` set. Four series:
  `agree_pass` (green), `agree_drop` (blue), `primary_pass_only`
  (orange = shadow stricter), `primary_drop_only` (red = shadow looser).
- **Shadow agreement rate** + **Shadow Δ pass rate** — 1h-windowed
  summary stats.

### Importing into Grafana

1. **Add a Prometheus datasource** scraping the daemon. Minimal
   `prometheus.yml` job:

   ```yaml
   - job_name: glassbox-prefilter
     metrics_path: /api/v1/metrics/prefilter
     scrape_interval: 30s
     static_configs:
       - targets: ['127.0.0.1:8790']
   ```

2. **Import the dashboard.** In Grafana UI:
   `Dashboards → New → Import → Upload JSON file → prefilter_dashboard.json`.
   Pick the Prometheus datasource you added in step 1 when prompted.

3. **Verify metric availability** before importing so empty panels don't
   look like a dashboard bug:

   ```bash
   curl -s http://127.0.0.1:8790/api/v1/metrics/prefilter | head -10
   ```

   Should return `# HELP glassbox_prefilter_pass_total ...` etc. If you
   get `# prefilter metrics disabled` instead, either:
   - The `gdelt_bulk` ingester isn't registered on the daemon
     (check `/api/v1/health/full`), or
   - `prometheus-client` isn't installed in the daemon venv (re-run
     `pip install -r 21_GLASSBOX_AI/requirements.txt`).

### Schema notes

Schema version 39 (Grafana 10+). For Grafana 8/9 compatibility,
adjust the heatmap panel's `options.calculate` field handling and
drop the `templating.list[0].type=datasource` block — older Grafana
versions used a different datasource-variable shape.
