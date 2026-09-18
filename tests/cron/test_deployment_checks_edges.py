"""Branch-focused coverage for shared deployment health checks."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/cron/deployment_checks.py"


def _mod(name="deployment_checks_edges"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _messages(module):
    return "\n".join(f"{level} {area} {msg}" for level, area, msg in module.F)


def test_yaml_and_pin_stamp_unwritable_paths(tmp_path, monkeypatch):
    c = _mod("deployment_checks_pin_edges")
    vault, data, hermes = tmp_path / "v", tmp_path / "d", tmp_path / "h"
    vault.mkdir(); data.mkdir(); hermes.mkdir()
    (vault / "engine.version").write_text("version: v1.0.0\nhermes_pin: hp-new\n")
    stamp = data / "engine-runtime.yaml"
    stamp.write_text("engine_release: v0.1.0\nhermes_pin: hp-old\n")
    (hermes / ".okengine_release").write_text("v1.0.0")
    (hermes / ".hermes_pin").write_text("hp-new")
    c.configure(vault, data, hermes)
    original = Path.write_text
    monkeypatch.setattr(Path, "write_text", lambda path, *a, **k: (
        (_ for _ in ()).throw(OSError("readonly")) if path == stamp else original(path, *a, **k)))
    c.check_pins()
    text = _messages(c)
    assert "stamp is not writable" in text

    broken = vault / "broken.yaml"
    broken.write_text("[bad")
    assert c._yaml(broken) is None
    assert "unparseable" in _messages(c)


def test_schema_composition_error_fallback_and_compare_helpers(tmp_path, monkeypatch):
    c = _mod("deployment_checks_schema_edges")
    (tmp_path / "wiki/missing").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {known: {}}\ntype_aliases: {alias: absent}\n"
        "partitioning: {namespaces: {not-created: {strategy: by-letter}}}\n")
    c.configure(tmp_path, tmp_path / "data", tmp_path / "hermes")
    fake_schema = SimpleNamespace(compose_schema=lambda *_a, **_k: (
        {"types": {"known": {}}}, ["compose error"]))
    monkeypatch.setitem(sys.modules, "schema_lib", fake_schema)
    c.check_schema()
    text = _messages(c)
    assert "composition: compose error" in text
    assert "not a composed type" in text
    assert "does not exist" in text

    assert c._artifact_missing_pack_governance({"a": 1}, [])
    assert c._artifact_missing_pack_governance([1], {})
    assert c._schema_documents_equal([], {}) is False
    assert c._schema_documents_equal({"_meta": 1, "a": 2}, {"_other": 3, "a": 2})

    monkeypatch.setitem(sys.modules, "schema_lib", SimpleNamespace(
        compose_schema=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("compose crash"))))
    c.reset(); c.check_schema()
    assert "falling back" in _messages(c)


def test_schema_artifact_full_compose_failures_and_fallback(tmp_path, monkeypatch):
    c = _mod("deployment_checks_schema_artifact_edges")
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {one: {}}\n")
    art = tmp_path / ".okengine/composed-schema.yaml"
    art.parent.mkdir()
    art.write_text("types: {one: {}}\n")
    c.configure(tmp_path, tmp_path / "data", tmp_path / "hermes")
    schema = SimpleNamespace(compose_schema=lambda *_a, **_k: ({"types": {"one": {}}}, []))
    monkeypatch.setitem(sys.modules, "schema_lib", schema)
    monkeypatch.setitem(sys.modules, "extension_compose", SimpleNamespace(
        composed_schema=lambda _p: (_ for _ in ()).throw(RuntimeError("full crash"))))
    c.check_schema()
    assert "full runtime composition crashed" in _messages(c)

    monkeypatch.delitem(sys.modules, "extension_compose", raising=False)
    # Force the import-safe fallback and its stale subset warning.
    schema.compose_schema = lambda *_a, **kwargs: (
        {"types": {"one": {}, "two": {}}}, []) if kwargs.get("fragments") == [] else (
        {"types": {"one": {}}}, [])
    import builtins
    original_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (
        (_ for _ in ()).throw(ImportError("missing")) if name == "extension_compose"
        else original_import(name, *a, **k)))
    c.reset(); c.check_schema()
    assert "STALE against base+pack" in _messages(c)


def test_cron_store_defensive_paths(tmp_path, monkeypatch):
    c = _mod("deployment_checks_cron_edges")
    data = tmp_path / "data"
    store = data / "cron-plus/jobs.json"
    store.parent.mkdir(parents=True)
    store.write_text("{bad")
    pids = data / "cron-plus/pids"
    pids.mkdir()
    (pids / "one.pid").write_text("1")
    sent = data / "cron-plus/.scheduler-stalled"
    sent.write_text("not-json")
    c.configure(tmp_path, data, tmp_path / "hermes")
    original_stat = Path.stat
    calls = {store: 0}
    def stat_second(path, *args, **kwargs):
        if path == store:
            calls[path] += 1
            if calls[path] == 2:
                raise OSError("stat")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat_second)
    c.check_crons()
    text = _messages(c)
    assert "unreadable job store" in text
    assert "jobs.json unparseable" in text
    assert c._cron_plus_tz_aware() is None

    store.write_text(json.dumps({"jobs": [{"id": "x", "name": "absolute", "script": "/missing.py"}]}))
    monkeypatch.setattr(Path, "stat", original_stat)
    c.reset(); c.check_crons()
    assert "missing script /missing.py" in _messages(c)

    runtime_script = data / "scripts/present.py"
    runtime_script.parent.mkdir(exist_ok=True)
    runtime_script.write_text("# present\n")
    store.write_text(json.dumps({"jobs": [{
        "id": "x", "name": "runtime-absolute", "script": "/opt/data/scripts/present.py",
    }]}))
    c.reset(); c.check_crons()
    assert "missing script /opt/data/scripts/present.py" not in _messages(c)


def test_timezone_invalid_json_zone_and_job_values(tmp_path, monkeypatch):
    c = _mod("deployment_checks_timezone_edges")
    data = tmp_path / "data"
    jf = data / "cron-plus/jobs.json"
    jf.parent.mkdir(parents=True)
    jf.write_text("{bad")
    c.configure(tmp_path, data, tmp_path / "hermes")
    monkeypatch.setenv("TZ", "America/New_York")
    c.check_timezone()
    assert c.F == []

    jf.write_text(json.dumps({"jobs": [{
        "name": "daily", "schedule": {"expr": "0 7 * * *"},
        "next_run_at": "bad-date"}]}))
    monkeypatch.setattr(c, "_cron_plus_tz_aware", lambda: True)
    monkeypatch.setattr("zoneinfo.ZoneInfo", lambda _tz: (_ for _ in ()).throw(ValueError("zone")))
    c.check_timezone()
    assert any(level == "FAIL" and name == "timezone" and "valid IANA" in detail
               for level, name, detail in c.F)


def test_partition_rules_extensions_and_read_errors(tmp_path, monkeypatch):
    c = _mod("deployment_checks_content_edges")
    wiki = tmp_path / "wiki"
    ns = wiki / "items"
    ns.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "partitioning: {namespaces: {items: {strategy: by-letter}, absent: {strategy: by-date}}}\n")
    unreadable = ns / "one.md"
    unreadable.write_text("page")
    c.configure(tmp_path, tmp_path / "data", tmp_path / "hermes")
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **k: (
        (_ for _ in ()).throw(OSError("read")) if path == unreadable else original(path, *a, **k)))
    c.check_partition_dups()

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "rules.yaml").write_text("[bad")
    c.check_rules()
    assert "unparseable" in _messages(c)

    data = tmp_path / "data"
    jf = data / "cron-plus/jobs.json"
    jf.parent.mkdir(parents=True)
    jf.write_text("{bad")
    c.configure(tmp_path, data, tmp_path / "hermes")
    c.check_extensions()


def test_cron_plus_pin_one_sided_symbolic_and_unreadable_states(tmp_path):
    c = _mod("deployment_checks_pin_edges")
    vault, data, hermes = tmp_path / "vault", tmp_path / "data", tmp_path / "hermes"
    vault.mkdir(); hermes.mkdir()
    config = data / "config"; git = data / "plugins/cron-plus/.git"
    config.mkdir(parents=True); git.mkdir(parents=True)
    (vault / "engine.version").write_text("version: v1.0.0\nhermes_pin: h1\n")
    (data / "engine-runtime.yaml").write_text("engine_release: v1.0.0\nhermes_pin: h1\n")
    c.configure(vault, data, hermes)

    (config / "cron-plus.pin").write_text("abc\n")
    c.check_pins()
    assert "expected pin or installed HEAD is missing" in _messages(c)

    c.F.clear()
    (git / "HEAD").write_text("ref: refs/heads/missing\n")
    c.check_pins()
    assert "revision is unreadable" in _messages(c)

    c.F.clear()
    (config / "cron-plus.pin").write_text("\n")
    (git / "HEAD").write_text("abc\n")
    c.check_pins()
    assert "revision is unreadable" in _messages(c)


def test_cron_store_stat_race_is_tolerated(tmp_path, monkeypatch):
    c = _mod("deployment_checks_store_stat_race")
    vault, data, hermes = tmp_path / "vault", tmp_path / "data", tmp_path / "hermes"
    vault.mkdir(); hermes.mkdir()
    runtime = data / "cron-plus"; runtime.mkdir(parents=True)
    jobs = runtime / "jobs.json"; jobs.write_text('{"jobs": []}')
    c.configure(vault, data, hermes)
    original_is_file, original_stat = Path.is_file, Path.stat
    monkeypatch.setattr(Path, "is_file", lambda p: True if p == jobs else original_is_file(p))
    monkeypatch.setattr(Path, "stat", lambda p, *a, **k: (
        (_ for _ in ()).throw(OSError("race")) if p == jobs else original_stat(p, *a, **k)))
    c.check_crons()

def test_ownership_unstattable_is_independent_of_pathlib_call_counts(tmp_path, monkeypatch):
    c = _mod("deployment_checks_owner_edges")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    target = wiki / "page.md"
    target.write_text("x")
    data = tmp_path / "data"
    runtime = data / "cron-plus"
    runtime.mkdir(parents=True)
    jobs = runtime / "jobs.json"
    jobs.write_text("{}")
    c.configure(tmp_path, data, tmp_path / "hermes")
    original = Path.stat
    def stat_target(path, *args, **kwargs):
        if path == target:
            raise OSError("stat")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat_target)
    c.check_ownership()
    assert "unstattable" in _messages(c)


def test_runtime_unstattable_paths_are_reported_with_one_stat_call(tmp_path, monkeypatch):
    c = _mod("deployment_checks_runtime_owner_edges")
    data = tmp_path / "data"
    runtime = data / "cron-plus"
    runtime.mkdir(parents=True)
    jobs = runtime / "jobs.json"
    jobs.write_text("{}")
    c.configure(tmp_path, data, tmp_path / "hermes")
    monkeypatch.setattr(c.os, "geteuid", lambda: 1000)
    original = Path.stat

    def stat_runtime(path, *args, **kwargs):
        if path in {runtime, jobs}:
            raise OSError("stat")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat_runtime)
    c.check_runtime_ownership()
    messages = _messages(c)
    assert "cron-plus (unstattable)" in messages
    assert "cron-plus/jobs.json (unstattable)" in messages


def test_runtime_ownership_compares_exact_uid_and_continues_past_non_dirs(
        tmp_path, monkeypatch):
    c = _mod("deployment_checks_runtime_exact_uid")
    data = tmp_path / "data"
    data.mkdir()
    # The first configured path exists but is not a directory. It must not stop
    # later runtime roots from being checked.
    (data / "cron-plus").write_text("not a directory")
    (data / "scripts").mkdir()
    owner = (data / "scripts").stat().st_uid
    c.configure(tmp_path, data, tmp_path / "hermes")

    monkeypatch.setattr(c.os, "geteuid", lambda: -1)
    c.check_runtime_ownership()
    assert f"scripts (uid {owner})" in _messages(c)

    c.F.clear()
    monkeypatch.setattr(c.os, "geteuid", lambda: owner)
    c.check_runtime_ownership()
    assert "scripts (uid" not in _messages(c)


def test_runtime_jobs_file_uses_the_same_exact_uid_rule(tmp_path, monkeypatch):
    c = _mod("deployment_checks_runtime_jobs_uid")
    data = tmp_path / "data"
    runtime = data / "cron-plus"
    runtime.mkdir(parents=True)
    jobs = runtime / "jobs.json"
    jobs.write_text("{}")
    owner = jobs.stat().st_uid
    c.configure(tmp_path, data, tmp_path / "hermes")

    monkeypatch.setattr(c.os, "geteuid", lambda: -1)
    c.check_runtime_ownership()
    assert f"cron-plus/jobs.json (uid {owner})" in _messages(c)

    c.F.clear()
    monkeypatch.setattr(c.os, "geteuid", lambda: owner)
    c.check_runtime_ownership()
    assert "cron-plus/jobs.json (uid" not in _messages(c)


def test_runtime_jobs_non_regular_path_is_ignored(tmp_path, monkeypatch):
    c = _mod("deployment_checks_runtime_jobs_non_regular")
    data = tmp_path / "data"
    jobs = data / "cron-plus" / "jobs.json"
    jobs.mkdir(parents=True)
    c.configure(tmp_path, data, tmp_path / "hermes")

    monkeypatch.setattr(c.os, "geteuid", lambda: -1)
    c.check_runtime_ownership()

    assert "cron-plus/jobs.json (uid" not in _messages(c)


def test_runtime_ownership_recurses_below_well_owned_roots(tmp_path, monkeypatch):
    c = _mod("deployment_checks_runtime_recursive")
    data = tmp_path / "data"
    nested = data / "cron-plus" / "runs" / "job" / "receipt.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}")
    lane_uid = 1000
    original = Path.stat

    def drifted_child(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == nested:
            return type(info)(tuple(info)[:4] + (lane_uid + 1,) + tuple(info)[5:])
        if path == data or data in path.parents:
            return type(info)(tuple(info)[:4] + (lane_uid,) + tuple(info)[5:])
        return info

    c.configure(tmp_path, data, tmp_path / "hermes")
    monkeypatch.setattr(c.os, "geteuid", lambda: lane_uid)
    monkeypatch.setattr(Path, "stat", drifted_child)
    c.check_runtime_ownership()
    assert "cron-plus/runs/job/receipt.json (file" in _messages(c)


def test_runtime_ownership_reports_child_race_and_bounds_findings(tmp_path, monkeypatch):
    c = _mod("deployment_checks_runtime_recursive_edges")
    data = tmp_path / "data"
    runtime = data / "cron-plus"
    runtime.mkdir(parents=True)
    raced = runtime / "a-raced.json"
    raced.write_text("{}")
    for index in range(30):
        (runtime / f"drift-{index}.json").write_text("{}")
    c.configure(tmp_path, data, tmp_path / "hermes")
    monkeypatch.setattr(c.os, "geteuid", lambda: -1)
    original = Path.stat
    original_rglob = Path.rglob

    def race_one_child(path, *args, **kwargs):
        if path == raced:
            raise OSError("removed during walk")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", race_one_child)
    monkeypatch.setattr(
        Path, "rglob",
        lambda path, pattern: [raced, *sorted(runtime.glob("drift-*.json"))]
        if path == runtime else original_rglob(path, pattern),
    )
    c.check_runtime_ownership()
    messages = _messages(c)
    assert "cron-plus/a-raced.json (unstattable)" in messages
    assert len([line for line in messages.splitlines() if "drift-" in line]) < 30


def test_auth_unverifiable_editing_mismatch_and_hardened_success(tmp_path, monkeypatch):
    c = _mod("deployment_checks_auth_edges")
    c.configure(tmp_path, tmp_path / "data", tmp_path / "hermes")
    monkeypatch.setenv("API_SERVER_ENABLED", "true")
    c.check_auth()
    assert "UNVERIFIABLE" in _messages(c)

    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(
        "platform_toolsets: {api_server: [okengine, okengine-write]}\n")
    c.configure(tmp_path, data, tmp_path / "hermes")
    monkeypatch.setattr(c, "is_editing", lambda _env: False)
    c.reset(); c.check_auth()
    assert "EDITING is off" in _messages(c)

    monkeypatch.setattr(c, "is_hardened", lambda _env: True)
    monkeypatch.setattr(c, "hardened_posture_violations", lambda _env: [])
    c.reset(); c.check_auth()
    assert "posture satisfied" in _messages(c)


def test_write_path_read_errors(tmp_path, monkeypatch):
    c = _mod("deployment_checks_write_edges")
    data, hermes = tmp_path / "data", tmp_path / "hermes"
    for name in c._WRITE_PATH_LIBS:
        (data / "scripts").mkdir(parents=True, exist_ok=True)
        (hermes / "scripts/cron").mkdir(parents=True, exist_ok=True)
        (data / "scripts" / name).write_text("same")
        (hermes / "scripts/cron" / name).write_text("same")
    for left, right in ((hermes / "config/base-schema.yaml", data / "config/base-schema.yaml"),
                        (hermes / "tools/schema_validator.py", data / "config/schema_validator.py")):
        left.parent.mkdir(parents=True, exist_ok=True); right.parent.mkdir(parents=True, exist_ok=True)
        left.write_text("same"); right.write_text("same")
    c.configure(tmp_path, data, hermes)
    failing = {hermes / "scripts/cron/schema_lib.py", hermes / "config/base-schema.yaml",
               hermes / "tools/schema_validator.py"}
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (
        (_ for _ in ()).throw(OSError("read")) if path in failing else original(path)))
    c.check_write_path_libs()
    assert _messages(c).count("cannot compare") >= 3


def test_operations_unreadable_alive_and_long_summaries(tmp_path, monkeypatch):
    c = _mod("deployment_checks_operation_edges")
    base = tmp_path / ".okengine/operations/runs/op"
    base.mkdir(parents=True)
    (base / "bad.json").write_text("{bad")
    (base / "alive.json").write_text(json.dumps(
        {"operation": "op", "run_id": "alive", "status": "running", "pid": 7}))
    for i in range(6):
        (base / f"stuck{i}.json").write_text(json.dumps(
            {"operation": "op", "run_id": f"stuck{i}", "status": "running", "pid": 0}))
        (base / f"failed{i}.json").write_text(json.dumps(
            {"operation": "op", "run_id": f"failed{i}", "status": "failed"}))
    c.configure(tmp_path)
    monkeypatch.setattr(c.os, "kill", lambda pid, sig: None if pid == 7 else (_ for _ in ()).throw(OSError()))
    c.check_operation_runs()
    text = _messages(c)
    assert "unreadable operation receipt" in text
    assert " …" in text


def test_pin_matching_hermes_marker_falls_through(tmp_path):
    c = _mod("deployment_checks_pin_fallthrough")
    vault, data, hermes = tmp_path / "v", tmp_path / "d", tmp_path / "h"
    vault.mkdir(); data.mkdir(); hermes.mkdir()
    (vault / "engine.version").write_text("version: v1.0.0\nhermes_pin: hp\n")
    (data / "engine-runtime.yaml").write_text("engine_release: v1.0.0\nhermes_pin: hp\n")
    (hermes / ".hermes_pin").write_text("hp")
    c.configure(vault, data, hermes)
    c.check_pins()
    assert c.F == []


def test_schema_multiple_namespaces_full_errors_and_clean_fallback(tmp_path, monkeypatch):
    c = _mod("deployment_checks_schema_loop_edges")
    (tmp_path / "wiki/exists").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {one: {}}\npartitioning: {namespaces: {exists: {}, missing: {}}}\n")
    art = tmp_path / ".okengine/composed-schema.yaml"
    art.parent.mkdir(); art.write_text("types: {one: {}}\n")
    c.configure(tmp_path, tmp_path / "data", tmp_path / "hermes")
    schema = SimpleNamespace(compose_schema=lambda *_a, **_k: ({"types": {"one": {}}}, []))
    monkeypatch.setitem(sys.modules, "schema_lib", schema)
    monkeypatch.setitem(sys.modules, "extension_compose", SimpleNamespace(
        composed_schema=lambda _p: ({"types": {"one": {}}}, ["one", "two"])))
    c.check_schema()
    assert _messages(c).count("full runtime composition:") == 2

    # Fallback available and clean exercises the non-warning subset path.
    import builtins
    original_import = builtins.__import__
    monkeypatch.delitem(sys.modules, "extension_compose", raising=False)
    monkeypatch.setattr(builtins, "__import__", lambda name, *a, **k: (
        (_ for _ in ()).throw(ImportError()) if name == "extension_compose"
        else original_import(name, *a, **k)))
    c.reset(); c.check_schema()
    assert "STALE against base+pack" not in _messages(c)

    calls = {"count": 0}
    def compose_with_fallback_failure(*_args, **kwargs):
        if kwargs.get("fragments") == []:
            raise RuntimeError("fallback compose")
        return {"types": {"one": {}}}, []
    schema.compose_schema = compose_with_fallback_failure
    c.reset(); c.check_schema()
    assert "STALE against base+pack" not in _messages(c)


def test_subdomain_parse_and_typed_info_paths(tmp_path):
    c = _mod("deployment_checks_subdomain_edges")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    bad = wiki / "bad/schema.yaml"; bad.parent.mkdir(); bad.write_text("[bad")
    good = wiki / "good/schema.yaml"; good.parent.mkdir(); good.write_text("types: {one: {}, two: {}}\n")
    c.configure(tmp_path)
    c.check_subdomains()
    text = _messages(c)
    assert "unparseable" in text
    assert "2 type(s)" in text


def test_pid_warning_timezone_loop_and_partition_skip_paths(tmp_path, monkeypatch):
    c = _mod("deployment_checks_loop_edges")
    data = tmp_path / "data"
    pids = data / "cron-plus/pids"
    pids.mkdir(parents=True)
    for i in range(6): (pids / f"{i}.pid").write_text("x")
    jf = data / "cron-plus/jobs.json"
    jf.write_text(json.dumps({"jobs": [
        {"id": "off", "name": "off", "enabled": False,
         "schedule": {"expr": "0 7 * * *"}},
        {"id": "naive", "name": "naive", "schedule": {"expr": "0 7 * * *"},
         "next_run_at": "2026-01-01T07:00:00"},
        {"id": "bad", "name": "bad", "schedule": {"expr": "0 7 * * *"},
         "next_run_at": "bad"},
    ]}))
    c.configure(tmp_path, data, tmp_path / "hermes")
    monkeypatch.setattr(c.os, "geteuid", lambda: -1)
    c.check_crons()
    assert "pidfile(s) owned by another uid" in _messages(c)
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setattr(c, "_cron_plus_tz_aware", lambda: True)
    c.check_timezone()

    vault = tmp_path / "vault"
    (vault / "wiki/items").mkdir(parents=True)
    (vault / "wiki/sub").mkdir()
    (vault / "schema.yaml").write_text(
        "partitioning: {namespaces: {flat: {strategy: flat}, missing: {strategy: by-date}, items: {strategy: by-letter}}}\n")
    (vault / "wiki/sub/schema.yaml").write_text(
        "partitioning: {namespaces: {nested: {strategy: by-letter}}}\n")
    for name in ("INDEX.md", "_meta.md", "README.md", "real.md"):
        (vault / "wiki/items" / name).write_text("---\ntype: x\n---\n")
    c.configure(vault, data, tmp_path / "hermes")
    c.check_partition_dups()
    c.configure(tmp_path / "no-vault", data, tmp_path / "hermes")
    c.check_partition_dups()


def test_rules_extensions_and_ownership_loop_exits(tmp_path, monkeypatch):
    c = _mod("deployment_checks_more_loops")
    cfg = tmp_path / "config"; cfg.mkdir()
    for name in ("one-rules.yaml", "two-rules.yaml"):
        (cfg / name).write_text("rules: [{id: same}, {id: same}]\n")
    (cfg / "three-rules.yaml").write_text("rules: [{id: unique}]\n")
    data = tmp_path / "data"
    (data / "cron-plus").mkdir(parents=True)
    (data / "cron-plus/jobs.json").write_text(json.dumps({"jobs": [
        {"script": "plain.py"}, {"script": "/scripts/ext/lane.py"}]}))
    c.configure(tmp_path, data, tmp_path / "hermes")
    c.check_rules(); c.check_extensions()
    assert _messages(c).count("duplicate rule") == 2

    wiki = tmp_path / "wiki"; wiki.mkdir()
    for i in range(27): (wiki / f"{i}.md").write_text("x")
    monkeypatch.setattr(c.os, "geteuid", lambda: -1)
    c.check_ownership()
    assert "+ path(s)" in _messages(c)


def test_runtime_owned_file_hardening_messages_and_absent_write_artifacts(tmp_path, monkeypatch):
    c = _mod("deployment_checks_clean_branch_edges")
    data, hermes = tmp_path / "data", tmp_path / "hermes"
    (data / "cron-plus").mkdir(parents=True)
    (data / "cron-plus/jobs.json").write_text("{}")
    (data / "scripts").mkdir(); (hermes / "scripts/cron").mkdir(parents=True)
    c.configure(tmp_path, data, hermes)
    monkeypatch.setattr(c.os, "geteuid", lambda: os.stat(data / "cron-plus").st_uid)
    c.check_runtime_ownership()

    monkeypatch.setattr(c, "is_hardened", lambda _env: True)
    monkeypatch.setattr(c, "hardened_posture_violations", lambda _env: ["one", "two"])
    c.check_auth()
    assert _messages(c).count("FAIL hardening") == 2
    c.check_write_path_libs()


def test_provenance_list_scan_and_success_only_operations(tmp_path, monkeypatch):
    c = _mod("deployment_checks_final_loops")
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  gateway:\n    environment:\n      - OTHER=x\n      - BADENTRY\n      - OKENGINE_PACK=pack\n")
    c.configure(tmp_path)
    c.check_provenance_env()
    assert c.F == []

    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  gateway:\n    environment: [OTHER=x, BADENTRY]\n")
    c.check_provenance_env()
    assert "OKENGINE_PACK is not set" in _messages(c)
    c.reset()
    (tmp_path / "docker-compose.yml").write_text(
        "services: {gateway: {environment: scalar}}\n")
    c.check_provenance_env()
    assert "OKENGINE_PACK is not set" in _messages(c)
    c.reset()

    base = tmp_path / ".okengine/operations/runs/op"
    base.mkdir(parents=True)
    (base / "ok.json").write_text(json.dumps(
        {"operation": "op", "run_id": "ok", "status": "succeeded"}))
    c.check_operation_runs()
    assert c.F == []
