"""Operator-facing lifecycle coverage for framework init/import/reconcile (#470)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]


def load(name: str):
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_init_version_fallbacks_and_refuses_guesses(monkeypatch):
    init = load("framework_init")
    monkeypatch.setattr(init, "_engine_meta", lambda: SimpleNamespace(
        engine_release=lambda: "", hermes_pin=lambda: ""))
    monkeypatch.setattr(init, "_manifest_scalar", lambda key: {
        "engine_release": "v9.1.0", "pinned_tag": "v8.2.0"}[key])
    assert init.engine_version() == "v9.1.0"
    assert init.hermes_pin() == "v8.2.0"

    monkeypatch.setattr(init, "_manifest_scalar", lambda _key: None)
    monkeypatch.setattr(init.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="v7.0.0\n"))
    assert init.engine_version() == "v7.0.0"
    monkeypatch.setattr(init.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=""))
    with pytest.raises(SystemExit, match="cannot determine engine_release"):
        init.engine_version()
    monkeypatch.setattr(init.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(SystemExit, match="cannot determine engine_release"):
        init.engine_version()
    with pytest.raises(SystemExit, match="cannot read the Hermes pin"):
        init.hermes_pin()


def test_init_manifest_reader_meta_success_and_ask(tmp_path, monkeypatch):
    init = load("framework_init")
    monkeypatch.setattr(init, "ENGINE_ROOT", tmp_path)
    (tmp_path / "engine-manifest.yaml").write_text("engine_release: v4\npinned_tag: h4\n")
    assert init._manifest_scalar("engine_release") == "v4"
    assert init._manifest_scalar("missing") is None
    monkeypatch.setattr(init, "_engine_meta", lambda: SimpleNamespace(
        engine_release=lambda: "v5", hermes_pin=lambda: "h5"))
    assert init.engine_version() == "v5" and init.hermes_pin() == "h5"
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert init._ask("value", "default") == "default"
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError()))
    assert init._ask("value") == ""
    monkeypatch.setattr(init, "ENGINE_ROOT", tmp_path / "absent")
    assert init._manifest_scalar("x") is None
    monkeypatch.setattr(init, "_engine_meta", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(init, "_manifest_scalar", lambda key: "v6" if key == "engine_release" else "h6")
    assert init.engine_version() == "v6" and init.hermes_pin() == "h6"


def test_init_render_runtime_and_main_rejections(tmp_path, monkeypatch, capsys):
    init = load("framework_init")
    skeleton = tmp_path / "skeleton"
    skeleton.mkdir()
    (skeleton / "feeds").mkdir()
    (skeleton / "{{PACK_UNDERSCORE}}_job.py").write_text("name={{PACK}}\n")
    (skeleton / "docker-compose.yml").write_text("services: {}\n")
    monkeypatch.setattr(init, "SKELETON", skeleton)
    monkeypatch.setattr(init, "engine_version", lambda: "v1")
    monkeypatch.setattr(init, "hermes_pin", lambda: "h1")
    monkeypatch.setattr(init, "_jitter_crons", lambda _dest: None)
    monkeypatch.setattr(init, "_layer_runtime", lambda dest, offset: (
        (dest / ".hermes-data").mkdir(parents=True, exist_ok=True)))

    dest = tmp_path / "okpack-demo"
    assert init.main([str(dest), "--domain", "Demo", "--no-compose"]) == 0
    assert (dest / "okpack_demo_job.py").read_text() == "name=okpack-demo\n"
    assert not (dest / "docker-compose.yml").exists()
    assert "scaffolded domain pack" in capsys.readouterr().out

    assert init.main([str(dest)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    monkeypatch.setattr(init, "SKELETON", tmp_path / "missing")
    assert init.main([str(tmp_path / "other")]) == 1
    assert "template not found" in capsys.readouterr().err


def test_init_feeds_ports_and_binary_skeleton(tmp_path, monkeypatch, capsys):
    init = load("framework_init")
    skeleton = tmp_path / "skeleton"
    skeleton.mkdir()
    (skeleton / "feeds").mkdir()
    (skeleton / "binary.bin").write_bytes(b"\xff\xfe")
    (skeleton / "docker-compose.yml").write_text("services: {}\n")
    feeds = tmp_path / "feeds.opml"
    feeds.write_text("<opml/>")
    monkeypatch.setattr(init, "SKELETON", skeleton)
    monkeypatch.setattr(init, "_tokens", lambda dest, domain, offset: {
        "PACK_UNDERSCORE": "demo", "ENGINE_VERSION": "v1", "TITLE": "Demo",
        "READER_PORT": "9210", "COCKPIT_PORT": "9211", "MCP_PORT": "8740"})
    monkeypatch.setattr(init, "_jitter_crons", lambda _d: None)
    monkeypatch.setattr(init, "_layer_runtime", lambda *_: None)
    dest = tmp_path / "demo"
    assert init.main([str(dest), "--feeds", str(feeds), "--port-offset", "10"]) == 0
    assert (dest / "feeds/feeds.opml").read_text() == "<opml/>"
    assert "ports: reader 9210" in capsys.readouterr().out


def test_init_warns_when_rendered_host_port_is_already_bound(tmp_path, monkeypatch, capsys):
    """Init provides the same early collision warning as framework pull."""
    import socket

    init = load("framework_init")
    skeleton = tmp_path / "skeleton"
    skeleton.mkdir()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    busy_port = listener.getsockname()[1]
    (skeleton / "docker-compose.yml").write_text(
        f'services:\n  reader:\n    ports:\n      - "127.0.0.1:{busy_port}:9200"\n'
    )
    monkeypatch.setattr(init, "SKELETON", skeleton)
    monkeypatch.setattr(init, "_jitter_crons", lambda _d: None)
    monkeypatch.setattr(init, "_layer_runtime", lambda *_: None)
    try:
        assert init.main([str(tmp_path / "pack"), "--domain", "Collision test"]) == 0
    finally:
        listener.close()

    output = capsys.readouterr().out
    assert "already in use" in output
    assert str(busy_port) in output


def test_init_busy_port_check_ignores_missing_compose(tmp_path, capsys):
    init = load("framework_init")
    init._warn_busy_host_ports(tmp_path)
    assert capsys.readouterr().out == ""


def test_init_interactive_and_runtime_port_rewrite(tmp_path, monkeypatch):
    init = load("framework_init")
    answers = iter(["", "Demo", "local", "", "bad"])
    monkeypatch.setattr(init, "_ask", lambda *a, **k: next(answers))
    assert init.main(["--interactive"]) == 1

    monkeypatch.setattr(init, "ENGINE_ROOT", tmp_path)
    template = tmp_path / "config" / "config.yaml.template"
    template.parent.mkdir()
    template.write_text("url: http://localhost:8730/mcp\n")
    dest = tmp_path / "pack"
    init._layer_runtime(dest, 20)
    assert "localhost:8750" in (dest / ".hermes-data" / "config.yaml").read_text()
    monkeypatch.setattr(init, "ENGINE_ROOT", tmp_path / "without-template")
    no_template = tmp_path / "no-template-pack"
    init._layer_runtime(no_template)
    assert not (no_template / ".hermes-data/config.yaml").exists()

    with pytest.raises(SystemExit) as exc:
        init.main([])
    assert exc.value.code == 2


def test_init_interactive_success_render_errors_and_jitter(tmp_path, monkeypatch, capsys):
    init = load("framework_init")
    original_jitter = init._jitter_crons
    skeleton = tmp_path / "skeleton"
    skeleton.mkdir()
    (skeleton / "file.txt").write_text("{{UNKNOWN_TOKEN}}\n")
    with pytest.raises(SystemExit, match="unsubstituted tokens"):
        init._render(tmp_path / "bad", {"PACK_UNDERSCORE": "x"})

    dest = tmp_path / "okpack-interactive"
    monkeypatch.setattr(init, "SKELETON", skeleton)
    monkeypatch.setattr(init, "_tokens", lambda *_: {
        "PACK_UNDERSCORE": "interactive", "ENGINE_VERSION": "v1", "TITLE": "Interactive"})
    monkeypatch.setattr(init, "_render", lambda d, _t: d.mkdir())
    monkeypatch.setattr(init, "_jitter_crons", lambda _d: None)
    monkeypatch.setattr(init, "_layer_runtime", lambda *_: None)
    answers = iter([str(dest), "Interactive", "local", "", "not-a-number"])
    monkeypatch.setattr(init, "_ask", lambda *a, **k: next(answers))
    assert init.main(["--interactive", "--no-compose"]) == 0

    cron_file = tmp_path / "cron.json"
    cron_file.write_text("[]")
    fake = SimpleNamespace(expand_file=lambda path: 2)
    monkeypatch.setattr(init.importlib.util if hasattr(init, "importlib") else importlib.util,
                        "module_from_spec", lambda _spec: fake)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *_: SimpleNamespace(
        loader=SimpleNamespace(exec_module=lambda _module: None)))
    original_jitter(tmp_path)
    assert "jittered 2" in capsys.readouterr().out
    fake.expand_file = lambda _path: 0
    original_jitter(tmp_path)
    assert "jittered" not in capsys.readouterr().out


def test_import_missing_vault_report_and_scaffold(tmp_path, monkeypatch, capsys):
    imp = load("framework_import")
    pack = tmp_path / "pack"
    pack.mkdir()
    assert imp.main([str(pack), "--vault", str(tmp_path / "missing")]) == 2
    assert "no wiki" in capsys.readouterr().err

    vault = tmp_path / "foreign" / "wiki"
    vault.mkdir(parents=True)
    (vault / "mystery.md").write_text("---\ntype: alien\n---\n")
    (pack / "schema.yaml").write_text(yaml.safe_dump({"types": {"actor": {}}}))
    monkeypatch.setattr(imp.import_lib, "layout_misplaced", lambda *_: {"alien -> entities": 1})
    monkeypatch.setattr(imp.import_lib, "id_collisions", lambda _vault: {
        "stamped": 1, "slug_collisions": ["x"],
        "authority_collisions": [("id:a", "one.md", "two.md")]})
    monkeypatch.setitem(sys.modules, "engine_meta", SimpleNamespace(engine_release=lambda: "v9"))
    assert imp.main([str(pack), "--vault", str(vault.parent), "--scaffold"]) == 0
    out = capsys.readouterr().out
    assert "NOT IN PACK" in out and "WRONG namespace" in out and "DUP id id:a" in out
    migration = pack / ".okengine/migrations/m_900_import_foreign_vault.py"
    assert 'TO = "v9"' in migration.read_text()
    assert imp._scaffold(pack) == 0
    assert "left untouched" in capsys.readouterr().out


def test_import_report_survives_id_collision_diagnostic_failure(tmp_path, monkeypatch, capsys):
    imp = load("framework_import")
    pack = tmp_path / "pack"
    pack.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "page.md").write_text("---\ntype: actor\n---\n")
    monkeypatch.setattr(imp.import_lib, "id_collisions", lambda _v: (
        _ for _ in ()).throw(RuntimeError("bad ids")))
    assert imp._report(pack, vault) == 0
    out = capsys.readouterr().out
    assert "could not compute" in out and "bad ids" in out


def test_import_report_without_pack_schema_explains_authority_limits(tmp_path, monkeypatch, capsys):
    imp = load("framework_import")
    pack = tmp_path / "pack"
    pack.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "page.md").write_text("---\ntype: actor\n---\n")
    monkeypatch.setattr(imp.import_lib, "id_collisions", lambda _vault: {
        "stamped": 0, "slug_collisions": [], "authority_collisions": []})
    assert imp._report(pack, vault) == 0
    assert "id authority bindings come from the pack schema" in capsys.readouterr().out


def _pair(pack: Path, name: str):
    local = pack / name
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("local\n")
    local.with_name(local.name + ".upstream").write_text("upstream\n")


def test_reconcile_validation_merge_failures_and_interactive(tmp_path, monkeypatch, capsys):
    rec = load("framework_reconcile")
    assert rec.pending(tmp_path / "missing") == []
    assert rec.main([str(tmp_path / "missing")]) == 2

    _pair(tmp_path, "schema.yaml")
    merged, error = rec.merge(tmp_path, "schema.yaml", "")
    assert merged is None and "merge needs" in error
    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert "could not run" in rec.merge(tmp_path, "schema.yaml", "bad")[1]
    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=7))
    assert "exited 7" in rec.merge(tmp_path, "schema.yaml", "bad")[1]

    actions = iter(["x", "s", "s"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(actions))
    assert rec.main([str(tmp_path), "--interactive", "--no-validate"]) == 0
    assert "choose a, k, m, s, or q" in capsys.readouterr().out

    actions = iter(["k", "k"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(actions))
    monkeypatch.setattr(rec, "_validate", lambda _pack: 9)
    assert rec.main([str(tmp_path), "--interactive"]) == 9


def test_reconcile_interactive_accept_merge_and_quit(tmp_path, monkeypatch):
    rec = load("framework_reconcile")
    _pair(tmp_path, "a.txt")
    assert rec._pair(tmp_path, "a.txt.upstream")[0] == Path("a.txt")
    _pair(tmp_path, "b.txt")
    answers = iter(["a", "q"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(rec, "_validate", lambda _pack: 0)
    assert rec._interactive(tmp_path, "", False) == 0
    assert not (tmp_path / "a.txt.upstream").exists()
    assert (tmp_path / "b.txt.upstream").exists()

    answers = iter(["m", "s"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(rec, "merge", lambda *_: (Path("b.txt"), None))
    assert rec._interactive(tmp_path, "tool", True) == 0


def test_reconcile_interactive_merge_error_then_accept(tmp_path, monkeypatch, capsys):
    rec = load("framework_reconcile")
    _pair(tmp_path, "a.txt")
    answers = iter(["m", "a"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(rec, "merge", lambda *_: (None, "broken tool"))
    assert rec._interactive(tmp_path, "tool", True) == 0
    assert "broken tool" in capsys.readouterr().err


def test_reconcile_pair_rejects_missing_and_absolute(tmp_path, capsys):
    rec = load("framework_reconcile")
    for value, text in [
        (str(tmp_path / "x"), "relative path"),
        ("missing", "local file does not exist"),
    ]:
        assert rec.main([str(tmp_path), "--show", value]) == 2
        assert text in capsys.readouterr().err
    (tmp_path / "local").write_text("x")
    assert rec.main([str(tmp_path), "--show", "local"]) == 2
    assert "no pending upstream copy" in capsys.readouterr().err


def test_reconcile_finish_skips_recompose_without_changes_or_retry_marker(
        tmp_path, monkeypatch):
    rec = load("framework_reconcile")
    monkeypatch.setattr(
        rec,
        "_recompose_schema",
        lambda _pack: (_ for _ in ()).throw(AssertionError("unexpected recompose")),
    )

    assert rec._finish(tmp_path, changed=False, no_validate=True) == 0


def test_pull_fetch_cleans_runtime_and_reports_clone_failures(tmp_path, monkeypatch):
    pull = load("framework_pull")
    dest = tmp_path / "pack"

    def clone(args):
        target = Path(args[-1])
        target.mkdir(parents=True)
        (target / ".env").write_text("secret")
        (target / "raw").mkdir()
        (target / "__pycache__").mkdir()

    monkeypatch.setattr(pull, "_git", clone)
    pull.fetch({"ref": None, "subdir": "", "giturl": "repo"}, dest, False)
    assert not (dest / ".env").exists() and not (dest / "raw").exists()
    assert not (dest / "__pycache__").exists()
    (dest / "operator-owned.txt").write_text("keep")
    with pytest.raises(SystemExit, match="not empty"):
        pull.fetch({"ref": None, "subdir": "", "giturl": "repo"}, dest, False)

    def failed(_args):
        raise subprocess.CalledProcessError(1, "git", stderr="first\nfatal: denied\n")

    monkeypatch.setattr(pull, "_git", failed)
    with pytest.raises(SystemExit, match="fatal: denied"):
        pull.fetch({"ref": None, "subdir": "", "giturl": "repo"}, tmp_path / "other", False)


def test_pull_subdir_catalog_network_and_engine_checks(tmp_path, monkeypatch, capsys):
    pull = load("framework_pull")

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def read(self):
            return json.dumps({"packs": []}).encode()

    monkeypatch.setattr(pull.urllib.request, "urlopen", lambda *a, **k: Response())
    assert pull.read_catalog("https://catalog")[0] == {"packs": []}
    monkeypatch.setattr(pull.urllib.request, "urlopen", lambda *a, **k: (
        _ for _ in ()).throw(pull.urllib.error.URLError("offline")))
    assert "network/DNS" in pull.read_catalog("https://catalog")[1]
    for code, expected in [(403, "forbidden"), (404, "not found"), (500, "HTTP 500")]:
        monkeypatch.setattr(pull.urllib.request, "urlopen", lambda *a, code=code, **k: (
            _ for _ in ()).throw(pull.urllib.error.HTTPError(
                "url", code, "error", {}, None)))
        assert expected in pull.read_catalog("https://catalog")[1]

    dest = tmp_path / "dest"
    def clone(args):
        work = Path(args[-1])
        (work / "packs" / "demo").mkdir(parents=True)
        (work / "packs" / "demo" / "pack.yaml").write_text("name: demo\n")
    monkeypatch.setattr(pull, "_git", clone)
    pull.fetch({"ref": "main", "subdir": "packs/demo", "giturl": "repo"}, dest, False)
    assert (dest / "pack.yaml").is_file()

    (dest / "engine.version").write_text("version: v1.2.0\n")
    monkeypatch.setattr(pull, "engine_version", lambda: "v2.0.0")
    monkeypatch.setattr(pull, "_engine_meta_mod", lambda: SimpleNamespace(
        satisfies_pin=lambda *_: False))
    pull._engine_check(dest)
    assert "different release series" in capsys.readouterr().out
    monkeypatch.setattr(pull, "engine_version", lambda: "v1.2.0")
    monkeypatch.setattr(pull, "_engine_meta_mod", lambda: SimpleNamespace(
        satisfies_pin=lambda *_: True))
    pull._engine_check(dest)
    assert "==" in capsys.readouterr().out
    monkeypatch.setattr(pull, "engine_version", lambda: "v1.2.9")
    pull._engine_check(dest)
    assert "compatible" in capsys.readouterr().out


def test_pull_error_and_loader_helpers(tmp_path, monkeypatch):
    pull = load("framework_pull")
    monkeypatch.setattr(pull.subprocess, "run", lambda *a, **k: (
        _ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(pull, "_engine_release_from_manifest", lambda: "")
    assert pull.engine_version() == "v0.0.0"
    monkeypatch.setenv("OKENGINE_GIT_SSH", "1")
    assert pull._giturl("owner/repo").startswith("git@github.com:")

    target = tmp_path / "missing-subdir"
    monkeypatch.setattr(pull, "_git", lambda args: Path(args[-1]).mkdir())
    with pytest.raises(SystemExit, match="subdir 'packs/nope'"):
        pull.fetch({"ref": None, "subdir": "packs/nope", "giturl": "repo"}, target, False)

    (tmp_path / "pack.yaml").write_text("broken")
    monkeypatch.setattr(pull, "_pack_meta_mod", lambda: SimpleNamespace(
        load_pack_meta=lambda _p: (_ for _ in ()).throw(ValueError())))
    assert pull._resolve_offset(None, tmp_path) == (0, "")
    assert pull._load_meta_safe(tmp_path) is None


def test_pull_dynamic_command_loaders(tmp_path, monkeypatch):
    pull = load("framework_pull")
    fake = SimpleNamespace(
        main=lambda argv: 6 if "--quiet" in argv else 7,
        load_pack_meta=lambda _path: {"name": "demo"})
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _module: None))
    monkeypatch.setattr(pull.importlib.util, "spec_from_file_location", lambda *_: spec)
    monkeypatch.setattr(pull.importlib.util, "module_from_spec", lambda _spec: fake)
    assert pull._engine_meta_mod() is fake
    assert pull._framework_upgrade_mod() is fake
    assert pull._validate(tmp_path) == 6
    assert pull._install_domain(tmp_path, tmp_path / "guest") == 7


def test_pull_runtime_jitter_and_bundle_failure_paths(tmp_path, monkeypatch, capsys):
    pull = load("framework_pull")
    monkeypatch.setattr(pull, "ENGINE_ROOT", tmp_path)
    template = tmp_path / "config/config.yaml.template"
    template.parent.mkdir()
    template.write_text("config\n")
    pull._layer_runtime(tmp_path / "dest")
    assert (tmp_path / "dest/.hermes-data/config.yaml").read_text() == "config\n"

    fake_jitter = SimpleNamespace(expand_file=lambda _path: 3)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *_: SimpleNamespace(
        loader=SimpleNamespace(exec_module=lambda _m: None)))
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda _s: fake_jitter)
    pull._jitter_crons(tmp_path)
    assert "jittered 3" in capsys.readouterr().out

    monkeypatch.setattr(pull, "_pack_meta_mod", lambda: SimpleNamespace(
        validate_bundle_recipe=lambda _meta: ["bad recipe"]))
    with pytest.raises(SystemExit, match="malformed bundle"):
        pull._expand_bundle({}, {}, tmp_path, None)


def test_pull_expand_bundle_member_failure(tmp_path, monkeypatch):
    pull = load("framework_pull")
    (tmp_path / "pack.yaml").write_text("name: bundle\n")
    monkeypatch.setattr(pull, "_pack_meta_mod", lambda: SimpleNamespace(
        validate_bundle_recipe=lambda _meta: []))
    monkeypatch.setattr(pull, "_resolve_member", lambda name, *_: {"name": name})
    monkeypatch.setattr(pull, "fetch", lambda spec, dest, force: (
        dest.mkdir(parents=True, exist_ok=True),
        (dest / "pack.yaml").write_text(f"name: {spec['name']}\n")))
    monkeypatch.setattr(pull, "_install_domain", lambda *_: 4)
    meta = {"bundle_host": "host", "bundle_compose": ["guest"]}
    with pytest.raises(SystemExit, match="exit 4"):
        pull._expand_bundle(meta, {}, tmp_path, None)


def test_pull_bundle_identity_and_member_resolution(tmp_path):
    pull = load("framework_pull")
    spec = {"giturl": "repo", "subdir": "packs/bundle", "ref": "main"}
    assert pull._resolve_member("host", spec, None)["subdir"] == "packs/host"
    with pytest.raises(SystemExit, match="not in the catalog"):
        pull._resolve_member("host", {**spec, "subdir": ""}, None)

    (tmp_path / "pack.yaml").write_text("name: host\ndescription: old\nowns: {}\n")
    pull._apply_bundle_identity(tmp_path, {"name": "bundle", "mission": "combined"})
    text = (tmp_path / "pack.yaml").read_text()
    assert "name: bundle" in text and "mission: combined" in text and "owns: {}" in text
    assert pull._bundle_identity(tmp_path)["name"] == "bundle"
    assert pull._bundle_identity(tmp_path / "missing") == {}


def test_pull_fresh_main_runs_complete_operator_workflow(tmp_path, monkeypatch, capsys):
    pull = load("framework_pull")
    dest = tmp_path / "out"
    monkeypatch.setattr(pull, "read_catalog", lambda _src: ({"packs": []}, None))
    monkeypatch.setattr(pull, "resolve", lambda *_: ({
        "name": "demo", "giturl": "repo", "subdir": "", "ref": None}, False))

    def fetch(_spec, target, _force):
        target.mkdir()
        (target / "pack.yaml").write_text("name: demo\nversion: 1.2.3\n")
    monkeypatch.setattr(pull, "fetch", fetch)
    monkeypatch.setattr(pull, "_jitter_crons", lambda _d: None)
    monkeypatch.setattr(pull, "_layer_runtime", lambda _d: None)
    monkeypatch.setattr(pull, "_resolve_offset", lambda *_: (20, "pack.yaml"))
    monkeypatch.setattr(pull, "_apply_port_offset", lambda *_: None)
    monkeypatch.setattr(pull, "_warn_busy_host_ports", lambda *_: None)
    monkeypatch.setattr(pull, "_engine_check", lambda *_: None)
    monkeypatch.setattr(pull, "_validate", lambda *_: 0)
    monkeypatch.setattr(pull, "_load_meta_safe", lambda _d: {
        "name": "demo", "version": "1.2.3"})
    recorded = []
    monkeypatch.setattr(pull, "_framework_upgrade_mod", lambda: SimpleNamespace(
        record_pack_version=lambda *a: recorded.append(a)))
    assert pull.main(["demo", str(dest), "--catalog", "x"]) == 0
    assert recorded and "uncurated pack" in capsys.readouterr().out


def test_pull_update_main_runs_migration_workflow(tmp_path, monkeypatch):
    pull = load("framework_pull")
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "pack.yaml").write_text("name: demo\nversion: 1.0.0\n")
    monkeypatch.setattr(pull, "read_catalog", lambda _src: ({"packs": []}, None))
    monkeypatch.setattr(pull, "resolve", lambda *_: ({
        "name": "demo", "giturl": "repo", "subdir": "", "ref": None}, True))

    def clone(args):
        clone_dir = Path(args[-1])
        clone_dir.mkdir()
        (clone_dir / "pack.yaml").write_text("name: demo\nversion: 1.1.0\n")
        (clone_dir / "new.txt").write_text("new\n")
    monkeypatch.setattr(pull, "_git", clone)
    monkeypatch.setattr(pull, "_load_meta_safe", lambda path: {
        "name": "demo", "version": "1.1.0" if "clone" in str(path) else "1.0.0"})
    upgrade = SimpleNamespace(
        installed_pack_version=lambda *a, **k: "1.0.0",
        run_pack_migrations=lambda *a, **k: 0)
    monkeypatch.setattr(pull, "_framework_upgrade_mod", lambda: upgrade)
    monkeypatch.setattr(pull, "_engine_check", lambda *_: None)
    monkeypatch.setattr(pull, "_validate", lambda *_: 0)
    assert pull.main(["demo", str(dest), "--update", "--catalog", "x"]) == 0
    assert (dest / "new.txt").is_file()


def test_pull_main_destination_and_update_rejections(tmp_path, monkeypatch):
    pull = load("framework_pull")
    monkeypatch.setattr(pull, "read_catalog", lambda _src: ({"packs": []}, None))
    monkeypatch.setattr(pull, "resolve", lambda *_: ({
        "name": "demo", "giturl": "repo", "subdir": "packs/demo", "ref": None}, True))
    with pytest.raises(SystemExit, match="existing pack dir"):
        pull.main(["demo", str(tmp_path / "missing"), "--update", "--catalog", "x"])

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "pack.yaml").write_text("name: demo\n")
    monkeypatch.setattr(pull, "_git", lambda _args: (
        _ for _ in ()).throw(subprocess.CalledProcessError(1, "git", stderr="clone bad")))
    with pytest.raises(SystemExit, match="clone bad"):
        pull.main(["demo", str(dest), "--update", "--ref", "v2", "--catalog", "x"])

    def clone_without_subdir(args):
        Path(args[-1]).mkdir()
    monkeypatch.setattr(pull, "_git", clone_without_subdir)
    with pytest.raises(SystemExit, match="subdir 'packs/demo'"):
        pull.main(["demo", str(dest), "--update", "--catalog", "x"])


def test_pull_main_into_bundle_offset_and_no_pack_warning(tmp_path, monkeypatch, capsys):
    pull = load("framework_pull")
    monkeypatch.setattr(pull, "read_catalog", lambda _src: ({"packs": []}, None))
    monkeypatch.setattr(pull, "resolve", lambda *_: ({
        "name": "bundle", "giturl": "repo", "subdir": "", "ref": "v1"}, True))
    monkeypatch.setattr(pull, "fetch", lambda _s, dest, _f: dest.mkdir(parents=True))
    metas = iter([
        {"kind": "bundle", "port_offset": 30},
        None,
    ])
    monkeypatch.setattr(pull, "_load_meta_safe", lambda _d: next(metas))
    monkeypatch.setattr(pull, "_expand_bundle", lambda *_: None)
    monkeypatch.setattr(pull, "_jitter_crons", lambda *_: None)
    monkeypatch.setattr(pull, "_layer_runtime", lambda *_: None)
    monkeypatch.setattr(pull, "_resolve_offset", lambda cli, _d: (cli, ""))
    monkeypatch.setattr(pull, "_apply_port_offset", lambda *_: None)
    monkeypatch.setattr(pull, "_warn_busy_host_ports", lambda *_: None)
    monkeypatch.setattr(pull, "_engine_check", lambda *_: None)
    into = tmp_path / "packs"
    assert pull.main(["bundle", "--into", str(into), "--no-validate", "--catalog", "x"]) == 0
    out = capsys.readouterr().out
    assert "bundle recipe's default" in out and "no pack.yaml" in out
