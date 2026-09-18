import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("bench_vault_test", REPO / "scripts" / "bench_vault.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_generate_vault_creates_sharded_entities_sources_and_review_markers(tmp_path):
    module = _load()
    module.gen_vault(tmp_path, 51)
    pages = list((tmp_path / "wiki").rglob("*.md"))
    assert len(pages) == 57
    first = tmp_path / "wiki" / "entities" / "0" / "entity-000000.md"
    last = tmp_path / "wiki" / "entities" / "0" / "entity-000050.md"
    assert "needs_review: true" in first.read_text()
    assert "needs_review: true" in last.read_text()
    assert "Prose citation only" in first.read_text()


def test_time_returns_elapsed_duration(monkeypatch):
    module = _load()
    ticks = iter([10.0, 10.25])
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(ticks))
    called = []
    assert module._time("ignored", lambda: called.append(True)) == 0.25
    assert called == [True]


def test_dynamic_loader_executes_requested_module(tmp_path, monkeypatch):
    module = _load()
    (tmp_path / "sample.py").write_text("answer = 42\n", encoding="utf-8")
    monkeypatch.setattr(module, "_HERE", tmp_path)
    loaded = module._load("bench_sample", "sample.py")
    assert loaded.answer == 42
    assert sys.modules["bench_sample"] is loaded


def test_main_uses_defaults_reports_each_operation_and_cleans_tempdir(tmp_path, monkeypatch):
    module = _load()
    root = tmp_path / "bench"
    root.mkdir()
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(root))
    monkeypatch.setattr(module, "_time", lambda label, fn: (fn(), 0.1)[1])

    class Audit:
        @staticmethod
        def main():
            print("audit noise")
            return 0

    monkeypatch.setattr(module, "_load", lambda name, rel: Audit)
    with redirect_stdout(io.StringIO()) as output:
        assert module.main(["3", "--target", "12"]) == 0
    text = output.getvalue()
    assert "generated 4 pages" in text
    assert "conformance_audit" in text and "grounding_audit" in text and "review_queue" in text
    assert "proj@12" in text
    assert not root.exists()


def test_main_reports_operation_error_and_still_cleans(tmp_path, monkeypatch):
    module = _load()
    root = tmp_path / "bench"
    root.mkdir()
    monkeypatch.setattr("tempfile.mkdtemp", lambda prefix: str(root))
    def load(name, rel):
        if name == "grounding_audit":
            raise RuntimeError("broken audit")
        return type("Audit", (), {"main": staticmethod(lambda: 0)})

    monkeypatch.setattr(module, "_load", load)
    with redirect_stdout(io.StringIO()) as output:
        assert module.main(["0"]) == 0
    assert "grounding_audit        ERROR: broken audit" in output.getvalue()
    assert not root.exists()
