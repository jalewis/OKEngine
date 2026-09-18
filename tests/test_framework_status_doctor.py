"""okengine#405: `framework status` / `framework doctor` — live diagnostics over the shared checks.

Pins the CLI contract: deployment resolution, the FAIL/OK/usage exit-code discipline, --json shape,
--checks selection (and rejection of unknown checks), and that both commands read the SAME shared
checks library the daily validator uses.
"""
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _deployment(tmp_path, *, shadow=False, stuck=False, stamp=True, jobs=True):
    dep = tmp_path / "dep"
    (dep / "wiki" / "operational").mkdir(parents=True)
    (dep / ".hermes-data" / "cron-plus").mkdir(parents=True)
    schema = "types: {actor: {}}\n"
    if shadow:
        schema += "type_aliases: {actor: actor}\n"
    (dep / "schema.yaml").write_text(schema)
    if stamp:
        (dep / ".hermes-data" / "engine-runtime.yaml").write_text(
            "engine_release: v0.13.5\nhermes_pin: abc123\n")
    if jobs:
        (dep / ".hermes-data" / "cron-plus" / "jobs.json").write_text(json.dumps(
            {"jobs": [{"id": "a", "name": "lane-a", "enabled": True},
                      {"id": "b", "name": "lane-b", "enabled": False}]}))
    if stuck:
        runs = dep / ".okengine" / "operations" / "runs" / "actor-review"
        runs.mkdir(parents=True)
        (runs / "r1.json").write_text(json.dumps(
            {"operation": "actor-review", "run_id": "r1", "status": "running", "pid": 999999}))
    return dep


def _call(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- doctor

def test_doctor_json_shape_and_fail_exit(tmp_path):
    doctor = _load("framework_doctor")
    dep = _deployment(tmp_path, shadow=True)
    code, out, _ = _call(doctor, [str(dep), "--checks", "schema", "--json"])
    payload = json.loads(out)
    assert payload["summary"]["status"] == "FAIL" and code == 1
    assert payload["checks"] == ["schema"]
    assert any(f["area"] == "schema" and f["level"] == "FAIL" for f in payload["findings"])


def test_doctor_clean_subset_is_ok_exit_zero(tmp_path):
    doctor = _load("framework_doctor")
    dep = _deployment(tmp_path)                     # no shadow, jobs present
    code, out, _ = _call(doctor, [str(dep), "--checks", "crons,subdomains"])
    assert code == 0
    assert "OK" in out


def test_doctor_unknown_check_is_usage_error(tmp_path):
    doctor = _load("framework_doctor")
    dep = _deployment(tmp_path)
    code, _, err = _call(doctor, [str(dep), "--checks", "nope"])
    assert code == 2 and "unknown check" in err


def test_doctor_missing_deployment_is_usage_error(tmp_path):
    doctor = _load("framework_doctor")
    code, _, err = _call(doctor, [str(tmp_path / "does-not-exist")])
    assert code == 2 and "not found" in err


def test_diagnostics_rejects_an_unrecognizable_existing_directory(tmp_path):
    diagnostics = _load("framework_diagnostics")
    empty = tmp_path / "empty"
    empty.mkdir()
    dep, err = diagnostics.resolve(str(empty))
    assert dep is None and "does not look like a deployment" in err


def test_doctor_prints_explicit_empty_findings_state(tmp_path, monkeypatch):
    doctor = _load("framework_doctor")
    dep = _deployment(tmp_path)
    monkeypatch.setattr(doctor.D, "run", lambda names: [])
    code, out, _ = _call(doctor, [str(dep), "--checks", "schema"])
    assert code == 0 and "(no findings)" in out


def test_doctor_human_output_lists_findings(tmp_path, monkeypatch):
    doctor = _load("framework_doctor")
    dep = _deployment(tmp_path)
    monkeypatch.setattr(doctor.D, "run", lambda names: [("INFO", "schema", "checked")])
    code, out, _ = _call(doctor, [str(dep), "--checks", "schema"])
    assert code == 0 and "INFO [schema] checked" in out


def test_doctor_list_checks_enumerates_the_registry(tmp_path):
    doctor = _load("framework_doctor")
    code, out, _ = _call(doctor, [str(_deployment(tmp_path)), "--list-checks"])
    assert code == 0
    for name in ("pins", "schema", "write-path", "operations"):
        assert name in out


# ---------------------------------------------------------------- status

def test_status_json_reports_version_scheduler_and_rollup(tmp_path):
    status = _load("framework_status")
    dep = _deployment(tmp_path, shadow=True, stuck=True)
    code, out, _ = _call(status, [str(dep), "--json"])
    payload = json.loads(out)
    assert payload["version"]["engine_release"] == "v0.13.5"
    assert payload["scheduler"]["enabled_lanes"] == 1 and payload["scheduler"]["total_lanes"] == 2
    assert payload["areas"].get("schema") == "FAIL"       # per-area worst-level roll-up
    assert payload["summary"]["status"] == "FAIL" and code == 1


def test_status_clean_deployment_is_ok(tmp_path):
    status = _load("framework_status")
    dep = _deployment(tmp_path)                            # no shadow
    code, out, _ = _call(status, [str(dep)])
    # a clean schema + present scheduler → no FAIL area
    assert code == 0
    assert "engine: v0.13.5" in out


def test_status_dead_scheduler_is_surfaced(tmp_path):
    status = _load("framework_status")
    dep = _deployment(tmp_path, jobs=False)
    code, out, _ = _call(status, [str(dep)])
    assert "NO jobs.json" in out


def test_status_helper_errors_stalled_scheduler_and_empty_rollup(tmp_path, monkeypatch):
    status = _load("framework_status")
    data = tmp_path / "data"; data.mkdir()
    assert status._stamp(data) == {"engine_release": None, "hermes_pin": None}
    (data / "engine-runtime.yaml").write_text(
        "comment without colon\nunknown: value\nengine_release: \nhermes_pin: pin\n")
    assert status._stamp(data) == {"engine_release": None, "hermes_pin": "pin"}

    cron = data / "cron-plus"; cron.mkdir()
    (cron / "jobs.json").write_text("{broken")
    assert status._scheduler(data)["unparseable"] is True
    (cron / "jobs.json").write_text('{"jobs": [{"enabled": true}]}')
    (cron / ".scheduler-stalled").write_text("")
    assert status._scheduler(data)["stalled"] is True

    assert status._rollup([
        ("FAIL", "area", "first"), ("INFO", "area", "lower"),
        ("UNKNOWN", "other", "default rank"),
    ]) == {"area": "FAIL", "other": "UNKNOWN"}

    dep = _deployment(tmp_path / "deployment")
    (dep / ".hermes-data/cron-plus/.scheduler-stalled").write_text("")
    monkeypatch.setattr(status.D, "run", lambda _checks: [])
    code, out, _ = _call(status, [str(dep)])
    assert code == 0 and "STALLED sentinel" in out and "  areas:" not in out

    monkeypatch.setattr(status.D, "resolve", lambda _value: (None, "bad deployment"))
    code, _, error = _call(status, ["missing"])
    assert code == status.D.EXIT_USAGE and "bad deployment" in error
