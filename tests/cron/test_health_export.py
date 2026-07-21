"""health_export (okengine#64): Prometheus metrics + transition-based alerts."""
import importlib.util, sys, json, os
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent.parent


def _dash(dd, name, body):
    (dd / f"{name}.md").write_text(f"---\ntype: dashboard\ntitle: {name}\nupdated: 2026-06-28T20:00:00Z\n---\n{body}\n")


def _lanes(dd, *, errored=(), offmodel=()):
    (dd / ".fleet-lanes.json").write_text(json.dumps({
        "updated": "2026-07-16T12:00:00Z",
        "errored": list(errored),
        "off-model": list(offmodel),
    }))


def _run(tmp, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp)); monkeypatch.setenv("METRICS_DIR", str(tmp / "metrics"))
    monkeypatch.delenv("ALERT_WEBHOOK", raising=False)
    spec = importlib.util.spec_from_file_location("health_export", REPO / "scripts/cron/health_export.py")
    m = importlib.util.module_from_spec(spec); sys.modules["health_export"] = m; spec.loader.exec_module(m)
    assert m.main() == 0


def test_metrics_and_alert(tmp_path, monkeypatch):
    dd = tmp_path / "wiki" / "dashboards"; dd.mkdir(parents=True)
    _dash(dd, "fleet-health", "- 🟢 ok: **51**  ·  🔴 stale: **0**  ·  🔴 errored: **1**  ·  🔴 off-model: **0**")
    _dash(dd, "source-grounding", "- in scope: **106**  ·  🟢 grounded: **43** (41%)  ·  🔴 ungrounded: **58**")
    _dash(dd, "review-queue", "**7 item(s) awaiting a human** · GROUNDING: **4**")
    _dash(dd, "conformance", "- pages checked: **8823**  ·  source-refs-are-pages: **21**")
    _run(tmp_path, monkeypatch)
    prom = (tmp_path / "metrics" / "okengine.prom").read_text()
    assert "okengine_grounding_pct 41" in prom
    assert "okengine_review_queue 7" in prom
    assert "okengine_conformance_violations 21" in prom
    assert "okengine_fleet_lanes_attention 1" in prom
    assert "okengine_health_overall 2" in prom            # red (1 errored + grounding 41%)
    alerts = (tmp_path / "wiki" / "dashboards" / "alerts.md").read_text()
    assert "overall health is RED" in alerts              # transition green->red on first run
    # second run, unchanged -> NO new alert appended
    before = alerts
    _run(tmp_path, monkeypatch)
    assert (tmp_path / "wiki" / "dashboards" / "alerts.md").read_text() == before


def test_equal_error_count_with_new_lane_identities_alerts(tmp_path, monkeypatch):
    """A/B recover while C/D fail: count stays 2, but the newly failed lanes must notify."""
    dd = tmp_path / "wiki" / "dashboards"
    dd.mkdir(parents=True)
    _dash(dd, "fleet-health",
          "- 🟢 ok: 50  ·  🔴 stale: 0  ·  🔴 errored: 2  ·  🔴 off-model: 0")
    _lanes(dd, errored=("lane-a", "lane-b"))
    _run(tmp_path, monkeypatch)
    alerts = dd / "alerts.md"
    before = alerts.read_text(encoding="utf-8")

    _lanes(dd, errored=("lane-c", "lane-d"))
    _run(tmp_path, monkeypatch)

    added = alerts.read_text(encoding="utf-8")[len(before):]
    assert "newly ERRORED: lane-c, lane-d" in added
    state = json.loads((tmp_path / "metrics" / ".health-state.json").read_text())
    assert state["errored_lanes"] == ["lane-c", "lane-d"]


def test_recovery_without_new_lane_does_not_alert(tmp_path, monkeypatch):
    dd = tmp_path / "wiki" / "dashboards"
    dd.mkdir(parents=True)
    _dash(dd, "fleet-health",
          "- 🟢 ok: 50  ·  🔴 stale: 0  ·  🔴 errored: 2  ·  🔴 off-model: 0")
    _lanes(dd, errored=("lane-a", "lane-b"))
    _run(tmp_path, monkeypatch)
    alerts = dd / "alerts.md"
    before = alerts.read_text(encoding="utf-8")

    _dash(dd, "fleet-health",
          "- 🟢 ok: 51  ·  🔴 stale: 0  ·  🔴 errored: 1  ·  🔴 off-model: 0")
    _lanes(dd, errored=("lane-b",))
    _run(tmp_path, monkeypatch)

    assert alerts.read_text(encoding="utf-8") == before


def test_stale_lane_sidecar_falls_back_to_counts(tmp_path, monkeypatch):
    dd = tmp_path / "wiki" / "dashboards"
    dd.mkdir(parents=True)
    _lanes(dd, errored=("old-lane",))
    _dash(dd, "fleet-health",
          "- 🟢 ok: 50  ·  🔴 stale: 0  ·  🔴 errored: 2  ·  🔴 off-model: 0")
    # Do not depend on filesystem timestamp granularity: some CI filesystems give two immediate
    # writes the same mtime, which makes the intentionally stale sidecar look current.
    dashboard_mtime = (dd / "fleet-health.md").stat().st_mtime
    os.utime(dd / ".fleet-lanes.json", (dashboard_mtime - 1, dashboard_mtime - 1))

    _run(tmp_path, monkeypatch)

    alerts = (dd / "alerts.md").read_text(encoding="utf-8")
    assert "a cron lane newly ERRORED (now 2)" in alerts
    state = json.loads((tmp_path / "metrics" / ".health-state.json").read_text())
    assert "errored_lanes" not in state


def test_stale_or_missing_monitor_exports_red(tmp_path, monkeypatch):  # invariant-audit #9
    """A missing/stale fleet-health dashboard means the upstream monitor is DEAD. health must
    export RED + monitor_stale=1, not a frozen last-green (dead-monitor-reads-healthy fail-open)."""
    (tmp_path / "wiki" / "dashboards").mkdir(parents=True)   # dashboards dir exists but NO fleet-health
    md = tmp_path / "metrics"
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("METRICS_DIR", str(md))
    monkeypatch.delenv("ALERT_WEBHOOK", raising=False)
    spec = importlib.util.spec_from_file_location("health_export", REPO / "scripts/cron/health_export.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["health_export"] = m
    spec.loader.exec_module(m)
    assert m.main() == 0
    prom = (md / "okengine.prom").read_text()
    assert "okengine_health_overall 2" in prom, "dead monitor must export RED, not green"
    assert "okengine_health_monitor_stale 1" in prom


def test_fresh_monitor_is_not_flagged_stale(tmp_path, monkeypatch):
    """A freshly-written fleet-health dashboard must NOT trip the freshness gate."""
    dd = tmp_path / "wiki" / "dashboards"
    dd.mkdir(parents=True)
    _dash(dd, "fleet-health", "- 🟢 ok: **51**  ·  🔴 stale: **0**  ·  🔴 errored: **0**  ·  🔴 off-model: **0**")
    md = tmp_path / "metrics"
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("METRICS_DIR", str(md))
    monkeypatch.delenv("ALERT_WEBHOOK", raising=False)
    spec = importlib.util.spec_from_file_location("health_export", REPO / "scripts/cron/health_export.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["health_export"] = m
    spec.loader.exec_module(m)
    assert m.main() == 0
    prom = (md / "okengine.prom").read_text()
    assert "okengine_health_monitor_stale 0" in prom


def test_webhook_failure_does_not_advance_state(tmp_path, monkeypatch):  # invariant-audit #20
    """The webhook is the only push channel without Prometheus. If it FAILS, the dedup state must
    NOT advance to the alerted level — else the transition test never re-fires and the RED alert is
    lost for the whole incident. Keep the prior state so the next run re-detects + retries."""
    import urllib.request
    dd = tmp_path / "wiki" / "dashboards"
    dd.mkdir(parents=True)
    _dash(dd, "fleet-health", "- 🟢 ok: **51**  ·  🔴 stale: **0**  ·  🔴 errored: **1**  ·  🔴 off-model: **0**")
    _dash(dd, "source-grounding", "- in scope: **106**  ·  🟢 grounded: **43** (41%)  ·  🔴 ungrounded: **58**")
    md = tmp_path / "metrics"
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("METRICS_DIR", str(md))
    monkeypatch.setenv("ALERT_WEBHOOK", "http://127.0.0.1:1/hook")   # set, but delivery will fail

    def _boom(*a, **k):
        raise OSError("webhook down")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    spec = importlib.util.spec_from_file_location("health_export", REPO / "scripts/cron/health_export.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["health_export"] = m
    spec.loader.exec_module(m)
    assert m.main() == 0
    sf = md / ".health-state.json"
    state = json.loads(sf.read_text()) if sf.is_file() else {}
    assert state.get("overall", 0) != 2, "dedup state advanced despite webhook failure — RED alert lost"


def test_heartbeat_gauge_emitted_even_when_monitor_dead(tmp_path, monkeypatch):  # invariant-audit B6.3
    """The other gauges only detect the UPSTREAM fleet-health dashboard going stale. Nothing catches
    health_export ITSELF dying, after which Prometheus scrapes the frozen .prom (last value maybe
    green) forever. A heartbeat gauge (unix time this exporter last wrote) lets an alert fire on
    `time() - okengine_health_export_timestamp_seconds > N` regardless of the frozen values. It must
    be present ALWAYS — including the dead-upstream-monitor path — and carry a fresh timestamp."""
    import time, re
    (tmp_path / "wiki" / "dashboards").mkdir(parents=True)   # no fleet-health -> upstream monitor dead
    md = tmp_path / "metrics"
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("METRICS_DIR", str(md))
    monkeypatch.delenv("ALERT_WEBHOOK", raising=False)
    before = time.time()
    spec = importlib.util.spec_from_file_location("health_export", REPO / "scripts/cron/health_export.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["health_export"] = m
    spec.loader.exec_module(m)
    assert m.main() == 0
    prom = (md / "okengine.prom").read_text()
    mt = re.search(r"^okengine_health_export_timestamp_seconds (\d+)$", prom, re.MULTILINE)
    assert mt, f"heartbeat gauge missing from export:\n{prom}"
    ts = int(mt.group(1))
    assert before - 5 <= ts <= time.time() + 5, f"heartbeat timestamp {ts} not fresh"
    assert "okengine_health_overall 2" in prom              # emitted alongside the RED dead-monitor verdict


def test_heartbeat_pings_external_dead_mans_switch(tmp_path, monkeypatch):  # invariant-audit #11
    """The detect->notify chain runs INSIDE the scheduler, so a webhook-only deployment can't detect
    the scheduler's OWN death. health_export must ping OKENGINE_HEARTBEAT_URL every run so an EXTERNAL
    dead-man's switch alerts when the ping stops."""
    import urllib.request
    dd = tmp_path / "wiki" / "dashboards"
    dd.mkdir(parents=True)
    _dash(dd, "fleet-health", "- 🟢 ok: **51**  ·  🔴 errored: **0**  ·  🔴 off-model: **0**")
    hits = []

    class _Resp:
        def read(self): return b""
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=8: hits.append(getattr(req, "full_url", str(req))) or _Resp())
    monkeypatch.setenv("OKENGINE_HEARTBEAT_URL", "http://dms.example/ping")
    _run(tmp_path, monkeypatch)
    assert any("dms.example" in str(h) for h in hits), hits
