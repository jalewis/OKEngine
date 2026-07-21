#!/usr/bin/env python3
"""health_export.py — structured observability: metrics + alerts (okengine#64).

Closes the detect→notify loop. The operator dashboard SHOWS health; this EXPORTS it:
  - METRICS: writes a Prometheus textfile (<METRICS_DIR>/okengine.prom) — point the node_exporter
    textfile collector at it and the existing Prometheus/Alertmanager gives graphs + alerting for
    free (don't rebuild alerting; feed the stack).
  - ALERTS: transition-based (no fatigue) — on a NEW problem since the last run (overall went red,
    or a cron lane newly errored/off-model) it appends a timestamped line to wiki/dashboards/
    alerts.md and POSTs ALERT_WEBHOOK (Slack-compatible {text}) if set, so a no-Prometheus
    deployment still gets notified.

  - HEARTBEAT: pings OKENGINE_HEARTBEAT_URL every run (a healthchecks.io-style dead-man's switch), so
    an EXTERNAL service detects the SCHEDULER's OWN death — which no in-scheduler lane can (if the
    ticker dies, this lane never runs, and its silence otherwise reads as healthy). Essential for a
    no-Prometheus, webhook-only deployment; the Prometheus path already exposes an equivalent
    `okengine_health_export_heartbeat_seconds` metric to alert on.

Deterministic (no_agent). Reads the health dashboards (graceful if a number is absent).
Env: WIKI_PATH (/opt/vault) · METRICS_DIR (/opt/data/metrics) · ALERT_WEBHOOK (optional) ·
     OKENGINE_HEARTBEAT_URL (optional external dead-man's switch)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
DDIR = WIKI / "dashboards"
MDIR = Path(os.environ.get("METRICS_DIR", "/opt/data/metrics"))
WEBHOOK = os.environ.get("ALERT_WEBHOOK", "").strip()
FLEET_LANES = DDIR / ".fleet-lanes.json"


def _http_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an http(s) URL with a host")
    return value


def _body(name: str) -> str:
    p = DDIR / f"{name}.md"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _int(rx, text, default=None):
    m = re.search(rx, text)
    return int(m.group(1)) if m else default


def _parse_fleet(fh: str) -> "dict[str, int | None]":
    """Fleet-lane counts from wiki/dashboards/fleet-health.md. fleet_health.py writes them as
    PLAIN text ('🔴 stale: 0  ·  🔴 errored: 1'), so the pattern must tolerate 0-2 asterisks
    (`\\*{0,2}`). The old bold-only `\\*\\*(\\d+)\\*\\*` matched nothing and silently parsed every
    count as None/0 — a dead-monitor-reads-healthy fail (the fleet Prometheus metrics all read 0
    regardless of real lane state). operator_dashboard.py already parses the plain form; the two
    consumers must agree. Contract pinned by tests/cron/test_health_export_fleet_parse.py."""
    def n(label: str):
        return _int(rf"{label}:\s*\*{{0,2}}(\d+)", fh)
    return {"stale": n("stale"), "errored": n("errored"),
            "off-model": n("off-model"), "ok": n("ok")}


def _dash_age_h(name: str) -> "float | None":
    """Hours since the dashboard file was last written, or None if it doesn't exist. health_export
    summarizes dashboards written by OTHER lanes, so a stale/absent source means the monitor DIED —
    not that everything is healthy. Without this the scrape falls to all-None -> overall 0 (GREEN),
    a dead-monitor-reads-healthy fail-open (okengine#178)."""
    import time
    p = DDIR / f"{name}.md"
    try:
        return (time.time() - p.stat().st_mtime) / 3600.0
    except OSError:
        return None


def _fleet_lane_sets() -> "dict[str, set[str]] | None":
    """Machine-readable lane identities from fleet_health.

    None means absent/malformed (mixed-version rollout or transient read problem), in which case
    transition detection falls back to the legacy counts. Never turn an unavailable sidecar into
    empty sets: that would falsely report every recovery and mask the next genuine failure.
    """
    try:
        # fleet_health publishes the dashboard first and the sidecar second. If its second write
        # failed, an older-but-valid sidecar must not be paired with the new dashboard: doing so
        # would recreate the very lane-swap masking this handoff fixes.
        dashboard_mtime = (DDIR / "fleet-health.md").stat().st_mtime
        if FLEET_LANES.stat().st_mtime < dashboard_mtime:
            return None
        data = json.loads(FLEET_LANES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    out = {}
    for key in ("errored", "off-model"):
        values = data.get(key) if isinstance(data, dict) else None
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            return None
        out[key] = {item for item in values if item}
    return out


def main() -> int:
    fh, g, rq, cf = _body("fleet-health"), _body("source-grounding"), _body("review-queue"), _body("conformance")
    _fleet = _parse_fleet(fh)
    stale, errored, offmodel, fleet_ok = _fleet["stale"], _fleet["errored"], _fleet["off-model"], _fleet["ok"]
    grounding = _int(r"grounded: \*\*\d+\*\* \((\d+)%\)", g)
    review = _int(r"\*\*(\d+) item\(s\) awaiting", rq)
    conf = _int(r"source-refs-are-pages: \*\*(\d+)\*\*", cf)
    attention = (stale or 0) + (errored or 0) + (offmodel or 0)
    # Freshness gate (#9): fleet-health is the PRIMARY source. If it is missing or older than
    # OKENGINE_HEALTH_MAX_AGE_H (default 6h = 2x the 3h cadence), the upstream monitor is DEAD —
    # force RED rather than exporting a frozen last-green. A dead monitor must never read healthy.
    max_age_h = float(os.environ.get("OKENGINE_HEALTH_MAX_AGE_H") or 6)
    fh_age = _dash_age_h("fleet-health")
    monitor_dead = fh_age is None or fh_age > max_age_h
    # overall: 0 green / 1 yellow / 2 red
    overall = 0
    if attention or (grounding is not None and grounding < 50) or monitor_dead:
        overall = 2
    elif (review or 0) or (conf or 0) or (grounding is not None and grounding < 80):
        overall = 1

    # --- Prometheus textfile ---
    # Heartbeat FIRST and ALWAYS emitted: the unix time THIS exporter last wrote. The other gauges
    # only detect the upstream fleet-health dashboard going stale (monitor_stale) — nothing catches
    # health_export ITSELF dying, after which Prometheus keeps scraping the frozen .prom (last value
    # green) indefinitely. A scheduler-independent alert `time() - okengine_health_export_timestamp_seconds
    # > N` fires when this lane stops running, regardless of what the frozen file says (invariant-audit B6.3).
    metrics = [
        ("okengine_health_export_timestamp_seconds",
         "unix time health_export last wrote (heartbeat; alert if time()-this exceeds ~2x the cadence)",
         int(datetime.now(timezone.utc).timestamp())),
        ("okengine_health_overall", "0=green 1=yellow 2=red", overall),
        ("okengine_health_monitor_stale", "1=fleet-health dashboard missing/stale (monitor dead)",
         1 if monitor_dead else 0),
        ("okengine_fleet_lanes_ok", "cron lanes healthy", fleet_ok),
        ("okengine_fleet_lanes_attention", "cron lanes stale+errored+off-model", attention),
        ("okengine_grounding_pct", "% synthesized pages citing a resolving source", grounding),
        ("okengine_review_queue", "pages awaiting human review", review),
        ("okengine_conformance_violations", "content-rule violations", conf),
    ]
    lines = []
    for name, help_, val in metrics:
        if val is None:
            continue
        lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge", f"{name} {val}"]
    MDIR.mkdir(parents=True, exist_ok=True)
    (MDIR / "okengine.prom").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # --- transition-based alert ---
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sf = MDIR / ".health-state.json"
    try:
        last = json.loads(sf.read_text())
    except Exception:
        last = {}
    lane_sets = _fleet_lane_sets()
    newly = []
    if overall == 2 and last.get("overall", 0) < 2:
        reasons = []
        if monitor_dead:
            reasons.append("fleet-health monitor is stale/missing — lanes may be dead (no data)")
        if attention:
            reasons.append(f"{attention} cron lane(s) need attention")
        if grounding is not None and grounding < 50:
            reasons.append(f"grounding {grounding}%")
        newly.append("overall health is RED — " + "; ".join(reasons or ["see operator dashboard"]))
    if lane_sets is not None:
        previous_errored = set(last.get("errored_lanes") or [])
        previous_offmodel = set(last.get("offmodel_lanes") or [])
        new_errored = sorted(lane_sets["errored"] - previous_errored)
        new_offmodel = sorted(lane_sets["off-model"] - previous_offmodel)
        if new_errored:
            newly.append("cron lane(s) newly ERRORED: " + ", ".join(new_errored))
        if new_offmodel:
            newly.append("synthesis lane(s) newly OFF-MODEL: " + ", ".join(new_offmodel))
    else:
        # Mixed-version rollout/backward compatibility: retain the old count signal until
        # fleet_health has emitted its first identity sidecar. It cannot detect set swaps, but is
        # preferable to dropping transition alerts entirely.
        if (errored or 0) > last.get("errored", 0):
            newly.append(f"a cron lane newly ERRORED (now {errored})")
        if (offmodel or 0) > last.get("offmodel", 0):
            newly.append(f"a synthesis lane fell OFF-MODEL (now {offmodel})")
    webhook_failed = False
    if newly:
        alerts = DDIR / "alerts.md"
        head = "" if alerts.is_file() else ("---\ntype: dashboard\ntitle: \"Alerts\"\n---\n\n# Alerts\n\n")
        with alerts.open("a", encoding="utf-8") as f:
            if head:
                f.write(head)
            for a in newly:
                f.write(f"- **{now}** 🔴 {a}\n")
        if WEBHOOK:
            try:
                import urllib.request
                payload = json.dumps({"text": f"[okengine] {now}\n" + "\n".join("• " + a for a in newly)}).encode()
                webhook_url = _http_url(WEBHOOK, "ALERT_WEBHOOK")
                urllib.request.urlopen(urllib.request.Request(  # nosec B310
                    webhook_url, data=payload, headers={"Content-Type": "application/json"}), timeout=8)
            except Exception as e:           # never fail the cron on a webhook hiccup
                webhook_failed = True
                print(f"  webhook delivery failed: {e}", file=sys.stderr)

    # Advance the dedup state ONLY when the alert was actually delivered (or there was nothing to
    # deliver). If the webhook — the only push channel for a no-Prometheus deployment — FAILED, keep
    # the prior state so the next run re-detects the same transition and RETRIES the push. Otherwise
    # the RED alert is lost for the whole incident (the transition test never fires again), leaving
    # only a stderr line and an alerts.md row the push exists to avoid needing (okengine#178).
    if not webhook_failed:
        state = {"overall": overall, "errored": errored or 0, "offmodel": offmodel or 0,
                 "at": now}
        if lane_sets is not None:
            state["errored_lanes"] = sorted(lane_sets["errored"])
            state["offmodel_lanes"] = sorted(lane_sets["off-model"])
        sf.write_text(json.dumps(state), encoding="utf-8")
    else:
        print("  health-state NOT advanced (webhook undelivered) — will retry the alert next run",
              file=sys.stderr)
    # EXTERNAL dead-man's switch (invariant-audit #11): the whole detect->notify chain runs INSIDE the
    # cron-plus scheduler, so if the SCHEDULER itself dies (CRON_PLUS_DISABLED, plugin removed, ticker
    # wedged) health_export never runs and its silence reads as healthy — a no-Prometheus, webhook-only
    # deployment has nothing watching the watcher. Ping OKENGINE_HEARTBEAT_URL on every run (a
    # healthchecks.io-style dead-man's switch); the EXTERNAL service alerts when the ping STOPS,
    # catching the scheduler's own death that no in-scheduler lane can. Best-effort, never fatal.
    hb_url = os.environ.get("OKENGINE_HEARTBEAT_URL", "").strip()
    if hb_url:
        try:
            import urllib.request
            heartbeat_url = _http_url(hb_url, "OKENGINE_HEARTBEAT_URL")
            urllib.request.urlopen(urllib.request.Request(heartbeat_url), timeout=8)  # nosec B310
        except Exception as e:                       # noqa: BLE001 — a dead-man's ping must never fail the lane
            print(f"  heartbeat ping failed: {e}", file=sys.stderr)
    states = {0: "🟢", 1: "🟡", 2: "🔴"}
    print(f"health-export: overall {states[overall]} -> {MDIR / 'okengine.prom'} "
          f"({sum(1 for _, _, v in metrics if v is not None)} metrics); "
          f"{len(newly)} new alert(s)")
    for a in newly:
        print(f"  🔴 ALERT: {a}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
