#!/usr/bin/env python3
"""health_export.py — structured observability: metrics + alerts (okengine#64).

Closes the detect→notify loop. The operator dashboard SHOWS health; this EXPORTS it:
  - METRICS: writes a Prometheus textfile (<METRICS_DIR>/okengine.prom) — point the node_exporter
    textfile collector at it and the existing Prometheus/Alertmanager gives graphs + alerting for
    free (don't rebuild alerting; feed the stack).
  - ALERTS: transition-based (no fatigue) — on a NEW problem since the last run (overall escalated,
    or a cron lane newly critical-stale/errored/off-model) it appends a timestamped line to wiki/dashboards/
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
from json import JSONDecodeError
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.append(str(Path(__file__).resolve().parent))  # staged sibling libs
import generated_page  # noqa: E402 — keeps a regenerated page's id (okengine#933)

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
DDIR = WIKI / "dashboards"
MDIR = Path(os.environ.get("METRICS_DIR", "/opt/data/metrics"))
WEBHOOK = os.environ.get("ALERT_WEBHOOK", "").strip()
FLEET_LANES = DDIR / ".fleet-lanes.json"
ESCALATION = DDIR / ".fleet-escalation.json"


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
    PLAIN text ('🟡 stale: 0 · 🟠 critical-stale: 0 · 🔴 errored: 1'), so the pattern must tolerate 0-2 asterisks
    (`\\*{0,2}`). The old bold-only `\\*\\*(\\d+)\\*\\*` matched nothing and silently parsed every
    count as None/0 — a dead-monitor-reads-healthy fail (the fleet Prometheus metrics all read 0
    regardless of real lane state). operator_dashboard.py already parses the plain form; the two
    consumers must agree. Contract pinned by tests/cron/test_health_export_fleet_parse.py."""
    def n(label: str):
        return _int(rf"{label}:\s*\*{{0,2}}(\d+)", fh)
    return {"stale": n("stale"), "critical-stale": n("critical-stale"),
            "errored": n("errored"), "invalid-schedule": n("invalid-schedule"),
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
    except (OSError, JSONDecodeError):
        return None
    out = {}
    for key in ("stale", "critical-stale", "errored", "off-model"):
        values = data.get(key) if isinstance(data, dict) else None
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            return None
        out[key] = {item for item in values if item}
    invalid = data.get("invalid-schedule") if isinstance(data, dict) else None
    if invalid is not None and (not isinstance(invalid, list)
                                or not all(isinstance(item, str) for item in invalid)):
        return None
    out["invalid-schedule"] = {item for item in (invalid or []) if item}
    return out


REMIND_AFTER_H = float(os.environ.get("ALERT_REMIND_AFTER_HOURS") or 24)
REMIND_EVERY_H = float(os.environ.get("ALERT_REMIND_EVERY_HOURS") or 24)


def _sustained_red(now_dt, reminded: dict) -> "tuple[list[str], dict]":
    """Lanes red for longer than REMIND_AFTER_H that are due another mention.

    Transition-only alerting is "no fatigue" at one end and total silence at the other: a lane that
    went red once alerts once and is never spoken of again. On a live vault that produced
    `wiki-health-audit` red since 2026-08-31 with 1,941 consecutive failing checks, correctly
    detected and escalated the whole time, and one alert line three weeks old (okengine#817).

    fleet_health already records `red_since` and `consecutive` per lane; this turns a sustained
    failure back into a signal on a bounded cadence — loud enough to notice, rare enough not to be
    filtered. Recovered lanes drop out of the reminder book so a later relapse reads as new.
    """
    try:
        data = json.loads(ESCALATION.read_text(encoding="utf-8"))
        lanes = data.get("lanes") if isinstance(data, dict) else None
        if not isinstance(lanes, dict):
            return [], reminded
    except (OSError, JSONDecodeError):
        return [], reminded          # absent sidecar is "unknown", never "nothing is wrong"
    due, book = [], {}
    for lane, st in sorted(lanes.items()):
        if not isinstance(st, dict):
            continue
        red_since = _parse_iso(st.get("red_since"))
        if red_since is None:
            continue
        age_h = (now_dt - red_since).total_seconds() / 3600.0
        book[lane] = reminded.get(lane)          # carry the last mention for still-red lanes only
        if age_h < REMIND_AFTER_H:
            continue
        last_ping = _parse_iso(reminded.get(lane))
        if last_ping is not None and (now_dt - last_ping).total_seconds() / 3600.0 < REMIND_EVERY_H:
            continue
        streak = st.get("consecutive")
        due.append(f"{lane} (red {age_h / 24:.1f}d"
                   + (f", {streak} consecutive checks" if isinstance(streak, int) else "") + ")")
        book[lane] = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return due, book


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> int:
    fh, g, rq, cf = _body("fleet-health"), _body("source-grounding"), _body("review-queue"), _body("conformance")
    _fleet = _parse_fleet(fh)
    stale = _fleet["stale"]
    critical_stale = _fleet["critical-stale"]
    errored, invalid_schedule, offmodel, fleet_ok = (
        _fleet["errored"], _fleet["invalid-schedule"], _fleet["off-model"], _fleet["ok"])
    grounding = _int(r"grounded: \*\*\d+\*\* \((\d+)%\)", g)
    review = _int(r"\*\*(\d+) item\(s\) awaiting", rq)
    conf = _int(r"source-refs-are-pages: \*\*(\d+)\*\*", cf)
    warnings = stale or 0
    critical = critical_stale or 0
    failures = (errored or 0) + (invalid_schedule or 0) + (offmodel or 0)
    attention = warnings + critical + failures
    # Freshness gate (#9): fleet-health is the PRIMARY source. If it is missing or older than
    # OKENGINE_HEALTH_MAX_AGE_H (default 1h = 4x the 15m cadence), the upstream monitor is DEAD —
    # force RED rather than exporting a frozen last-green. A dead monitor must never read healthy.
    max_age_h = float(os.environ.get("OKENGINE_HEALTH_MAX_AGE_H") or 1)
    fh_age = _dash_age_h("fleet-health")
    monitor_dead = fh_age is None or fh_age > max_age_h
    # overall: 0 green / 1 yellow / 2 orange / 3 red
    overall = 0
    if failures or (grounding is not None and grounding < 50) or monitor_dead:
        overall = 3
    elif critical:
        overall = 2
    elif warnings or (review or 0) or (conf or 0) or (grounding is not None and grounding < 80):
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
        ("okengine_health_overall", "0=green 1=yellow 2=orange 3=red", overall),
        ("okengine_health_monitor_stale", "1=fleet-health dashboard missing/stale (monitor dead)",
         1 if monitor_dead else 0),
        ("okengine_fleet_lanes_ok", "cron lanes healthy", fleet_ok),
        ("okengine_fleet_lanes_stale", "cron lanes stale (warning)", warnings),
        ("okengine_fleet_lanes_critical_stale", "cron lanes critically stale", critical),
        ("okengine_fleet_lanes_invalid_schedule", "cron lanes with unschedulable expressions",
         invalid_schedule),
        ("okengine_fleet_lanes_failures", "cron lanes errored+invalid-schedule+off-model", failures),
        ("okengine_fleet_lanes_attention", "all cron lanes needing attention", attention),
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
    if overall == 3 and last.get("overall", 0) < 3:
        reasons = []
        if monitor_dead:
            reasons.append("fleet-health monitor is stale/missing — lanes may be dead (no data)")
        if failures:
            reasons.append(f"{failures} cron lane failure(s)")
        if grounding is not None and grounding < 50:
            reasons.append(f"grounding {grounding}%")
        newly.append("overall health is RED — " + "; ".join(reasons or ["see operator dashboard"]))
    if lane_sets is not None:
        previous_errored = set(last.get("errored_lanes") or [])
        previous_offmodel = set(last.get("offmodel_lanes") or [])
        previous_critical = set(last.get("critical_stale_lanes") or [])
        previous_invalid = set(last.get("invalid_schedule_lanes") or [])
        new_errored = sorted(lane_sets["errored"] - previous_errored)
        new_offmodel = sorted(lane_sets["off-model"] - previous_offmodel)
        new_critical = sorted(lane_sets["critical-stale"] - previous_critical)
        new_invalid = sorted(lane_sets["invalid-schedule"] - previous_invalid)
        if new_errored:
            newly.append("cron lane(s) newly ERRORED: " + ", ".join(new_errored))
        if new_offmodel:
            newly.append("synthesis lane(s) newly OFF-MODEL: " + ", ".join(new_offmodel))
        if new_critical:
            newly.append("cron lane(s) escalated to CRITICAL-STALE: " + ", ".join(new_critical))
        if new_invalid:
            newly.append("cron lane(s) newly INVALID-SCHEDULE: " + ", ".join(new_invalid))
    else:
        # Mixed-version rollout/backward compatibility: retain the old count signal until
        # fleet_health has emitted its first identity sidecar. It cannot detect set swaps, but is
        # preferable to dropping transition alerts entirely.
        if (errored or 0) > last.get("errored", 0):
            newly.append(f"a cron lane newly ERRORED (now {errored})")
        if (offmodel or 0) > last.get("offmodel", 0):
            newly.append(f"a synthesis lane fell OFF-MODEL (now {offmodel})")
        if (critical_stale or 0) > last.get("critical_stale", 0):
            newly.append(f"a cron lane escalated to CRITICAL-STALE (now {critical_stale})")
        if (invalid_schedule or 0) > last.get("invalid_schedule", 0):
            newly.append(f"a cron lane has an INVALID-SCHEDULE (now {invalid_schedule})")
    now_dt = datetime.now(timezone.utc)
    still_red, remind_book = _sustained_red(now_dt, dict(last.get("reminded_at") or {}))
    if still_red:
        newly.append(f"cron lane(s) STILL failing: {'; '.join(still_red)}")
    webhook_failed = False
    if newly:
        alerts = DDIR / "alerts.md"
        head = "" if alerts.is_file() else generated_page.with_id(
            alerts, "---\ntype: dashboard\ntitle: \"Alerts\"\n---\n\n# Alerts\n\n")
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
        state = {"overall": overall, "stale": stale or 0,
                 "critical_stale": critical_stale or 0,
                 "errored": errored or 0, "invalid_schedule": invalid_schedule or 0,
                 "offmodel": offmodel or 0,
                 "at": now,
                 "reminded_at": {k: v for k, v in remind_book.items() if v}}
        if lane_sets is not None:
            state["errored_lanes"] = sorted(lane_sets["errored"])
            state["offmodel_lanes"] = sorted(lane_sets["off-model"])
            state["critical_stale_lanes"] = sorted(lane_sets["critical-stale"])
            state["invalid_schedule_lanes"] = sorted(lane_sets["invalid-schedule"])
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
    states = {0: "🟢", 1: "🟡", 2: "🟠", 3: "🔴"}
    print(f"health-export: overall {states[overall]} -> {MDIR / 'okengine.prom'} "
          f"({sum(1 for _, _, v in metrics if v is not None)} metrics); "
          f"{len(newly)} new alert(s)")
    for a in newly:
        print(f"  🔴 ALERT: {a}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
