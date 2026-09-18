"""Guided pack-update reconciliation regressions (#61)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "framework_reconcile.py"


def _load():
    spec = importlib.util.spec_from_file_location("framework_reconcile", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pair(pack: Path, rel: str, local: str = "local\n", upstream: str = "upstream\n"):
    path = pack / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(local)
    path.with_name(path.name + ".upstream").write_text(upstream)
    return path


def test_framework_cli_dispatches_reconcile():
    spec = importlib.util.spec_from_file_location("framework", REPO / "scripts" / "framework.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._COMMANDS["reconcile"] == (
        "framework_reconcile",
        "framework_reconcile.py",
    )


def test_list_and_inline_diff(tmp_path, capsys):
    module = _load()
    _pair(tmp_path, "schema.yaml", "types: {a: {}}\n", "types: {a: {}, b: {}}\n")

    assert module.main([str(tmp_path)]) == 0
    assert "schema.yaml.upstream" in capsys.readouterr().out

    assert module.main([str(tmp_path), "--show", "schema.yaml"]) == 0
    shown = capsys.readouterr().out
    assert "--- schema.yaml (local)" in shown
    assert "+++ schema.yaml (upstream)" in shown
    assert "+types: {a: {}, b: {}}" in shown


def test_accept_is_atomic_and_validates_after_final_resolution(tmp_path, monkeypatch):
    module = _load()
    local = _pair(tmp_path, "README.md")
    validated = []
    order = []
    monkeypatch.setattr(module, "_recompose_schema", lambda pack: order.append("compose") or [])
    monkeypatch.setattr(
        module, "_validate", lambda pack: (order.append("validate"), validated.append(pack), 0)[2]
    )

    assert module.main([str(tmp_path), "--accept", "README.md"]) == 0

    assert local.read_text() == "upstream\n"
    assert not (tmp_path / "README.md.upstream").exists()
    assert validated == [tmp_path.resolve()]
    assert order == ["compose", "validate"]


def test_accept_establishes_retry_marker_before_replacing_input(tmp_path, monkeypatch):
    module = _load()
    local = _pair(tmp_path, "schema.yaml")
    real_replace = module.os.replace

    def checked_replace(source, destination):
        assert (tmp_path / ".okengine/recompose-required.upstream").is_file()
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", checked_replace)
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: [])
    monkeypatch.setattr(module, "_validate", lambda _pack: 0)

    assert module.main([str(tmp_path), "--accept", "schema.yaml"]) == 0
    assert local.read_text() == "upstream\n"


def test_keep_preserves_local_and_waits_to_validate_when_more_pending(tmp_path, monkeypatch):
    module = _load()
    first = _pair(tmp_path, "pack.yaml")
    _pair(tmp_path, "schema.yaml")
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: [])
    monkeypatch.setattr(module, "_validate", lambda _pack: (_ for _ in ()).throw(
        AssertionError("must not validate with pending files")))

    assert module.main([str(tmp_path), "--keep", "pack.yaml"]) == 0

    assert first.read_text() == "local\n"
    assert not (tmp_path / "pack.yaml.upstream").exists()
    assert (tmp_path / "schema.yaml.upstream").exists()


def test_schema_accept_recomposes_even_when_another_definition_is_pending(tmp_path, monkeypatch):
    module = _load()
    _pair(tmp_path, "schema.yaml", "types: {old: {}}\n", "types: {new: {}}\n")
    _pair(tmp_path, "README.md")
    artifact = tmp_path / ".okengine" / "composed-schema.yaml"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("types: {stale: {}}\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_validate",
        lambda _pack: (_ for _ in ()).throw(AssertionError("validation must wait for README")),
    )

    assert module.main([str(tmp_path), "--accept", "schema.yaml"]) == 0
    assert not artifact.exists(), "the old taxonomy must not remain authoritative while pending"
    assert (tmp_path / "README.md.upstream").exists()


def test_pending_ignores_runtime_trees_pull_never_reconciles(tmp_path):
    module = _load()
    _pair(tmp_path, "schema.yaml")
    for root in ("wiki", "raw", ".hermes-data"):
        stray = tmp_path / root / "capture.upstream"
        stray.parent.mkdir(parents=True)
        stray.write_text("runtime content", encoding="utf-8")

    assert module.pending(tmp_path) == [Path("schema.yaml.upstream")]


def test_pending_ignores_retained_snapshots_and_rollback_quarantine(tmp_path):
    module = _load()
    _pair(tmp_path, "schema.yaml")
    for root in (".okengine/snapshots/20260916/tree", ".okengine/rolled-back/recovery"):
        stray = tmp_path / root / "schema.yaml.upstream"
        stray.parent.mkdir(parents=True)
        stray.write_text("rollback evidence", encoding="utf-8")

    assert module.pending(tmp_path) == [Path("schema.yaml.upstream")]


def test_merge_runs_tool_with_local_and_upstream_then_validates(tmp_path, monkeypatch):
    module = _load()
    local = _pair(tmp_path, "schema.yaml")
    calls = []

    def fake_run(command, check):
        calls.append((command, check))
        Path(command[-2]).write_text(Path(command[-1]).read_text() + "merged\n")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: [])
    monkeypatch.setattr(module, "_validate", lambda _pack: 0)

    assert module.main([
        str(tmp_path), "--merge", "schema.yaml", "--merge-tool", "fake-tool --flag",
    ]) == 0

    assert calls[0][0][:2] == ["fake-tool", "--flag"]
    assert local.read_text() == "upstream\nmerged\n"
    assert not (tmp_path / "schema.yaml.upstream").exists()


def test_merge_tool_may_consume_upstream_without_skipping_recompose(tmp_path, monkeypatch):
    module = _load()
    local = _pair(tmp_path, "schema.yaml")
    composed = []

    def consuming_tool(command, check):
        local.write_text(Path(command[-1]).read_text(), encoding="utf-8")
        Path(command[-1]).unlink()
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(module.subprocess, "run", consuming_tool)
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: composed.append(True) or [])
    monkeypatch.setattr(module, "_validate", lambda _pack: 0)

    assert module.main([
        str(tmp_path), "--merge", "schema.yaml", "--merge-tool", "consuming-tool",
    ]) == 0
    assert composed == [True]


def test_recompose_interrupt_leaves_retry_marker(tmp_path, monkeypatch):
    module = _load()
    _pair(tmp_path, "schema.yaml")
    monkeypatch.setattr(
        module, "_recompose_schema", lambda _pack: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    assert module.main([str(tmp_path), "--accept", "schema.yaml"]) == 1
    assert (tmp_path / ".okengine/recompose-required.upstream").is_file()


def test_failed_or_noop_merge_retains_pending_copy(tmp_path, monkeypatch):
    module = _load()
    _pair(tmp_path, "schema.yaml")
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda _command, check: type("Result", (), {"returncode": 0})(),
    )

    assert module.main([
        str(tmp_path), "--merge", "schema.yaml", "--merge-tool", "fake-tool",
    ]) == 1
    assert (tmp_path / "schema.yaml.upstream").exists()


def test_paths_cannot_escape_pack(tmp_path):
    module = _load()
    outside = tmp_path.parent / "outside"
    outside.write_text("local")
    (tmp_path.parent / "outside.upstream").write_text("upstream")

    assert module.main([str(tmp_path), "--accept", "../outside"]) == 2
    assert outside.read_text() == "local"


def test_resolved_symlink_cannot_escape_pack(tmp_path):
    module = _load()
    outside = tmp_path.parent / "outside-local"
    outside.write_text("local")
    (tmp_path / "linked").symlink_to(outside)
    (tmp_path / "linked.upstream").write_text("upstream")
    try:
        module._pair(tmp_path, "linked")
    except ValueError as exc:
        assert "stay inside" in str(exc)
    else:
        raise AssertionError("escaping symlink was accepted")


def test_validate_loads_validator_and_empty_pack_has_no_review_hint(tmp_path, monkeypatch, capsys):
    module = _load()
    fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda loaded: setattr(
        loaded, "main", lambda argv: 7 if argv == [str(tmp_path), "--quiet"] else 8)))
    monkeypatch.setattr(module.importlib.util, "spec_from_file_location", lambda *_: fake_spec)
    monkeypatch.setattr(module.importlib.util, "module_from_spec", lambda _spec: SimpleNamespace())
    assert module._validate(tmp_path) == 7

    assert module.main([str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "pending upstream changes: 0" in output
    assert "review with" not in output


def test_reconcile_fails_before_validation_when_schema_recomposition_fails(
    tmp_path, monkeypatch, capsys
):
    module = _load()
    _pair(tmp_path, "schema.yaml", "types: {old: {}}\n", "types: {new: {}}\n")
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: ["broken fragment"])
    monkeypatch.setattr(
        module,
        "_validate",
        lambda _pack: (_ for _ in ()).throw(AssertionError("must not validate stale schema")),
    )

    assert module.main([str(tmp_path), "--accept", "schema.yaml"]) == 1
    assert "composed-schema regeneration failed" in capsys.readouterr().err
    assert (tmp_path / ".okengine/recompose-required.upstream").is_file()


def test_validation_failure_keeps_retry_marker_until_success(tmp_path, monkeypatch):
    module = _load()
    _pair(tmp_path, "schema.yaml")
    validation_results = iter((1, 0))
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: [])
    monkeypatch.setattr(module, "_validate", lambda _pack: next(validation_results))

    assert module.main([str(tmp_path), "--accept", "schema.yaml"]) == 1
    marker = tmp_path / ".okengine/recompose-required"
    marker_upstream = marker.with_name(marker.name + ".upstream")
    assert marker.is_file() and marker_upstream.is_file()

    assert module._finish(tmp_path, False, False) == 0
    assert not marker.exists() and not marker_upstream.exists()


def test_retry_accept_validation_failure_is_retried_by_plain_reconcile(tmp_path, monkeypatch):
    module = _load()
    _pair(tmp_path, "schema.yaml")
    validation_results = iter((1, 1, 0))
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: [])
    monkeypatch.setattr(module, "_validate", lambda _pack: next(validation_results))

    assert module.main([str(tmp_path), "--accept", "schema.yaml"]) == 1
    assert module.main([
        str(tmp_path), "--accept", ".okengine/recompose-required",
    ]) == 1
    marker = tmp_path / ".okengine/recompose-required"
    assert marker.is_file()
    assert module.main([str(tmp_path)]) == 0
    assert not marker.exists()


def test_no_validate_leaves_validation_owed_marker(tmp_path, monkeypatch):
    module = _load()
    _pair(tmp_path, "schema.yaml")
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: [])

    assert module.main([str(tmp_path), "--accept", "schema.yaml", "--no-validate"]) == 0
    assert (tmp_path / ".okengine/recompose-required.upstream").is_file()


def test_failed_recomposition_has_an_operator_retry_path(tmp_path, monkeypatch):
    module = _load()
    _pair(tmp_path, "schema.yaml", "types: {old: {}}\n", "types: {new: {}}\n")
    outcomes = iter((["broken fragment"], []))
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: next(outcomes))
    monkeypatch.setattr(module, "_validate", lambda _pack: 0)

    assert module.main([str(tmp_path), "--accept", "schema.yaml"]) == 1
    assert module.main([
        str(tmp_path), "--accept", ".okengine/recompose-required",
    ]) == 0
    assert not (tmp_path / ".okengine/recompose-required").exists()
    assert not (tmp_path / ".okengine/recompose-required.upstream").exists()


def test_interactive_retry_marker_success_always_validates(tmp_path, monkeypatch):
    module = _load()
    marker = _pair(tmp_path, ".okengine/recompose-required")
    validated = []
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: [])
    monkeypatch.setattr(module, "_validate", lambda pack: validated.append(pack) or 0)
    monkeypatch.setattr("builtins.input", lambda _prompt: "skip")

    assert module.main([str(tmp_path), "--interactive"]) == 0
    assert validated == [tmp_path.resolve()]
    assert not marker.exists()
    assert not marker.with_name(marker.name + ".upstream").exists()


def test_interactive_error_after_accept_still_recomposes(tmp_path, monkeypatch):
    module = _load()
    _pair(tmp_path, "schema.yaml", "types: {old: {}}\n", "types: {new: {}}\n")
    orphan = tmp_path / "templates/item.md.upstream"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("upstream without local", encoding="utf-8")
    composed = []
    monkeypatch.setattr(module, "_recompose_schema", lambda _pack: composed.append(True) or [])
    monkeypatch.setattr("builtins.input", lambda _prompt: "accept")

    assert module.main([str(tmp_path), "--interactive", "--no-validate"]) == 2
    assert composed == [True]
    assert not (tmp_path / "schema.yaml.upstream").exists()


def test_reconcile_removes_stale_artifact_when_no_schema_extensions_remain(tmp_path):
    module = _load()
    _pair(tmp_path, "schema.yaml", "types: {old: {}}\n", "types: {new: {}}\n")
    artifact = tmp_path / ".okengine" / "composed-schema.yaml"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("types: {stale: {}}\n", encoding="utf-8")

    assert module.main([str(tmp_path), "--accept", "schema.yaml", "--no-validate"]) == 0
    assert not artifact.exists(), "reconcile must not leave the old generated taxonomy in force"


def test_recompose_schema_adds_source_path_and_returns_errors(tmp_path, monkeypatch):
    module = _load()
    source_root = str(REPO / "src")
    monkeypatch.setattr(module.sys, "path", [entry for entry in module.sys.path
                                             if entry != source_root])
    fake = SimpleNamespace(write_composed_schema=lambda _pack: [ValueError("broken")])
    monkeypatch.setattr(module.importlib.util, "module_from_spec", lambda _spec: fake)
    monkeypatch.setattr(
        module.importlib.util,
        "spec_from_file_location",
        lambda name, _path: SimpleNamespace(
            name=name, loader=SimpleNamespace(exec_module=lambda _module: None)
        ),
    )
    assert module._recompose_schema(tmp_path) == ["broken"]
    assert module.sys.path[0] == source_root


def test_interactive_error_before_change_does_not_recompose(tmp_path, monkeypatch):
    module = _load()
    orphan = tmp_path / "schema.yaml.upstream"
    orphan.write_text("upstream without local")
    monkeypatch.setattr(
        module,
        "_recompose_schema",
        lambda _pack: (_ for _ in ()).throw(AssertionError("must not recompose")),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "accept")
    assert module.main([str(tmp_path), "--interactive", "--no-validate"]) == 2
