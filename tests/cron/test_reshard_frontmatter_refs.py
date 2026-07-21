"""Regression: a reshard must rewrite BARE path references, not only [[wikilinks]].

Incident (2026-07-20, okcti): as the CTI actor packs grew `entities/` past the
reshard threshold, reshard-oversized split big buckets a level deeper
(`entities/a/admin-338` -> `entities/a/d/admin-338`). Its link rewriter only
matches `[[wikilink]]` syntax, so every assessment record — which points at its
entity through a BARE frontmatter scalar `subject: entities/a/admin-338`, not a
wikilink — was left pointing at the pre-reshard path. Cockpit joins the entity
row (current path) against the assessment index (stale subject), missed, and
showed "Review not run" for 100 of 303 records whose bucket had split.
"""
import importlib.util
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def _load(root: Path):
    okf_spec = importlib.util.spec_from_file_location(
        "okf_migrate", REPO / "scripts" / "cron" / "okf_migrate.py")
    okf = importlib.util.module_from_spec(okf_spec)
    sys.modules["okf_migrate"] = okf
    okf_spec.loader.exec_module(okf)

    spec = importlib.util.spec_from_file_location(
        "reshard_oversized", REPO / "scripts" / "cron" / "reshard_oversized.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reshard_oversized"] = mod
    spec.loader.exec_module(mod)
    mod.VAULT = root
    mod.WIKI = root / "wiki"
    return mod, okf


def test_make_path_rewriter_matches_whole_tokens_only():
    """Bare-path rewriter is boundary-safe: it must not corrupt a longer slug, an
    already-deeper path, or a wikilink (make_rewriter's job)."""
    _, okf = _load(REPO)  # module only; no vault touched
    mp = {"entities/a/admin-338": "entities/a/d/admin-338",
          "entities/a/foo": "entities/a/f/foo"}
    pat, repl = okf.make_path_rewriter(mp)

    def rw(s):
        return pat.sub(repl, s)

    assert rw("subject: entities/a/admin-338") == "subject: entities/a/d/admin-338"
    assert rw('"entities/a/foo"') == '"entities/a/f/foo"'
    # a longer slug that merely starts with a key must be untouched
    assert rw("entities/a/admin-338-bis") == "entities/a/admin-338-bis"
    assert rw("entities/a/foobar") == "entities/a/foobar"
    # the already-deeper (new) path must be untouched — no double-rewrite
    assert rw("entities/a/d/admin-338") == "entities/a/d/admin-338"
    # a wikilink is left for make_rewriter (the `[` lookbehind), not mangled here
    assert rw("[[entities/a/foo|Foo]]") == "[[entities/a/foo|Foo]]"


def test_make_path_rewriter_empty_map_is_a_noop():
    _, okf = _load(REPO)
    pat, repl = okf.make_path_rewriter({})
    assert pat.sub(repl, "entities/a/foo") == "entities/a/foo"


def test_reshard_rewrites_bare_frontmatter_subject_and_wikilink(tmp_path):
    """End-to-end: split an oversized `entities/a` bucket and confirm a referencing
    assessment record has BOTH its bare `subject:` field and its body wikilink repointed."""
    ent = tmp_path / "wiki" / "entities" / "a"
    (ent / "d").mkdir(parents=True)             # bucket already split -> `a` is oversized (residual sweep)
    (ent / "d" / "apt-existing.md").write_text("---\ntype: entity\n---\n")
    (ent / "admin-338.md").write_text("---\ntype: entity\ntitle: admin@338\n---\n")

    assess = tmp_path / "wiki" / "assessments" / "a"
    assess.mkdir(parents=True)
    (assess / "admin-338-china.md").write_text(
        "---\n"
        "type: assessment\n"
        "subject: entities/a/admin-338\n"           # BARE scalar — the field that used to go stale
        "subject_ref: G0018\n"
        "related: entities/a/admin-338-bis\n"       # a DIFFERENT page — must NOT be touched
        "status: active\n"
        "---\n"
        "Reported association of [[entities/a/admin-338|admin@338]] with a country.\n")

    mod, _ = _load(tmp_path)
    moved = mod._apply("entities", "second-letter", 500, apply=True)
    assert moved == 1

    # the entity page moved a level deeper
    assert (ent / "d" / "admin-338.md").is_file()
    assert not (ent / "admin-338.md").exists()

    text = (assess / "admin-338-china.md").read_text()
    assert "subject: entities/a/d/admin-338\n" in text          # bare field repointed
    assert "[[entities/a/d/admin-338|admin@338]]" in text       # wikilink repointed
    assert "related: entities/a/admin-338-bis\n" in text        # boundary: sibling slug untouched
    assert "subject_ref: G0018\n" in text                       # non-path field untouched
