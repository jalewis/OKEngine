import importlib.util
import re
import runpy
import sys
from pathlib import Path

import pytest
REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "repair_malformed_slugs.py"


def _load():
    spec = importlib.util.spec_from_file_location("repair_malformed_slugs", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def test_repairs_slug_and_exact_wikilinks(tmp_path):
    mod = _load()
    wiki = tmp_path / "wiki"
    old = wiki / "entities" / "a" / "this is malformed.md"
    old.parent.mkdir(parents=True)
    old.write_text("---\ntype: entity\nname: Clean Actor\n---\nbody\n")
    ref = wiki / "briefings" / "b.md"
    ref.parent.mkdir()
    ref.write_text("[[entities/a/this is malformed]]\n")
    (ref.parent / "unrelated.md").write_text("no matching link\n")

    planned, errors = mod.repair(tmp_path, apply=False)
    assert errors == []
    assert planned == [("entities/a/this is malformed", "entities/a/clean-actor")]
    assert old.exists()

    moved, errors = mod.repair(tmp_path, apply=True)
    assert errors == []
    assert moved == planned
    assert not old.exists()
    assert (wiki / "entities" / "a" / "clean-actor.md").exists()
    assert "[[entities/a/clean-actor]]" in ref.read_text()


def test_helpers_long_slug_and_non_entities_are_ignored(tmp_path):
    mod = _load()
    assert mod.malformed(Path("has space.md"))
    assert not mod.malformed(Path("good.md"))
    long = "a" * 100
    slug = mod.bounded_slug(long)
    assert len(slug) <= mod.MAX_ENTITY_SLUG_LEN and slug != long
    assert mod.bounded_slug("Simple Name") == "simple-name"
    wiki = tmp_path / "wiki"
    (wiki / "sources").mkdir(parents=True)
    (wiki / "sources" / "bad name.md").write_text("x")
    assert mod.repair(tmp_path) == ([], [])


def test_repair_yaml_fallback_collision_and_same_target(tmp_path, monkeypatch):
    mod = _load()
    wiki = tmp_path / "wiki" / "entities" / "a"
    wiki.mkdir(parents=True)
    bad = wiki / "bad yaml.md"
    bad.write_text("---\n[\n---\nbody")
    collision = wiki / "bad-yaml.md"
    collision.write_text("existing")
    moves, errors = mod.repair(tmp_path)
    assert moves == [] and "collision" in errors[0]

    collision.unlink()
    # A normalizer that returns the original malformed stem exercises the
    # no-op guard (important on custom normalization policies).
    monkeypatch.setattr(mod, "bounded_slug", lambda value: bad.stem)
    assert mod.repair(tmp_path) == ([], [])


def test_repair_scan_read_and_reference_write_races(tmp_path, monkeypatch):
    mod = _load()
    wiki = tmp_path / "wiki"
    old = wiki / "entities" / "a" / "bad name.md"
    old.parent.mkdir(parents=True)
    old.write_text("---\nname: Good\n---\n")
    ref = wiki / "briefings" / "ref.md"
    ref.parent.mkdir()
    ref.write_text("[[entities/a/bad name]]")
    vanished = wiki / "entities" / "a" / "gone name.md"
    vanished.write_text("---\nname: Gone\n---\n")
    original_read = Path.read_text
    original_write = Path.write_text

    def flaky_read(path, *args, **kwargs):
        if path == vanished:
            raise OSError("gone")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    monkeypatch.setattr(
        Path, "write_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("readonly"))
            if path == ref else original_write(path, *a, **k)
        ),
    )
    original_raw_write = mod._write
    monkeypatch.setattr(mod, "_write", lambda path, text: (
        (_ for _ in ()).throw(OSError("readonly")) if path == ref else original_raw_write(path, text)))
    moves, errors = mod.repair(tmp_path, apply=True)
    assert ("entities/a/bad name", "entities/a/good") in moves
    assert any("could not rewrite" in x for x in errors)


def test_main_dry_apply_error_and_entrypoint(tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "repair", lambda *_a, **_k: ([("old", "new")], []))
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path)])
    assert mod.main() == 0
    assert "WOULD MOVE" in capsys.readouterr().out
    monkeypatch.setattr(mod, "repair", lambda *_a, **_k: ([("old", "new")], ["bad"]))
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path), "--apply"])
    assert mod.main() == 1
    captured = capsys.readouterr()
    assert "MOVE old -> new" in captured.out and "ERROR bad" in captured.err

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--vault", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0


# ── okengine#997: content pages under a reserved or dot-leading basename ─────────────────────────

import json  # noqa: E402

_SCHEMA = ("partitioning:\n  namespaces:\n"
           "    entities: {strategy: by-letter, reshard_by: second-letter}\n"
           "    concepts: {strategy: by-letter}\n    notes: {strategy: flat}\n"
           "types:\n  publisher: {}\n  concept: {}\n")


def _write(wiki, rel, text):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _reserved_vault(tmp_path):
    (tmp_path / "schema.yaml").write_text(_SCHEMA)
    wiki = tmp_path / "wiki"
    _write(wiki, "entities/h/e/health.md",
           "---\ntype: publisher\nid: health\ntitle: Health\n---\n# Health\n")
    _write(wiki, "entities/h/e/health-isac.md", "---\ntype: publisher\nid: health-isac\n---\n")
    _write(wiki, "concepts/_/.net-framework-code-injection.md", "---\ntype: concept\n---\n# .NET\n")
    _write(wiki, "concepts/n/net-old.md", "---\ntype: concept\n---\nlive survivor\n")
    _write(wiki, "concepts/_/.net-old.md", "---\ntype: concept\nstatus: tombstoned\n"
                                          "superseded_by: '[[concepts/n/net-old]]'\n---\n")
    _write(wiki, "index.md", "- [[entities/h/e/health]] — Health\n"
                             "- [[entities/h/e/health-isac]] — near miss, untouched\n"
                             "- [[concepts/_/.net-framework-code-injection|.NET]]\n"
                             "- [[concepts/_/.net-old]] — old\n")
    _write(wiki, "entities/h/e/INDEX.md", "---\ntype: dashboard\n---\n| [[entities/h/e/health|health]] |\n")
    _write(wiki, "assessments/a.md", "---\ntype: assessment\nsubject: entities/h/e/health\n---\n"
                                     "see [[health]] and entities/h/e/healthcare and "
                                     "[[.net-framework-code-injection|.NET]]\n")
    return tmp_path, wiki


def test_reserved_plan_dry_run_reports_without_writing(tmp_path):
    mod = _load()
    root, wiki = _reserved_vault(tmp_path)
    before = {p: p.read_text() for p in wiki.rglob("*.md")}
    moves, retired, review, rewritten, unqualified, errors = mod.repair_reserved(root)
    assert errors == []
    assert moves == [("concepts/_/.net-framework-code-injection",
                      "concepts/d/dotnet-framework-code-injection"),
                     ("entities/h/e/health", "entities/h/e/health-publisher")]
    assert retired == [("concepts/_/.net-old", "concepts/n/net-old")] and review == []
    assert rewritten == [("assessments/a.md", 1), ("entities/h/e/INDEX.md", 1), ("index.md", 3)]
    # a reserved stem is the root structural file's link, not the page's; a dot stem is reported
    assert unqualified == ["assessments/a.md: [[.net-framework-code-injection]]"]
    assert {p: p.read_text() for p in wiki.rglob("*.md")} == before      # dry run wrote nothing


def test_reserved_apply_renames_live_pages_drops_tombstones_and_keeps_id(tmp_path):
    mod = _load()
    root, wiki = _reserved_vault(tmp_path)
    moves, retired, _review, _rewritten, _unq, errors = mod.repair_reserved(root, apply=True)
    assert errors == [] and len(moves) == 2 and len(retired) == 1
    new = wiki / "entities" / "h" / "e" / "health-publisher.md"
    assert new.is_file() and not (wiki / "entities" / "h" / "e" / "health.md").exists()
    assert "id: health\n" in new.read_text()                      # identity is immutable
    assert (wiki / "concepts" / "d" / "dotnet-framework-code-injection.md").is_file()
    assert not (wiki / "concepts" / "_" / ".net-old.md").exists()  # tombstone dropped, not renamed
    assert not list(wiki.rglob("*net-old-*"))
    index = (wiki / "index.md").read_text()
    assert "[[entities/h/e/health-publisher]]" in index
    assert "[[entities/h/e/health-isac]]" in index                # a longer slug is never touched
    assert "[[concepts/d/dotnet-framework-code-injection|.NET]]" in index
    assert "[[concepts/n/net-old]] — old" in index                # straight to the survivor
    a = (wiki / "assessments" / "a.md").read_text()
    assert "subject: entities/h/e/health-publisher\n" in a
    assert "entities/h/e/healthcare" in a and "[[health]]" in a   # unqualified links left alone
    assert "[[.net-framework-code-injection|.NET]]" in a
    assert mod.repair_reserved(root) == ([], [], [], [], [], [])      # converged


def test_tombstone_with_a_broken_pointer_is_reported_not_touched(tmp_path):
    """The live-vault case: superseded_by names a path that does not exist."""
    mod = _load()
    (tmp_path / "schema.yaml").write_text(_SCHEMA)
    wiki = tmp_path / "wiki"
    tomb = _write(wiki, "concepts/_/.net-x.md", "---\ntype: concept\nstatus: tombstoned\n"
                                                "superseded_by: concepts/_/net-x\n---\n")
    _write(wiki, "concepts/_/.dead.md", "---\ntype: concept\nstatus: tombstoned\n"
                                        "superseded_by: concepts/d/dead\n---\n")
    _write(wiki, "concepts/d/dead.md", "---\ntype: concept\nstatus: tombstoned\n---\n")
    _write(wiki, "concepts/_/.nopointer.md", "---\ntype: concept\nstatus: tombstoned\n---\n")
    moves, retired, review, rewritten, unqualified, errors = mod.repair_reserved(tmp_path, apply=True)
    assert (moves, retired, review, rewritten, unqualified) == ([], [], [], [], [])
    assert errors == [
        "concepts/_/.dead.md: tombstone superseded_by 'concepts/d/dead' does not resolve to a "
        "live page; fix the pointer, then re-run",
        "concepts/_/.net-x.md: tombstone superseded_by 'concepts/_/net-x' does not resolve to a "
        "live page; fix the pointer, then re-run",
        "concepts/_/.nopointer.md: tombstone superseded_by None does not resolve to a live page; "
        "fix the pointer, then re-run",
    ]
    assert tomb.is_file()


def test_superseded_target_accepts_path_wikilink_alias_anchor_and_md(tmp_path):
    mod = _load()
    (tmp_path / "wiki" / "concepts" / "n").mkdir(parents=True)
    (tmp_path / "wiki" / "concepts" / "n" / "x.md").write_text("---\ntype: concept\n---\n")
    for ref in ("concepts/n/x", "[[concepts/n/x|X]]", "wiki/concepts/n/x.md", "concepts/n/x#h"):
        assert mod._superseded_target(tmp_path, {"superseded_by": ref}) == "concepts/n/x", ref
    assert mod._superseded_target(tmp_path, {"superseded_by": "  "}) is None


def test_reserved_only_moves_report_no_unqualified_links(tmp_path):
    mod = _load()
    root, wiki = _reserved_vault(tmp_path)
    (wiki / "concepts" / "_" / ".net-framework-code-injection.md").unlink()
    (wiki / "concepts" / "_" / ".net-old.md").unlink()
    moves, retired, _review, _rewritten, unqualified, errors = mod.repair_reserved(root)
    assert moves == [("entities/h/e/health", "entities/h/e/health-publisher")]
    assert retired == [] and unqualified == [] and errors == []


def test_only_tombstones_still_rewrite_and_drop(tmp_path):
    mod = _load()
    root, wiki = _reserved_vault(tmp_path)
    (wiki / "concepts" / "_" / ".net-framework-code-injection.md").unlink()
    (wiki / "entities" / "h" / "e" / "health.md").unlink()
    moves, retired, _review, rewritten, _unq, errors = mod.repair_reserved(root, apply=True)
    assert moves == [] and retired == [("concepts/_/.net-old", "concepts/n/net-old")]
    assert rewritten == [("index.md", 1)] and errors == []


def test_reserved_plan_flat_namespace_sub_domain_and_collisions(tmp_path):
    mod = _load()
    (tmp_path / "schema.yaml").write_text(_SCHEMA)
    wiki = tmp_path / "wiki"
    _write(wiki, "notes/x/log.md", "---\ntype: concept\n---\n")
    (wiki / "market").mkdir(parents=True, exist_ok=True)
    (wiki / "market" / "schema.yaml").write_text(_SCHEMA)
    _write(wiki, "market/entities/h/o/hot.md", "---\ntype: publisher\n---\n")
    _write(wiki, "entities/b/u/bundle.md", "---\ntype: publisher\n---\n")
    _write(wiki, "entities/b/u/bundle-publisher.md", "---\ntype: publisher\n---\n")
    moves, retired, _review, errors = mod.plan_reserved(tmp_path)
    assert moves == [("market/entities/h/o/hot", "market/entities/h/o/hot-publisher"),
                     ("notes/x/log", "notes/x/log-concept")]
    assert retired == []
    assert errors == ["entities/b/u/bundle.md -> collision at entities/b/u/bundle-publisher.md"]


def test_reserved_plan_two_pages_claiming_one_seat(tmp_path):
    mod = _load()
    (tmp_path / "schema.yaml").write_text(_SCHEMA)
    wiki = tmp_path / "wiki"
    for rel in ("concepts/i/index.md", "concepts/legacy/index.md"):
        _write(wiki, rel, "---\ntype: concept\n---\n")
    moves, _retired, _review, errors = mod.plan_reserved(tmp_path)
    assert moves == [("concepts/i/index", "concepts/c/concept-index")]
    assert errors == ["concepts/legacy/index.md -> collision at concepts/c/concept-index.md"]


def test_reserved_plan_reports_an_unmintable_name_and_continues(tmp_path):
    mod = _load()
    (tmp_path / "schema.yaml").write_text(
        _SCHEMA + "reserved_files: [bundle-publisher.md, publisher-bundle.md]\n")
    wiki = tmp_path / "wiki"
    for rel in ("entities/b/u/bundle.md", "entities/h/e/health.md"):
        _write(wiki, rel, "---\ntype: publisher\n---\n")
    moves, _retired, _review, errors = mod.plan_reserved(tmp_path)
    assert moves == [("entities/h/e/health", "entities/h/e/health-publisher")]
    assert len(errors) == 1 and errors[0].startswith("entities/b/u/bundle.md: no mintable")


def test_reserved_rewrite_read_and_write_failures(tmp_path, monkeypatch):
    mod = _load()
    root, wiki = _reserved_vault(tmp_path)
    unreadable = _write(wiki, "aa-unreadable.md", "[[entities/h/e/health]]")
    index = wiki / "index.md"
    original_read, original_write = Path.read_text, Path.write_text

    def flaky_read(path, *a, **k):
        if path == unreadable:
            raise OSError("gone")
        return original_read(path, *a, **k)

    def flaky_write(path, *a, **k):
        if path == index:
            raise OSError("readonly")
        return original_write(path, *a, **k)

    raw_read, raw_write = mod._read, mod._write
    monkeypatch.setattr(mod, "_read", lambda path: None if path == unreadable else raw_read(path))
    monkeypatch.setattr(mod, "_write", lambda path, text: (
        (_ for _ in ()).throw(OSError("readonly")) if path == index else raw_write(path, text)))
    _moves, _retired, _review, rewritten, _unq, errors = mod.repair_reserved(root, apply=True)
    assert errors == ["could not rewrite reference in index.md"]
    assert "aa-unreadable.md" not in [rel for rel, _ in rewritten]


def test_namespace_follows_walk_up_sub_domains(tmp_path):
    mod = _load()
    wiki = tmp_path / "wiki"
    for d in ("market", "a", "a/b"):
        (wiki / d).mkdir(parents=True, exist_ok=True)
        (wiki / d / "schema.yaml").write_text("{}\n")
    assert mod._namespace(wiki, "entities/h/e/health") == "entities"
    assert mod._namespace(wiki, "market/entities/h/health") == "market/entities"
    assert mod._namespace(wiki, "a/b/entities/x/y") == "a/b/entities"
    assert mod._namespace(wiki, "a/b/x") == "a/b"
    assert mod._namespace(wiki, "market/x") == "market"
    assert mod._namespace(wiki, "concepts/x") == "concepts"


def test_rewrite_references_defaults_to_dry_run_and_apply_creates_new_shards(tmp_path):
    mod = _load()
    root, wiki = _reserved_vault(tmp_path)
    moves, _retired, _review, _errors = mod.plan_reserved(root)
    before = (wiki / "index.md").read_text()
    rewritten, _unq, errors = mod.rewrite_references(root, moves)
    assert ("index.md", 2) in rewritten and errors == []
    assert (wiki / "index.md").read_text() == before
    assert not (wiki / "concepts" / "d").exists()
    _write(wiki, "entities/_/.zz.md", "---\ntype: publisher\n---\n")
    mod.repair_reserved(root, apply=True)                   # missing shard dirs are created
    assert (wiki / "concepts" / "d" / "dotnet-framework-code-injection.md").is_file()
    assert (wiki / "entities" / "d" / "o" / "dotzz.md").is_file()   # two new levels


def test_reserved_apply_is_one_journaled_corpus_transaction(tmp_path):
    """The apply takes the corpus fence and journals exactly the pages it renames, rewrites and
    drops."""
    mod = _load()
    root, _wiki = _reserved_vault(tmp_path)
    mod.repair_reserved(root, apply=True)
    journal = root / ".okengine" / "corpus" / "journal.jsonl"
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(records) == 1
    rec = records[0]
    assert (rec["writer"], rec["operation"], rec["status"]) == (
        "repair_malformed_slugs", "reserved-basename-rename", "committed")
    assert {c["path"] for c in rec["affected_paths"]} == {
        "wiki/entities/h/e/health.md", "wiki/entities/h/e/health-publisher.md",
        "wiki/concepts/_/.net-framework-code-injection.md",
        "wiki/concepts/d/dotnet-framework-code-injection.md", "wiki/concepts/_/.net-old.md",
        "wiki/index.md", "wiki/entities/h/e/INDEX.md", "wiki/assessments/a.md",
    }


def test_reserved_dry_run_takes_no_transaction(tmp_path):
    mod = _load()
    root, _wiki = _reserved_vault(tmp_path)
    mod.repair_reserved(root)
    assert not (root / ".okengine" / "corpus" / "journal.jsonl").exists()


def test_main_reports_reserved_moves_drops_and_rewrites(tmp_path, monkeypatch, capsys):
    mod = _load()
    root, _wiki = _reserved_vault(tmp_path)
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(root)])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "WOULD MOVE entities/h/e/health -> entities/h/e/health-publisher (reserved basename)" in out
    assert ("WOULD DROP tombstone concepts/_/.net-old (references -> its superseded_by "
            "concepts/n/net-old)") in out
    assert "WOULD REWRITE index.md: 3 reference(s)" in out
    assert "UNQUALIFIED assessments/a.md: [[.net-framework-code-injection]]" in out
    assert "2 reserved-basename move(s), 1 tombstone(s) dropped, 0 for review, 3 file(s)" in out
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(root), "--apply"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "REWROTE index.md: 3 reference(s)" in out and "DROP tombstone concepts/_/.net-old" in out


def test_scaffold_shaped_live_pages_are_listed_for_review_never_renamed(tmp_path, monkeypatch, capsys):
    """A pack template (`predictions/_template.md`, read by name by the pack's crons) or an
    `INDEX-x`/`index-pNN`-shaped page is reported, not renamed — and is not an error."""
    mod = _load()
    (tmp_path / "schema.yaml").write_text(_SCHEMA)
    wiki = tmp_path / "wiki"
    tpl = _write(wiki, "predictions/_template.md", "---\ntype: prediction\n---\n")
    _write(wiki, "concepts/i/index-p02.md", "---\ntype: concept\n---\n")
    _write(wiki, "concepts/i/INDEX-p03.md", "---\ntype: dashboard\n---\n")  # generated: silent
    moves, retired, review, rewritten, unqualified, errors = mod.repair_reserved(tmp_path, apply=True)
    assert review == ["concepts/i/index-p02.md", "predictions/_template.md"]
    assert (moves, retired, rewritten, unqualified, errors) == ([], [], [], [], [])
    assert tpl.is_file()
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path)])
    assert mod.main() == 0
    assert "REVIEW predictions/_template.md" in capsys.readouterr().out


def test_namespace_never_runs_past_the_page_segment(tmp_path):
    mod = _load()
    wiki = tmp_path / "wiki"
    for d in ("x", "x/y", "x/y/z"):
        (wiki / d).mkdir(parents=True, exist_ok=True)
        (wiki / d / "schema.yaml").write_text("{}\n")
    assert mod._namespace(wiki, "x/y/z/w") == "x/y/z"


# --- okengine#994: the slug repair reports the references it rewrites -----------------------------

@pytest.mark.parametrize("apply", [False, True])
def test_the_slug_repair_counts_the_references_it_rewrites(tmp_path, monkeypatch, capsys, apply):
    """QA of okengine#994: the whitespace/run-on move rewrote a reference on apply while the report
    said "0 file(s) with rewritten references", in the dry run and the apply alike."""
    mod = _load()
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities" / "a" / "this is bad.md").write_text("---\ntype: entity\nname: Clean Actor\n---\n")
    (wiki / "briefings").mkdir()
    ref = wiki / "briefings" / "b.md"
    ref.write_text("[[entities/a/this is bad]] and [[entities/a/this is bad]]\n")
    (wiki / "briefings" / "other.md").write_text("unrelated\n")
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path), *(["--apply"] if apply else [])])
    assert mod.main() == 0
    out = capsys.readouterr().out
    verb = "REWROTE" if apply else "WOULD REWRITE"
    assert f"{verb} briefings/b.md: 2 reference(s)" in out
    assert "1 file(s) with rewritten references" in out
    assert ("clean-actor" in ref.read_text()) is apply, "a dry run writes nothing"


def test_a_failed_reference_rewrite_is_an_error_not_a_rewrite(tmp_path, monkeypatch):
    mod = _load()
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities" / "a" / "this is bad.md").write_text("---\nname: Clean Actor\n---\n")
    (wiki / "b.md").write_text("[[entities/a/this is bad]]\n")
    real = mod._write

    def refuse(page, text):
        if page.name == "b.md":
            raise OSError("read-only")
        return real(page, text)

    monkeypatch.setattr(mod, "_write", refuse)
    rewritten = []
    _moves, errors = mod.repair(tmp_path, apply=True, rewritten=rewritten)
    assert rewritten == [] and errors == ["could not rewrite reference in b.md"]


def test_repair_is_a_dry_run_by_default(tmp_path):
    mod = _load()
    old = tmp_path / "wiki" / "entities" / "a" / "this is bad.md"
    old.parent.mkdir(parents=True)
    old.write_text("---\nname: Clean Actor\n---\n")
    moves, _errors = mod.repair(tmp_path)
    assert moves and old.exists()


def test_a_failed_reference_rewrite_does_not_stop_the_others(tmp_path, monkeypatch):
    mod = _load()
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities" / "a" / "this is bad.md").write_text("---\nname: Clean Actor\n---\n")
    for name in ("a.md", "b.md", "c.md"):
        (wiki / name).write_text("[[entities/a/this is bad]]\n")
    real = mod._write

    def refuse_one(page, text):
        if page.name == "b.md":
            raise OSError("read-only")
        return real(page, text)

    monkeypatch.setattr(mod, "_write", refuse_one)
    rewritten = []
    mod.repair(tmp_path, apply=True, rewritten=rewritten)
    assert sorted(rel for rel, _n in rewritten) == ["a.md", "c.md"]


# --- okengine#994 QA of !1279: mutually-linked malformed pages, and CRLF ----------------------------

def _mutual(tmp_path):
    wiki = tmp_path / "wiki"
    for rel, text in {
        "entities/f/foo bar.md": "---\ntitle: Foo Bar\n---\n\nSee [[entities/g/gamma delta]].\n",
        "entities/g/gamma delta.md":
            "---\ntitle: Gamma Delta\n---\n\nSee [[entities/f/foo bar]] and [[entities/f/foo bar]].\n",
        "entities/f/other.md": "---\ntitle: Other\n---\n\nSee [[entities/f/foo bar]].\n",
    }.items():
        (wiki / rel).parent.mkdir(parents=True, exist_ok=True)
        (wiki / rel).write_text(text)
    crlf = wiki / "concepts" / "c" / "crlf.md"
    crlf.parent.mkdir(parents=True)
    crlf.write_bytes(b"---\r\ntype: concept\r\n---\r\n\r\nCites [[entities/g/gamma delta]].\r\n")
    return wiki, crlf


def test_mutually_linked_malformed_pages_leave_no_dangling_link(tmp_path, monkeypatch, capsys):
    """QA of !1279: the apply loop skipped a page that an earlier move had already renamed, so
    `gamma-delta.md` kept linking `[[entities/f/foo bar]]` after foo bar moved, while the dry run
    counted that rewrite. Every page's references are rewritten wherever the page now sits."""
    mod = _load()
    wiki, _crlf = _mutual(tmp_path)
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path)])
    assert mod.main() == 0
    dry = capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path), "--apply"])
    assert mod.main() == 0
    applied = capsys.readouterr().out
    assert "4 file(s) with rewritten references" in dry
    assert "4 file(s) with rewritten references" in applied, "dry run and apply agree"
    texts = {p.relative_to(wiki).as_posix(): p.read_text() for p in wiki.rglob("*.md")}
    assert sorted(texts) == ["concepts/c/crlf.md", "entities/f/foo-bar.md", "entities/f/other.md",
                             "entities/g/gamma-delta.md"]
    assert not any("foo bar]]" in t or "gamma delta]]" in t for t in texts.values()), texts
    assert texts["entities/g/gamma-delta.md"].count("[[entities/f/foo-bar]]") == 2


def test_a_crlf_page_keeps_crlf_when_its_reference_is_rewritten(tmp_path):
    mod = _load()
    _wiki, crlf = _mutual(tmp_path)
    mod.repair(tmp_path, apply=True)
    assert crlf.read_bytes() == (
        b"---\r\ntype: concept\r\n---\r\n\r\nCites [[entities/g/gamma-delta]].\r\n")


def test_a_crlf_page_keeps_crlf_when_a_reserved_reference_is_rewritten(tmp_path):
    mod = _load()
    root, wiki = _reserved_vault(tmp_path)
    page = wiki / "notes" / "crlf.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"x\r\nsee [[entities/h/e/health]]\r\n")
    mod.repair_reserved(root, apply=True)
    data = page.read_bytes()
    assert b"[[entities/h/e/health" in data and b"[[entities/h/e/health]]" not in data
    assert data.count(b"\r\n") == 2 and b"\n" not in data.replace(b"\r\n", b"")


def test_an_unreadable_page_during_apply_is_skipped_and_the_rest_rewritten(tmp_path, monkeypatch):
    mod = _load()
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities" / "a" / "this is bad.md").write_text("---\nname: Clean Actor\n---\n")
    for name in ("a.md", "u.md", "z.md"):
        (wiki / name).write_text("[[entities/a/this is bad]]\n")
    real = mod._read
    monkeypatch.setattr(mod, "_read", lambda page: None if page.name == "u.md" else real(page))
    rewritten = []
    _moves, errors = mod.repair(tmp_path, apply=True, rewritten=rewritten)
    assert sorted(rel for rel, _n in rewritten) == ["a.md", "z.md"] and errors == []
    assert "this is bad" in (wiki / "u.md").read_text()


def test_read_is_none_for_a_page_that_vanished_and_keeps_crlf_otherwise(tmp_path):
    mod = _load()
    ghost = tmp_path / "ghost.md"
    ghost.symlink_to(tmp_path / "gone.md")  # listed by a glob, gone on read
    assert mod._read(ghost) is None
    kept = tmp_path / "k.md"
    kept.write_bytes(b"a\r\nb\n")
    assert mod._read(kept) == "a\r\nb\n"


# --- okengine#994 QA of 62e6d0c5: counts, self-links and prefix slugs ------------------------------

def _actor(wiki, rel, title, body):
    page = wiki / f"{rel}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: actor\ntitle: {title}\n---\n\n{body}\n")
    return page


def _report(mod, tmp_path, monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path), *argv])
    assert mod.main() == 0
    out = capsys.readouterr().out
    return int(re.search(r"(\d+) file\(s\) with rewritten references", out).group(1)), out


def _dangling(wiki):
    names = {p.relative_to(wiki).as_posix()[:-3] for p in wiki.rglob("*.md")}
    refs = [(p.name, r.strip()) for p in wiki.rglob("*.md")
            for r in re.findall(r"\[\[([^\]|#]+)", p.read_text())]
    return [(n, r) for n, r in refs if r.startswith("entities/") and r not in names]


def test_a_mesh_of_malformed_pages_counts_each_file_once_in_dry_run_and_apply(
        tmp_path, monkeypatch, capsys):
    """QA: five malformed pages each linking the other four. Apply counted a page rewritten both
    before and after its own move under two paths (8 files) while the dry run said 5."""
    mod = _load()
    wiki = tmp_path / "wiki"
    for i in range(1, 6):
        refs = " ".join(f"[[entities/m/page n{j}]]" for j in range(1, 6) if j != i)
        _actor(wiki, f"entities/m/page n{i}", f"Page N{i}", f"Refs {refs}.")
    dry, _ = _report(mod, tmp_path, monkeypatch, capsys)
    applied, out = _report(mod, tmp_path, monkeypatch, capsys, "--apply")
    assert dry == applied == 5, out
    assert "REWROTE entities/m/page n1.md: 4 reference(s)" in out, "reported by original path"
    assert _dangling(wiki) == []


def test_a_page_linking_itself_follows_its_own_rename(tmp_path, monkeypatch, capsys):
    mod = _load()
    wiki = tmp_path / "wiki"
    _actor(wiki, "entities/f/foo bar", "Foo Bar", "Self [[entities/f/foo bar]].")
    dry, _ = _report(mod, tmp_path, monkeypatch, capsys)
    applied, _ = _report(mod, tmp_path, monkeypatch, capsys, "--apply")
    assert dry == applied == 1
    assert "Self [[entities/f/foo-bar]]." in (wiki / "entities/f/foo-bar.md").read_text()
    assert _dangling(wiki) == []


@pytest.mark.parametrize("first", ["foo bar baz", "foo bar"])
def test_a_slug_that_prefixes_another_does_not_corrupt_its_links(tmp_path, monkeypatch, first):
    """QA: `body.replace("entities/f/foo bar", ...)` turned `[[entities/f/foo bar baz]]` into the
    dangling `[[entities/f/foo-bar baz]]`. The corruption depends on which page is processed
    first, and `rglob` order is the filesystem's (hash order on ext4, not creation order), so the
    scan order is forced here, both ways. Whole-token rewriting maps each link to its own page."""
    mod = _load()
    wiki = tmp_path / "wiki"
    second = "foo bar" if first == "foo bar baz" else "foo bar baz"
    for slug in (first, second):
        _actor(wiki, f"entities/f/{slug}", slug.title(), "x")
    real_rglob = Path.rglob
    head = f"{first}.md"
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter(sorted(
        real_rglob(self, pattern), key=lambda p: (p.name != head, str(p)))))
    watch = _actor(wiki, "concepts/c/watch", "Watch",
                   "Refs [[entities/f/foo bar baz]] and [[entities/f/foo bar]]; "
                   "bare entities/f/foo bar baz and entities/f/foo bar.")
    _moves, errors = mod.repair(tmp_path, apply=True)
    assert errors == []
    assert "Refs [[entities/f/foo-bar-baz]] and [[entities/f/foo-bar]]; " \
           "bare entities/f/foo-bar-baz and entities/f/foo-bar." in watch.read_text()
    assert _dangling(wiki) == []


def test_a_bare_reference_first_in_a_yaml_flow_list_is_rewritten(tmp_path):
    """`related: [entities/f/foo bar]`: the shared path rewriter excluded any preceding `[`, so the
    first item of a flow list stayed dangling. Only `[[` (a wikilink) is excluded now."""
    mod = _load()
    wiki = tmp_path / "wiki"
    _actor(wiki, "entities/f/foo bar", "Foo Bar", "x")
    page = wiki / "concepts" / "c" / "crlf.md"
    page.parent.mkdir(parents=True)
    page.write_bytes(b"---\r\nrelated: [entities/f/foo bar, entities/f/foo bar]\r\n---\r\n"
                     b"See [[entities/f/foo bar|Foo]].\r\n")
    mod.repair(tmp_path, apply=True)
    assert page.read_bytes() == (b"---\r\nrelated: [entities/f/foo-bar, entities/f/foo-bar]\r\n"
                                 b"---\r\nSee [[entities/f/foo-bar|Foo]].\r\n")


def test_two_pages_claiming_one_new_name_is_a_collision_not_two_moves(tmp_path):
    mod = _load()
    wiki = tmp_path / "wiki"
    _actor(wiki, "entities/f/foo  bar", "Foo Bar", "x")
    _actor(wiki, "entities/f/foo bar", "Foo Bar", "x")
    moves, errors = mod.plan_slugs(tmp_path)
    assert len(moves) == 1 and len(errors) == 1 and "collision at entities/f/foo-bar.md" in errors[0]


def test_a_dry_run_counts_by_original_path_and_writes_nothing(tmp_path):
    mod = _load()
    wiki = tmp_path / "wiki"
    page = _actor(wiki, "entities/f/foo bar", "Foo Bar", "Self [[entities/f/foo bar]].")
    rewritten = []
    mod.repair(tmp_path, rewritten=rewritten)
    assert rewritten == [("entities/f/foo bar.md", 1)] and page.exists()
