import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("pipeline_watch", REPO / "ci/pipeline_watch.py")
watch = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)


def test_systemd_units_distinguish_incidents_and_notify_on_process_failure():
    units = REPO / "deploy/systemd"
    primary = (units / "okengine-ci-watch.service").read_text(encoding="utf-8")
    liveness = (units / "okengine-ci-watch-liveness.service").read_text(encoding="utf-8")
    assert "SuccessExitStatus=1" in primary
    assert "OnFailure=okengine-ci-watch-failure.service" in primary
    assert "OnFailure=okengine-ci-watch-failure.service" in liveness


def pipeline(number, source, status, created="2026-08-27T12:00:00Z"):
    return {"id": number, "source": source, "status": status, "created_at": created,
            "sha": f"{number:040d}", "web_url": f"https://git/pipelines/{number}"}


def test_latest_surfaces_ignore_running_and_keep_branch_separate_from_schedule():
    values = [
        pipeline(9, "push", "running"), pipeline(8, "schedule", "failed"),
        pipeline(7, "push", "success"), pipeline(6, "schedule", "success"),
    ]
    assert watch.latest_by_kind(values) == {
        "default-branch": values[2], "scheduled": values[1],
    }


def test_failed_lanes_and_streak_stop_at_first_nonmatching_run():
    current = pipeline(5, "schedule", "failed", "2026-08-27T12:00:00Z")
    older = [
        pipeline(4, "schedule", "failed", "2026-08-26T12:00:00Z"),
        pipeline(3, "push", "failed", "2026-08-26T11:00:00Z"),
        pipeline(2, "schedule", "failed", "2026-08-25T12:00:00Z"),
        pipeline(1, "schedule", "success", "2026-08-24T12:00:00Z"),
    ]
    jobs = {
        5: [{"name": "mutation-critical", "status": "failed"}],
        4: [{"name": "mutation-critical", "status": "failed"}],
        3: [{"name": "mutation-critical", "status": "failed"}],
        2: [{"name": "lint", "status": "failed"}],
    }
    assert watch.failed_lanes(jobs[5]) == ["mutation-critical"]
    assert watch.lane_streak("mutation-critical", current, older,
                             lambda item: jobs[item["id"]]) == (2, "2026-08-26T12:00:00Z")
    assert watch.lane_streak("mutation-critical", current, [], lambda item: []) == (
        1, "2026-08-27T12:00:00Z")


def test_latest_mutation_progress_ignores_noise_and_malformed_records():
    trace = "\n".join([
        "ordinary output",
        "2026-08-27T12:00:00Z 01E mutation-progress not-json",
        "2026-08-27T12:00:01Z 01E mutation-progress "
        '{"event":"target_start","path":"scripts/x.py"}',
        "2026-08-27T12:01:01Z 01E mutation-progress "
        '{"event":"target_heartbeat","path":"scripts/x.py","elapsed_seconds":60}',
    ])
    timestamp, payload = watch.latest_mutation_progress(trace)
    assert timestamp == dt.datetime(2026, 8, 27, 12, 1, 1, tzinfo=dt.timezone.utc)
    assert payload["event"] == "target_heartbeat"
    assert payload["path"] == "scripts/x.py"


def test_glab_text_returns_trace_and_reports_api_failure(monkeypatch):
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs:
                        watch.subprocess.CompletedProcess([], 0, "trace", ""))
    assert watch.glab_text("projects/24/jobs/1/trace") == "trace"
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs:
                        watch.subprocess.CompletedProcess([], 1, "", "denied"))
    with pytest.raises(RuntimeError, match="denied"):
        watch.glab_text("projects/24/jobs/1/trace")


def test_latest_mutation_progress_ignores_bad_timestamp_and_non_event_payloads():
    trace = "\n".join([
        'not-a-time 01E mutation-progress {"event":"target_start"}',
        '2026-08-27T12:00:00Z 01E mutation-progress {"path":"x.py"}',
    ])
    assert watch.latest_mutation_progress(trace) is None


def test_running_mutation_stalls_flags_stale_and_absent_progress(monkeypatch):
    now = dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc)
    pipelines = [pipeline(10, "api", "running")]
    jobs = [
        {"id": 101, "name": "mutation-full: [1/3]", "status": "running",
         "started_at": "2026-08-27T12:50:00Z", "web_url": "https://git/jobs/101"},
        {"id": 102, "name": "mutation-full: [2/3]", "status": "running",
         "started_at": "2026-08-27T12:59:30Z", "web_url": "https://git/jobs/102"},
        {"id": 103, "name": "unit-suite", "status": "running",
         "started_at": "2026-08-27T12:00:00Z", "web_url": "https://git/jobs/103"},
    ]
    monkeypatch.setattr(watch, "glab_json", lambda path: jobs)

    def trace(path):
        if "/101/" in path:
            return ("2026-08-27T12:55:00Z 01E mutation-progress "
                    '{"event":"target_heartbeat","path":"scripts/x.py"}\n')
        if "/102/" in path:
            return ("2026-08-27T12:59:30Z 01E mutation-progress "
                    '{"event":"target_heartbeat","path":"scripts/y.py"}\n')
        raise AssertionError(path)

    monkeypatch.setattr(watch, "glab_text", trace)
    stalls = watch.running_mutation_stalls("24", pipelines, now, 120)
    assert [stall["job_id"] for stall in stalls] == [101]
    assert stalls[0]["target"] == "scripts/x.py"
    assert stalls[0]["silent_seconds"] == 300

    monkeypatch.setattr(watch, "glab_text", lambda path: "no progress lines")
    stalls = watch.running_mutation_stalls("24", pipelines, now, 120)
    assert [stall["job_id"] for stall in stalls] == [101]
    assert stalls[0]["last_event"] is None
    assert stalls[0]["silent_seconds"] == 600


def test_collect_appends_running_mutation_stalls(monkeypatch):
    now = dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc)
    pipelines = [pipeline(10, "api", "running")]
    monkeypatch.setattr(watch, "glab_json", lambda *args: pipelines)
    monkeypatch.setattr(watch, "running_mutation_stalls", lambda *args: [{
        "job_id": 101, "job_name": "mutation-critical: [1/4]",
    }])
    report = watch.collect("24", "main", 10, now)
    assert report["incidents"] == [{
        "kind": "default-branch", "undetectable": True,
        "reason": "no finished pipeline in query window",
    }, {
        "kind": "scheduled", "undetectable": True,
        "reason": "no finished pipeline in query window",
    }, {
        "kind": "running-mutation", "stalled_jobs": [{
            "job_id": 101, "job_name": "mutation-critical: [1/4]",
        }],
    }]


def test_runner_capacity_incident_distinguishes_idle_capacity_from_saturation(monkeypatch):
    now = dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc)
    pending = [{
        "id": 44, "name": "diff-check", "status": "pending",
        "created_at": "2026-08-27T12:57:00Z", "web_url": "https://git/jobs/44",
        "pipeline": {"id": 9, "web_url": "https://git/pipelines/9"},
    }]
    active = {
        15: [{"id": 1}],
        16: [{"id": 2}, {"id": 3}, {"id": 4}],
    }
    manager = [{"id": 11, "system_id": "r_system", "status": "online"}]

    def api(path):
        if path.startswith("projects/"):
            return pending
        if "/jobs?status=running" in path:
            return active[int(path.split("/")[1])]
        if path.endswith("/managers"):
            return manager
        raise AssertionError(path)

    monkeypatch.setattr(watch, "glab_json", api)
    incident = watch.runner_capacity_incident("24", [15, 16], 5, now)
    assert incident == {
        "kind": "runner-capacity", "runner_ids": [15, 16], "manager_ids": [11],
        "manager_system_ids": ["r_system"], "active_builds": 4,
        "expected_capacity": 5, "pending_jobs": 1, "oldest_queued_seconds": 180,
        "oldest_job_id": 44, "oldest_job_name": "diff-check",
        "oldest_job_url": "https://git/jobs/44", "pipeline_id": 9,
        "pipeline_url": "https://git/pipelines/9",
    }

    active[16].append({"id": 5})
    assert watch.runner_capacity_incident("24", [15, 16], 5, now) is None


def test_runner_capacity_incident_is_disabled_or_ignores_fresh_queue(monkeypatch):
    monkeypatch.setattr(watch, "glab_json", lambda path: pytest.fail("unexpected API call"))
    now = dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc)
    assert watch.runner_capacity_incident("24", [], 5, now) is None
    assert watch.runner_capacity_incident("24", [16], 0, now) is None

    pending = [{"id": 1, "created_at": "2026-08-27T12:59:00Z"}]
    monkeypatch.setattr(watch, "glab_json", lambda path: pending)
    assert watch.runner_capacity_incident("24", [16], 5, now) is None

    monkeypatch.setattr(watch, "glab_json", lambda path: [])
    assert watch.runner_capacity_incident("24", [16], 5, now) is None


def test_render_and_fingerprint_include_runner_capacity_details():
    incident = {
        "kind": "runner-capacity", "runner_ids": [15, 16], "manager_ids": [11],
        "manager_system_ids": ["r_system"], "active_builds": 4,
        "expected_capacity": 5, "pending_jobs": 2, "oldest_queued_seconds": 140,
        "oldest_job_id": 44, "oldest_job_name": "diff-check",
        "oldest_job_url": "https://git/jobs/44", "pipeline_id": 9,
        "pipeline_url": "https://git/pipelines/9",
    }
    report = {"checked_at": "now", "incidents": [incident]}
    text = watch.render(report, "owner")
    assert "active/expected builds: `4/5`" in text
    assert "runners `[15, 16]` / managers `[11]`" in text
    assert "oldest queue age: `140s`" in text
    before = watch.fingerprint(report)
    incident["active_builds"] = 3
    assert watch.fingerprint(report) != before


def test_render_and_fingerprint_include_running_mutation_stall():
    report = {"checked_at": "now", "incidents": [{
        "kind": "running-mutation", "stalled_jobs": [{
            "job_id": 101, "job_name": "mutation-full: [1/3]",
            "job_url": "https://git/jobs/101", "target": "scripts/x.py",
            "last_event": "target_heartbeat",
            "last_progress_at": "2026-08-27T12:55:00+00:00", "silent_seconds": 300,
        }],
    }]}
    text = watch.render(report, "owner")
    assert "Running mutation jobs without a fresh heartbeat" in text
    assert "target `scripts/x.py` silent for 300s" in text
    first = watch.fingerprint(report)
    report["incidents"][0]["stalled_jobs"][0]["target"] = "scripts/y.py"
    assert watch.fingerprint(report) != first


def test_render_names_lane_duration_and_merge_attribution():
    report = {
        "checked_at": "2026-08-27T13:00:00+00:00", "incidents": [{
            "kind": "default-branch", "pipeline_id": 10,
            "pipeline_url": "https://git/pipelines/10",
            "lanes": [{"name": "coverage-floor", "consecutive_failures": 3,
                       "duration": "2.0 days", "failing_since": "2026-08-25T13:00:00Z"}],
            "attribution": "Introduced at `abc` by !9 — change (Author).",
        }],
    }
    text = watch.render(report, "maintainer")
    assert "@maintainer" in text
    assert "`coverage-floor`: 3 consecutive failure(s), failing for 2.0 days" in text
    assert "Introduced at `abc` by !9" in text


def test_fingerprint_changes_with_streak_but_not_check_timestamp():
    report = {"checked_at": "first", "incidents": [{
        "kind": "scheduled", "pipeline_id": 4,
        "lanes": [{"name": "mutation", "consecutive_failures": 2}],
    }]}
    first = watch.fingerprint(report)
    report["checked_at"] = "second"
    assert watch.fingerprint(report) == first
    report["incidents"][0]["lanes"][0]["consecutive_failures"] = 3
    assert watch.fingerprint(report) != first


def test_liveness_fails_closed_for_missing_and_stale_state(tmp_path):
    now = dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc)
    state = tmp_path / "state.json"
    assert watch.check_liveness(state, 900, now) == 1

    state.write_text(json.dumps({"checked_at": "2026-08-27T12:00:00+00:00"}))
    assert watch.check_liveness(state, 900, now) == 1

    state.write_text(json.dumps({"checked_at": "2026-08-27T12:50:01+00:00"}))
    assert watch.check_liveness(state, 900, now) == 0


def test_collect_reports_undetectable_when_a_surface_has_no_finished_run(monkeypatch):
    values = [pipeline(3, "push", "success")]
    monkeypatch.setattr(watch, "glab_json", lambda *_args: values)
    report = watch.collect("24", "main", 30,
                           dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc))
    assert report["incidents"] == [{
        "kind": "scheduled", "undetectable": True,
        "reason": "no finished pipeline in query window",
    }]


def test_sync_issue_creates_durable_alert(monkeypatch):
    report = {"checked_at": "2026-08-27T13:00:00+00:00", "incidents": [{
        "kind": "scheduled", "pipeline_id": 4, "pipeline_url": "https://git/p/4",
        "lanes": [{"name": "mutation", "consecutive_failures": 2,
                   "duration": "1.0 days", "failing_since": "2026-08-26T13:00:00Z"}],
    }]}
    calls = []

    def fake_glab(*args):
        calls.append(args)
        if args == ("user",):
            return {"id": 7}
        if "issues?state=opened" in " ".join(args):
            return []
        if "POST" in args:
            return {"iid": 88}
        raise AssertionError(args)

    monkeypatch.setattr(watch, "glab_json", fake_glab)
    assert watch.sync_issue("24", report, "maintainer", {}) == 88
    assert any("title=" + watch.ALERT_TITLE in arg for call in calls for arg in call)
    create_call = next(call for call in calls if "POST" in call)
    description_index = next(index for index, arg in enumerate(create_call)
                             if arg.startswith("description="))
    assert create_call[description_index - 1] == "--raw-field"
    assert create_call[description_index].startswith("description=@maintainer")


def test_sync_issue_closes_recovered_alert(monkeypatch):
    calls = []

    def fake_glab(*args):
        calls.append(args)
        if "issues?state=opened" in " ".join(args):
            return [{"iid": 88, "title": watch.ALERT_TITLE}]
        if "PUT" in args:
            return {"iid": 88}
        raise AssertionError(args)

    monkeypatch.setattr(watch, "glab_json", fake_glab)
    assert watch.sync_issue("24", {"incidents": []}, "maintainer", {}) is None
    assert any("state_event=close" in arg for call in calls for arg in call)


@pytest.mark.parametrize("returncode,stdout,stderr,message", [
    (1, "", "forge down", "forge down"),
    (1, "", "", "glab api projects/24 failed"),
    (0, "diagnostic", "", "no JSON payload"),
])
def test_glab_json_fails_closed(monkeypatch, returncode, stdout, stderr, message):
    class Result:
        pass
    result = Result()
    result.returncode, result.stdout, result.stderr = returncode, stdout, stderr
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(RuntimeError, match=message):
        watch.glab_json("projects/24")


def test_glab_json_accepts_diagnostics_before_payload(monkeypatch):
    result = type("Result", (), {
        "returncode": 0, "stdout": 'notice\n[{"id": 1}]', "stderr": ""})()
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs: result)
    assert watch.glab_json("projects/24") == [{"id": 1}]


def test_atomic_json_creates_parent_replaces_and_cleans_temporary(tmp_path, monkeypatch):
    target = tmp_path / "state" / "watch.json"
    watch.atomic_json(target, {"answer": 42})
    assert json.loads(target.read_text()) == {"answer": 42}
    assert list(target.parent.iterdir()) == [target]

    monkeypatch.setattr(watch.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(OSError, match="no"):
        watch.atomic_json(target, {"answer": 43})
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("start,expected", [
    ("2026-08-25T12:00:00Z", "2.0 days"),
    ("2026-08-27T11:00:00Z", "2.0 hours"),
    ("2026-08-27T12:59:59Z", "1 minutes"),
    ("2026-08-27T14:00:00Z", "1 minutes"),
])
def test_human_age_units_and_future_clamp(start, expected):
    now = dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc)
    assert watch.human_age(start, now) == expected


def test_attribution_handles_missing_sha_merge_and_plain_commit(monkeypatch):
    assert "no SHA" in watch.attribution("24", {})

    def merged(path):
        if path.endswith("/merge_requests"):
            return [{"iid": 9, "title": "Fix"}]
        return {"title": "Commit", "author_name": "Author"}
    monkeypatch.setattr(watch, "glab_json", merged)
    assert "by !9" in watch.attribution("24", {"sha": "a" * 40})

    monkeypatch.setattr(watch, "glab_json", lambda path: [] if path.endswith("merge_requests")
                        else {"title": "Commit"})
    text = watch.attribution("24", {"sha": "b" * 40})
    assert "Current red revision" in text and "unknown author" in text


def test_collect_builds_failed_incident_caches_jobs_and_degrades_attribution(monkeypatch):
    values = [
        pipeline(5, "push", "failed", "2026-08-27T12:00:00Z"),
        pipeline(4, "push", "failed", "2026-08-26T12:00:00Z"),
        pipeline(3, "push", "success", "2026-08-25T12:00:00Z"),
        pipeline(2, "schedule", "success", "2026-08-27T11:00:00Z"),
    ]
    calls = []

    def api(path):
        calls.append(path)
        if "pipelines?" in path:
            return values
        if "/pipelines/5/jobs" in path or "/pipelines/4/jobs" in path:
            return [{"name": "lint", "status": "failed"}]
        raise ValueError("commit unavailable")

    monkeypatch.setattr(watch, "glab_json", api)
    report = watch.collect("24", "main", 30,
                           dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc))
    assert len(report["incidents"]) == 1
    incident = report["incidents"][0]
    assert incident["lanes"][0]["consecutive_failures"] == 2
    assert "Attribution unavailable" in incident["attribution"]
    assert sum("/pipelines/5/jobs" in call for call in calls) == 1


def test_collect_handles_failed_schedule_without_commit_attribution(monkeypatch):
    values = [
        pipeline(5, "schedule", "failed"),
        # Repeated id deliberately exercises the job cache while walking history.
        pipeline(5, "schedule", "failed", "2026-08-26T12:00:00Z"),
        pipeline(4, "push", "success"),
    ]
    calls = []

    def api(path):
        calls.append(path)
        return values if "pipelines?" in path else [{"name": "mutation", "status": "failed"}]

    monkeypatch.setattr(watch, "glab_json", api)
    report = watch.collect("24", "main", 30,
                           dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc))
    incident = report["incidents"][0]
    assert incident["kind"] == "scheduled" and incident["attribution"] is None
    assert sum("/pipelines/5/jobs" in call for call in calls) == 1


def test_render_undetectable_and_incident_without_attribution():
    report = {"checked_at": "now", "incidents": [
        {"kind": "scheduled", "undetectable": True, "reason": "missing"},
        {"kind": "default-branch", "pipeline_id": 1, "pipeline_url": "url", "lanes": []},
    ]}
    text = watch.render(report, "owner")
    assert "UNDETECTABLE — missing" in text
    assert "default-branch pipeline [1]" in text


def test_find_alert_issue_requires_exact_title(monkeypatch):
    monkeypatch.setattr(watch, "glab_json", lambda *args: [
        {"iid": 1, "title": "similar"}, {"iid": 2, "title": watch.ALERT_TITLE}])
    assert watch.find_alert_issue("24")["iid"] == 2
    monkeypatch.setattr(watch, "glab_json", lambda *args: [])
    assert watch.find_alert_issue("24") is None


def test_sync_unchanged_existing_issue_is_noop(monkeypatch):
    report = {"checked_at": "now", "incidents": [{
        "kind": "scheduled", "pipeline_id": 4, "pipeline_url": "url", "lanes": []}]}
    fingerprint = watch.fingerprint(report)
    calls = []
    monkeypatch.setattr(watch, "find_alert_issue", lambda project: {"iid": 88})
    monkeypatch.setattr(watch, "glab_json", lambda *args: calls.append(args) or {"iid": 88})
    assert watch.sync_issue("24", report, "owner", {"alert_fingerprint": fingerprint}) == 88
    assert calls == []


def test_sync_changed_existing_issue_updates(monkeypatch):
    report = {"checked_at": "now", "incidents": [{
        "kind": "scheduled", "pipeline_id": 5, "pipeline_url": "url",
        "lanes": [{"name": "mutation", "consecutive_failures": 3,
                   "duration": "1.0 days", "failing_since": "yesterday"}]}]}
    calls = []
    monkeypatch.setattr(watch, "find_alert_issue", lambda project: {"iid": 88})
    monkeypatch.setattr(watch, "glab_json", lambda *args: calls.append(args) or {"iid": 88})
    assert watch.sync_issue("24", report, "owner", {"alert_fingerprint": "old"}) == 88
    assert "PUT" in calls[0]


def test_sync_healthy_without_existing_issue_is_noop(monkeypatch):
    monkeypatch.setattr(watch, "find_alert_issue", lambda project: None)
    monkeypatch.setattr(watch, "glab_json", lambda *args: pytest.fail("unexpected update"))
    assert watch.sync_issue("24", {"incidents": []}, "owner", {}) is None


@pytest.mark.parametrize("body,expected", [
    ('{"checked_at": "now"}', {"checked_at": "now"}),
    ('["not", "an", "object"]', {}),
    ('not json', {}),
])
def test_load_state_requires_json_object(tmp_path, body, expected):
    path = tmp_path / "state.json"
    path.write_text(body)
    assert watch.load_state(path) == expected


def test_main_covers_healthy_alert_and_liveness_modes(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state.json"
    now = dt.datetime(2026, 8, 27, 13, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(watch, "utc_now", lambda: now)
    monkeypatch.setattr(watch, "collect", lambda *args: {
        "checked_at": now.isoformat(), "latest": {}, "incidents": []})
    monkeypatch.setattr(watch, "mutation_dropout_incident", lambda *args: None)
    monkeypatch.setattr(watch, "sync_issue", lambda *args: None)
    assert watch.main(["--state", str(state)]) == 0
    assert "pipeline-watch: healthy" in capsys.readouterr().out
    assert json.loads(state.read_text())["alert_fingerprint"] is None

    capacity = {
        "kind": "runner-capacity", "runner_ids": [16], "manager_ids": [11],
        "manager_system_ids": ["r_system"], "active_builds": 0,
        "expected_capacity": 1, "pending_jobs": 1, "oldest_queued_seconds": 180,
        "oldest_job_id": 44, "oldest_job_name": "diff-check",
        "oldest_job_url": "https://git/jobs/44", "pipeline_id": 9,
        "pipeline_url": "https://git/pipelines/9",
    }
    monkeypatch.setattr(watch, "runner_capacity_incident", lambda *args: capacity)
    assert watch.main([
        "--state", str(state), "--runner-ids", "16", "--runner-capacity", "1",
    ]) == 1
    assert "Runnable jobs waiting" in capsys.readouterr().out
    monkeypatch.setattr(watch, "runner_capacity_incident", lambda *args: None)

    monkeypatch.setattr(watch, "mutation_dropout_incident", lambda *args: {
        "kind": "mutation-dropout", "threshold": 3, "detail": "critical.py: 3 runs",
    })
    assert watch.main(["--state", str(state)]) == 1
    assert "critical.py" in capsys.readouterr().out
    monkeypatch.setattr(watch, "mutation_dropout_incident", lambda *args: None)

    state.write_text(json.dumps({"checked_at": now.isoformat()}))
    assert watch.main(["--state", str(state), "--check-liveness"]) == 0

    monkeypatch.setattr(watch, "collect", lambda *args: {
        "checked_at": now.isoformat(), "latest": {}, "incidents": [
            {"kind": "scheduled", "undetectable": True, "reason": "none"}]})
    assert watch.main(["--state", str(state)]) == 1
    assert "UNDETECTABLE" in capsys.readouterr().out


def test_utc_now_is_aware():
    assert watch.utc_now().tzinfo == dt.timezone.utc


def test_entrypoint_distinguishes_tool_failure_from_observed_incident(monkeypatch, capsys):
    monkeypatch.setattr(watch, "main", lambda: 1)
    assert watch.entrypoint() == 1

    def fail():
        raise RuntimeError("GitLab unavailable")

    monkeypatch.setattr(watch, "main", fail)
    assert watch.entrypoint() == 2
    assert "pipeline-watch: ERROR: GitLab unavailable" in capsys.readouterr().err


def test_mutation_dropout_detector_runs_outside_ci_and_returns_named_signal(
        tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1].endswith("fetch_mutation_summaries.py"):
            return watch.subprocess.CompletedProcess(command, 0, "fetched", "")
        return watch.subprocess.CompletedProcess(
            command, 1, "mutation-history: 1 expected\n  ✗ critical.py: no score in 3 runs", ""
        )

    monkeypatch.setattr(watch.subprocess, "run", run)
    incident = watch.mutation_dropout_incident("24", tmp_path, threshold=3)
    assert incident["kind"] == "mutation-dropout"
    assert "critical.py" in incident["detail"]
    assert calls[0][1].endswith("fetch_mutation_summaries.py")
    assert calls[1][1].endswith("mutation_history.py")


def test_mutation_dropout_detector_is_quiet_when_latest_run_is_measured(
        tmp_path, monkeypatch):
    results = iter([
        watch.subprocess.CompletedProcess([], 0, "fetched", ""),
        watch.subprocess.CompletedProcess([], 0, "every expected target produced a score", ""),
    ])
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs: next(results))
    assert watch.mutation_dropout_incident("24", tmp_path) is None


def test_mutation_dropout_detector_fails_closed_on_fetch_or_detector_error(
        tmp_path, monkeypatch):
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs:
                        watch.subprocess.CompletedProcess([], 1, "", "fetch denied"))
    with pytest.raises(RuntimeError, match="fetch denied"):
        watch.mutation_dropout_incident("24", tmp_path)

    results = iter([
        watch.subprocess.CompletedProcess([], 0, "fetched", ""),
        watch.subprocess.CompletedProcess([], 1, "", "history corrupt"),
    ])
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs: next(results))
    with pytest.raises(RuntimeError, match="history corrupt"):
        watch.mutation_dropout_incident("24", tmp_path)


def test_render_and_fingerprint_include_mutation_dropout():
    report = {"checked_at": "now", "incidents": [{
        "kind": "mutation-dropout", "threshold": 3,
        "detail": "critical.py: no score in the last 3 runs",
    }]}
    assert "Critical targets missing" in watch.render(report, "owner")
    assert "critical.py" in watch.render(report, "owner")
    assert watch.fingerprint(report) != watch.fingerprint({
        "checked_at": "later", "incidents": [{
            "kind": "mutation-dropout", "threshold": 3, "detail": "other.py",
        }],
    })
