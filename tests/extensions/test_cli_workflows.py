"""Operator workflow coverage for framework extensions (#471)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location(
        "framework_extensions_workflows", REPO / "scripts/framework_extensions.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def ext(ext_id="demo", *, tier="pack", kind="declarative"):
    return {"id": ext_id, "tier": tier, "dir": "/ext",
            "manifest": {"id": ext_id, "kind": kind, "version": "1", "name": "Demo"}}


def discovery(*, extensions=None, errors=None, enabled=None, effective=None):
    extensions = [ext()] if extensions is None else extensions
    enabled = {} if enabled is None else enabled
    effective = set(enabled) if effective is None else effective
    return SimpleNamespace(
        discover=lambda _pack: (extensions, errors or []),
        load_enabled_state=lambda _pack: (enabled, []),
        effective_enabled=lambda _pack, _exts: (effective, []),
        resolve_enabled=lambda ids, _exts: (
            [e for e in extensions if e["id"] in set(ids)], []),
        is_core=lambda e: e["tier"] == "engine",
        set_enabled=lambda *_args: [],
    )


def test_list_json_human_empty_and_errors(tmp_path, monkeypatch, capsys):
    cli = load()
    monkeypatch.setattr(cli, "_discovery", lambda: discovery(
        extensions=[ext("core", tier="engine"), ext("opt")],
        enabled={"opt": {}}, effective={"core", "opt"}))
    assert cli.main(["list", str(tmp_path), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["extensions"]
    assert {r["state"] for r in rows} == {"enabled (core default)", "enabled (explicit)"}
    assert cli.main(["list", str(tmp_path)]) == 0
    assert "ID" in capsys.readouterr().out

    monkeypatch.setattr(cli, "_discovery", lambda: discovery(
        extensions=[], errors=["duplicate id"]))
    assert cli.main(["list", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "no extensions discovered" in captured.out and "duplicate id" in captured.err


def test_inspect_missing_valid_and_invalid_manifest(tmp_path, monkeypatch, capsys):
    cli = load()
    disc = discovery(enabled={"demo": {"config": {"mode": "safe"}}})
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    manifest = SimpleNamespace(validate_manifest=lambda _m: ([], ["old field"]))
    monkeypatch.setattr(cli, "_manifest", lambda: manifest)
    assert cli.main(["inspect", str(tmp_path), "demo"]) == 0
    captured = capsys.readouterr()
    assert 'config:  {"mode": "safe"}' in captured.out and "WARN: old field" in captured.err
    assert cli.main(["inspect", str(tmp_path), "missing"]) == 1
    assert "not discovered" in capsys.readouterr().err
    monkeypatch.setattr(cli, "_discovery", lambda: discovery(
        extensions=[], errors=["discovery failed"]))
    assert cli.main(["inspect", str(tmp_path), "missing"]) == 1
    assert "discovery failed" in capsys.readouterr().err

    monkeypatch.setattr(cli, "_manifest", lambda: SimpleNamespace(
        validate_manifest=lambda _m: (["bad"], [])))
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    assert cli.main(["inspect", str(tmp_path), "demo"]) == 1
    capsys.readouterr()

    monkeypatch.setattr(cli, "_manifest", lambda: SimpleNamespace(
        validate_manifest=lambda _m: ([], [])))
    for enabled in ({"demo": {}}, {"demo": True}):
        monkeypatch.setattr(cli, "_discovery", lambda enabled=enabled: discovery(enabled=enabled))
        assert cli.main(["inspect", str(tmp_path), "demo"]) == 0
        assert "config:" not in capsys.readouterr().out


def test_validate_strict_and_stage_panels(tmp_path, monkeypatch, capsys):
    cli = load()
    monkeypatch.setattr(cli, "_discovery", lambda: discovery(enabled={"demo": {}}))
    monkeypatch.setattr(cli, "_manifest", lambda: SimpleNamespace(
        validate_manifest=lambda _m: ([], ["deprecated"])))
    assert cli.main(["validate", str(tmp_path)]) == 0
    assert "WARN" in capsys.readouterr().out
    assert cli.main(["validate", str(tmp_path), "--strict-warnings"]) == 1
    monkeypatch.setattr(cli, "_manifest", lambda: SimpleNamespace(
        validate_manifest=lambda _m: ([], [])))
    assert cli.main(["validate", str(tmp_path)]) == 0
    assert "no conflicts" in capsys.readouterr().out
    failing = discovery(errors=["broken discovery"])
    monkeypatch.setattr(cli, "_discovery", lambda: failing)
    assert cli.main(["validate", str(tmp_path)]) == 1
    assert "FAIL: broken discovery" in capsys.readouterr().out

    monkeypatch.setattr(cli, "_discovery", lambda: discovery(enabled={"demo": {}}))
    monkeypatch.setattr(cli, "_load", lambda _name: SimpleNamespace(
        collect_reader_panels=lambda _resolved: ({"actor": {"panel": "x"}}, [])))
    assert cli.main(["stage-panels", str(tmp_path)]) == 0
    staged = json.loads((tmp_path / ".okengine/reader-panels.json").read_text())
    assert "actor" in staged


def test_stage_plan_sidecar_and_disable_paths(tmp_path, monkeypatch, capsys):
    cli = load()
    composer = SimpleNamespace(
        staging_targets=lambda _pack: ([{"id": "demo", "dir": "/ext"}], []),
        effective_records=lambda _pack: ([{"id": "demo"}], []),
        sidecar_compose_override=lambda _pack: ({"services": {}}, {}, []),
        write_composed_schema=lambda _pack: [],
    )
    monkeypatch.setattr(cli, "_composer", lambda: composer)
    assert cli.main(["stage-plan", str(tmp_path)]) == 0
    assert "demo\t/ext" in capsys.readouterr().out
    assert (tmp_path / ".okengine/extensions-effective.yaml").is_file()
    effective = tmp_path / ".okengine/extensions-effective.yaml"
    effective.unlink()
    effective.mkdir()
    assert cli.main(["stage-plan", str(tmp_path)]) == 0
    assert "could not record extensions-effective" in capsys.readouterr().err
    assert cli.main(["sidecar-generate", str(tmp_path)]) == 0
    assert "no enabled sidecar" in capsys.readouterr().out

    stale = tmp_path / ".okengine/generated/sidecars.compose.yml"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("services: {stale: {}}\n")
    assert cli.main(["sidecar-generate", str(tmp_path)]) == 0
    assert not stale.exists(), "a disabled sidecar must not remain attached to Compose"

    monkeypatch.setattr(cli, "_discovery", lambda: discovery(enabled={}))
    assert cli.main(["disable", str(tmp_path), "demo"]) == 0
    assert "not enabled" in capsys.readouterr().out

    disc = discovery(enabled={"demo": {}})
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    monkeypatch.setattr(cli, "_tokens", lambda: SimpleNamespace(revoke=lambda *_: None))
    assert cli.main(["disable", str(tmp_path), "demo"]) == 0
    assert "disabled: demo" in capsys.readouterr().out


def test_manifest_rejection_matrix(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "extension_manifest_workflows", REPO / "scripts/extension_manifest.py")
    manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest)
    bad = {
        "id": "demo",
        "kind": "operation",
        "version": "1.0.0",
        "trust": "in-gateway",
        "requires": "bad",
        "capabilities": {
            "write_policy": {
                "unknown": True,
                "operations": ["explode"],
                "paths": "bad",
                "body": "rewrite",
            }
        },
        "operations": {
            "": {},
            "broken": "not-a-block",
        },
        "reader_panels": [1],
    }
    errors, _ = manifest.validate_manifest(bad)
    joined = "\n".join(errors)
    for expected in (
        "requires: must be a mapping",
        "unknown keys",
        "paths must be a string list",
        "rule_id is required",
        "unknown values",
        "body must be",
        "operations key",
        "must be a block",
        "reader_panels[0] must be a mapping",
    ):
        assert expected in joined

    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    (ext_dir / "extension.yaml").write_text("id: demo\n")
    monkeypatch.setattr(manifest, "yaml", None)
    try:
        manifest.load_manifest(ext_dir)
    except manifest.ManifestError as exc:
        assert "PyYAML not available" in str(exc)
    else:
        raise AssertionError("missing PyYAML must fail closed")

    bad_op = {
        **bad,
        "requires": {"engine": 1},
        "capabilities": [],
        "operation": {
            "prompt": 1,
            "prompt_file": 2,
            "adversarial_fixtures": [],
            "after": "bad",
        },
        "operations": {},
    }
    errors, _ = manifest.validate_manifest(bad_op)
    joined = "\n".join(errors)
    for expected in ("prompt must", "prompt_file must", "adversarial_fixtures",
                     ".after must", "requires.engine must", "capabilities: must",
                     "'operations' map is empty"):
        assert expected in joined


def test_enable_missing_validation_failure_and_already_enabled(tmp_path, monkeypatch, capsys):
    cli = load()
    monkeypatch.setattr(cli, "_discovery", lambda: discovery(extensions=[]))
    assert cli.main(["enable", str(tmp_path), "missing"]) == 1
    assert "not discovered" in capsys.readouterr().err
    monkeypatch.setattr(cli, "_discovery", lambda: discovery(
        extensions=[], errors=["ambiguous tree"]))
    assert cli.main(["enable", str(tmp_path), "missing"]) == 1
    assert "FAIL: ambiguous tree" in capsys.readouterr().err

    target = ext("demo", tier="operator", kind="operation")
    target["manifest"]["trust"] = "in-gateway"
    disc = discovery(extensions=[target], enabled={})
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    monkeypatch.setattr(cli, "_manifest", lambda: SimpleNamespace(
        validate_manifest=lambda _m: ([], ["manifest warning"])))
    composer = SimpleNamespace(
        compose=lambda _resolved: ({}, [], []),
        compose_check=lambda *_: [],
        write_composed_schema=lambda *_: [],
    )
    monkeypatch.setattr(cli, "_composer", lambda: composer)
    assert cli.main(["enable", str(tmp_path), "demo"]) == 1
    assert "allow-untrusted" in capsys.readouterr().err

    disc = discovery(extensions=[target], enabled={"demo": {}})
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    token = SimpleNamespace(
        scopes_from_manifest=lambda _m: (["read"], ["write"]),
        write_capability_from_manifest=lambda _m: {},
        reconcile=lambda *_: {"rotated": False, "changed": True},
    )
    monkeypatch.setattr(cli, "_tokens", lambda: token)
    assert cli.main(["enable", str(tmp_path), "demo", "--allow-untrusted"]) == 0
    enabled_out = capsys.readouterr().out
    assert "token scopes reconciled" in enabled_out and "manifest warning" in enabled_out

    monkeypatch.setattr(cli, "_materialize_policy", lambda _pack: ["policy race"])
    assert cli.main(["enable", str(tmp_path), "demo", "--allow-untrusted"]) == 1
    assert "policy regen: policy race" in capsys.readouterr().err


def test_materialize_policy_reports_loader_failure(tmp_path, monkeypatch):
    cli = load()
    monkeypatch.setattr(cli, "_policy", lambda: (_ for _ in ()).throw(RuntimeError("broken")))
    assert cli._materialize_policy(tmp_path) == ["broken"]


def test_stage_and_sidecar_errors_then_generation(tmp_path, monkeypatch, capsys):
    cli = load()
    monkeypatch.setattr(cli, "_composer", lambda: SimpleNamespace(
        staging_targets=lambda _pack: ([], ["broken"]),
    ))
    assert cli.main(["stage-plan", str(tmp_path)]) == 1
    assert "FAIL: broken" in capsys.readouterr().err

    monkeypatch.setattr(cli, "_composer", lambda: SimpleNamespace(
        sidecar_compose_override=lambda _pack: ({}, {}, ["sidecar bad"])))
    assert cli.main(["sidecar-generate", str(tmp_path)]) == 1
    assert "sidecar bad" in capsys.readouterr().err

    override = {"services": {"demo": {"image": "x"}}}
    wrappers = {"demo": "#!/bin/sh\n"}
    stale = tmp_path / ".okengine/generated/old/trigger.sh"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    monkeypatch.setattr(cli, "_composer", lambda: SimpleNamespace(
        TRIGGER_NAME="trigger.sh",
        sidecar_compose_override=lambda _pack: (override, wrappers, []),
    ))
    assert cli.main(["sidecar-generate", str(tmp_path)]) == 0
    compose = tmp_path / ".okengine/generated/sidecars.compose.yml"
    wrapper = tmp_path / ".okengine/generated/demo/trigger.sh"
    assert compose.is_file() and wrapper.is_file()
    assert not stale.exists()
    assert compose.stat().st_mode & 0o777 == 0o600
    assert wrapper.stat().st_mode & 0o777 == 0o755

    stubborn = tmp_path / ".okengine/generated/stubborn/trigger.sh"
    stubborn.parent.mkdir()
    stubborn.write_text("old")
    (stubborn.parent / "keep").write_text("occupied")
    assert cli.main(["sidecar-generate", str(tmp_path)]) == 0
    assert not stubborn.exists() and stubborn.parent.is_dir()


def test_stage_panels_discovery_and_composition_failures(tmp_path, monkeypatch, capsys):
    cli = load()
    monkeypatch.setattr(cli, "_discovery", lambda: discovery(errors=["duplicate"]))
    assert cli.main(["stage-panels", str(tmp_path)]) == 1
    assert "duplicate" in capsys.readouterr().err

    monkeypatch.setattr(cli, "_discovery", lambda: discovery())
    monkeypatch.setattr(cli, "_load", lambda _name: SimpleNamespace(
        collect_reader_panels=lambda _resolved: ({}, ["panel collision"])))
    assert cli.main(["stage-panels", str(tmp_path)]) == 1
    assert "panel collision" in capsys.readouterr().err


def test_seed_about_skips_bad_schema_and_preserves_existing(tmp_path):
    cli = load()
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    (ext_dir / "about.md").write_text("# Demo\n")
    (ext_dir / "bad.yaml").write_text("{bad")
    (ext_dir / "good.yaml").write_text("owns:\n  namespaces: [findings]\n")
    target = {
        "dir": str(ext_dir),
        "manifest": {"schema": ["bad.yaml", "good.yaml"]},
    }
    existing = tmp_path / "wiki/findings/_about.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("operator copy\n")
    cli._seed_about(tmp_path, target)
    assert existing.read_text() == "operator copy\n"


def test_enable_dependency_state_write_and_regen_paths(tmp_path, monkeypatch, capsys):
    cli = load()
    monkeypatch.setattr(cli, "_materialize_policy", lambda _pack: [])
    target = ext()
    target["manifest"]["requires"] = {"extensions": ["dep"]}
    extensions = [target, ext("dep")]
    monkeypatch.setattr(cli, "_manifest", lambda: SimpleNamespace(
        validate_manifest=lambda _m: ([], [])))
    composer = SimpleNamespace(
        compose=lambda _resolved: ({}, [], []),
        compose_check=lambda *_: [],
        write_composed_schema=lambda *_: ["regen warning"],
    )
    monkeypatch.setattr(cli, "_composer", lambda: composer)
    monkeypatch.setattr(cli, "_discovery", lambda: discovery(
        extensions=extensions, enabled={}))
    assert cli.main(["enable", str(tmp_path), "demo"]) == 1
    assert "which is not enabled" in capsys.readouterr().err

    disc = discovery(extensions=extensions, enabled={"dep": {}})
    disc.set_enabled = lambda *_: ["cannot write state"]
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    assert cli.main(["enable", str(tmp_path), "demo"]) == 1
    assert "cannot write state" in capsys.readouterr().err

    disc.set_enabled = lambda *_: []
    monkeypatch.setattr(cli, "_tokens", lambda: SimpleNamespace(
        scopes_from_manifest=lambda _m: ([], []),
        write_capability_from_manifest=lambda _m: {},
        reconcile=lambda *_: {"rotated": False, "changed": False},
    ))
    assert cli.main(["enable", str(tmp_path), "demo"]) == 0
    captured = capsys.readouterr()
    assert "enabled: demo" in captured.out and "regen warning" in captured.err


def test_disable_state_and_schema_failures(tmp_path, monkeypatch, capsys):
    cli = load()
    monkeypatch.setattr(cli, "_materialize_policy", lambda _pack: [])
    monkeypatch.setattr(cli, "_discovery", lambda: SimpleNamespace(
        load_enabled_state=lambda _p: ({}, ["enabled state bad"])))
    assert cli.main(["disable", str(tmp_path), "demo"]) == 1
    assert "enabled state bad" in capsys.readouterr().err

    disc = discovery(enabled={"demo": {}})
    disc.set_enabled = lambda *_: ["state failure"]
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    assert cli.main(["disable", str(tmp_path), "demo"]) == 1
    assert "state failure" in capsys.readouterr().err

    disc.set_enabled = lambda *_: []
    monkeypatch.setattr(cli, "_tokens", lambda: SimpleNamespace(revoke=lambda *_: None))
    monkeypatch.setattr(cli, "_composer", lambda: SimpleNamespace(
        write_composed_schema=lambda *_: ["unresolvable extension"]))
    assert cli.main(["disable", str(tmp_path), "demo"]) == 1
    assert "NOT regenerated" in capsys.readouterr().err


def test_extension_toggle_fails_loud_when_policy_regeneration_fails(
        tmp_path, monkeypatch, capsys):
    cli = load()
    target = ext()
    monkeypatch.setattr(cli, "_manifest", lambda: SimpleNamespace(
        validate_manifest=lambda _m: ([], [])))
    disc = discovery(extensions=[target], enabled={})
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    monkeypatch.setattr(cli, "_composer", lambda: SimpleNamespace(
        compose=lambda _resolved: ({}, [], []),
        compose_check=lambda *_: [],
        write_composed_schema=lambda *_: [],
    ))
    monkeypatch.setattr(cli, "_tokens", lambda: SimpleNamespace(
        scopes_from_manifest=lambda _m: ([], []),
        write_capability_from_manifest=lambda _m: {},
        reconcile=lambda *_: {"rotated": False, "changed": False},
    ))
    monkeypatch.setattr(cli, "_materialize_policy", lambda _pack: ["bad policy"])

    assert cli.main(["enable", str(tmp_path), "demo"]) == 1
    assert "effective-policy.json was NOT regenerated" in capsys.readouterr().err

    disc = discovery(extensions=[target], enabled={"demo": {}})
    disc.set_enabled = lambda *_: []
    monkeypatch.setattr(cli, "_discovery", lambda: disc)
    monkeypatch.setattr(cli, "_tokens", lambda: SimpleNamespace(revoke=lambda *_: None))
    assert cli.main(["disable", str(tmp_path), "demo"]) == 1
    assert "effective-policy.json was NOT regenerated" in capsys.readouterr().err


def test_purge_guard_dry_run_and_delete_warning(tmp_path, monkeypatch, capsys):
    cli = load()
    monkeypatch.setattr(cli, "_discovery", lambda: SimpleNamespace(
        load_enabled_state=lambda _p: ({}, ["bad state"])))
    assert cli.main(["purge", str(tmp_path), "demo"]) == 1

    monkeypatch.setattr(cli, "_discovery", lambda: SimpleNamespace(
        load_enabled_state=lambda _p: ({"demo": {}}, [])))
    assert cli.main(["purge", str(tmp_path), "demo"]) == 1
    assert "still enabled" in capsys.readouterr().err

    monkeypatch.setattr(cli, "_discovery", lambda: SimpleNamespace(
        load_enabled_state=lambda _p: ({}, [])))
    monkeypatch.setattr(cli, "_composer", lambda: SimpleNamespace(
        purge_targets=lambda *_: [Path("generated/a.md")]))
    assert cli.main(["purge", str(tmp_path), "demo"]) == 0
    assert "would purge" in capsys.readouterr().out
    assert cli.main(["purge", str(tmp_path), "demo", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "purged 0" in captured.out and "WARN" in captured.err
    monkeypatch.setattr(cli, "_composer", lambda: SimpleNamespace(
        purge_targets=lambda *_: []))
    assert cli.main(["purge", str(tmp_path), "demo"]) == 0
    assert "nothing to purge" in capsys.readouterr().out


def test_discovery_enabled_state_fail_closed_matrix(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "extension_discovery_workflows", REPO / "scripts/extension_discovery.py")
    disc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(disc)
    fake_yaml = SimpleNamespace(
        safe_load=lambda text: {"enabled": []},
        safe_dump=lambda doc, **_k: "enabled: {}\n")
    em = SimpleNamespace(yaml=fake_yaml)
    monkeypatch.setattr(disc, "_manifest_mod", lambda: em)
    state = tmp_path / ".okengine/extensions.yaml"
    state.parent.mkdir()
    state.write_text("enabled: []\n")
    enabled, errors = disc.load_enabled_state(tmp_path)
    assert enabled == {} and "'enabled' must be a mapping" in errors[0]

    fake_yaml.safe_load = lambda _text: (_ for _ in ()).throw(ValueError("bad yaml"))
    assert "unparseable enabled-state" in disc.load_enabled_state(tmp_path)[1][0]
    assert disc._load_disabled(tmp_path) == set()

    em.yaml = None
    assert "PyYAML not available" in disc.load_enabled_state(tmp_path)[1][0]
    assert "PyYAML not available" in disc.set_enabled(tmp_path, "demo", True)[0]

    em.yaml = fake_yaml
    fake_yaml.safe_load = lambda _text: [1]
    assert "must be a YAML mapping" in disc.load_enabled_state(tmp_path)[1][0]
    monkeypatch.setattr(disc, "load_enabled_state", lambda _p: ({}, ["read failed"]))
    assert disc.set_enabled(tmp_path, "demo", True) == ["read failed"]


def test_discovery_config_override_rejections(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "extension_discovery_config", REPO / "scripts/extension_discovery.py")
    disc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(disc)
    record = ext()
    cases = [
        ("bad", "config override must be a mapping"),
        ({"x": 1}, "unknown config override"),
    ]
    for overrides, expected in cases:
        monkeypatch.setattr(disc, "discover", lambda _p: ([record], []))
        monkeypatch.setattr(disc, "load_enabled_state",
                            lambda _p, value=overrides: ({"demo": {"config": value}}, []))
        monkeypatch.setattr(disc, "effective_enabled", lambda *_: ({"demo"}, []))
        monkeypatch.setattr(disc, "resolve_enabled", lambda *_: ({"demo": record}, []))
        _, errors = disc.resolve_for_pack(Path("/pack"))
        assert expected in errors[0]

    record_bad_decl = ext()
    record_bad_decl["manifest"]["config"] = [1]
    monkeypatch.setattr(disc, "discover", lambda _p: ([record_bad_decl], []))
    monkeypatch.setattr(disc, "load_enabled_state",
                        lambda _p: ({"demo": {"config": {"x": 1}}}, []))
    monkeypatch.setattr(disc, "effective_enabled", lambda *_: ({"demo"}, []))
    monkeypatch.setattr(disc, "resolve_enabled",
                        lambda *_: ({"demo": record_bad_decl}, []))
    assert "manifest config must be a mapping" in disc.resolve_for_pack(Path("/pack"))[1][0]


def test_compose_helper_rejection_and_override_matrix(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "extension_compose_workflows", REPO / "scripts/extension_compose.py")
    comp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comp)
    for ext_id, key in [("", "x"), ("okengine.demo", "")]:
        try:
            comp.extension_config_env_name(ext_id, key)
        except ValueError:
            pass
        else:
            raise AssertionError("empty env token must fail")
    assert comp._env_value(True) == "true" and comp._env_value(None) == ""
    assert comp._in_gateway_config_env("demo", {"config": [1]})[1]
    _, errors = comp._in_gateway_config_env(
        "demo", {"config": {"a-b": 1, "a_b": 2}})
    assert "both map" in errors[0]

    assert comp._iter_ops({"operations": {}})[1]
    assert comp._iter_ops({"operations": {"x": "bad"}})[1]
    assert comp._iter_ops({})[1]
    assert comp._resolve_prompt({"prompt_file": "x"}, None)[1]
    assert comp._resolve_prompt({"prompt_file": "x"}, tmp_path)[1]

    okd = tmp_path / ".okengine"
    okd.mkdir()
    jobs = [{"name": "demo", "schedule": {"kind": "cron", "expr": "0 0 * * *"}}]
    matrices = [
        ("extension-models.json", comp._apply_model_overrides,
         {"missing": "m", "demo": ""}, ("no extension job", "non-empty string")),
        ("extension-schedules.json", comp._apply_schedule_overrides,
         {"missing": "0 0 * * *", "demo": "bad"}, ("no extension job", "5-field")),
        ("extension-prompts.json", comp._apply_prompt_overrides,
         {"missing": "x", "demo": ""}, ("no extension job", "non-empty string")),
    ]
    for filename, function, payload, expected in matrices:
        (okd / filename).write_text(json.dumps(payload))
        errors = function(jobs, tmp_path)
        assert all(any(text in error for error in errors) for text in expected)
        (okd / filename).write_text("[]")
        assert "must be a" in function(jobs, tmp_path)[0]
        (okd / filename).write_text("{bad")
        assert filename in function(jobs, tmp_path)[0]

    failures = [
        ("sidecar", {}, "requires operation.entrypoint.image"),
        ("sidecar", {"entrypoint": {"image": {}}, "schedule": {}}, "operation.schedule"),
        ("in-gateway", {"entrypoint": 1, "schedule": {"kind": "cron", "expr": "x"}},
         "entrypoint must be"),
        ("in-gateway", {"entrypoint": "", "schedule": {"kind": "cron", "expr": "x"}},
         "script is empty"),
        ("in-gateway", {"schedule": {"kind": "cron", "expr": "x"}}, "no_agent"),
    ]
    for trust, operation, expected in failures:
        job, errors, _ = comp._synthesize_one("demo", {}, trust, None, operation)
        assert job is None and expected in errors[0]

    job, errors, _ = comp._synthesize_one(
        "demo", {}, "in-gateway", None,
        {"entrypoint": "run.py", "no_agent": True,
         "schedule": {"kind": "cron", "expr": "0 7 * * 1"}},
    )
    assert job is None and "herd-prone" in errors[0]

    cron_dir = tmp_path / "dropins/crons"
    cron_dir.mkdir(parents=True)
    bad_file = cron_dir / "bad.cron.json"
    bad_file.write_text("{bad")
    assert "invalid JSON" in comp._ops_from_dropins(cron_dir.parent)[1]
    bad_file.write_text("[]")
    assert "must be a JSON object" in comp._ops_from_dropins(cron_dir.parent)[1]
    bad_file.write_text("{}")
    _, error = comp._collect_ops(
        {"operations": {"bad": {"schedule": {}}}}, cron_dir.parent)
    assert "duplicate operation" in error

    agent = {
        "prompt": "work",
        "schedule": {"kind": "cron", "expr": "1 0 * * *"},
    }
    assert "output_contract" in comp._synthesize_one(
        "demo", {}, "in-gateway", None, agent)[1][0]


def test_compose_resolution_sidecar_schema_and_secret_edges(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "extension_compose_edges", REPO / "scripts/extension_compose.py")
    comp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comp)
    record = ext()
    record["manifest"].update({"kind": "operation", "trust": "in-gateway",
                               "description": " multi\n line ", "operation": {
        "entrypoint": "job.py", "schedule": {"kind": "cron", "expr": "@jitter:daily"}}})
    disc = SimpleNamespace(resolve_for_pack=lambda _p: ({"demo": record}, []))
    monkeypatch.setattr(comp, "_discovery_mod", lambda: disc)
    assert comp.effective_ids(tmp_path)[0] == ["demo"]
    assert comp.effective_records(tmp_path)[0][0]["description"] == "multi line"
    assert comp.staging_targets(tmp_path)[0] == [{"id": "demo", "dir": "/ext"}]

    jobs, errors, _ = comp.compose({"demo": record}, {"demo"})
    assert jobs and "collides with" in errors[0]
    assert comp.image_ref({"registry": "r", "tag": "t", "digest": "sha256:x"}) == \
        "r:t@sha256:x"

    sidecar = ext("side", kind="operation")
    sidecar["manifest"].update({"trust": "sidecar", "operation": {
        "entrypoint": {"image": {}}, "schedule": {"kind": "cron", "expr": "@jitter:daily"}}})
    disc.resolve_for_pack = lambda _p: ({"side": sidecar}, [])
    assert "digest-pinned" in comp.sidecar_specs(tmp_path)[1][0]

    fragments = tmp_path / "fragments"
    fragments.mkdir()
    rec = ext()
    rec["dir"] = str(fragments)
    rec["manifest"]["schema"] = ["missing.yaml", "bad.yaml", "list.yaml"]
    (fragments / "bad.yaml").write_text("{bad")
    (fragments / "list.yaml").write_text("- item\n")
    _, errors = comp._fragments_from_resolved({"demo": rec})
    assert any("not found" in e for e in errors)
    assert any("unparseable" in e for e in errors)
    assert any("not a mapping" in e for e in errors)

    assert comp.purge_targets(tmp_path, "demo") == []
    secrets = tmp_path / ".okengine/extension-secrets.json"
    secrets.parent.mkdir()
    secrets.write_text("[]")
    assert comp._read_secrets(tmp_path) == {}
    secrets.write_text("{bad")
    assert comp._read_secrets(tmp_path) == {}


def test_extension_compose_remaining_contract_edges(tmp_path, monkeypatch):
    comp = importlib.util.module_from_spec(spec := importlib.util.spec_from_file_location(
        "extension_compose_final_edges", REPO / "scripts/extension_compose.py"))
    spec.loader.exec_module(comp)

    env, errors = comp._in_gateway_config_env("demo", {"config": {"---": "x"}})
    assert env == {} and errors
    assert "non-empty string" in comp._iter_ops({"operations": {1: {}}})[1]
    assert comp._ops_from_dropins(None) == ([], None)
    cron = tmp_path / "dropins" / "crons"
    cron.mkdir(parents=True)
    (cron / ".cron.json").write_text("{}")
    assert "invalid cron drop-in filename" in comp._ops_from_dropins(cron.parent)[1]

    monkeypatch.setattr(
        comp, "_output_contract_mod",
        lambda: SimpleNamespace(validate=lambda *_a: ["contract invalid"]),
    )
    agent = {
        "prompt": "act", "schedule": {"kind": "cron", "expr": "1 * * * *"},
        "output_contract": {}, "adversarial_fixtures": ["fixture"],
    }
    assert "contract invalid" in comp._synthesize_one(
        "demo", {}, "in-gateway", None, agent)[1]
    monkeypatch.setattr(
        comp, "_output_contract_mod", lambda: SimpleNamespace(validate=lambda *_a: []),
    )
    agent["adversarial_fixtures"] = []
    assert "adversarial_fixtures" in comp._synthesize_one(
        "demo", {}, "in-gateway", None, agent)[1][0]

    deterministic = {
        "entrypoint": "run.py", "no_agent": True,
        "schedule": {"kind": "cron", "expr": "1 * * * *"}, "after": [],
    }
    job, errors, _ = comp._synthesize_one(
        "demo", {}, "in-gateway", None, deterministic, tmp_path)
    assert job is not None and "after" not in job and not errors

    nonop = ext()
    disc = SimpleNamespace(resolve_for_pack=lambda _p: ({"demo": nonop}, []))
    monkeypatch.setattr(comp, "_discovery_mod", lambda: disc)
    assert comp.staging_targets(tmp_path)[0] == []

    side = ext("side", kind="operation")
    side["manifest"].update({
        "trust": "sidecar", "operation": {}, "operations": {"two": {}},
    })
    disc.resolve_for_pack = lambda _p: ({"side": side}, [])
    assert "exactly one" in comp.sidecar_specs(tmp_path)[1][0]
    assert comp.image_ref({"registry": "registry"}) == "registry"
    svc = comp.render_sidecar_service({
        "id": "side", "image": "image@sha256:x", "command": ["run"], "limits": {},
    }, "http://read", "http://write", "read-token", "write-token")
    assert svc["command"] == ["run"]

    panels, panel_errors = comp.collect_reader_panels({
        "x": {"manifest": {"reader_panels": [None, {}, {"type": "page"}]}},
    })
    assert list(panels) == ["page"] and panel_errors == []

    monkeypatch.setattr(comp, "_compose", lambda _p: ({"types": {}}, [], ["bad"]))
    assert comp.composed_schema(tmp_path) == ({"types": {}}, ["bad"])

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "page.md"
    page.write_text("---\nextension_id: demo\n---\n")
    original = comp.Path.read_text
    monkeypatch.setattr(
        comp.Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == page else original(self, *a, **k),
    )
    assert comp.purge_targets(tmp_path, "demo") == []

    jobs = [{"name": "deterministic", "no_agent": True}]
    overrides = tmp_path / ".okengine" / "extension-prompts.json"
    overrides.parent.mkdir(exist_ok=True)
    overrides.write_text(json.dumps({"deterministic": "use model"}))
    errors = comp._apply_prompt_overrides(jobs, tmp_path)
    assert "uncontracted deterministic operation" in errors[0]
