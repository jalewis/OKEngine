"""source_normalize (okengine#563) — lift body-only citations into `sources:` frontmatter.

The defect: a page cites `[[sources/...]]` in a `## Sources` body section and has no `sources:`
frontmatter key. Every consumer (review_autoverify._grade_evidence, the cockpit's _provenance, the
reader's) reads `fm["sources"]` only, so real, checkable evidence grades as unsourced and renders as
"no sources".

The contract under test is as much about what this must NOT touch as what it fixes: it must never
promote a dangling ref (manufacturing a citation to nothing), never rewrite a page that already has
a `sources:` key, and never treat a `source` record's own body references as its provenance.
"""
import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "source_normalize.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="source_normalize absent")


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("source_normalize", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    return m


def _page(p: Path, fm: dict, body: str = "body\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body, encoding="utf-8")


def _src(vault: Path, rel: str):
    _page(vault / "wiki" / (rel + ".md"), {"type": "source", "publisher": "The Register"})


def _fm(p: Path) -> dict:
    import re
    return yaml.safe_load(re.match(r"\A---\n(.*?\n)---", p.read_text(), re.S).group(1))


def test_resolving_body_citation_is_lifted_to_frontmatter(tmp_path):
    """The reported shape: real evidence in the body, invisible to every consumer."""
    v = tmp_path
    _src(v, "sources/2026/07/07/register-story")
    page = v / "wiki" / "entities" / "z" / "z-pentest.md"
    _page(page, {"type": "actor", "name": "Z"},
          body="## Sources\n- [[sources/2026/07/07/register-story]] — The Register\n")
    assert _load(v).main([]) == 0
    assert _fm(page)["sources"] == ["sources/2026/07/07/register-story"]


def test_dangling_ref_is_never_promoted(tmp_path):
    """Promoting a link to a page that does not exist would manufacture a citation to nothing —
    strictly worse than the missing key, because it would then GRADE as sourced."""
    v = tmp_path
    (v / "wiki").mkdir(parents=True)
    page = v / "wiki" / "entities" / "d" / "dangling.md"
    _page(page, {"type": "actor", "name": "D"},
          body="## Sources\n- [[sources/does/not/exist]]\n")
    assert _load(v).main([]) == 0
    assert "sources" not in _fm(page)


def test_dangling_only_page_is_reported_not_silently_skipped(tmp_path, capsys):
    """"cites nothing" and "cites only things that do not exist" are different facts; a silent skip
    would collapse them and hide a data-quality problem."""
    v = tmp_path
    (v / "wiki").mkdir(parents=True)
    _page(v / "wiki" / "entities" / "d" / "dangling.md", {"type": "actor", "name": "D"},
          body="## Sources\n- [[sources/does/not/exist]]\n")
    assert _load(v).main([]) == 0
    out = capsys.readouterr().out
    assert "body cites only unresolvable sources" in out, out
    assert "1 skipped (dangling only)" in out, out


def test_page_with_existing_sources_key_is_untouched(tmp_path):
    """Not broken: it already grades and renders. Merging into an existing list is a different,
    riskier edit and is deliberately out of scope."""
    v = tmp_path
    _src(v, "sources/a")
    _src(v, "sources/b")
    page = v / "wiki" / "entities" / "b" / "both.md"
    _page(page, {"type": "actor", "name": "B", "sources": ["sources/a"]},
          body="## Sources\n- [[sources/b]]\n")
    before = page.read_text()
    assert _load(v).main([]) == 0
    assert page.read_text() == before


def test_source_records_own_body_links_are_not_its_provenance(tmp_path):
    """A source page IS the primary document — its upstream url/raw is its grounding. Body links on
    it are references to other records, not the evidence for itself."""
    v = tmp_path
    _src(v, "sources/other")
    page = v / "wiki" / "sources" / "story.md"
    _page(page, {"type": "source", "publisher": "The Register", "url": "https://example.invalid/x"},
          body="See also [[sources/other]]\n")
    before = page.read_text()
    assert _load(v).main([]) == 0
    assert page.read_text() == before


def test_body_and_other_frontmatter_survive_the_rewrite(tmp_path):
    """The rewrite splices frontmatter and body back together; a wrong offset silently eats page
    content, which is the failure mode that makes this class of edit dangerous."""
    v = tmp_path
    _src(v, "sources/keep")
    page = v / "wiki" / "entities" / "k" / "keep.md"
    _page(page, {"type": "actor", "name": "Keep", "aliases": ["K1"], "version": 3},
          body="# Heading\n\nprose that must survive\n\n## Sources\n- [[sources/keep]]\n")
    assert _load(v).main([]) == 0
    fm, txt = _fm(page), page.read_text()
    assert fm["name"] == "Keep" and fm["aliases"] == ["K1"] and fm["version"] == 3
    assert fm["sources"] == ["sources/keep"]
    assert "# Heading" in txt and "prose that must survive" in txt
    assert txt.count("---") >= 2


def test_dedupes_and_preserves_first_appearance_order(tmp_path):
    v = tmp_path
    for r in ("sources/one", "sources/two"):
        _src(v, r)
    page = v / "wiki" / "entities" / "m" / "multi.md"
    _page(page, {"type": "actor", "name": "M"},
          body="[[sources/two]] then [[sources/one]] then [[sources/two]] again\n")
    assert _load(v).main([]) == 0
    assert _fm(page)["sources"] == ["sources/two", "sources/one"]


def test_is_idempotent(tmp_path):
    v = tmp_path
    _src(v, "sources/x")
    page = v / "wiki" / "entities" / "i" / "idem.md"
    _page(page, {"type": "actor", "name": "I"}, body="[[sources/x]]\n")
    m = _load(v)
    assert m.main([]) == 0
    once = page.read_text()
    assert m.main([]) == 0
    assert page.read_text() == once


def test_dry_run_writes_nothing(tmp_path, capsys):
    v = tmp_path
    _src(v, "sources/x")
    page = v / "wiki" / "entities" / "i" / "dry.md"
    _page(page, {"type": "actor", "name": "I"}, body="[[sources/x]]\n")
    before = page.read_text()
    assert _load(v).main(["--dry-run"]) == 0
    assert page.read_text() == before
    assert "1 page(s) lifted" in capsys.readouterr().out


def test_prose_wikilink_with_overlong_component_does_not_abort(tmp_path):
    """A prose citation containing a slash must never be walked as a filesystem path — an overlong
    component raises OSError and would abort the scan for every later page."""
    v = tmp_path
    _src(v, "sources/ok")
    page = v / "wiki" / "entities" / "p" / "prose.md"
    _page(page, {"type": "actor", "name": "P"},
          body=f"[[sources/{'x' * 300}]] and [[sources/ok]]\n")
    assert _load(v).main([]) == 0
    assert _fm(page)["sources"] == ["sources/ok"]


def test_non_source_wikilinks_are_ignored(tmp_path):
    """Links to other entities are not citations."""
    v = tmp_path
    (v / "wiki").mkdir(parents=True)
    page = v / "wiki" / "entities" / "e" / "ent.md"
    _page(page, {"type": "actor", "name": "E"}, body="related: [[entities/a/other]]\n")
    assert _load(v).main([]) == 0
    assert "sources" not in _fm(page)


def test_dashboards_are_exempt(tmp_path):
    """A dashboard is a GENERATED aggregate view: its body list of source records is a rendering,
    not a citation, and the next regeneration would overwrite a lifted field anyway."""
    v = tmp_path
    _src(v, "sources/a")
    p = v / "wiki" / "dashboards" / "overview.md"
    _page(p, {"type": "dashboard", "name": "X"}, body="## Sources\n- [[sources/a]]\n")
    before = p.read_text()
    assert _load(v).main([]) == 0
    assert p.read_text() == before


def test_non_entity_pages_are_repaired_too(tmp_path):
    """The repair is NOT confined to entities/. On one live vault 365 non-entity pages carried the
    defect: 325 showed a false "no sources" badge and 41 were stuck in needs_review, held for a
    human because the grading lane could not see evidence the page actually cited."""
    v = tmp_path
    _src(v, "sources/a")
    for rel, ty in (("briefings/weekly.md", "briefing"), ("trends/theme-x.md", "trend"),
                    ("concepts/c.md", "concept"), ("entities/m/mal.md", "malware")):
        p = v / "wiki" / rel
        _page(p, {"type": ty, "name": "X"}, body="## Sources\n- [[sources/a]]\n")
        assert _load(v).main([]) == 0
        assert _fm(p)["sources"] == ["sources/a"], rel


def test_ref_missing_the_sources_prefix_is_repaired(tmp_path):
    """A citation naming a real record in an unresolvable shape grades as nothing — the evidence is
    there, the pointer is malformed."""
    v = tmp_path
    _src(v, "sources/priv/hub/rec")
    page = v / "wiki" / "entities" / "p" / "prefix.md"
    _page(page, {"type": "actor", "name": "P", "sources": ["priv/hub/rec"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["sources"] == ["sources/priv/hub/rec"]


def test_id_form_ref_is_resolved_by_unique_basename(tmp_path):
    v = tmp_path
    _src(v, "sources/priv/hub/abc123")
    page = v / "wiki" / "entities" / "i" / "idform.md"
    _page(page, {"type": "actor", "name": "I", "sources": ["source:hub:abc123"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["sources"] == ["sources/priv/hub/abc123"]


def test_ambiguous_basename_is_never_guessed(tmp_path):
    """Two records sharing a basename cannot be told apart; picking one would silently attribute the
    page to the wrong record — strictly worse than leaving the ref unresolved."""
    v = tmp_path
    _src(v, "sources/a/dup")
    _src(v, "sources/b/dup")
    page = v / "wiki" / "entities" / "a" / "amb.md"
    _page(page, {"type": "actor", "name": "A", "sources": ["source:hub:dup"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["sources"] == ["source:hub:dup"], "must stay unresolved rather than guess"


def test_resolvable_refs_are_left_alone(tmp_path):
    v = tmp_path
    _src(v, "sources/ok/rec")
    page = v / "wiki" / "entities" / "o" / "ok.md"
    _page(page, {"type": "actor", "name": "O", "sources": ["sources/ok/rec"]})
    before = page.read_text()
    assert _load(v).main([]) == 0
    assert page.read_text() == before


def _schema(vault, ref_fields):
    (vault / "schema.yaml").write_text(
        "conformance:\n  rules:\n    - id: refs-are-pages\n      kind: ref_fields\n"
        "      fields: [" + ", ".join(ref_fields) + "]\n", encoding="utf-8")


def test_a_dangling_ref_is_repointed_at_the_real_page(tmp_path):
    """The live shape: assessments referenced `entities/v/o/volt-typhoon` while the page lives at
    `entities/v/volt-typhoon`. The pack shards entities by first letter and reshards to a SECOND
    letter only past 500 per bucket, so the writer assumed a depth only some buckets have. 103
    edges across 29 subjects were silently dropped by every consumer."""
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "volt-typhoon.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "a1.md"
    _page(a, {"type": "assessment", "name": "A", "subject": "entities/v/o/volt-typhoon"})
    assert _load(v).main([]) == 0
    assert _fm(a)["subject"] == "entities/v/volt-typhoon"


def test_only_schema_declared_ref_fields_are_repaired(tmp_path):
    """`id` is path-SHAPED but is an IDENTITY — rewriting it breaks the id-index and every link that
    resolves through it. `raw` is a storage location. A blanket path repair would have rewritten 397
    `id` and 53 `raw` values on one live vault."""
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "volt-typhoon.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "a2.md"
    _page(a, {"type": "assessment", "name": "A", "id": "entities/v/o/volt-typhoon",
              "raw": "entities/v/o/volt-typhoon"})
    assert _load(v).main([]) == 0
    fm = _fm(a)
    assert fm["id"] == "entities/v/o/volt-typhoon", "identity must never be re-pointed"
    assert fm["raw"] == "entities/v/o/volt-typhoon", "a storage path is not a graph edge"


def test_an_ambiguous_basename_is_never_guessed(tmp_path):
    """Two pages sharing a basename cannot be told apart; pointing the edge at one would attach the
    assessment to the wrong subject — worse than leaving it dangling."""
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "dup.md", {"type": "actor", "name": "D1"})
    _page(v / "wiki" / "entities" / "v" / "x" / "dup.md", {"type": "actor", "name": "D2"})
    a = v / "wiki" / "assessments" / "a3.md"
    _page(a, {"type": "assessment", "name": "A", "subject": "entities/v/q/dup"})
    assert _load(v).main([]) == 0
    assert _fm(a)["subject"] == "entities/v/q/dup", "must stay dangling rather than guess"


def test_a_ref_into_a_different_namespace_is_not_matched(tmp_path):
    """Basename resolution is scoped to the ref's own namespace — a same-named page elsewhere is a
    different thing."""
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "concepts" / "c" / "thing.md", {"type": "concept", "name": "T"})
    a = v / "wiki" / "assessments" / "a4.md"
    _page(a, {"type": "assessment", "name": "A", "subject": "entities/t/h/thing"})
    assert _load(v).main([]) == 0
    assert _fm(a)["subject"] == "entities/t/h/thing"


def test_ref_repair_is_idempotent(tmp_path):
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "volt-typhoon.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "a5.md"
    _page(a, {"type": "assessment", "name": "A", "subject": "entities/v/o/volt-typhoon"})
    m = _load(v)
    assert m.main([]) == 0
    once = a.read_text()
    assert m.main([]) == 0
    assert a.read_text() == once


def test_a_block_scalar_terminator_is_never_swallowed(tmp_path):
    """A ref line can be the LAST line of a multi-line quoted scalar, so the closing quote belongs
    to the block, not the value. An optional-and-discarded trailing quote made the whole document
    unparsable — it did, to a live page."""
    v = tmp_path
    _schema(v, ["superseded_by"])
    _src(v, "sources/real")
    p = v / "wiki" / "entities" / "b" / "block.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: actor\nname: B\n"
        "tombstone_reason: 'Wrong path. Moving.\n\n  superseded_by: sources/bad/real'\n"
        "---\n\nbody\n", encoding="utf-8")
    assert _load(v).main([]) == 0
    import yaml as _y
    fm = _y.safe_load(re.match(r"\A---\n(.*?\n)---", p.read_text(), re.S).group(1))
    assert isinstance(fm, dict), "frontmatter must still parse"
    assert "Wrong path" in str(fm.get("tombstone_reason"))


def test_a_quoted_ref_keeps_its_quotes(tmp_path):
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "q.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("---\ntype: assessment\nname: A\nsubject: 'entities/v/o/vt'\n---\n\nx\n",
                 encoding="utf-8")
    assert _load(v).main([]) == 0
    txt = a.read_text()
    assert "subject: 'entities/v/vt'" in txt, txt


# ---------------------------------------------------------------------------
# Malformed input, unreadable pages and mid-scan mutation.
#
# Every one of these must degrade to "unknown" and let the scan CONTINUE. This
# lane exists because one wrong shape made evidence invisible across a whole
# vault; a lane that aborts on the first bad page leaves the rest of that vault
# unrepaired, which is the same failure wearing a different hat.
# ---------------------------------------------------------------------------

_REAL_IS_FILE = Path.is_file
_REAL_WRITE = Path.write_text
_EIO = "zz-eio-zz"      # deliberately not a substring of any test name (tmp_path carries those)


def _is_file_raiser(marker: str):
    """`Path.is_file` that raises OSError for paths containing `marker`, real everywhere else."""
    def is_file(self):
        if marker in self.as_posix():
            raise OSError(5, "EIO")
        return _REAL_IS_FILE(self)
    return is_file


def _write_then(mutate):
    """`Path.write_text` that applies `mutate(path)` after a successful write — the glob-then-read
    race, made deterministic."""
    def write_text(self, data, *a, **kw):
        n = _REAL_WRITE(self, data, *a, **kw)
        mutate(self)
        return n
    return write_text


def test_a_page_without_frontmatter_has_no_frontmatter(tmp_path):
    assert _load(tmp_path)._frontmatter("# just a heading\n") == {}


def test_unparsable_frontmatter_reads_as_empty_not_as_an_abort(tmp_path):
    """Unparsable YAML is UNKNOWN. Raising here would take down the scan of every LATER page on
    account of one malformed one."""
    assert _load(tmp_path)._frontmatter("---\nsources: [unclosed\n---\nbody\n") == {}


def test_frontmatter_that_is_not_a_mapping_reads_as_empty(tmp_path):
    assert _load(tmp_path)._frontmatter("---\n- a\n- b\n---\nbody\n") == {}


def test_a_source_ref_that_cannot_be_stat_ed_does_not_resolve(tmp_path, monkeypatch):
    """An I/O error is not evidence that a page exists. Resolving on failure would promote a
    citation to something nobody could read."""
    v = tmp_path
    (v / "wiki" / "sources").mkdir(parents=True)
    m = _load(v)
    monkeypatch.setattr(m.Path, "is_file", _is_file_raiser(_EIO))
    assert m._resolves(f"sources/{_EIO}") is False


def test_an_unparsable_schema_disables_ref_repair_rather_than_guessing(tmp_path):
    """No governing schema means no declared ref fields, which means no repair. The alternative —
    falling back to "anything path-shaped" — rewrote 397 `id` and 53 `raw` values on a live vault."""
    v = tmp_path
    (v / "wiki").mkdir(parents=True)
    (v / ".okengine").mkdir(parents=True)
    (v / ".okengine" / "composed-schema.yaml").write_text("conformance: [oops\n", encoding="utf-8")
    m = _load(v)
    assert m._governing_schema() == {}
    assert m.declared_ref_fields(m._governing_schema()) == set()


def test_rules_that_are_not_ref_fields_contribute_nothing(tmp_path):
    """The rules list is heterogeneous — other kinds, and entries that are not mappings at all, must
    be stepped over rather than ending the scan of the rules after them."""
    m = _load(tmp_path)
    schema = {"conformance": {"rules": [
        {"kind": "required_fields", "fields": ["title"]},
        "not-a-mapping",
        {"kind": "ref_fields", "fields": ["subject"]},
    ]}}
    assert m.declared_ref_fields(schema) == {"subject"}


def test_the_page_index_is_built_once(tmp_path):
    """It walks the WHOLE vault. Rebuilding it per ref turns a linear scan quadratic."""
    v = tmp_path
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    m = _load(v)
    first = m._page_index()
    _page(v / "wiki" / "entities" / "v" / "later.md", {"type": "actor", "name": "L"})
    assert m._page_index() is first


def test_the_basename_index_is_built_once(tmp_path):
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    m = _load(v)
    first = m._basename_index()
    assert m._basename_index() is first


def test_a_bare_name_is_not_a_page_reference(tmp_path):
    """No namespace means nothing to scope a basename match to, so there is no unique answer."""
    m = _load(tmp_path)
    assert m.canonical_pathref("volt-typhoon") is None
    assert m.canonical_pathref("") is None
    assert m.canonical_pathref(None) is None


def test_an_already_resolvable_pathref_is_returned_unchanged(tmp_path):
    v = tmp_path
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    m = _load(v)
    assert m.canonical_pathref("entities/v/vt.md") == "entities/v/vt"


def test_a_pathref_that_cannot_be_stat_ed_is_not_guessed_at(tmp_path, monkeypatch):
    """The basename fallback WOULD have found this page uniquely. An I/O failure must not be
    laundered into a confident re-point."""
    v = tmp_path
    _page(v / "wiki" / "entities" / "v" / f"{_EIO}.md", {"type": "actor", "name": "E"})
    m = _load(v)
    monkeypatch.setattr(m.Path, "is_file", _is_file_raiser(_EIO))
    assert m.canonical_pathref(f"entities/v/o/{_EIO}") is None


def test_an_empty_ref_is_not_a_citation(tmp_path):
    m = _load(tmp_path)
    assert m.canonical_ref("   ") is None
    assert m.canonical_ref(None) is None


def test_an_already_resolvable_ref_is_returned_unchanged(tmp_path):
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    assert _load(v).canonical_ref("sources/2026/07/07/story") == "sources/2026/07/07/story"


def test_an_unresolvable_ref_in_no_recognised_shape_is_left_alone(tmp_path):
    """Not resolvable, not a missing prefix, not an ID form: there is no repair to make, and
    inventing one would attribute the page to a record that was never cited."""
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    assert _load(v).canonical_ref("sources/nope/missing") is None


def test_generated_and_archival_files_are_never_rewritten(tmp_path):
    """INDEX pages are regenerated (a lifted field would be overwritten anyway), `_`/`.` files are
    machinery, and a `.bak` is a snapshot of a past state — rewriting one corrupts the record of
    what it was."""
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    body = "## Sources\n- [[sources/2026/07/07/story]]\n"
    names = ["INDEX.md", "_scratch.md", ".hidden.md", "page.bak.md"]
    for n in names:
        _page(v / "wiki" / "concepts" / n, {"type": "concept", "name": "N"}, body=body)
    assert _load(v).main([]) == 0
    for n in names:
        assert "sources" not in _fm(v / "wiki" / "concepts" / n), n


def test_an_unreadable_page_does_not_abort_the_scan(tmp_path):
    """A directory named `*.md` matches the glob and raises IsADirectoryError on read. It sorts
    BEFORE the repairable page, so a propagating error would cost every page after it."""
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    (v / "wiki" / "concepts" / "aaa-a-directory.md").mkdir(parents=True)
    good = v / "wiki" / "concepts" / "good.md"
    _page(good, {"type": "concept", "name": "G"},
          body="## Sources\n- [[sources/2026/07/07/story]]\n")
    assert _load(v).main([]) == 0
    assert _fm(good)["sources"] == ["sources/2026/07/07/story"]


def test_a_page_with_no_frontmatter_at_all_is_left_exactly_as_it_is(tmp_path):
    """There is nowhere to lift a citation TO. Synthesising a frontmatter block would change what
    the page IS on the strength of a body link."""
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    p = v / "wiki" / "concepts" / "raw.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    before = "# no frontmatter\n[[sources/2026/07/07/story]]\n"
    p.write_text(before, encoding="utf-8")
    assert _load(v).main([]) == 0
    assert p.read_text() == before


def test_a_non_string_entry_in_a_ref_field_is_stepped_over(tmp_path):
    """A list ref field can hold anything a writer put there. One bad element must not cost the
    repairable ones beside it (okengine#348: a bare int alias took down a whole lane)."""
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "mixed.md"
    _page(a, {"type": "assessment", "name": "A", "subject": [123, "entities/v/o/vt"]})
    assert _load(v).main([]) == 0
    assert _fm(a)["subject"] == [123, "entities/v/vt"], (
        "the string beside the bad element must still be re-pointed")


def test_a_ref_field_value_with_no_namespace_is_not_a_page_reference(tmp_path):
    """`subject: vt` names no namespace. Repairing it by basename would reach across the vault."""
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "bare.md"
    _page(a, {"type": "assessment", "name": "A", "subject": "vt"})
    assert _load(v).main([]) == 0
    assert _fm(a)["subject"] == "vt"


def test_a_ref_field_whose_target_cannot_be_stat_ed_is_left_alone(tmp_path, monkeypatch):
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / f"{_EIO}.md", {"type": "actor", "name": "E"})
    a = v / "wiki" / "assessments" / "unreadable.md"
    _page(a, {"type": "assessment", "name": "A", "subject": f"entities/v/o/{_EIO}"})
    m = _load(v)
    monkeypatch.setattr(m.Path, "is_file", _is_file_raiser(_EIO))
    assert m.main([]) == 0
    assert _fm(a)["subject"] == f"entities/v/o/{_EIO}"


def test_a_repairable_edge_the_substitution_cannot_match_is_not_counted(tmp_path, capsys):
    """An inline flow list holds a repairable VALUE on a LINE the pattern does not match. Reporting
    it as re-pointed would claim work that was never written — and claim it again every run."""
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "flow.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("---\ntype: assessment\nname: A\nsubject: [entities/v/o/vt]\n---\n\nx\n",
                 encoding="utf-8")
    before = a.read_text()
    assert _load(v).main([]) == 0
    assert a.read_text() == before
    assert "0 dangling edge(s) re-pointed" in capsys.readouterr().out


def test_dry_run_reports_edges_without_re_pointing_them(tmp_path, capsys):
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "dry.md"
    _page(a, {"type": "assessment", "name": "A", "subject": "entities/v/o/vt"})
    before = a.read_text()
    assert _load(v).main(["--dry-run"]) == 0
    assert a.read_text() == before
    out = capsys.readouterr().out
    assert "entities/v/o/vt -> entities/v/vt" in out
    assert "1 dangling edge(s) re-pointed" in out and "[dry-run]" in out


def test_a_page_that_vanishes_after_its_edge_is_written_does_not_abort_the_scan(tmp_path, monkeypatch):
    """glob-then-read race: the page is gone by the time it is re-read for the sources pass. The
    edge repair already landed; the rest of the vault still has to be scanned."""
    v = tmp_path
    _schema(v, ["subject"])
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "vanishes.md"
    _page(a, {"type": "assessment", "name": "A", "subject": "entities/v/o/vt"})
    m = _load(v)
    monkeypatch.setattr(m.Path, "write_text", _write_then(lambda p: p.unlink()))
    assert m.main([]) == 0
    assert not a.exists()


def test_a_page_rewritten_out_from_under_the_scan_is_not_processed_further(tmp_path, monkeypatch):
    """A concurrent writer replaced the file with something that has no frontmatter. The re-read
    sees that, and the sources pass must not run against a stale in-memory head."""
    v = tmp_path
    _schema(v, ["subject"])
    _src(v, "sources/2026/07/07/story")
    _page(v / "wiki" / "entities" / "v" / "vt.md", {"type": "actor", "name": "VT"})
    a = v / "wiki" / "assessments" / "clobbered.md"
    _page(a, {"type": "assessment", "name": "A", "subject": "entities/v/o/vt"},
          body="## Sources\n- [[sources/2026/07/07/story]]\n")
    m = _load(v)
    monkeypatch.setattr(
        m.Path, "write_text",
        _write_then(lambda p: _REAL_WRITE(p, "clobbered\n", encoding="utf-8")
                    if p.name == "clobbered.md" else None))
    assert m.main([]) == 0
    assert a.read_text() == "clobbered\n"


def test_a_source_records_own_sources_list_is_not_repaired(tmp_path):
    """A `source` record IS the primary document. Its `sources:` entries are references, not its
    provenance, and re-pointing them would rewrite what the record cites."""
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    rec = v / "wiki" / "sources" / "2026" / "07" / "07" / "citing.md"
    _page(rec, {"type": "source", "publisher": "X", "sources": ["story"]})
    assert _load(v).main([]) == 0
    assert _fm(rec)["sources"] == ["story"]


def test_a_repairable_source_ref_the_substitution_cannot_match_is_not_counted(tmp_path, capsys):
    """Same contract as the edge case: report what was WRITTEN. An inline flow list yields a
    repairable value on an unmatchable line, and counting it invents a repair."""
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    p = v / "wiki" / "concepts" / "c" / "flow.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: concept\nname: C\nsources: [2026/07/07/story]\n---\n\nx\n",
                 encoding="utf-8")
    before = p.read_text()
    assert _load(v).main([]) == 0
    assert p.read_text() == before
    assert "0 unresolvable ref(s) repaired" in capsys.readouterr().out


def test_dry_run_reports_source_ref_repairs_without_writing_them(tmp_path, capsys):
    """The counter is what an operator reads before authorising the real run — it has to describe
    the same edit the real run would make, on a vault that is still untouched."""
    v = tmp_path
    _src(v, "sources/2026/07/07/story")
    p = v / "wiki" / "concepts" / "c" / "dryref.md"
    _page(p, {"type": "concept", "name": "C", "sources": ["2026/07/07/story"]})
    before = p.read_text()
    assert _load(v).main(["--dry-run"]) == 0
    assert p.read_text() == before
    out = capsys.readouterr().out
    assert "2026/07/07/story -> sources/2026/07/07/story" in out
    assert "1 unresolvable ref(s) repaired" in out and "[dry-run]" in out


def test_a_repeated_citation_costs_one_resolution(tmp_path, monkeypatch):
    """Dedupe is a SET membership test, and it records every distinct ref EXAMINED -- not only the
    ones that resolved.

    The old form was `ref not in out` against a list: O(n) per citation, so a page carrying tens of
    thousands of them cost O(n^2). Worse, a ref that does NOT resolve never landed in `out`, so
    every repeat of it re-ran `_resolves` -- a filesystem stat each time.

    Measured: this lane wedged on a live vault's `log.md` (4.3 MB, tens of thousands of citations)
    and never returned. Asserting the resolution COUNT pins the fix at the thing that actually
    scaled, rather than timing the loop."""
    v = tmp_path
    _src(v, "sources/2026/07/07/real")
    m = _load(v)

    calls: list[str] = []
    real = m._resolves
    monkeypatch.setattr(m, "_resolves", lambda ref: (calls.append(ref), real(ref))[1])

    body = ("## Sources\n"
            + "- [[sources/2026/07/07/real]]\n" * 40        # resolves, repeated
            + "- [[sources/does/not/exist]]\n" * 40)        # does NOT resolve, repeated
    assert m.body_citations(body) == ["sources/2026/07/07/real"]
    assert sorted(calls) == ["sources/2026/07/07/real", "sources/does/not/exist"], (
        f"each distinct ref must be resolved exactly once; got {len(calls)} calls")


def test_first_appearance_order_survives_the_set_dedupe(tmp_path):
    """The set is for membership only -- the list still carries order. A citation list reordered by
    hashing would churn the frontmatter of every page it touched on the next run."""
    v = tmp_path
    for slug in ("alpha", "bravo", "charlie"):
        _src(v, f"sources/2026/07/07/{slug}")
    m = _load(v)
    body = ("[[sources/2026/07/07/charlie]] [[sources/2026/07/07/alpha]] "
            "[[sources/2026/07/07/charlie]] [[sources/2026/07/07/bravo]] "
            "[[sources/2026/07/07/alpha]]")
    assert m.body_citations(body) == ["sources/2026/07/07/charlie",
                                      "sources/2026/07/07/alpha",
                                      "sources/2026/07/07/bravo"]


def test_structural_pages_are_never_normalised(tmp_path):
    """`log.md` is an append-only run log; BUNDLE/HEALTH/AGENTS are generated machinery. Lifting an
    operational log's body citations into `sources:` would manufacture a frontmatter list of every
    source the vault has ever touched -- meaningless as provenance, and unbounded in size.

    `corpus_audit._STRUCTURAL` already recognises this set; this lane skipped `_`/`.`/INDEX/`.bak`
    but not `log`, and `log.md` is precisely where it wedged on the live vault."""
    v = tmp_path
    _src(v, "sources/2026/07/07/real")
    body = "## Sources\n- [[sources/2026/07/07/real]]\n"
    pages = {}
    for stem in ("log", "BUNDLE", "HEALTH", "AGENTS"):
        p = v / "wiki" / f"{stem}.md"
        _page(p, {"type": "concept", "name": stem}, body=body)
        pages[stem] = (p, p.read_text(encoding="utf-8"))

    # a normal page in the same run still gets normalised, so this is a skip and not a no-op run
    normal = v / "wiki" / "concepts" / "c" / "normal.md"
    _page(normal, {"type": "concept", "name": "N"}, body=body)

    assert _load(v).main([]) == 0
    for stem, (p, before) in pages.items():
        assert p.read_text(encoding="utf-8") == before, f"{stem}.md must not be rewritten"
        assert "sources" not in _fm(p), stem
    assert _fm(normal)["sources"] == ["sources/2026/07/07/real"], (
        "the ordinary page must still be normalised in the same run")
