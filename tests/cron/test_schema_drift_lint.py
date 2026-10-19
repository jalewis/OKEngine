"""schema_drift_lint: the corpus-wide conformance monitor, run in-process against disposable vaults.

The monitor runs the SAME validator the write guard uses across a whole vault and renders
``wiki/operational/schema-conformance.md``. These tests pin what the operator reads off that page:
which pages count, how each violation is classified, and when the optional ratchet alarms.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "schema_drift_lint.py"
_SEQ = itertools.count()


def _load(monkeypatch, vault: Path):
    """A fresh module instance whose vault constants point at ``vault``."""
    spec = importlib.util.spec_from_file_location(f"schema_drift_lint_t{next(_SEQ)}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "VAULT", vault)
    monkeypatch.setattr(module, "WIKI", vault / "wiki")
    monkeypatch.setattr(module, "REPORT", vault / "wiki" / "operational" / "schema-conformance.md")
    monkeypatch.delenv("SCHEMA_DRIFT_BASELINE", raising=False)
    return module


def _vault(tmp_path: Path, schema: str = "types:\n  entity: {required: [type, owner]}\n") -> Path:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "schema.yaml").write_text(schema, encoding="utf-8")
    return vault


def _page(vault: Path, rel: str, text: str) -> Path:
    path = vault / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _report(vault: Path) -> str:
    return (vault / "wiki" / "operational" / "schema-conformance.md").read_text(encoding="utf-8")


def _stdout_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_missing_wiki_dir_is_an_error_exit(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    module = _load(monkeypatch, vault)

    rc = module.main()

    assert rc == 1
    assert "wiki dir not found" in capsys.readouterr().err


def test_conformant_vault_reports_full_conformance_and_never_wakes(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    _page(vault, "entities/acme.md", '---\nid: "entities:acme"\ntype: entity\nowner: ops\n---\nbody\n')
    module = _load(monkeypatch, vault)

    rc = module.main()

    assert rc == 0
    assert _stdout_json(capsys) == {"wakeAgent": False}
    report = _report(vault)
    assert "- **In-scope pages scanned:** 1" in report
    assert "- **Non-conformant:** 0" in report
    assert "- **Conformance:** 100.00%" in report
    assert "## Sample" not in report


def test_empty_wiki_counts_as_fully_conformant(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    module = _load(monkeypatch, vault)

    rc = module.main()

    assert rc == 0
    assert "- **Conformance:** 100.00%" in _report(vault)


def test_backup_and_scratch_artifacts_are_not_counted(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    _page(vault, "entities/acme.md.bak.md", "no frontmatter\n")
    _page(vault, "entities/_test_page.md", "no frontmatter\n")
    _page(vault, "_archive/old.md", "no frontmatter\n")
    _page(vault, "entities/good.md", '---\nid: "entities:good"\ntype: entity\nowner: ops\n---\n')
    module = _load(monkeypatch, vault)

    module.main()

    report = _report(vault)
    assert "- **In-scope pages scanned:** 1" in report
    assert "- **Non-conformant:** 0" in report


def test_an_unreadable_md_entry_is_skipped_not_fatal(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    (vault / "wiki" / "entities" / "dir-named.md").mkdir(parents=True)
    _page(vault, "entities/good.md", '---\nid: "entities:good"\ntype: entity\nowner: ops\n---\n')
    module = _load(monkeypatch, vault)

    rc = module.main()

    assert rc == 0
    assert "- **In-scope pages scanned:** 1" in _report(vault)


def test_each_violation_class_is_bucketed_and_counted(tmp_path, monkeypatch):
    vault = _vault(tmp_path, "types:\n  entity: {required: [type, owner, sector]}\nstrict_types: true\n")
    _page(vault, "entities/nofm.md", "just prose\n")
    _page(vault, "entities/typeless.md", '---\nid: "entities:typeless"\ntitle: T\n---\n')
    _page(vault, "entities/thin.md", '---\nid: "entities:thin"\ntype: entity\n---\n')
    _page(vault, "entities/broken.md", '---\nid: [unclosed\n---\n')
    _page(vault, "entities/listy.md", "---\n- a\n- b\n---\n")
    _page(vault, "entities/unknown.md", '---\nid: "entities:unknown"\ntype: nonsense\n---\n')
    module = _load(monkeypatch, vault)

    module.main()

    report = _report(vault)
    assert "| no-frontmatter | 1 |" in report
    assert "| typeless (no type:) | 1 |" in report
    assert "| missing required field(s) | 1 |" in report
    assert "| broken frontmatter | 2 |" in report
    assert "| other | 1 |" in report
    assert "- **Non-conformant:** 6" in report


def test_a_page_missing_only_its_id_is_not_reported_as_typeless(tmp_path, monkeypatch):
    # Regression: `id` joined `type` on the OKF required floor, so "missing required field(s): id"
    # was bucketed "typeless (no type:)" although the page declares its type.
    vault = _vault(tmp_path)
    _page(vault, "entities/noid.md", "---\ntype: entity\nowner: ops\n---\n")
    module = _load(monkeypatch, vault)

    module.main()

    report = _report(vault)
    assert "typeless" not in report
    assert "| missing OKF-required field(s) | 1 |" in report


def test_missing_type_fields_are_tallied_by_type_and_by_field(tmp_path, monkeypatch):
    vault = _vault(tmp_path, "types:\n  entity: {required: [type, owner, sector]}\n")
    _page(vault, "entities/a.md", '---\nid: "entities:a"\ntype: entity\n---\n')
    _page(vault, "entities/b.md", '---\nid: "entities:b"\ntype: entity\nowner: x\n---\n')
    module = _load(monkeypatch, vault)

    module.main()

    report = _report(vault)
    assert "## Missing-required-field pages by type" in report
    assert "| entity | 2 |" in report
    assert "| sector | 2 |" in report
    assert "| owner | 1 |" in report


def test_the_sample_lists_at_most_forty_pages(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    for i in range(41):
        _page(vault, f"entities/p{i:02d}.md", "no frontmatter\n")
    module = _load(monkeypatch, vault)

    module.main()

    sample = _report(vault).split("## Sample (first 40)", 1)[1]
    assert sample.count("\n- `wiki/entities/") == 40


def test_regression_past_the_baseline_fails_the_run(tmp_path, monkeypatch, capsys):
    vault = _vault(tmp_path)
    _page(vault, "entities/a.md", "no frontmatter\n")
    _page(vault, "entities/b.md", "no frontmatter\n")
    module = _load(monkeypatch, vault)
    monkeypatch.setenv("SCHEMA_DRIFT_BASELINE", "1")

    rc = module.main()

    assert rc == 1
    captured = capsys.readouterr()
    assert "exceeds baseline 1" in captured.err
    assert json.loads(captured.out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_a_count_at_the_baseline_passes(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    _page(vault, "entities/a.md", "no frontmatter\n")
    module = _load(monkeypatch, vault)
    monkeypatch.setenv("SCHEMA_DRIFT_BASELINE", "1")

    assert module.main() == 0


def test_a_non_numeric_baseline_is_ignored(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    _page(vault, "entities/a.md", "no frontmatter\n")
    module = _load(monkeypatch, vault)
    monkeypatch.setenv("SCHEMA_DRIFT_BASELINE", "none")

    assert module.main() == 0
