import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "ci/corpus_write_gate.py"
    spec = importlib.util.spec_from_file_location("corpus_write_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_rejects_new_direct_wiki_write(tmp_path):
    module = _module()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "extensions").mkdir()
    (tmp_path / "okengine-mcp").mkdir()
    (tmp_path / "tools").mkdir()
    target = tmp_path / "scripts/new_writer.py"
    target.write_text("def write(wiki):\n    (wiki / 'page.md').write_text('x')\n")
    assert module.findings(tmp_path) == [
        "scripts/new_writer.py:2: direct canonical write_text()"
    ]


def test_gate_ignores_non_wiki_approved_and_unparseable_files(tmp_path):
    module = _module()
    for top in module.ROOTS:
        (tmp_path / top).mkdir()
    (tmp_path / "scripts/ordinary.py").write_text("other.write_text('x')\n")
    approved = tmp_path / "scripts/framework_extensions.py"
    approved.write_text("wiki.write_text('x')\n")
    (tmp_path / "tools/broken.py").write_text("not valid python (\n")
    assert module.findings(tmp_path) == []


def test_cli_reports_success_and_failure(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(module, "findings", lambda root: [])
    assert module.main() == 0
    assert "no ungoverned" in capsys.readouterr().out
    monkeypatch.setattr(module, "findings", lambda root: ["bad.py:1"])
    assert module.main() == 1
    output = capsys.readouterr().out
    assert "must use corpus_transaction" in output and "bad.py:1" in output
