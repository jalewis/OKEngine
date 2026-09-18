"""dedup-partition-collisions link-rewrite count must reflect ACTUAL rewrites, not re.subn's
match count.

Regression: make_rewriter's repl returns any link NOT in move_map unchanged, but re.subn counts
every `[[ns/…]]` match as a substitution — so the tally added untouched links too. It reported
~19,675 "links rewritten" for 2 entities when only 6 actually changed.
"""
import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "dedup_partition_collisions", REPO / "scripts/cron/dedup_partition_collisions.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["dedup_partition_collisions"] = m
    spec.loader.exec_module(m)
    return m


def test_rewrite_links_counts_only_actual_rewrites(tmp_path):
    m = _load()
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    # one file with FOUR [[entities/…]] links; only ONE points at a dropped (move_map) path
    (wiki / "entities" / "ref.md").write_text(
        "See [[entities/8/8220-gang]] and [[entities/0-9/7-zip]] and "
        "[[entities/a/apt29]] and [[entities/m/mirai]].\n", encoding="utf-8")
    move_map = {"entities/8/8220-gang": "entities/0-9/8220-gang"}       # exactly one real rewrite
    n = m._rewrite_links(tmp_path, "entities", move_map, apply=True)
    assert n == 1, f"expected 1 actual rewrite, got {n} (subn over-count regressed)"
    txt = (wiki / "entities" / "ref.md").read_text()
    assert "[[entities/0-9/8220-gang]]" in txt                          # the one rewrite applied
    assert "[[entities/a/apt29]]" in txt and "[[entities/m/mirai]]" in txt   # non-targets untouched


def test_rewrite_links_zero_when_no_matches(tmp_path):
    m = _load()
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "entities" / "ref.md").write_text("[[entities/a/apt29]] only.\n", encoding="utf-8")
    n = m._rewrite_links(tmp_path, "entities", {"entities/8/8220-gang": "entities/0-9/8220-gang"}, apply=True)
    assert n == 0                                                        # nothing pointed at the dropped path


def test_merge_frontmatter_unions_lists_case_insensitively_and_keeps_first_scalar():
    m = _load()
    merged = m._merge_fm([
        {"type": "actor", "name": "First", "aliases": ["Qilin"], "empty": ""},
        {"name": "Second", "aliases": ["qilin", "Agenda"], "tags": ["ransomware"]},
    ])
    assert merged == {
        "type": "actor",
        "name": "First",
        "aliases": ["Qilin", "Agenda"],
        "tags": ["ransomware"],
    }


def test_namespaces_reads_root_and_subdomain_partition_schemas(tmp_path):
    m = _load()
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities:\n      strategy: by-letter\n"
        "    flat-pages:\n      strategy: flat\n")
    sub = tmp_path / "wiki" / "intel"
    sub.mkdir(parents=True)
    (sub / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    reports:\n      strategy: by-date\n")
    assert m._namespaces(tmp_path, None) == ["entities", "intel/reports"]
    assert m._namespaces(tmp_path, "chosen") == ["chosen"]


def test_main_dry_run_then_apply_merges_deletes_rewrites_and_is_idempotent(tmp_path):
    m = _load()
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities:\n      strategy: by-letter\n")
    entities = tmp_path / "wiki" / "entities"
    (entities / "q").mkdir(parents=True)
    flat = entities / "qilin.md"
    shard = entities / "q" / "qilin.md"
    flat.write_text("---\ntype: actor\naliases: [Agenda]\n---\n\nShort.\n")
    shard.write_text("---\ntype: actor\naliases: [Gold Feather]\n---\n\nA materially longer body.\n")
    ref = tmp_path / "wiki" / "ref.md"
    ref.write_text("See [[entities/qilin]].\n")

    with redirect_stdout(io.StringIO()) as out:
        assert m.main(["--root", str(tmp_path)]) == 0
    assert "DRY-RUN" in out.getvalue()
    assert flat.exists() and "[[entities/qilin]]" in ref.read_text()

    with redirect_stdout(io.StringIO()) as out:
        assert m.main(["--root", str(tmp_path), "--apply"]) == 0
    assert not flat.exists()
    merged = shard.read_text()
    assert "Agenda" in merged and "Gold Feather" in merged
    assert "needs_review: true" in merged
    assert "A materially longer body." in merged
    assert "[[entities/q/qilin]]" in ref.read_text()
    assert "1 removed" in out.getvalue()

    with redirect_stdout(io.StringIO()) as out:
        assert m.main(["--root", str(tmp_path), "--apply"]) == 0
    assert "0 duplicate copy(ies)" in out.getvalue()


def test_read_namespace_and_missing_content_edges(tmp_path):
    m = _load()
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    assert m._namespaces(empty_root, None) == []
    plain = tmp_path / "plain.md"
    plain.write_text("body")
    assert m._read(plain) == ({}, "")
    malformed = tmp_path / "bad.md"
    malformed.write_text("---\n[\n---\nbody")
    assert m._read(malformed) == ({}, "body")
    assert m.dedup_namespace(tmp_path, "entities", apply=True) == ({}, [], 0)

    (tmp_path / "schema.yaml").write_text("[")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    ordinary = wiki / "ordinary"
    ordinary.mkdir()
    bad_domain = wiki / "bad-domain"
    bad_domain.mkdir()
    (bad_domain / "schema.yaml").write_text("[")
    assert m._namespaces(tmp_path, None) == []


def test_generated_pages_unlink_and_link_io_failures(tmp_path, monkeypatch, capsys):
    m = _load()
    root = tmp_path
    base = root / "wiki/entities"
    (base / "a").mkdir(parents=True)
    for name in ("INDEX.md", "_generated.md"):
        (base / name).write_text("ignored")
        (base / "a" / name).write_text("ignored")
    for path in (base / "same.md", base / "a/same.md"):
        path.write_text("---\ntype: actor\n---\nsame body\n")
    monkeypatch.setattr(m.okf_migrate, "write_key", lambda *_a: "entities/a/same")
    original_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("denied"))
        if self == base / "same.md" else original_unlink(self, *a, **k),
    )
    move_map, review, removed = m.dedup_namespace(root, "entities", apply=True)
    assert move_map and review == [] and removed == 0
    assert "remove failed" in capsys.readouterr().err

    git = root / "wiki/.git/ref.md"
    git.parent.mkdir()
    git.write_text("[[entities/same]]")
    unreadable = root / "wiki/unreadable.md"
    unreadable.write_text("[[entities/same]]")
    write_fail = root / "wiki/write-fail.md"
    write_fail.write_text("[[entities/same]]")
    original_read = Path.read_text
    original_write = Path.write_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == unreadable else original_read(self, *a, **k),
    )
    monkeypatch.setattr(
        Path, "write_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("readonly"))
        if self == write_fail else original_write(self, *a, **k),
    )
    assert m._rewrite_links(root, "entities", move_map, apply=True) >= 1
    assert "link rewrite failed" in capsys.readouterr().err
    # Dry-run counts the same rewrite without entering the writer.
    assert m._rewrite_links(root, "entities", move_map, apply=False) >= 1
