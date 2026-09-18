"""Regression: the deterministic bare-name → canonical-entity link normalizer (#153)."""
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "normalize_bare_name_links.py"


def _load():
    os.environ.setdefault("WIKI_PATH", "/nonexistent-vault")
    spec = importlib.util.spec_from_file_location("normalize_bare_name_links", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["normalize_bare_name_links"] = m
    spec.loader.exec_module(m)
    return m


def test_rewrites_bare_name_preserving_display():
    m = _load()
    idx = {"qilin": {"entities/q/qilin"}, "velvet-ant": {"entities/v/velvet-ant"}}
    valid = {"entities/q/qilin", "qilin", "entities/v/velvet-ant", "velvet-ant"}
    body, fixes = m.rewrite_text("By [[Qilin]] and [[Velvet Ant|the group]].", idx, valid)
    assert "[[entities/q/qilin]]" in body
    assert "[[entities/v/velvet-ant|the group]]" in body      # display preserved
    assert len(fixes) == 2


def test_skips_ambiguous_missing_pathform_junk_and_resolving():
    m = _load()
    idx = {"qilin": {"entities/q/qilin"}, "dup": {"entities/a/dup", "entities/b/dup"}}
    valid = {"entities/q/qilin", "qilin"}
    src = "[[Dup]] [[Nonexistent]] [[concepts/foo]] [[1]] [[qilin]]"
    body, fixes = m.rewrite_text(src, idx, valid)
    assert fixes == []           # ambiguous / missing / path-form / numeric-junk / already-resolving
    assert body == src


def test_idempotent_second_pass_is_noop():
    m = _load()
    idx = {"qilin": {"entities/q/qilin"}}
    valid = {"entities/q/qilin", "qilin"}
    once, f1 = m.rewrite_text("Hit [[Qilin]].", idx, valid)
    twice, f2 = m.rewrite_text(once, idx, valid)
    assert f1 and not f2 and once == twice


def test_build_index_supports_scalar_and_list_aliases(tmp_path):
    m = _load()
    m.VAULT = tmp_path
    m.WIKI = tmp_path / "wiki"
    m.ENT_DIR = m.WIKI / "entities"
    (m.ENT_DIR / "q").mkdir(parents=True)
    (m.ENT_DIR / "q" / "qilin.md").write_text(
        "---\nname: Qilin\naliases: Agenda, Gold Feather\n---\n")
    (m.ENT_DIR / "v").mkdir()
    (m.ENT_DIR / "v" / "velvet-ant.md").write_text(
        "---\nname: Velvet Ant\naliases:\n  - DEV-0832\n---\n")

    index, valid = m.build_index()

    assert index["agenda"] == {"entities/q/qilin"}
    assert index["dev-0832"] == {"entities/v/velvet-ant"}
    assert {"entities/q/qilin", "qilin"} <= valid


def test_main_dry_run_then_apply_preserves_frontmatter_and_writes_report(tmp_path):
    m = _load()
    m.VAULT = tmp_path
    m.WIKI = tmp_path / "wiki"
    m.ENT_DIR = m.WIKI / "entities"
    m.OUT_DIR = m.WIKI / "operational"
    (m.ENT_DIR / "q").mkdir(parents=True)
    entity = m.ENT_DIR / "q" / "qilin.md"
    entity.write_text("---\nname: Qilin\n---\n# Qilin\n")
    source = m.WIKI / "source.md"
    source.write_text("---\ntitle: '[[Qilin]] in frontmatter'\n---\nSee [[Qilin]].\n")

    m.DRY_RUN = True
    with redirect_stdout(io.StringIO()) as out:
        assert m.main() == 0
    assert "would fix 1" in out.getvalue()
    assert "See [[Qilin]]" in source.read_text()

    m.DRY_RUN = False
    with redirect_stdout(io.StringIO()):
        assert m.main() == 0
    text = source.read_text()
    assert "title: '[[Qilin]] in frontmatter'" in text
    assert "See [[entities/q/qilin]]" in text
    assert "fixed **1**" in (m.OUT_DIR / "bare-name-link-normalize.md").read_text()


def test_main_skips_cleanly_without_entity_namespace(tmp_path):
    m = _load()
    m.WIKI = tmp_path / "wiki"
    m.ENT_DIR = m.WIKI / "entities"
    with redirect_stdout(io.StringIO()) as out:
        assert m.main() == 0
    assert '"wakeAgent": false' in out.getvalue()


def test_index_and_main_tolerate_filesystem_and_frontmatter_edges(tmp_path, monkeypatch):
    m = _load()
    m.VAULT = tmp_path
    m.WIKI = tmp_path / "wiki"
    m.ENT_DIR = m.WIKI / "entities"
    m.OUT_DIR = m.WIKI / "operational"
    ent = m.ENT_DIR / "a"
    ent.mkdir(parents=True)
    bad = ent / "bad.md"
    bad.write_text("---\n[\n---\n")
    odd = ent / "odd.md"
    odd.write_text("---\nname: Odd\naliases: {bad: shape}\n---\n")
    plain = ent / "plain.md"
    plain.write_text("body")
    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda self: False if self == plain else original_is_file(self),
    )
    index, _ = m.build_index()
    assert index["odd"] == {"entities/a/odd"}
    monkeypatch.setattr(Path, "is_file", original_is_file)

    hidden = m.WIKI / ".hidden.md"
    hidden.write_text("[[Odd]]")
    m.OUT_DIR.mkdir()
    unreadable = m.WIKI / "unreadable.md"
    unreadable.write_text("[[Odd]]")
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == unreadable else original_read(self, *a, **k),
    )
    m.DRY_RUN = False
    original_write = Path.write_text
    monkeypatch.setattr(
        Path, "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("readonly"))
        if self.name == "bare-name-link-normalize.md" else original_write(self, *a, **k),
    )
    assert m.main() == 0


def test_split_body_without_frontmatter_and_sample_limit(tmp_path):
    m = _load()
    assert m._split_body("plain") == ("", "plain")
    m.WIKI = tmp_path / "wiki"
    m.ENT_DIR = m.WIKI / "entities"
    m.OUT_DIR = m.WIKI / "operational"
    (m.ENT_DIR / "a").mkdir(parents=True)
    (m.ENT_DIR / "a/a.md").write_text("---\nname: Alpha\n---\n")
    for i in range(14):
        (m.WIKI / f"p{i}.md").write_text("[[Alpha]]")
    m.DRY_RUN = True
    with redirect_stdout(io.StringIO()) as out:
        assert m.main() == 0
    assert out.getvalue().count("-> [[entities/a/a]]") == 12
