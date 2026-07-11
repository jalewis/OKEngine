# Observability — metrics, alerts, logs (#64)

OKEngine's health is visible three ways, in increasing automation:

## Dashboards (in-vault)
The `operator-dashboard` rolls the per-area dashboards (fleet-health, source-grounding, review-queue,
conformance, kb-health) into `wiki/dashboards/operator.md` with an overall 🟢/🟡/🔴 + drill-down.

## Metrics (Prometheus)
`health-export` writes a Prometheus textfile to `<METRICS_DIR>/okengine.prom` (default
`/opt/data/metrics`):

```
okengine_health_overall          # 0 green / 1 yellow / 2 red
okengine_fleet_lanes_ok
okengine_fleet_lanes_attention   # stale + errored + off-model
okengine_grounding_pct           # % of synthesized pages citing a resolving source
okengine_review_queue            # pages awaiting a human
okengine_conformance_violations
```

Point the node_exporter **textfile collector** at `METRICS_DIR` (mount it / set
`--collector.textfile.directory`) and Prometheus scrapes it — then your existing **Alertmanager**
handles alerting + history + graphs. Example rule:

```yaml
- alert: OkengineUnhealthy
  expr: okengine_health_overall >= 2
  for: 1h
  annotations: {summary: "okengine vault health is RED"}
- alert: OkengineGroundingLow
  expr: okengine_grounding_pct < 50
```

**Also alert on the metrics going STALE — a dead fleet reads as healthy.** The value alerts above
only fire while `health-export` keeps rewriting the textfile. If that cron (or the whole gateway)
dies, the textfile collector keeps exporting the LAST-written values — frozen green — so
`okengine_health_overall >= 2` never fires and the outage is invisible. Guard the freshness of the
file itself, using the collector's built-in per-file mtime metric plus an `absent()` catch:

```yaml
- alert: OkengineMetricsStale       # health-export stopped rewriting the file (dead cron/fleet)
  expr: time() - node_textfile_mtime_seconds{file="okengine.prom"} > 3 * 3600
  for: 15m
  annotations: {summary: "okengine health metrics are stale (>3h) — fleet may be dead"}
- alert: OkengineMetricsAbsent       # series gone entirely (gateway down / scrape target lost)
  expr: absent(okengine_health_overall)
  for: 15m
  annotations: {summary: "okengine health metrics absent — gateway or scrape target down"}
```

Set the `3 * 3600` threshold above `health-export`'s own schedule interval (a couple of run
periods) so a single skipped run doesn't page. Deployments without a textfile collector that emits
`node_textfile_mtime_seconds` should instead alert on `absent()` alone, or add a self-timestamp
gauge to the export.

## Alerts (standalone)
For deployments without Prometheus, `health-export` is **transition-based** (no fatigue): when the
overall goes red, or a lane newly errors / falls off-model, it appends a timestamped line to
`wiki/dashboards/alerts.md` and POSTs `ALERT_WEBHOOK` (Slack-compatible `{text}`) if set.

## Logs
cron-plus already writes a per-run log per lane under `/opt/data/logs/cron-plus/`; `fleet-health`
mines them for stale/errored/off-model. (Distributed tracing is out of scope.)
