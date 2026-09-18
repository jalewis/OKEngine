"""Defensive and alternate-path coverage for framework_upgrade."""
from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/framework_upgrade.py"


def _mod(name="framework_upgrade_branch_edges"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _meta(version="v1.0.0"):
    def semver(value):
        try:
            return tuple(int(x) for x in str(value).lstrip("v").split("."))
        except (TypeError, ValueError):
            return None
    return SimpleNamespace(
        _semver=semver, engine_release=lambda: version, hermes_pin=lambda: None,
        satisfies_pin=lambda pin, target: semver(pin)[:2] == semver(target)[:2])


def test_malformed_pin_and_state(tmp_path):
    m = _mod()
    (tmp_path / "engine.version").write_text("[broken\n")
    assert m.read_pin(tmp_path) == (None, None)
    state = tmp_path / m.STATE_REL
    state.parent.mkdir()
    state.write_text("{broken")
    assert m.read_state(tmp_path) == {}


def test_load_migrations_restores_engine_script_import_path(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_import_path_edge")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    engine_scripts = str(SCRIPT.resolve().parent)
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != engine_scripts])
    assert m.load_migrations(migrations) == []
    assert sys.path[0] == engine_scripts


def test_default_validator_all_adapter_outcomes(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_validator_edges")
    fake = SimpleNamespace(main=lambda _argv: 1)
    original = importlib.util.module_from_spec
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda spec:
                        fake if spec.name == "framework_validate" else original(spec))
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda name, _path:
                        SimpleNamespace(name=name, loader=SimpleNamespace(exec_module=lambda _m: None)))
    assert m._default_validator(tmp_path)[0] is False
    fake.main = lambda _argv: 0
    assert m._default_validator(tmp_path)[0] is True
    fake.main = lambda _argv: (_ for _ in ()).throw(RuntimeError("validator crash"))
    assert "skipped" in m._default_validator(tmp_path)[1]


def test_failure_map_import_skip_and_read_errors(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_failure_map_edges")
    monkeypatch.setattr(importlib.util, "spec_from_file_location",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no validator")))
    assert m._page_failure_map(tmp_path) == {}
    assert m._unknown_type_map(tmp_path) == {}

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "_hidden.md").write_text("x")
    (wiki / "INDEX.md").write_text("x")
    page = wiki / "page.md"
    page.write_text("x")
    validator = SimpleNamespace(
        schema_reject_reason=lambda *_a: (_ for _ in ()).throw(RuntimeError("bad page")))
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda _s: validator)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda name, _path:
                        SimpleNamespace(name=name, loader=SimpleNamespace(exec_module=lambda _m: None)))
    assert m._page_failure_map(tmp_path) == {}


def test_unknown_type_map_defensive_shapes(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_unknown_edges")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for name in ("notok.md", "nofm.md", "scalar.md", "notype.md", "noschema.md", "unknown.md", "boom.md"):
        (wiki / name).write_text(name)

    class Match:
        def __init__(self, value): self.value = value
        def group(self, _n): return self.value
    current = {"name": ""}
    validator = SimpleNamespace()
    validator._evaluate = lambda path, _content: (("reject", "x") if path.endswith("notok.md") else
                                                  (_ for _ in ()).throw(RuntimeError("boom"))
                                                  if path.endswith("boom.md") else ("ok", None))
    validator._FM_RE = SimpleNamespace(match=lambda content: None if content == "nofm.md" else Match(content))
    validator.yaml = SimpleNamespace(safe_load=lambda raw: (
        [] if raw == "scalar.md" else {} if raw == "notype.md" else {"type": "mystery"}))
    validator._find_schema = lambda path: None if path.endswith("noschema.md") else Path("schema")
    validator._load_schema = lambda _p: {"types": {}}
    validator._base_merged = lambda schema: schema
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda _s: validator)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda name, _path:
                        SimpleNamespace(name=name, loader=SimpleNamespace(exec_module=lambda _m: None)))
    result = m._unknown_type_map(tmp_path)
    assert "mystery" in result


def test_cap_breaks_and_misc_helpers(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_helper_edges")
    monkeypatch.setattr(m, "_unknown_type_map", lambda root: (
        {} if root.name == "before" else {"x": ["a", "b"]}))
    assert len(m._unknown_type_regressions(tmp_path / "before", tmp_path / "after", cap=1)) == 1
    monkeypatch.setattr(m, "_page_failure_map", lambda root: (
        {} if root.name == "before" else {"a": "bad", "b": "bad"}))
    assert len(m._conformance_regressions(tmp_path / "before", tmp_path / "after", cap=1)) == 1

    assert m.prune_snapshots(tmp_path, -1) == 0
    assert m.changelog_impact("text", "bad", "1.0.0", _meta()) == []
    assert m.installed_pack_version(tmp_path, "p", "fallback") == "fallback"
    m.record_pack_version(tmp_path, "p", "1.0.0")
    assert m.installed_pack_version(tmp_path, "p") == "1.0.0"


def test_pack_migration_invalid_incoming_and_regression_rollbacks(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_pack_edge")
    monkeypatch.setattr(m, "_engine_meta", _meta)
    assert m.run_pack_migrations(tmp_path, "p", "0.1.0", None, apply=False) == 0

    migration = m.Migration("one", "0.1.0", "0.2.0", "test", lambda _p, _d: ["changed"])
    monkeypatch.setattr(m, "load_migrations", lambda _p: [migration])
    monkeypatch.setattr(m, "snapshot", lambda *_a, **_k: tmp_path / "snap")
    monkeypatch.setattr(m, "added_since_snapshot", lambda *_a: set())
    monkeypatch.setattr(m, "changed_since_snapshot", lambda *_a: set())
    monkeypatch.setattr(m, "restore", lambda *_a, **_k: 1)
    monkeypatch.setattr(m.shutil, "rmtree", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "VALIDATOR", lambda _p: (True, "ok"))
    monkeypatch.setattr(m, "_conformance_regressions", lambda *_a, **_k: ["bad"])
    assert m.run_pack_migrations(tmp_path, "p", "0.1.0", "0.2.0", apply=True) == 1
    (tmp_path / m.STATE_REL).unlink()
    monkeypatch.setattr(m, "_conformance_regressions", lambda *_a, **_k: [])
    monkeypatch.setattr(m, "_unknown_type_regressions", lambda *_a, **_k: ["unknown"])
    assert m.run_pack_migrations(tmp_path, "p", "0.1.0", "0.2.0", apply=True) == 1


def test_recompose_schema_import_path_and_error(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_recompose_edges")
    source_root = str(REPO / "src")
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != source_root])
    fake = SimpleNamespace(write_composed_schema=lambda _pack: ["broken"])
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda _spec: fake)
    monkeypatch.setattr(
        importlib.util,
        "spec_from_file_location",
        lambda name, _path: SimpleNamespace(
            name=name, loader=SimpleNamespace(exec_module=lambda _module: None)
        ),
    )
    with pytest.raises(RuntimeError, match="broken"):
        m._recompose_schema(tmp_path)
    assert sys.path[0] == source_root


def test_unfinished_snapshot_filters_and_rollback_plan(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_unfinished_snapshot_edges")
    assert m._unfinished_pack_snapshot(tmp_path, "p", "0.2.0") is None
    base = tmp_path / m.SNAPSHOTS_REL
    base.mkdir(parents=True)
    (base / "link").symlink_to(tmp_path)
    invalid = base / "invalid"
    invalid.mkdir()
    (invalid / "manifest.json").write_text("{broken")
    confirmed = base / "confirmed"
    confirmed.mkdir()
    (confirmed / "manifest.json").write_text(json.dumps({
        "pack": "p", "to": "0.2.0", "confirmed_good_at": "now",
    }))
    old = base / "20260101"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps({"pack": "p", "to": "0.2.0"}))
    newest = base / "20260102"
    newest.mkdir()
    (newest / "manifest.json").write_text(json.dumps({"pack": "p", "to": "0.2.0"}))
    assert m._unfinished_pack_snapshot(tmp_path, "p", "0.2.0") == newest

    (newest / m.ROLLBACK_PLAN).write_text(json.dumps({
        "added": {"new.txt": "sha256:x"}, "modified": {"old.txt": "sha256:y"},
    }))
    seen = {}
    monkeypatch.setattr(m, "restore", lambda vault, snap, **kwargs: seen.update(kwargs) or 2)
    assert m._rollback_pack_snapshot(tmp_path, newest) == 2
    assert seen == {"added": {Path("new.txt")}, "modified": {Path("old.txt")}}


def test_render_and_main_early_paths(tmp_path, monkeypatch, capsys):
    m = _mod("framework_upgrade_main_edges")
    for status, phrase in (("current", "nothing to do"), ("compatible", "compatible"),
                           ("unknown", "cannot compare")):
        assert phrase in m.render(m.Plan(status, None, None, None))
    already = m.Plan("upgrade", "v0.1.0", "v1.0.0", None, already_applied=["one"])
    assert "already applied" in m.render(already)

    assert m.main([str(tmp_path / "missing")]) == 2
    pack = tmp_path / "pack"
    pack.mkdir()
    monkeypatch.setattr(m, "_engine_meta", lambda: _meta())
    monkeypatch.setattr(m, "load_all_migrations",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("broken migration")))
    assert m.main([str(pack)]) == 2
    assert "broken migration" in capsys.readouterr().err

    monkeypatch.setattr(m, "load_all_migrations", lambda *_a: [])
    monkeypatch.setattr(m, "plan_upgrade", lambda *_a: m.Plan("unknown", None, "v1.0.0", None))
    assert m.main([str(pack)]) == 2
    monkeypatch.setattr(m, "plan_upgrade", lambda *_a: m.Plan("current", "v1.0.0", "v1.0.0", None))
    assert m.main([str(pack), "--apply"]) == 0


def test_script_entrypoint(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(tmp_path / "missing")])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 2


def test_snapshot_and_changed_file_oserror_edges(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_snapshot_edges")
    pack = tmp_path / "pack"
    pack.mkdir()
    source = pack / "source.txt"
    source.write_text("old")
    original_lstat = Path.lstat
    failed = {"once": False}
    def lstat_once(path):
        if path == source and not failed["once"]:
            failed["once"] = True
            raise OSError("stat")
        return original_lstat(path)
    monkeypatch.setattr(Path, "lstat", lstat_once)
    monkeypatch.setattr(m.shutil, "disk_usage", lambda _p: (_ for _ in ()).throw(OSError("disk")))
    snap = m.snapshot(pack, "one")
    assert snap.is_dir()

    # A deleted snapshot member and an unreadable current member are both restore candidates.
    tree_file = snap / "tree/source.txt"
    tree_file.write_text("old")
    source.unlink(missing_ok=True)
    assert Path("source.txt") not in m.changed_since_snapshot(pack, snap)
    source.write_text("new")
    original_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (
        (_ for _ in ()).throw(OSError("read")) if path == source else original_read_bytes(path)))
    assert Path("source.txt") in m.changed_since_snapshot(pack, snap)


def test_restore_multiple_added_and_snapshot_members(tmp_path):
    m = _mod("framework_upgrade_restore_edges")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "keep.txt").write_text("live")
    (pack / "added.txt").write_text("added")
    snap = tmp_path / "snap"
    tree = snap / "tree"
    tree.mkdir(parents=True)
    (tree / "keep.txt").write_text("old")
    (tree / "gone.txt").write_text("restored")
    count = m.restore(pack, snap, added=[Path("keep.txt"), Path("missing.txt"), Path("added.txt")],
                      modified={Path("gone.txt")})
    assert count == 2
    assert (pack / "keep.txt").read_text() == "live"
    assert (pack / "gone.txt").read_text() == "restored"


def test_pack_migration_record_false_branches(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_pack_record_edges")
    monkeypatch.setattr(m, "_engine_meta", _meta)
    assert m.run_pack_migrations(tmp_path, "p", None, "0.2.0",
                                 apply=False, record=False) == 0
    monkeypatch.setattr(m, "load_migrations", lambda _p: [])
    assert m.run_pack_migrations(tmp_path, "p", "0.1.0", "0.2.0",
                                 apply=False, record=False) == 0


def test_main_dry_preview_and_apply_failure_without_snapshot(tmp_path, monkeypatch, capsys):
    m = _mod("framework_upgrade_main_more_edges")
    pack = tmp_path / "pack"
    pack.mkdir()
    migration = m.Migration("one", "v0.1.0", "v1.0.0", "test",
                            lambda _p, dry: ["preview" if dry else "apply"])
    plan = m.Plan("upgrade", "v0.1.0", "v1.0.0", None, [migration])
    monkeypatch.setattr(m, "_engine_meta", lambda: _meta())
    monkeypatch.setattr(m, "load_all_migrations", lambda *_a: [migration])
    monkeypatch.setattr(m, "plan_upgrade", lambda *_a: plan)
    assert m.main([str(pack)]) == 0
    assert "would apply" in capsys.readouterr().out

    monkeypatch.setattr(m, "apply_upgrade",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("apply crash")))
    assert m.main([str(pack), "--apply", "--no-snapshot"]) == 1
    assert "NO automatic rollback" in capsys.readouterr().out


def test_symlink_metadata_failure_and_symlink_change_loop(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_symlink_edges")
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.symlink_to("target")
    monkeypatch.setattr(m.shutil, "copystat",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("metadata")))
    m._copy_scoped_file(src, dst)
    assert dst.is_symlink() and dst.readlink() == Path("target")

    pack = tmp_path / "pack"
    tree = tmp_path / "snap/tree"
    pack.mkdir()
    tree.mkdir(parents=True)
    (pack / "one").symlink_to("new")
    (tree / "one").symlink_to("old")
    (pack / "two").symlink_to("same")
    (tree / "two").symlink_to("same")
    changed = m.changed_since_snapshot(pack, tree.parent)
    assert Path("one") in changed and Path("two") not in changed


def test_unknown_alias_nonmapping_branch(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_alias_edges")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("page")
    match = SimpleNamespace(group=lambda _n: "page")
    validator = SimpleNamespace(
        _evaluate=lambda *_a: ("ok", None),
        _FM_RE=SimpleNamespace(match=lambda _c: match),
        yaml=SimpleNamespace(safe_load=lambda _raw: {"type": "unknown"}),
        _find_schema=lambda _p: Path("schema"),
        _load_schema=lambda _p: {"types": {}, "type_aliases": ["not", "mapping"]},
        _base_merged=lambda schema: schema,
    )
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda _s: validator)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda name, _path:
                        SimpleNamespace(name=name, loader=SimpleNamespace(exec_module=lambda _m: None)))
    assert m._unknown_type_map(tmp_path) == {"unknown": ["page.md"]}


def test_main_successful_no_snapshot_branches(tmp_path, monkeypatch):
    m = _mod("framework_upgrade_main_success_edges")
    pack = tmp_path / "pack"
    pack.mkdir()
    monkeypatch.setattr(m, "_engine_meta", lambda: _meta())
    monkeypatch.setattr(m, "load_all_migrations", lambda *_a: [])
    current = m.Plan("current", "v1.0.0", "v1.0.0", None)
    monkeypatch.setattr(m, "plan_upgrade", lambda *_a: current)
    assert m.main([str(pack)]) == 0

    upgrade = m.Plan("upgrade", "v0.1.0", "v1.0.0", None)
    monkeypatch.setattr(m, "plan_upgrade", lambda *_a: upgrade)
    monkeypatch.setattr(m, "apply_upgrade", lambda *_a: [])
    assert m.main([str(pack), "--apply", "--no-snapshot", "--no-validate"]) == 0
    monkeypatch.setattr(m, "VALIDATOR", lambda _p: (True, "ok"))
    assert m.main([str(pack), "--apply", "--no-snapshot"]) == 0
