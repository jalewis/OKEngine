"""health_export (okengine#64): Prometheus metrics + transition-based alerts."""
import importlib.util, sys, json
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent.parent


def _dash(dd, name, body):
    (dd / f"{name}.md").write_text(f"---\ntype: dashboard\ntitle: {name}\nupdated: 2026-06-28T20:00:00Z\n---\n{body}\n")


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
