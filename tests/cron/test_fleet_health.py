"""fleet_health (okengine#161): flags stale / errored / off-model / never-run lanes from the
deployed jobs.json + run logs."""
import importlib.util
import builtins
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


def _module():
    spec = importlib.util.spec_from_file_location(
        "fleet_health_memory", REPO / "scripts/cron/fleet_health.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(tmp, jobs, logs, monkeypatch):
    (tmp / "wiki").mkdir(parents=True, exist_ok=True)
    jp = tmp / "jobs.json"
    jp.write_text(json.dumps({"jobs": jobs}))
    ld = tmp / "logs"
    ld.mkdir()
    for name, content, age_s in logs:
        stamp = datetime.fromtimestamp(
            time.time() - age_s, tz=timezone.utc
        ).strftime("%Y%m%d-%H%M%S")
        f = ld / f"{name.replace(':', '_')}-{stamp}.log"
        terminal = (
            "ERROR cron-plus.runner: agent run failed: Script exited\n"
            if "Traceback (most recent call last)" in content
            else f"INFO cron-plus.runner: cron-plus runner completed (job={name})\n"
        )
        f.write_text(content + terminal)
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


def _write_run(log_dir, name, stamp, content, mtime):
    import os
    path = log_dir / f"{name.replace(':', '_')}-{stamp}.log"
    path.write_text(content)
    os.utime(path, (mtime, mtime))
    return path


def test_cron_error_reports_missing_parser_and_parser_exception(monkeypatch):
    module = _module()
    real_import = builtins.__import__

    def no_croniter(name, *args, **kwargs):
        if name == "croniter":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_croniter)
    assert "parser unavailable" in module._cron_error("0 0 * * *")
    monkeypatch.setattr(builtins, "__import__", real_import)

    class BrokenCroniter:
        @staticmethod
        def is_valid(_expr):
            return True

        def __init__(self, *_args):
            raise ValueError("cannot schedule")

    monkeypatch.setitem(sys.modules, "croniter", type("CroniterModule", (), {"croniter": BrokenCroniter})())
    assert "cannot compute a next fire" in module._cron_error("0 0 * * *")


def test_errored_offmodel_neverrun(tmp_path, monkeypatch):
    jobs = [
        {"name": "good", "enabled": True, "schedule": {"expr": "0 0 * * *"}},
        {"name": "broken", "enabled": True, "schedule": {"expr": "0 0 * * *"}},
        {"name": "brief", "enabled": True, "schedule": {"expr": "0 0 * * *"}, "model": "deepseek-flash"},
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


def test_invalid_schedule_is_red_not_benign_never_run(tmp_path, monkeypatch):
    jobs = [
        {"name": "dead", "enabled": True, "schedule": {"expr": "99 99 * * *"}},
        {"name": "weekly-waiting", "enabled": True,
         "schedule": {"expr": "5 9 * * SUN"}},
    ]

    dash = _run(tmp_path, jobs, [], monkeypatch)
    sidecar = json.loads((tmp_path / "wiki" / "dashboards" / ".fleet-lanes.json").read_text())

    assert "🔴 INVALID-SCHEDULE | dead" in dash
    assert "🔴 invalid-schedule: 1" in dash and "**🔴 attention needed**" in dash
    assert "🟡 never-run | weekly-waiting" in dash
    assert sidecar["invalid-schedule"] == ["dead"]


@pytest.mark.parametrize("payload", [json.dumps(["not", "an", "object"]), "not JSON"])
def test_non_object_qmd_snapshot_is_not_published_to_consumers(
        tmp_path, monkeypatch, payload):
    """The sidecar contract is an object even when qmd leaves a malformed JSON root behind."""
    qmd = tmp_path / "search-telemetry.json"
    qmd.write_text(payload)
    monkeypatch.setenv("OKENGINE_QMD_STATS", str(qmd))
    _run(tmp_path, [], [], monkeypatch)
    sidecar = json.loads(
        (tmp_path / "wiki" / "dashboards" / ".fleet-lanes.json").read_text())
    assert sidecar["search_telemetry"] == {}


def test_object_qmd_snapshot_is_published_unchanged(tmp_path, monkeypatch):
    """The valid-object half of the sidecar branch must remain observable."""
    qmd = tmp_path / "search-telemetry.json"
    snapshot = {"search": {"calls": 2, "p95_ms": 7}}
    qmd.write_text(json.dumps(snapshot))
    monkeypatch.setenv("OKENGINE_QMD_STATS", str(qmd))
    _run(tmp_path, [], [], monkeypatch)
    sidecar = json.loads(
        (tmp_path / "wiki" / "dashboards" / ".fleet-lanes.json").read_text())
    assert sidecar["search_telemetry"] == snapshot


def test_stale(tmp_path, monkeypatch):
    pytest.importorskip("croniter")
    jobs = [{"name": "daily", "enabled": True, "schedule": {"expr": "0 0 * * *"}}]
    dash = _run(tmp_path, jobs, [("daily", "ok\n", 5 * 86400)], monkeypatch)   # 5d old, daily cadence
    assert "🟡 STALE" in dash and "daily" in dash
    assert "🟡 stale: 1" in dash
    assert "**🟡 warning**" in dash


def test_critical_stale_escalates_to_orange(tmp_path, monkeypatch):
    pytest.importorskip("croniter")
    jobs = [{"name": "quarter-hour", "enabled": True,
             "schedule": {"expr": "*/15 * * * *"}}]
    dash = _run(tmp_path, jobs, [("quarter-hour", "ok\n", 3 * 3600)], monkeypatch)
    assert "🟠 CRITICAL-STALE" in dash
    assert "🟠 critical-stale: 1" in dash
    assert "**🟠 degraded**" in dash
    assert "cadence ~15m" in dash


def test_terminal_error_remains_red_even_when_old_enough_to_be_stale(tmp_path, monkeypatch):
    pytest.importorskip("croniter")
    jobs = [{"name": "failed", "enabled": True, "schedule": {"expr": "*/15 * * * *"}}]
    logs = [("failed", "Traceback (most recent call last):\nRuntimeError: failed\n", 3 * 3600)]
    dash = _run(tmp_path, jobs, logs, monkeypatch)
    assert "🔴 ERRORED" in dash
    assert "CRITICAL-STALE | failed" not in dash


def test_freshness_copy_describes_threshold_not_current_overdue_state(tmp_path, monkeypatch):
    jobs = [{"name": "fleet-health", "enabled": True,
             "schedule": {"expr": "*/15 * * * *"}}]
    dash = _run(tmp_path, jobs, [("fleet-health", "ok\n", 60)], monkeypatch)
    assert "considered stale when older than 45m" in dash
    assert "refresh is overdue" not in dash


def test_search_timeout_and_saturation_are_distinct(tmp_path, monkeypatch):
    jobs = [
        {"name": "timeout", "enabled": True, "schedule": {"expr": "0 * * * *"}},
        {"name": "capacity", "enabled": True, "schedule": {"expr": "0 * * * *"}},
    ]
    logs = [
        ("timeout", "okengine-mcp: SEARCH_TIMEOUT after 120s\n", 60),
        ("capacity", "(search saturated: qmd capacity is busy; retry with backoff)\n", 60),
    ]
    dash = _run(tmp_path, jobs, logs, monkeypatch)
    assert "🔴 TIMEOUT" in dash and "timeout" in dash
    assert "🟠 SATURATED" in dash and "capacity" in dash
    assert "timeout: 1" in dash and "saturated: 1" in dash


def test_cron_hard_timeout_is_not_hidden_in_generic_error_bucket(tmp_path, monkeypatch):
    jobs = [{"name": "lacuna", "enabled": True, "schedule": {"expr": "0 * * * *"}}]
    logs = [(
        "lacuna",
        "ERROR cron.scheduler: TimeoutError: cron-plus run exceeded hard timeout of 1200s\n"
        "ERROR cron-plus.runner: agent run failed: TimeoutError\n",
        60,
    )]

    dash = _run(tmp_path, jobs, logs, monkeypatch)

    assert "🔴 TIMEOUT | lacuna" in dash
    assert "1200s hard timeout" in dash
    assert "ERRORED | lacuna" not in dash
    assert "🔴 timeout: 1" in dash and "🔴 errored: 0" in dash


def test_deployment_validate_requires_an_explicit_verdict(tmp_path, monkeypatch):
    jobs = [{"name": "deployment-validate", "enabled": True,
             "schedule": {"expr": "0 * * * *"}}]

    absent = _run(tmp_path / "absent", jobs, [(
        "deployment-validate",
        "INFO cron.scheduler: wakeAgent=false gate — silent run\n",
        60,
    )], monkeypatch)
    assert "🟠 UNDETECTABLE | deployment-validate" in absent
    assert "without a deployment-validate PASS/FAIL verdict" in absent
    assert "🟠 undetectable: 1" in absent

    present = _run(tmp_path / "present", jobs, [(
        "deployment-validate",
        "deployment-validate: PASS (0 fail, 0 warn) -> report\n",
        60,
    )], monkeypatch)
    assert "🟢 ok | deployment-validate" in present
    assert "UNDETECTABLE | deployment-validate" not in present


# A real passing deployment-validate log, as cron-plus writes it: the script's stdout (and so its
# verdict) is not persisted on a silent run. Captured from a live gateway (#757).
_SILENT_PASS_LOG = (
    "INFO cron.scheduler: Job '70c0b69cc497' (no_agent): wakeAgent=false gate — silent run\n"
    "INFO cron-plus.runner: agent returned [SILENT] — skipping delivery\n"
)
_DEPLOY_JOBS = [{"name": "deployment-validate", "enabled": True,
                 "schedule": {"expr": "10 12 * * *"}}]


def _deployment_report(tmp, text, age_s):
    report = tmp / "wiki" / "operational" / "deployment-validation.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text)
    import os
    os.utime(report, (time.time() - age_s, time.time() - age_s))
    return report


def test_silent_pass_reads_the_verdict_from_the_report_this_run_wrote(tmp_path, monkeypatch):
    _deployment_report(tmp_path, "# Deployment validation\n\n**PASS** — 0 fail · 2 warn\n", 30)
    dash = _run(tmp_path, _DEPLOY_JOBS, [("deployment-validate", _SILENT_PASS_LOG, 60)], monkeypatch)
    assert "🟢 ok | deployment-validate" in dash
    assert "🟠 undetectable: 0" in dash


def test_a_report_older_than_the_run_cannot_vouch_for_it(tmp_path, monkeypatch):
    _deployment_report(tmp_path, "**PASS** — 0 fail · 0 warn\n", 7200)
    dash = _run(tmp_path, _DEPLOY_JOBS, [("deployment-validate", _SILENT_PASS_LOG, 60)], monkeypatch)
    assert "🟠 UNDETECTABLE | deployment-validate" in dash


def test_a_report_written_in_the_run_start_second_counts(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir(parents=True)
    start = int(time.time()) - 60
    stamp = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    _write_run(tmp_path / "logs", "deployment-validate", stamp, _SILENT_PASS_LOG, start + 5)
    report = _deployment_report(tmp_path, "**PASS** — 0 fail · 0 warn\n", 0)
    import os
    os.utime(report, (start, start))
    dash = _run_existing_logs(tmp_path, _DEPLOY_JOBS, monkeypatch)
    assert "🟢 ok | deployment-validate" in dash


@pytest.mark.parametrize("report", [
    "# Deployment validation\n\nno verdict here\n",
    "PASS — 0 fail · 0 warn\n",            # not the bold verdict line the script writes
    "**PASS** — zero fail\n",
])
def test_a_report_without_the_verdict_line_stays_undetectable(tmp_path, monkeypatch, report):
    _deployment_report(tmp_path, report, 30)
    dash = _run(tmp_path, _DEPLOY_JOBS, [("deployment-validate", _SILENT_PASS_LOG, 60)], monkeypatch)
    assert "🟠 UNDETECTABLE | deployment-validate" in dash


def test_an_unreadable_report_stays_undetectable(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "operational" / "deployment-validation.md").mkdir(parents=True)
    dash = _run(tmp_path, _DEPLOY_JOBS, [("deployment-validate", _SILENT_PASS_LOG, 60)], monkeypatch)
    assert "🟠 UNDETECTABLE | deployment-validate" in dash


def test_a_fail_verdict_on_a_completed_run_is_errored_not_ok(tmp_path, monkeypatch):
    _deployment_report(tmp_path / "report", "**FAIL** — 1 fail · 0 warn\n", 30)
    reported = _run(tmp_path / "report", _DEPLOY_JOBS,
                    [("deployment-validate", _SILENT_PASS_LOG, 60)], monkeypatch)
    logged = _run(tmp_path / "log", _DEPLOY_JOBS, [(
        "deployment-validate", "deployment-validate: FAIL (1 fail, 0 warn) -> report\n", 60,
    )], monkeypatch)
    for dash in (reported, logged):
        assert "🔴 ERRORED | deployment-validate | last run: deployment-validate reported FAIL" in dash
        assert "🔴 errored: 1" in dash


def test_the_logged_verdict_wins_over_the_report(tmp_path, monkeypatch):
    _deployment_report(tmp_path, "**FAIL** — 1 fail · 0 warn\n", 30)
    dash = _run(tmp_path, _DEPLOY_JOBS, [(
        "deployment-validate", "deployment-validate: PASS (0 fail, 0 warn) -> report\n", 60,
    )], monkeypatch)
    assert "🟢 ok | deployment-validate" in dash


def test_other_lanes_are_not_judged_by_the_deployment_report(tmp_path, monkeypatch):
    _deployment_report(tmp_path, "**FAIL** — 1 fail · 0 warn\n", 30)
    jobs = [{"name": "ensure-readable", "enabled": True, "schedule": {"expr": "*/15 * * * *"}}]
    dash = _run(tmp_path, jobs, [("ensure-readable", _SILENT_PASS_LOG, 60)], monkeypatch)
    assert "🟢 ok | ensure-readable" in dash


def test_sidecar_publishes_every_classification_bucket(tmp_path, monkeypatch):
    _run(tmp_path, _DEPLOY_JOBS, [("deployment-validate", _SILENT_PASS_LOG, 60)], monkeypatch)
    sidecar = json.loads((tmp_path / "wiki" / "dashboards" / ".fleet-lanes.json").read_text())
    module = sys.modules["fleet_health"]
    assert {key for key, value in sidecar.items() if isinstance(value, list)} == set(
        module.LANE_BUCKETS)


def test_lane_buckets_match_what_observability_validation_cross_checks():
    spec = importlib.util.spec_from_file_location(
        "observability_validate_parity", REPO / "scripts/cron/observability_validate.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    assert sorted(_module().LANE_BUCKETS) == sorted(validator.LANE_KEYS)


def test_dashboard_and_sidecar_describe_one_read_of_the_search_telemetry(tmp_path, monkeypatch):
    """The read-MCP republishes on every call; two reads let the two surfaces disagree (#757)."""
    telemetry = tmp_path / "search-telemetry.json"
    monkeypatch.setenv("OKENGINE_QMD_STATS", str(telemetry))
    calls = {"n": 0}

    def publish():
        calls["n"] += 1
        telemetry.write_text(json.dumps({
            "updated": "2026-09-14T05:00:00Z", "concurrency_limit": 2, "saturated": 0,
            "search": {"ok": calls["n"], "timeouts": 0, "errors": 0, "calls": calls["n"]},
            "maintenance": {"ok": 0, "timeouts": 0, "errors": 0, "calls": 0},
        }))

    publish()
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        text = real_read_text(self, *args, **kwargs)
        if self == telemetry:
            publish()  # the artifact moves on the instant after every read
        return text

    monkeypatch.setattr(Path, "read_text", read_text)
    dash = _run(tmp_path, _DEPLOY_JOBS, [("deployment-validate", _SILENT_PASS_LOG, 60)], monkeypatch)
    monkeypatch.setattr(Path, "read_text", real_read_text)
    sidecar = json.loads((tmp_path / "wiki" / "dashboards" / ".fleet-lanes.json").read_text())
    assert sidecar["search_telemetry"]["search"]["calls"] == 1
    assert "search 1 call(s)" in dash


def test_explicitly_absent_telemetry_is_unknown_without_rereading(tmp_path, monkeypatch):
    telemetry = tmp_path / "search-telemetry.json"
    telemetry.write_text(json.dumps({
        "updated": "2026-09-14T05:00:00Z", "concurrency_limit": 2, "saturated": 0,
        "search": {"calls": 3}, "maintenance": {"calls": 1}}))
    monkeypatch.setenv("OKENGINE_QMD_STATS", str(telemetry))
    module = _module()
    now = datetime(2026, 9, 14, 5, 1, tzinfo=timezone.utc)
    assert module.qmd_search_health(now, None)[0] == "unknown"
    assert module.qmd_search_health(now)[0] == "ok"
    assert module.read_qmd_telemetry()["search"]["calls"] == 3
    telemetry.write_text("{not json")
    assert module.read_qmd_telemetry() is None


def test_latest_terminal_failure_then_success_clears_error(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    _write_run(
        tmp_path / "logs", "lane", "20260722-235959",
        "2026-07-22 19:59:59 ERROR cron-plus.runner: agent run failed: Script exited\n",
        time.time() + 100,
    )
    _write_run(
        tmp_path / "logs", "lane", "20260723-000001",
        "2026-07-22 20:00:01 INFO cron-plus.runner: cron-plus runner completed (job=lane)\n",
        time.time() - 100,
    )
    jobs = [{"name": "lane", "enabled": True, "schedule": {"expr": "0 0 1 1 *"}}]
    dash = _run_existing_logs(tmp_path, jobs, monkeypatch)
    assert "🟢 ok | lane" in dash
    assert "ERRORED | lane" not in dash


def test_silent_no_agent_completion_is_terminal_success(tmp_path, monkeypatch):
    """Deterministic jobs end in SILENT, not the generic runner-completed marker."""
    (tmp_path / "logs").mkdir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    _write_run(
        tmp_path / "logs", "ensure-readable", stamp,
        "INFO cron.scheduler: Job 'x' (no_agent): wakeAgent=false gate — silent run\n"
        "INFO cron-plus.runner: agent returned [SILENT] — skipping delivery\n",
        time.time() - 60,
    )
    jobs = [{"name": "ensure-readable", "enabled": True,
             "schedule": {"expr": "*/15 * * * *"}}]
    dash = _run_existing_logs(tmp_path, jobs, monkeypatch)
    assert "🟢 ok | ensure-readable" in dash
    assert "STALE | ensure-readable" not in dash


def test_latest_terminal_success_then_failure_sets_error_and_ignores_active(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    _write_run(
        tmp_path / "logs", "lane", "20260723-035959",
        "INFO cron-plus.runner: cron-plus runner completed (job=lane)\n",
        time.time() + 200,
    )
    _write_run(
        tmp_path / "logs", "lane", "20260723-040001",
        "ERROR cron-plus.runner: invalid completion receipt: bad hash\n",
        time.time() - 200,
    )
    _write_run(
        tmp_path / "logs", "lane", "20260723-050001",
        "INFO run_agent: generation still in progress\n",
        time.time() + 400,
    )
    jobs = [{"name": "lane", "enabled": True, "schedule": {"expr": "0 0 1 1 *"}}]
    dash = _run_existing_logs(tmp_path, jobs, monkeypatch)
    assert "ERRORED | lane" in dash
    assert "invalid completion receipt" in dash


def test_closed_legacy_log_is_terminal_but_recent_markerless_log_is_active(
    tmp_path, monkeypatch
):
    (tmp_path / "logs").mkdir()
    old = time.time() - 7200
    _write_run(
        tmp_path / "logs", "lane", "20260723-020000",
        "deterministic script output\n", old,
    )
    _write_run(
        tmp_path / "logs", "lane", "20260723-030000",
        "INFO run_agent: generation still in progress\n", time.time(),
    )
    jobs = [{"name": "lane", "enabled": True, "schedule": {"expr": "0 0 1 1 *"}}]
    dash = _run_existing_logs(tmp_path, jobs, monkeypatch)
    assert "🟢 ok | lane" in dash
    assert "never-run | lane" not in dash


def _run_existing_logs(tmp, jobs, monkeypatch):
    (tmp / "wiki").mkdir(parents=True, exist_ok=True)
    jp = tmp / "jobs.json"
    jp.write_text(json.dumps({"jobs": jobs}))
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    monkeypatch.setenv("CRON_JOBS", str(jp))
    monkeypatch.setenv("CRON_LOGS", str(tmp / "logs"))
    spec = importlib.util.spec_from_file_location(
        "fleet_health_sequence", REPO / "scripts/cron/fleet_health.py"
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules["fleet_health_sequence"] = m
    spec.loader.exec_module(m)
    assert m.main() == 0
    return (tmp / "wiki" / "dashboards" / "fleet-health.md").read_text()


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


def test_log_timestamp_read_races_missing_dirs_and_missing_jobs(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "fleet_health_edges", REPO / "scripts/cron/fleet_health.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    plain = tmp_path / "plain.log"; plain.write_text("x")
    assert m._run_timestamp(plain) == plain.stat().st_mtime
    malformed = tmp_path / "lane-20269999-999999.log"; malformed.write_text("x")
    assert m._run_timestamp(malformed) == malformed.stat().st_mtime
    monkeypatch.setattr(m, "LOGS", tmp_path / "missing")
    assert m._latest_log("lane") == (None, None, None)
    monkeypatch.setattr(m, "JOBS", tmp_path / "missing-jobs.json")
    assert m.main() == 1
    assert "no jobs.json" in capsys.readouterr().err


def test_latest_log_and_tail_read_errors_are_tolerated(tmp_path, monkeypatch):
    logs = tmp_path / "logs"; logs.mkdir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    unreadable = _write_run(logs, "lane", stamp,
                            "INFO cron-plus.runner: cron-plus runner completed (job=lane)\n",
                            time.time() - 60)
    jp = tmp_path / "jobs.json"
    jp.write_text(json.dumps({"jobs": [{"name": "lane", "enabled": True,
                                        "schedule": {"expr": "0 * * * *"}}]}))
    (tmp_path / "wiki").mkdir()
    spec = importlib.util.spec_from_file_location(
        "fleet_health_read_edges", REPO / "scripts/cron/fleet_health.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    monkeypatch.setattr(m, "LOGS", logs); monkeypatch.setattr(m, "JOBS", jp)
    monkeypatch.setattr(m, "WIKI", tmp_path / "wiki")
    original = Path.read_text
    calls = {unreadable: 0}
    def flaky(path, *args, **kwargs):
        if path == unreadable:
            calls[path] += 1
            if calls[path] in {1, 3}:
                raise OSError("race")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", flaky)
    assert m._latest_log("lane") == (None, None, None)
    calls[unreadable] = 1  # next read succeeds for discovery; following tail read fails
    assert m.main() == 0


# --- slot starvation on the SCHEDULED path (okengine#614) --------------------

_KILLED = "TimeoutError: cron-plus run exceeded hard timeout of 600s"
_DENIED = ("TimeoutError: model slot unavailable after 300.0s "
           "(identity=custom|http://gpu:11436/v1|m, limit=4)")


def _run_canonical(tmp, jobs, runs, monkeypatch, capsys=None):
    """fleet_health against the CANONICAL layout: <data>/cron-plus/jobs.json + runs/.

    The older _run helper puts jobs.json at the tmp root, which is not where a deployment keeps
    it -- and the starvation scan resolves its run records relative to that.
    """
    (tmp / "wiki").mkdir(parents=True, exist_ok=True)
    cp = tmp / "cron-plus"; cp.mkdir(parents=True, exist_ok=True)
    jp = cp / "jobs.json"
    jp.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    ld = tmp / "logs"; ld.mkdir(exist_ok=True)
    for job_id, recs in (runs or {}).items():
        d = cp / "runs" / job_id
        d.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(recs):
            (d / f"r{i}.json").write_text(json.dumps(r), encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    monkeypatch.setenv("CRON_JOBS", str(jp))
    monkeypatch.setenv("CRON_LOGS", str(ld))
    spec = importlib.util.spec_from_file_location(
        "fleet_health", REPO / "scripts/cron/fleet_health.py")
    m = importlib.util.module_from_spec(spec); sys.modules["fleet_health"] = m
    spec.loader.exec_module(m)
    rc = m.main()
    return rc, (tmp / "wiki" / "dashboards" / "fleet-health.md").read_text()


_LANE = [{"name": "raw-backfill", "enabled": True,
          "schedule": {"expr": "*/15 * * * *"}}]


def test_scheduled_lane_reports_slot_starvation(tmp_path, monkeypatch, capsys):
    """THE point of okengine#614. This ran only in the operator-invoked fleet_status.py, so
    1,558 kills piled up over six weeks with nobody looking. It must now surface on the
    monitor's own cadence, with no operator involved."""
    rc, dash = _run_canonical(tmp_path, _LANE, {"a": [
        {"lane": "raw-backfill", "status": "failed", "error": _DENIED,
         "executed_tool_call_turns": 0},
    ]}, monkeypatch)
    out = capsys.readouterr().out
    assert "slot starvation" in out
    assert "never got an inference slot" in out
    assert "CAPACITY" in out
    assert "raw-backfill" in out
    assert "slot-starved" in dash


def test_a_stall_is_reported_separately_and_does_not_say_raise_concurrency(tmp_path, monkeypatch,
                                                                          capsys):
    """A run that held a slot and returned nothing needs the opposite fix. Reported under the
    starvation heading it told one live deployment to raise concurrency against 0 starvation."""
    rc, dash = _run_canonical(tmp_path, _LANE, {"a": [
        {"lane": "raw-backfill", "status": "failed", "error": _KILLED,
         "executed_tool_call_turns": 0},
    ]}, monkeypatch)
    out = capsys.readouterr().out
    assert "stalled runs" in out.lower()
    assert "LATENCY/LANE, not capacity" in out
    assert "slot starvation: none" in out.lower(), "must NOT be counted as starvation"
    assert "stalled" in dash


def test_scheduled_lane_says_clean_when_it_is(tmp_path, monkeypatch, capsys):
    """Silence is not success -- the clean case must announce itself, as the sibling qmd line does."""
    _run_canonical(tmp_path, _LANE, {"a": [
        {"lane": "raw-backfill", "status": "succeeded", "executed_tool_call_turns": 3},
    ]}, monkeypatch)
    out = capsys.readouterr().out
    assert "slot starvation: none" in out.lower()
    assert "stalled runs: none" in out.lower()


def test_scheduled_lane_reports_undetectable_with_no_run_records(tmp_path, monkeypatch, capsys):
    _run_canonical(tmp_path, _LANE, {}, monkeypatch)
    out = capsys.readouterr().out
    assert "UNDETECTABLE" in out and "not a pass" in out


def test_an_overrun_that_did_work_is_not_reported_as_starvation(tmp_path, monkeypatch, capsys):
    _run_canonical(tmp_path, _LANE, {"a": [
        {"lane": "raw-backfill", "status": "failed", "error": _KILLED,
         "executed_tool_call_turns": 5},
    ]}, monkeypatch)
    out = capsys.readouterr().out
    assert "all with work executed" in out
    assert "slot starvation: none" in out.lower()


def test_non_canonical_jobs_path_falls_back_to_hermes_home_not_a_wrong_sibling(tmp_path, monkeypatch):
    """A relocated CRON_JOBS must not silently address some unrelated directory: that would read
    UNDETECTABLE forever while looking correctly configured."""
    spec = importlib.util.spec_from_file_location(
        "fleet_health", REPO / "scripts/cron/fleet_health.py")
    monkeypatch.setenv("CRON_JOBS", str(tmp_path / "elsewhere" / "jobs.json"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "declared"))
    m = importlib.util.module_from_spec(spec); sys.modules["fleet_health"] = m
    spec.loader.exec_module(m)
    assert m._data_dir() == Path(tmp_path / "declared")

    monkeypatch.setenv("CRON_JOBS", str(tmp_path / "d" / "cron-plus" / "jobs.json"))
    m2 = importlib.util.module_from_spec(spec); sys.modules["fleet_health"] = m2
    spec.loader.exec_module(m2)
    assert m2._data_dir() == Path(tmp_path / "d")


def test_memory_events_report_cap_hits_and_oom_kills(tmp_path):
    m = _module()
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\nmax 4276\noom 2\noom_kill 2\n")
    state, detail = m.memory_pressure(events)
    assert state == "error"
    assert "2 OOM kill" in detail and "4276 memory-cap hit" in detail


def test_missing_memory_events_are_unknown_not_healthy(tmp_path):
    m = _module()
    assert m.memory_pressure(tmp_path / "missing")[0] == "unknown"


def test_memory_events_warn_and_clean_states(tmp_path):
    m = _module()
    events = tmp_path / "memory.events"
    events.write_text("max 3\noom_kill 0\n")
    assert m.memory_pressure(events)[0] == "warn"
    events.write_text("max 0\noom_kill 0\n")
    assert m.memory_pressure(events)[0] == "ok"


def test_scheduled_dashboard_surfaces_timeout_and_memory_pressure(tmp_path, monkeypatch):
    events = tmp_path / "memory.events"
    events.write_text("max 7\noom_kill 0\n")
    monkeypatch.setenv("OKENGINE_MEMORY_EVENTS", str(events))
    jobs = [{**_LANE[0], "timeout": 900}]
    records = [{"lane": "raw-backfill", "status": "succeeded", "duration_seconds": 890}
               for _ in range(20)]
    _rc, dashboard = _run_canonical(tmp_path, jobs, {"a": records}, monkeypatch)
    assert "TIMEOUT-PRESSURE" in dashboard
    assert "memory-cap hit" in dashboard
