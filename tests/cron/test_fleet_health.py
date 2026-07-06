"""fleet_health (okengine#161): flags stale / errored / off-model / never-run lanes from the
deployed jobs.json + run logs."""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


def _run(tmp, jobs, logs, monkeypatch):
    (tmp / "wiki").mkdir(parents=True, exist_ok=True)
    jp = tmp / "jobs.json"
    jp.write_text(json.dumps({"jobs": jobs}))
    ld = tmp / "logs"
    ld.mkdir()
    for name, content, age_s in logs:
        f = ld / f"{name.replace(':', '_')}-20260101-000000.log"
        f.write_text(content)
        import os
        os.utime(f, (time.time() - age_s, time.time() - age_s))
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    monkeypatch.setenv("CRON_JOBS", str(jp))
    monkeypatch.setenv("CRON_LOGS", str(ld))
    spec = importlib.util.spec_from_file_location("fleet_health", REPO / "scripts/cron/fleet_health.py")
    m = importlib.util.module_from_spec(spec); sys.modules["fleet_health"] = m
    spec.loader.exec_module(m)
    assert m.main() == 0
    return (tmp / "wiki" / "dashboards" / "fleet-health.md").read_text()


def test_errored_offmodel_neverrun(tmp_path, monkeypatch):
    jobs = [
        {"name": "good", "enabled": True, "schedule": {"expr": "0 0 * * *"}},
        {"name": "broken", "enabled": True, "schedule": {"expr": "0 0 * * *"}},
        {"name": "brief", "enabled": True, "schedule": {"expr": "0 0 * * *"}, "model": "deepseek-v4-pro"},
        {"name": "never", "enabled": True, "schedule": {"expr": "0 0 * * *"}},
        {"name": "off", "enabled": False, "schedule": {"expr": "0 0 * * *"}},   # disabled -> ignored
    ]
    logs = [
        ("good", "all good\nwakeAgent false\n", 3600),
        ("broken", "...\nTraceback (most recent call last):\nValueError: boom\n", 3600),
        ("brief", "agent_init provider=openrouter model=nvidia/nemotron:free\n", 3600),
        # 'never' has no log
    ]
    dash = _run(tmp_path, jobs, logs, monkeypatch)
    assert "ERRORED" in dash and "broken" in dash
    assert "OFF-MODEL" in dash and "brief" in dash
    assert "never-run" in dash and "never" in dash
    assert "off" not in dash.split("All enabled")[-1]      # disabled lane excluded


def test_stale(tmp_path, monkeypatch):
    pytest.importorskip("croniter")
    jobs = [{"name": "daily", "enabled": True, "schedule": {"expr": "0 0 * * *"}}]
    dash = _run(tmp_path, jobs, [("daily", "ok\n", 5 * 86400)], monkeypatch)   # 5d old, daily cadence
    assert "STALE" in dash and "daily" in dash


def test_foreign_owned_dashboard_fails_loud_not_crash(tmp_path, monkeypatch, capsys):  # invariant-audit #10
    """If the dashboard file is foreign-owned (root, from a bare docker exec), the lane uid can't
    overwrite it. A raw PermissionError would crash the monitor ON ITS OWN OUTPUT with no peer.
    The lane must fail loud with the repair (return 1), not traceback."""
    import os
    import stat
    if os.geteuid() == 0:
        import pytest
        pytest.skip("root can write any file — the permission trap needs a non-root uid")
    (tmp_path / "wiki" / "dashboards").mkdir(parents=True)
    out = tmp_path / "wiki" / "dashboards" / "fleet-health.md"
    out.write_text("stale-green")
    os.chmod(out, 0)                                     # unwritable (simulates a root-owned file)
    jp = tmp_path / "jobs.json"
    jp.write_text(json.dumps({"jobs": [{"name": "x", "enabled": True, "schedule": {"expr": "0 0 * * *"}}]}))
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("CRON_JOBS", str(jp))
    monkeypatch.setenv("CRON_LOGS", str(tmp_path / "logs"))
    spec = importlib.util.spec_from_file_location("fleet_health", REPO / "scripts/cron/fleet_health.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["fleet_health"] = m
    spec.loader.exec_module(m)
    try:
        rc = m.main()
    finally:
        os.chmod(out, stat.S_IWUSR | stat.S_IRUSR)      # restore so tmp cleanup works
    assert rc == 1
    assert "foreign-owned" in capsys.readouterr().err
