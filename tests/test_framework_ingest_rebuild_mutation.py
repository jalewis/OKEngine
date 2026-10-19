"""Behavioral contracts for scripts/framework_ingest_rebuild.py that its critical mutation
campaign showed were unpinned (okengine#612): CLI dispatch on runtime argv, required
arguments, exit-code and status mapping, output format, manifest discovery, and the
canonical-page tripwire."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import create_autospec

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "framework_ingest_rebuild_mutation", REPO / "scripts/framework_ingest_rebuild.py")
m = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = m
_SPEC.loader.exec_module(m)


def _argv(*words: str) -> list[str]:
    """Build argv the way sys.argv arrives: fresh str objects, never interned literals."""
    return ["".join(list(word)) for word in words]


def _deployment(tmp_path: Path, *connectors: str) -> Path:
    dep = tmp_path / "dep"
    (dep / "wiki/entities").mkdir(parents=True)
    (dep / "connectors").mkdir()
    (dep / "wiki/entities/a.md").write_text("canonical\n")
    for name in connectors:
        (dep / f"connectors/{name}.yaml").write_text(f"id: {name}\n")
    return dep


def _call(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def _completed(code: int = 0):
    return m.subprocess.CompletedProcess([], code, "ok", "")


def _fake_run(returncodes: dict[str, int] | None = None):
    """An autospecced `_run` recording (script, args) and answering per-connector exit codes."""
    returncodes = returncodes or {}
    calls: list[tuple[str, list[str] | None]] = []

    def answer(script, dep, args=None):
        calls.append((script, args))
        manifest = Path(args[args.index("--manifest") + 1]).stem if args and "--manifest" in args else ""
        return _completed(returncodes.get(manifest, returncodes.get(script, 0)))

    return create_autospec(m._run, side_effect=answer), calls


def _manifest_names(calls) -> list[str]:
    return [Path(args[args.index("--manifest") + 1]).stem for _script, args in calls]


# --- _run --------------------------------------------------------------------------------------

def test_engine_root_is_the_checkout_holding_scripts_cron():
    assert m.ENGINE == REPO


def test_a_script_sorting_before_the_sentinels_runs_as_a_cron_script(tmp_path):
    dep = _deployment(tmp_path)
    run = create_autospec(m.subprocess.run, return_value=_completed())
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(m.subprocess, "run", run)
        m._run("00_bootstrap.py", dep, ["--x"])
    assert run.call_count == 1
    command = run.call_args.args[0]
    assert command == [sys.executable, str(REPO / "scripts/cron/00_bootstrap.py"), "--x"]
    assert run.call_args.kwargs["env"]["WIKI_PATH"] == str(dep)
    assert run.call_args.kwargs["text"] is True
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["check"] is False


def test_gateway_identity_probe_captures_text_without_raising(tmp_path):
    dep = _deployment(tmp_path)
    run = create_autospec(m.subprocess.run, return_value=_completed(5))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(m.subprocess, "run", run)
        assert m._run("@source-hydrate", dep, []).returncode == 5
    kwargs = run.call_args.kwargs
    assert kwargs["text"] is True and kwargs["capture_output"] is True
    assert kwargs["check"] is False


# --- ingest dispatch ---------------------------------------------------------------------------

def test_status_from_runtime_argv_reports_without_running_connectors(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, "alpha", "beta")
    health = dep / ".okengine/connectors/health"
    health.mkdir(parents=True)
    (health / "a.json").write_text('{"connector_id":"alpha","ok":true}')
    (health / "b.json").write_text('{"connector_id":"beta","ok":false}')
    run, calls = _fake_run()
    monkeypatch.setattr(m, "_run", run)
    code, out, _ = _call(m.ingest, _argv("status", str(dep), "--json"))
    payload = json.loads(out)
    assert code == 0
    assert calls == []
    assert payload["connectors"] == ["alpha", "beta"]
    assert payload["failed"] == ["beta"], "only connectors whose health is not ok are failed"


def test_named_run_from_runtime_argv_runs_only_that_connector(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, "alpha", "beta")
    run, calls = _fake_run()
    monkeypatch.setattr(m, "_run", run)
    code, _, _ = _call(m.ingest, _argv("run", str(dep), "--connector", "beta"))
    assert code == 0
    assert _manifest_names(calls) == ["beta"]


def test_retry_from_runtime_argv_runs_only_failed_connectors(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, "alpha", "beta")
    health = dep / ".okengine/connectors/health"
    health.mkdir(parents=True)
    (health / "a.json").write_text('{"connector_id":"alpha","ok":true}')
    (health / "b.json").write_text('{"connector_id":"beta","ok":false}')
    run, calls = _fake_run()
    monkeypatch.setattr(m, "_run", run)
    code, _, _ = _call(m.ingest, _argv("retry", str(dep), "--failed"))
    assert code == 0
    assert _manifest_names(calls) == ["beta"]


def test_an_unreadable_health_record_does_not_hide_later_failures(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, "alpha", "beta")
    health = dep / ".okengine/connectors/health"
    health.mkdir(parents=True)
    (health / "a-invalid.json").write_text("{not json")
    (health / "b-failed.json").write_text('{"connector_id":"beta","ok":false}')
    run, calls = _fake_run()
    monkeypatch.setattr(m, "_run", run)
    code, out, _ = _call(m.ingest, ["status", str(dep), "--json"])
    assert code == 0 and json.loads(out)["failed"] == ["beta"]
    _call(m.ingest, ["retry", str(dep), "--failed"])
    assert _manifest_names(calls) == ["beta"]


def test_a_failed_connector_run_exits_one(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, "alpha", "beta")
    run, _calls = _fake_run({"beta": 4})
    monkeypatch.setattr(m, "_run", run)
    code, out, _ = _call(m.ingest, ["run", str(dep), "--json"])
    assert json.loads(out)["status"] == "failed"
    assert code == 1


@pytest.mark.parametrize("argv", [
    [],
    ["retry", "DEP"],
])
def test_ingest_requires_a_command_and_retry_requires_failed(tmp_path, argv):
    dep = _deployment(tmp_path, "alpha")
    argv = [str(dep) if word == "DEP" else word for word in argv]
    with pytest.raises(SystemExit) as raised:
        _call(m.ingest, argv)
    assert raised.value.code == 2


def test_ingest_output_is_compact_sorted_json_unless_json_flag_asks_for_indent(tmp_path,
                                                                               monkeypatch):
    dep = _deployment(tmp_path, "alpha")
    run, _calls = _fake_run()
    monkeypatch.setattr(m, "_run", run)
    _code, compact, _ = _call(m.ingest, ["status", str(dep)])
    assert compact == '{"connectors": ["alpha"], "failed": [], "health": []}\n'
    _code, pretty, _ = _call(m.ingest, ["status", str(dep), "--json"])
    assert pretty == ('{\n  "connectors": [\n    "alpha"\n  ],\n  "failed": [],\n'
                      '  "health": []\n}\n')


# --- manifests ---------------------------------------------------------------------------------

def test_manifests_under_sources_connectors_are_found_without_a_connectors_dir(tmp_path):
    dep = tmp_path / "dep"
    (dep / "wiki").mkdir(parents=True)
    (dep / "sources/connectors").mkdir(parents=True)
    (dep / "sources/connectors/feed.yml").write_text("id: nested.feed\n")
    assert m._manifests(dep) == {"nested.feed": dep / "sources/connectors/feed.yml"}


def test_a_non_string_connector_id_is_refused(tmp_path):
    dep = _deployment(tmp_path)
    (dep / "connectors/numeric.yaml").write_text("id: 123\n")
    code, _, err = _call(m.ingest, ["status", str(dep)])
    assert code == 1 and "has no id" in err


# --- sources -----------------------------------------------------------------------------------

def test_hydrate_from_runtime_argv_reports_success_for_exit_zero(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    run, calls = _fake_run()
    monkeypatch.setattr(m, "_run", run)
    code, out, _ = _call(m.sources, _argv("hydrate", str(dep), "--missing-body", "--json"))
    payload = json.loads(out)
    assert code == 0
    assert calls == [("source_hydrate.py", ["--limit", "20"])]
    assert payload["status"] == "succeeded"


@pytest.mark.parametrize("returncode", [1, -9])
def test_a_nonzero_or_signal_exit_is_a_failed_source_operation(tmp_path, monkeypatch,
                                                                returncode):
    dep = _deployment(tmp_path)
    run, _calls = _fake_run({"classify_sources.py": returncode})
    monkeypatch.setattr(m, "_run", run)
    code, out, _ = _call(m.sources, ["reconcile", str(dep), "--json"])
    assert json.loads(out)["status"] == "failed"
    assert code == returncode


def test_sources_requires_a_command_and_hydrate_requires_missing_body(tmp_path):
    dep = _deployment(tmp_path)
    for argv in ([], ["hydrate", str(dep)]):
        with pytest.raises(SystemExit) as raised:
            _call(m.sources, argv)
        assert raised.value.code == 2


def test_sources_json_output_is_indented_and_sorted(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    run, _calls = _fake_run()
    monkeypatch.setattr(m, "_run", run)
    _code, out, _ = _call(m.sources, ["reconcile", str(dep), "--json"])
    assert out == ('{\n  "operation": "reconcile",\n  "status": "succeeded",\n'
                   '  "stderr": "",\n  "stdout": "ok"\n}\n')


# --- rebuild -----------------------------------------------------------------------------------

def test_rebuild_requires_a_family(tmp_path):
    dep = _deployment(tmp_path)
    with pytest.raises(SystemExit) as raised:
        _call(m.rebuild, [str(dep)])
    assert raised.value.code == 2


def test_rebuild_json_output_is_indented_and_sorted(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    run, _calls = _fake_run()
    monkeypatch.setattr(m, "_run", run)
    _code, out, _ = _call(m.rebuild, [str(dep), "--dashboards", "--json"])
    assert out == (
        '{\n  "children": [\n    {\n      "exit_code": 0,\n      "family": "dashboards",\n'
        '      "script": "refresh_kb_dashboards.py",\n      "stderr": "",\n'
        '      "stdout": "ok"\n    }\n  ],\n  "families": [\n    "dashboards"\n  ],\n'
        '  "status": "succeeded"\n}\n')


def test_paginated_index_pages_are_derived_outside_dashboards(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    paged = dep / "wiki/entities/INDEX-p2.md"
    paged.write_text("page two\n")
    assert "wiki/entities/INDEX-p2.md" not in m._canonical_snapshot(dep)

    def regenerate(script, deployment, args=None):
        paged.write_text("regenerated page two, longer\n")
        return _completed()

    monkeypatch.setattr(m, "_run", create_autospec(m._run, side_effect=regenerate))
    code, _, err = _call(m.rebuild, [str(dep), "--indexes"])
    assert code == 0, err


def test_the_tripwire_covers_every_canonical_page_beside_derived_ones(tmp_path):
    dep = _deployment(tmp_path)
    (dep / "wiki/INDEX.md").write_text("root index\n")
    expected = {"wiki/entities/a.md"}
    for name in ("alpha", "beta", "gamma", "delta"):
        (dep / f"wiki/{name}").mkdir()
        (dep / f"wiki/{name}/INDEX.md").write_text("derived\n")
        (dep / f"wiki/{name}/page.md").write_text("canonical\n")
        expected.add(f"wiki/{name}/page.md")
    assert set(m._canonical_snapshot(dep)) == expected
