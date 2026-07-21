"""Guided pack-update reconciliation regressions (#61)."""

import importlib.util
from pathlib import Path


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
    monkeypatch.setattr(module, "_validate", lambda pack: validated.append(pack) or 0)

    assert module.main([str(tmp_path), "--accept", "README.md"]) == 0

    assert local.read_text() == "upstream\n"
    assert not (tmp_path / "README.md.upstream").exists()
    assert validated == [tmp_path.resolve()]


def test_keep_preserves_local_and_waits_to_validate_when_more_pending(tmp_path, monkeypatch):
    module = _load()
    first = _pair(tmp_path, "pack.yaml")
    _pair(tmp_path, "schema.yaml")
    monkeypatch.setattr(module, "_validate", lambda _pack: (_ for _ in ()).throw(
        AssertionError("must not validate with pending files")))

    assert module.main([str(tmp_path), "--keep", "pack.yaml"]) == 0

    assert first.read_text() == "local\n"
    assert not (tmp_path / "pack.yaml.upstream").exists()
    assert (tmp_path / "schema.yaml.upstream").exists()


def test_merge_runs_tool_with_local_and_upstream_then_validates(tmp_path, monkeypatch):
    module = _load()
    local = _pair(tmp_path, "schema.yaml")
    calls = []

    def fake_run(command, check):
        calls.append((command, check))
        Path(command[-2]).write_text(Path(command[-1]).read_text() + "merged\n")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_validate", lambda _pack: 0)

    assert module.main([
        str(tmp_path), "--merge", "schema.yaml", "--merge-tool", "fake-tool --flag",
    ]) == 0

    assert calls[0][0][:2] == ["fake-tool", "--flag"]
    assert local.read_text() == "upstream\nmerged\n"
    assert not (tmp_path / "schema.yaml.upstream").exists()


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
