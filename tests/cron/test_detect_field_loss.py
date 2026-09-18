from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "detect_field_loss.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_detect_reports_protected_field_and_source_link_loss(tmp_path, monkeypatch):
    module = _load("detect_field_loss_missing_fields")
    current = tmp_path / "wiki" / "entities" / "a" / "acme.md"
    current.parent.mkdir(parents=True)
    current.write_text("---\ntype: entity\nsources: []\n---\n# Acme\n", encoding="utf-8")
    old = "---\ntype: entity\nconfidence: high\nsources: ['[[sources/2026/report]]']\n---\n# Acme\n"

    module.VAULT = tmp_path
    module.GUARDED = ["wiki/entities"]
    module.CURATED_FIELDS = {"confidence", "sources"}

    def fake_git(*args):
        if args[:2] == ("rev-list", "-1"):
            return "base\n"
        if args[0] == "diff":
            return "wiki/entities/a/acme.md\n"
        if args[0] == "show":
            return old
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git", fake_git)
    losses, baseline = module.detect()

    assert baseline == "base"
    assert losses == [{
        "file": "wiki/entities/a/acme.md",
        "lost": ["confidence", "sources(-1)"],
    }]


def test_detect_does_not_report_changed_or_unprotected_values(tmp_path, monkeypatch):
    module = _load("detect_field_loss_changed_values")
    current = tmp_path / "wiki" / "entities" / "a" / "acme.md"
    current.parent.mkdir(parents=True)
    current.write_text("---\ntype: entity\nconfidence: low\nsources: []\n---\n", encoding="utf-8")
    old = "---\ntype: entity\nconfidence: high\nsources: ['[[sources/2026/report]]']\n---\n"
    module.VAULT = tmp_path
    module.GUARDED = ["wiki/entities"]
    module.CURATED_FIELDS = {"confidence"}
    monkeypatch.setattr(module, "_git", lambda *args: (
        "base\n" if args[:2] == ("rev-list", "-1") else
        "wiki/entities/a/acme.md\n" if args[0] == "diff" else old
    ))

    assert module.detect() == ([], "base")


def test_detect_does_not_report_when_protected_source_links_are_retained(tmp_path, monkeypatch):
    module = _load("detect_field_loss_retained_sources")
    current = tmp_path / "wiki" / "entities" / "a" / "acme.md"
    current.parent.mkdir(parents=True)
    page = "---\ntype: entity\nsources: ['[[sources/report]]']\n---\n"
    current.write_text(page, encoding="utf-8")
    module.VAULT = tmp_path
    module.GUARDED = ["wiki/entities"]
    module.CURATED_FIELDS = {"sources"}
    monkeypatch.setattr(module, "_git", lambda *args: (
        "base\n" if args[:2] == ("rev-list", "-1") else
        "wiki/entities/a/acme.md\n" if args[0] == "diff" else page
    ))

    assert module.detect() == ([], "base")


def test_guarded_namespaces_fall_back_to_real_pack_content(tmp_path, monkeypatch):
    module = _load("detect_field_loss_guarded_fallback")
    wiki = tmp_path / "wiki"
    for namespace in ("entities", "operational", ".hidden", "_private"):
        path = wiki / namespace / "page.md"
        path.parent.mkdir(parents=True)
        path.write_text("# Page\n", encoding="utf-8")
    (wiki / "empty").mkdir()
    module.VAULT = tmp_path
    module._SCHEMA = {}
    monkeypatch.setattr(module.schema_lib, "knowledge_namespaces", lambda _schema: set())
    monkeypatch.setattr(module.schema_lib, "excluded_dirs", lambda _schema: {"operational"})

    assert module._guarded() == ["wiki/entities"]

    module.VAULT = tmp_path / "missing"
    assert module._guarded() == []


def test_guarded_namespaces_use_declared_schema_without_scanning_wiki(tmp_path, monkeypatch):
    module = _load("detect_field_loss_guarded_declared")
    module.VAULT = tmp_path
    module._SCHEMA = {"knowledge_namespaces": ["entities"]}
    monkeypatch.setattr(
        module.schema_lib,
        "knowledge_namespaces",
        lambda schema: {"sources", "entities"},
    )
    monkeypatch.setattr(
        module.schema_lib,
        "excluded_dirs",
        lambda _schema: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )

    assert module._guarded() == ["wiki/entities", "wiki/sources"]


def test_git_and_frontmatter_helpers_fail_closed(monkeypatch):
    module = _load("detect_field_loss_helpers")

    def successful_run(args, *, capture_output, text, check):
        assert args[:2] == ["git", "-C"]
        assert capture_output and text and check
        return SimpleNamespace(stdout="result\n")

    monkeypatch.setattr(module.subprocess, "run", successful_run)
    assert module._git("status") == "result\n"

    def failed_run(args, *, capture_output, text, check):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(module.subprocess, "run", failed_run)
    assert module._git("status") == ""
    assert module._fm("body only") is None
    assert module._fm("---\nvalue: [\n---\n") is None
    assert module._fm("---\n- scalar\n---\n") is None
    assert module._source_links({"basis": ["[[sources/report]]"], "sources": "scalar"}) == {
        "[[sources/report]]"
    }


def test_detect_uses_root_fallback_and_handles_unreliable_pages(tmp_path, monkeypatch):
    module = _load("detect_field_loss_root_fallback")
    module.VAULT = tmp_path
    module.GUARDED = ["wiki/entities"]
    module.CURATED_FIELDS = {"confidence"}
    invalid_current = tmp_path / "wiki/entities/i/invalid.md"
    invalid_current.parent.mkdir(parents=True)
    invalid_current.write_text("not frontmatter\n", encoding="utf-8")

    changed = "\n".join([
        "_private.md",
        "wiki/entities/_archive/old.md",
        "wiki/entities/b/broken-old.md",
        "wiki/entities/d/deleted.md",
        "wiki/entities/i/invalid.md",
    ])

    def fake_git(*args):
        if args[:2] == ("rev-list", "-1"):
            return ""
        if args[:2] == ("rev-list", "--max-parents=0"):
            return "root\n"
        if args[0] == "diff":
            return changed
        if args[0] == "show" and args[1].endswith("broken-old.md"):
            return "not frontmatter"
        if args[0] == "show":
            return "---\nconfidence: high\n---\n"
        raise AssertionError(args)

    monkeypatch.setattr(module, "_git", fake_git)
    assert module.detect() == ([{
        "file": "wiki/entities/d/deleted.md",
        "lost": ["<file deleted>"],
    }], "root")

    monkeypatch.setattr(module, "_git", lambda *_args: "")
    assert module.detect() == ([], None)


def test_write_report_records_loss_and_replaces_same_day_snapshot(tmp_path):
    module = _load("detect_field_loss_report")
    module.VAULT = tmp_path
    module.OP_DIR = tmp_path / "wiki" / "operational"
    module.SNAP = module.OP_DIR / "field-loss-snapshots.md"
    module.WINDOW_DAYS = 3
    losses = [
        {"file": "wiki/entities/z/zeta.md", "lost": ["confidence"]},
        {"file": "wiki/entities/a/acme.md", "lost": ["sources(-1)"]},
    ]

    module.write_report(losses, "abcdef123456", "2026-09-13")
    report = (module.OP_DIR / "field-loss-2026-09-13.md").read_text(encoding="utf-8")
    assert "**2 page(s) lost curated fields:**" in report
    assert report.index("[[entities/a/acme]]") < report.index("[[entities/z/zeta]]")
    assert "baseline `abcdef12`" in report

    module.write_report([], None, "2026-09-13")
    report = (module.OP_DIR / "field-loss-2026-09-13.md").read_text(encoding="utf-8")
    assert "No curated-field losses detected" in report
    snapshot = module.SNAP.read_text(encoding="utf-8")
    assert snapshot.count("| 2026-09-13 |") == 1
    assert "| 2026-09-13 | 0 |" in snapshot


def test_main_writes_and_announces_detected_losses(monkeypatch, capsys):
    module = _load("detect_field_loss_main")
    captured = {}
    losses = [{"file": "wiki/entities/a/acme.md", "lost": ["confidence"]}]
    monkeypatch.setattr(module, "detect", lambda: (losses, "abcdef123456"))

    def capture_report(found, baseline, today):
        captured.update(found=found, baseline=baseline, today=today)

    monkeypatch.setattr(module, "write_report", capture_report)

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "=== detect-field-loss ===" in output
    assert "pages with lost curated fields: 1" in output
    assert "wiki/entities/a/acme.md: confidence" in output
    assert '"wakeAgent": false' in output
    assert captured["found"] == losses
    assert captured["baseline"] == "abcdef123456"
    assert captured["today"]
