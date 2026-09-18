from __future__ import annotations

import importlib.util
import os
import runpy
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "rebuild_index.py"


def _load(vault: Path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    spec = importlib.util.spec_from_file_location(f"rebuild_index_{id(vault)}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(path: Path, fm: str = "", body: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\n{body}")


def test_namespace_fallback_parse_titles_and_missing_dirs(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m.schema_lib, "knowledge_namespaces", lambda _s: {"declared"})
    assert m.knowledge_namespaces() == ["declared"]
    monkeypatch.setattr(m.schema_lib, "knowledge_namespaces", lambda _s: [])
    monkeypatch.setattr(m.schema_lib, "excluded_dirs", lambda _s: {"excluded"})
    wiki = tmp_path / "wiki"
    assert m.knowledge_namespaces() == []
    (wiki / ".hidden").mkdir(parents=True)
    (wiki / "_private").mkdir()
    (wiki / "excluded").mkdir()
    (wiki / "empty").mkdir()
    _page(wiki / "custom" / "a.md", "type: note\n", "# [[entities/a/acme|Acme]] *News*\n")
    assert m.knowledge_namespaces() == ["custom"]
    assert m.list_dir("missing") == []
    assert m.group_by_type("missing") == {}
    assert m.short_title("slug", None) == "slug"
    assert m.short_title("slug", "***") == "slug"
    assert m.short_title("slug", "[[entities/a/acme|ignored]] `News`") == "acme News"

    page = wiki / "custom" / "a.md"
    fm, title = m.parse_page(page)
    assert fm["type"] == "note" and title.startswith("[[")
    page.write_text("plain\n# Heading\n")
    assert m.parse_page(page) == (None, "Heading")
    page.write_text("---\n[\n---\n# H\n")
    assert m.parse_page(page) == (None, "H")
    page.write_text("---\n- one\n---\n")
    assert m.parse_page(page) == (None, None)
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert m.parse_page(page) == (None, None)


def test_list_and_group_filter_operational_untyped_and_shards(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    root = tmp_path / "wiki" / "entities"
    _page(root / "_skip.md", "type: actor\n", "# Skip\n")
    _page(root / "a" / "actor.md", "type: actor\n", "# Actor\n")
    _page(root / "b" / "operational.md", "type: dashboard\n", "# Dash\n")
    _page(root / "c" / "untyped.md", "", "")
    assert m.is_operational({"type": "dashboard"}) and not m.is_operational(None)
    lines = m.list_dir("entities", limit=1, sort_recent=True)
    assert len(lines) <= 1
    assert all("operational" not in line for line in m.list_dir("entities"))
    groups = m.group_by_type("entities")
    assert set(groups) == {"actor", "untyped"}
    assert "[[entities/a/actor]] — Actor" in groups["actor"][0]


def test_render_every_namespace_mode_drift_empty_and_lint(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    wiki = tmp_path / "wiki"
    (wiki / "sources").mkdir(parents=True)
    (wiki / "entities").mkdir()
    (wiki / "questions").mkdir()
    _page(wiki / "sources" / "2026-01-01-a.md", "type: source\n", "# A\n")
    _page(wiki / "entities" / "a.md", "type: actor\n", "# A\n")
    _page(wiki / "entities" / "z.md", "type: drift\n", "# Z\n")
    _page(wiki / "lint-2026.md", "type: report\n", "# Lint\n")
    monkeypatch.setattr(m, "knowledge_namespaces",
                        lambda: ["sources", "entities", "questions"])
    monkeypatch.setattr(m.schema_lib, "canonical_types", lambda _s: {"actor", "missing"})
    body = m.render_index()
    assert "Sources (most recent 30)" in body
    assert "### actor (1)" in body and "### drift (1) — DRIFT" in body
    assert "## Questions (0)" in body and "_(none)_" in body
    assert "Latest weekly audit" in body

    monkeypatch.setattr(m, "knowledge_namespaces", lambda: [])
    body = m.render_index()
    assert "no knowledge namespaces found" in body


def test_main_missing_dry_write_backup_and_entrypoint(tmp_path, monkeypatch, capsys):
    missing = _load(tmp_path / "missing", monkeypatch)
    monkeypatch.setattr(sys, "argv", ["rebuild_index"])
    assert missing.main() == 1
    assert "vault not found" in capsys.readouterr().err

    wiki = tmp_path / "vault" / "wiki"
    wiki.mkdir(parents=True)
    m = _load(tmp_path / "vault", monkeypatch)
    monkeypatch.setattr(m, "render_index", lambda: "# Index\n")
    monkeypatch.setattr(sys, "argv", ["rebuild_index", "--dry-run"])
    assert m.main() == 0
    assert capsys.readouterr().out == "# Index\n"

    monkeypatch.setattr(sys, "argv", ["rebuild_index"])
    assert m.main() == 0
    assert not m.INDEX.with_name("index.md.bak").exists()
    capsys.readouterr()

    m.INDEX.write_text("old")
    monkeypatch.setattr(sys, "argv", ["rebuild_index"])
    assert m.main() == 0
    assert m.INDEX.read_text() == "# Index\n"
    assert m.INDEX.with_name("index.md.bak").read_text() == "old"
    assert '"wakeAgent": false' in capsys.readouterr().out

    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
