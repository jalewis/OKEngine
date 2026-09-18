import importlib.util
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


def _load(root):
    cron = REPO / "scripts" / "cron"
    sys.path.insert(0, str(cron))
    spec = importlib.util.spec_from_file_location(
        "reshard_oversized_runtime", cron / "reshard_oversized.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    module.VAULT = root
    module.WIKI = root / "wiki"
    return module


def test_frontmatter_reader_fails_closed(tmp_path, monkeypatch):
    module = _load(tmp_path)
    missing = tmp_path / "missing.md"
    assert module._fm(missing) == {}
    malformed = tmp_path / "bad.md"
    malformed.write_text("---\ntype: [\n---\n")
    assert module._fm(malformed) == {}


def test_discovers_root_and_subdomain_reshard_configuration(tmp_path):
    module = _load(tmp_path)
    module.WIKI.mkdir()
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  reshard_over: 4\n  namespaces:\n"
        "    entities: {reshard_by: second-letter}\n"
        "    ignored: {reshard_by: not-applicable}\n")
    sub = module.WIKI / "guest"
    sub.mkdir()
    (sub / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    sources: {reshard_by: day}\n")
    assert module._reshardable_namespaces() == [
        ("entities", "second-letter", 4),
        ("sources", "day", 4),
        ("concepts", "second-letter", 4),
        ("guest/sources", "day", 500),
    ]


def test_unknown_reshard_strategy_fails_loudly(tmp_path):
    module = _load(tmp_path)
    module.WIKI.mkdir()
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {reshard_by: year}\n"
    )

    with pytest.raises(ValueError, match="unsupported.*year"):
        module._reshardable_namespaces()


def test_root_schema_discovery_does_not_require_a_wiki_directory(tmp_path):
    module = _load(tmp_path)
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {reshard_by: second-letter}\n"
    )

    assert module._reshardable_namespaces() == [
        ("entities", "second-letter", 500),
        ("sources", "day", 500),
        ("concepts", "second-letter", 500),
    ]


def test_apply_dry_run_move_failure_and_main_idempotency(tmp_path, monkeypatch):
    module = _load(tmp_path)
    bucket = module.WIKI / "entities" / "a"
    (bucket / "p").mkdir(parents=True)
    (bucket / "apt.md").write_text("---\ntype: actor\n---\n")
    assert module._apply("entities", "second-letter", 500, apply=False) == 0
    assert (bucket / "apt.md").exists()

    monkeypatch.setattr(module.os, "rename",
                        lambda *_a: (_ for _ in ()).throw(OSError("denied")))
    with redirect_stderr(io.StringIO()) as error:
        assert module._apply("entities", "second-letter", 500, apply=True) == 0
    assert "denied" in error.getvalue()

    monkeypatch.setattr(module, "_reshardable_namespaces",
                        lambda: [("entities", "second-letter", 500)])
    monkeypatch.setattr(module, "_apply", lambda ns, rb, n, apply: 2)
    with redirect_stdout(io.StringIO()) as output:
        assert module.main(["--dry-run"]) == 0
    assert "2 files (dry-run)" in output.getvalue()


def test_schema_discovery_oversized_and_apply_filesystem_edges(tmp_path, monkeypatch):
    module = _load(tmp_path)
    module.WIKI.mkdir()
    plain = tmp_path / "plain.md"
    plain.write_text("body")
    assert module._fm(plain) == {}

    # No root schema, a malformed subdomain schema, and an ordinary directory.
    bad = module.WIKI / "bad"
    bad.mkdir()
    (bad / "schema.yaml").write_text("[")
    ordinary = module.WIKI / "ordinary"
    ordinary.mkdir()
    (module.WIKI / "ordinary-file").write_text("not a namespace")
    assert module._reshardable_namespaces() == []

    # A glob match that is not a directory is ignored.
    entities = module.WIKI / "entities"
    entities.mkdir()
    (entities / "not-a-dir").write_text("x")
    assert list(module._oversized("entities/*", 0)) == []
    assert module._apply("entities", "second-letter", 500, apply=True) == 0

    bucket = entities / "a"
    bucket.mkdir()
    source = bucket / "alpha.md"
    source.write_text("entities/a/alpha")
    git_page = module.WIKI / ".git" / "ignored.md"
    git_page.parent.mkdir()
    git_page.write_text("entities/a/alpha")
    unreadable = module.WIKI / "unreadable.md"
    unreadable.write_text("entities/a/alpha")
    write_fail = module.WIKI / "write-fail.md"
    write_fail.write_text("entities/a/alpha")
    clean = module.WIKI / "clean.md"
    clean.write_text("entities/other/value")
    monkeypatch.setattr(
        module, "_build_map",
        lambda *_a: {"entities/a/alpha": "entities/a/l/alpha"},
    )
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
    assert module._apply("entities", "second-letter", 500, apply=True) == 1
    assert (module.WIKI / "entities/a/l/alpha.md").is_file()


def test_main_apply_with_no_targets(tmp_path, monkeypatch):
    module = _load(tmp_path)
    monkeypatch.setattr(module, "_reshardable_namespaces", lambda: [])
    with redirect_stdout(io.StringIO()) as output:
        assert module.main([]) == 0
    assert "0 files resharded" in output.getvalue()
