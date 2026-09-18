"""review_autoverify (okengine#313) — deterministic evidence-graded clearing of needs_review.

The contract under test: the lane clears the flag ONLY by registry arithmetic (1×A or 2×B by
default), refuses whenever anything else is wrong with the page, stamps an auditable basis, and
never lets an ungraded citation count.
"""
import importlib.util
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "review_autoverify.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="review_autoverify absent")

SCHEMA = """\
source_registry:
  Microsoft: {reliability: A}
  MITRE ATT&CK: {reliability: A}
  MISP galaxy: {reliability: B}
  URLhaus: {reliability: B}
types:
  actor: {required: [type, name]}
  source: {required: [type]}
"""


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("review_autoverify", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    # schema_lib is a sys.modules singleton whose _SCHEMA_CACHE keys by path WITHOUT mtime —
    # a rewritten fixture schema.yaml would be served stale across loads; clear per load.
    import sys
    sl = sys.modules.get("schema_lib")
    if sl is not None:
        for cache in ("_SCHEMA_CACHE", "_BASE_CACHE", "_COMPOSED_CACHE"):
            getattr(sl, cache, {}).clear()
    return m


def _page(p: Path, fm: dict, body: str = "body\n"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n" + body, encoding="utf-8")


def _vault(tmp_path: Path, schema: str = SCHEMA) -> Path:
    (tmp_path / "schema.yaml").write_text(schema, encoding="utf-8")
    (tmp_path / "wiki").mkdir(exist_ok=True)
    return tmp_path


def _fm(p: Path) -> dict:
    import re
    return yaml.safe_load(re.match(r"\A---\n(.*?\n)---", p.read_text(), re.S).group(1))


def test_single_A_prose_source_clears(tmp_path, capsys):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-x.md"
    _page(page, {"type": "actor", "name": "APT X", "needs_review": True,
                 "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert "needs_review" not in fm
    assert fm["review_status"] == "auto-verified"
    assert "Microsoft" in fm["auto_verified_basis"] and "A-grade" in fm["auto_verified_basis"]
    assert fm["auto_verified_at"]


def test_single_B_holds_but_two_distinct_B_clear(tmp_path):
    v = _vault(tmp_path)
    one_b = v / "wiki" / "entities" / "m" / "misp-only.md"
    two_b = v / "wiki" / "entities" / "c" / "corroborated.md"
    _page(one_b, {"type": "actor", "name": "MispOnly", "needs_review": True,
                  "sources": ["MISP galaxy"]})
    _page(two_b, {"type": "actor", "name": "Corr", "needs_review": True,
                  "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    # okengine#554: an actor page always publishes, so the 1xB / 2xB distinction is now carried by
    # the DERIVED band and consensus count instead of by holding the page. The property under test
    # is unchanged -- one B-grade publisher is weaker evidence than two.
    assert _fm(one_b)["attribution_confidence"] == "moderate"
    assert _fm(one_b)["consensus"] == 1
    assert _fm(two_b)["attribution_confidence"] == "high"
    assert _fm(two_b)["consensus"] == 2


def test_linked_source_page_grades_by_publisher(tmp_path):
    v = _vault(tmp_path)
    _page(v / "wiki" / "sources" / "2026" / "07" / "ms-report.md",
          {"type": "source", "publisher": "Microsoft", "url": "https://example.com/x"})
    page = v / "wiki" / "entities" / "s" / "spider.md"
    _page(page, {"type": "actor", "name": "Spider", "needs_review": True,
                 "sources": ["sources/2026/07/ms-report"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["review_status"] == "auto-verified"
    assert "Microsoft" in fm["auto_verified_basis"]


def test_refusals_hold_even_with_A_evidence(tmp_path):
    v = _vault(tmp_path)
    conflicted = v / "wiki" / "entities" / "c" / "conflicted.md"
    missing = v / "wiki" / "entities" / "m" / "nameless.md"
    grounding = v / "wiki" / "entities" / "g" / "grounded-bad.md"
    _page(conflicted, {"type": "actor", "name": "C", "needs_review": True,
                       "sources": ["Microsoft"],
                       "conflicts": [{"field": "origin"}]})
    _page(missing, {"type": "actor", "needs_review": True, "sources": ["Microsoft"]})  # no name
    _page(grounding, {"type": "actor", "name": "G", "needs_review": True,
                      "sources": ["Microsoft"]},
          body="## Grounding check\n\n2 unsupported claims found\n")
    assert _load(v).main([]) == 0
    for p in (conflicted, missing, grounding):
        assert _fm(p)["needs_review"] is True, p.name
        assert "review_status" not in _fm(p)


def test_tombstoned_review_is_dropped_as_lifecycle_resolution(tmp_path, capsys):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "r" / "retired.md"
    following = v / "wiki" / "entities" / "z" / "following.md"
    _page(page, {"type": "actor", "name": "Retired", "status": "tombstoned",
                 "needs_review": True, "sources": ["Microsoft"],
                 "review_status": "auto-verified", "auto_verified_basis": "stale"})
    _page(following, {"type": "actor", "name": "Following", "needs_review": True,
                      "sources": ["Microsoft"]})

    assert _load(v).main([]) == 0

    fm = _fm(page)
    assert "needs_review" not in fm
    assert fm["status"] == "tombstoned"
    assert fm["review_status"] == "dropped-tombstoned"
    assert fm["review_checked_at"]
    assert "auto_verified_basis" not in fm
    assert _fm(following)["review_status"] == "auto-verified"
    out = capsys.readouterr().out
    assert "1 tombstoned review(s) dropped" in out


def test_tombstoned_review_dry_run_reports_without_rewriting(tmp_path, capsys):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "r" / "retired.md"
    _page(page, {"type": "actor", "name": "Retired", "status": "tombstoned",
                 "needs_review": True})
    before = page.read_text()

    assert _load(v).main(["--dry-run"]) == 0

    assert page.read_text() == before
    assert "drop  entities/r/retired.md" in capsys.readouterr().out


def test_tombstoned_review_collapses_duplicate_legacy_latches_to_one_stamp(tmp_path):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "r" / "retired.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: actor\nname: Retired\nstatus: tombstoned\n"
        "needs_review: true\nneeds_review: true\n---\n\nbody\n"
    )

    assert _load(v).main([]) == 0

    text = page.read_text()
    assert "needs_review:" not in text
    assert text.count("review_status: dropped-tombstoned") == 1
    assert text.count("review_checked_at:") == 1


def test_unregistered_prose_source_never_counts(tmp_path):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "f" / "forged.md"
    _page(page, {"type": "actor", "name": "F", "needs_review": True,
                 "sources": ["Totally Real Vendor", "some blog"]})
    assert _load(v).main([]) == 0
    # publishes (okengine#554) but the ungraded prose contributes NOTHING: no corroboration, and
    # no derived band invented out of sources the registry does not grade.
    fm = _fm(page)
    assert fm["consensus"] == 0
    assert "attribution_confidence" not in fm


def test_prose_with_slash_or_overlong_component_does_not_abort_lane(tmp_path):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "p" / "prose-ref.md"
    prose = "AI/toolchain prose citation " + ("x" * 300)
    _page(page, {
        "type": "actor",
        "name": "ProseRef",
        "needs_review": True,
        "sources": [prose, "Microsoft"],
    })

    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["review_status"] == "auto-verified"
    assert "Microsoft" in fm["auto_verified_basis"]


def test_idempotent_and_dry_run(tmp_path, capsys):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-y.md"
    _page(page, {"type": "actor", "name": "APT Y", "needs_review": True,
                 "sources": ["MITRE ATT&CK"]})
    m = _load(v)
    # dry-run: reports the clear but writes nothing
    assert m.main(["--dry-run"]) == 0
    assert _fm(page)["needs_review"] is True
    # real run clears; a second run is a no-op on the same page
    assert m.main([]) == 0
    first = page.read_text()
    assert m.main([]) == 0
    assert page.read_text() == first
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_pack_can_disable_and_tune(tmp_path):
    off = SCHEMA + "review_autoverify: {enabled: false}\n"
    v = _vault(tmp_path, off)
    page = v / "wiki" / "entities" / "a" / "apt-z.md"
    # `malware` rather than `actor`: actors always publish now (okengine#554), so the enabled/
    # a_sources knobs are exercised on a type that still carries the evidence bar.
    _page(page, {"type": "malware", "name": "APT Z", "needs_review": True, "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True           # disabled -> untouched
    # tuned: require 2×A — one A no longer clears
    tuned = SCHEMA + "review_autoverify: {a_sources: 2}\n"
    (v / "schema.yaml").write_text(tuned, encoding="utf-8")
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True

def test_two_pages_same_publisher_are_one_voice_not_corroboration(tmp_path):
    """Two B-grade articles from the SAME outlet must not clear as 2xB — corroboration means two
    DIFFERENT publishers (caught live: two Akamai pages auto-clearing a lacuna page)."""
    v = _vault(tmp_path)
    for i in (1, 2):
        _page(v / "wiki" / "sources" / "2026" / "07" / f"urlhaus-{i}.md",
              {"type": "source", "publisher": "URLhaus", "url": f"https://example.com/{i}"})
    same = v / "wiki" / "entities" / "s" / "same-voice.md"
    _page(same, {"type": "actor", "name": "SameVoice", "needs_review": True,
                 "sources": ["sources/2026/07/urlhaus-1", "sources/2026/07/urlhaus-2"]})
    mixed = v / "wiki" / "entities" / "m" / "mixed-voices.md"
    _page(mixed, {"type": "actor", "name": "Mixed", "needs_review": True,
                  "sources": ["sources/2026/07/urlhaus-1", "MISP galaxy"]})
    assert _load(v).main([]) == 0
    # 2 pages, 1 publisher -> ONE voice: consensus must not reach 2 and the band must not reach the
    # 2xB level. If this ever counts pages, `consensus` silently becomes a citation count.
    assert _fm(same)["consensus"] == 1
    assert _fm(same)["attribution_confidence"] == "moderate"
    assert _fm(mixed)["review_status"] == "auto-verified"   # URLhaus + MISP galaxy = 2 distinct B

def test_judgment_types_are_never_evidence_cleared(tmp_path):
    """An assessment's needs_review guards the JUDGMENT, not the citations — A-grade sources must
    not clear it (raised by the okcti question: verified pages still carry open CHE assessments)."""
    v = _vault(tmp_path, SCHEMA + "types:\n  assessment: {required: [type]}\n")
    judgment = v / "wiki" / "assessments" / "actor-x-association.md"
    _page(judgment, {"type": "assessment", "needs_review": True,
                     "sources": ["Microsoft", "MITRE ATT&CK"]})     # 2xA — would clear an entity
    assert _load(v).main([]) == 0
    fm = _fm(judgment)
    assert fm["needs_review"] is True and "review_status" not in fm
    # and the exemption is schema-tunable: an empty exempt list restores evidence-clearing
    (v / "schema.yaml").write_text(
        SCHEMA + "types:\n  assessment: {required: [type]}\nreview_autoverify: {exempt_types: []}\n",
        encoding="utf-8")
    import os, time
    future = time.time() + 5
    os.utime(v / "schema.yaml", (future, future))   # defeat schema_lib's mtime cache (same-second rewrite)
    assert _load(v).main([]) == 0
    assert _fm(judgment)["review_status"] == "auto-verified"


def test_helper_and_scan_edge_paths(tmp_path,monkeypatch):
    m=_load(tmp_path)
    assert m._frontmatter("plain")=={}
    assert m._frontmatter("---\n[bad\n---\n")=={}
    assert m._frontmatter("---\n- x\n---\n")=={}
    assert m._registry({"source_registry":[]})=={}
    assert m._registry({"source_registry":{"X":{},"Y":{"reliability":" b "}}})=={"Y":"B"}
    assert m._policy({"review_autoverify":"bad"})["enabled"]
    assert m._required_fields({"types":{"x":[]}},"x")==[]
    assert m._source_page(None) is None
    assert m._source_page("https://example.test") is None
    assert m._source_page("../sources/x") is None
    assert m._source_page("sources/../x") is None
    assert m.main([])==1
    unique=vault_source=tmp_path/"wiki/sources/2026/x.md"
    unique.parent.mkdir(parents=True,exist_ok=True);unique.write_text("---\ntype: source\n---\n")
    assert m._source_page("sources/x")==unique
    second=tmp_path/"wiki/sources/2025/x.md";second.parent.mkdir(parents=True);second.write_text("x")
    assert m._source_page("sources/missing/x") is None
    original_rglob = Path.rglob
    monkeypatch.setattr(
        Path, "rglob",
        lambda self, pattern: (_ for _ in ()).throw(OSError("scan failed")),
    )
    assert m._source_page("sources/unresolvable") is None
    monkeypatch.setattr(Path, "rglob", original_rglob)
    assert m._grade_evidence({"sources":[1]},{"P":"A"})=={}
    monkeypatch.setattr(m,"_source_page",lambda _ref:unique)
    original=Path.read_text
    monkeypatch.setattr(Path,"read_text",lambda self,*a,**k:
                        (_ for _ in ()).throw(OSError()) if self==unique else original(self,*a,**k))
    assert m._grade_evidence({"sources":["sources/x"]},{"P":"A"})=={}
    monkeypatch.undo()
    assert m._basis({})==""
    assert m.main([])==0
    v=_vault(tmp_path)
    m=_load(v)
    for name,text in {
      "_skip.md":"plain","INDEX.md":"plain","plain.md":"plain",
      "false.md":"---\ntype: actor\nneeds_review: false\n---\n",
      "string.md":"---\ntype: actor\nneeds_review: 'true'\n---\n",
      "tomb.md":"---\ntype: actor\nname: T\nneeds_review: true\nstatus: tombstoned\nsources: [Microsoft]\n---\n",
    }.items():
      p=v/"wiki/entities"/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    assert m.main([])==0
    tomb_fm = _fm(v/"wiki/entities/tomb.md")
    assert "needs_review" not in tomb_fm
    assert tomb_fm["review_status"] == "dropped-tombstoned"

    gone = v / "wiki/entities/gone.md"
    gone.write_text("---\ntype: actor\nneeds_review: true\n---\n")
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("gone"))
        if self == gone else original(self, *a, **k),
    )
    assert m.main([]) == 0


def test_yaml_true_text_with_nonboolean_decoded_value_is_skipped(tmp_path,monkeypatch):
    v=_vault(tmp_path)
    page=v/"wiki/entities/x.md";page.parent.mkdir(parents=True,exist_ok=True)
    page.write_text("---\ntype: actor\nneeds_review: true\n---\n")
    m=_load(v)
    monkeypatch.setattr(m,"_frontmatter",lambda _text:{"type":"actor","needs_review":"true"})
    assert m.main([])==0
    assert "needs_review: true" in page.read_text()


# --- source-page self-grading (okengine#549) ------------------------------

SELF_SCHEMA = SCHEMA + "types:\n  source: {required: [type]}\n"


def _src(vault, rel, publisher=None, flagged=True):
    p = vault / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = "---\ntype: source\n"
    if publisher is not None:
        fm += f"publisher: {publisher}\n"
    if flagged:
        fm += "needs_review: true\n"
    p.write_text(fm + "---\n\nbody\n", encoding="utf-8")
    return p


def _vault_with(tmp_path, schema=SCHEMA):
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(schema, encoding="utf-8")
    return tmp_path


def test_source_page_clears_on_its_own_publisher(tmp_path, capsys):
    """A `source` page IS the primary document — it cites nothing by construction, so the
    cited-evidence arithmetic could never clear it and 282 pages sat in the human queue for the
    crime of being sources (okengine#549)."""
    v = _vault_with(tmp_path)
    _src(v, "sources/a", "Microsoft")
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out
    txt = (v / "wiki" / "sources" / "a.md").read_text()
    assert "review_status: auto-verified" in txt
    assert "A-grade publisher: Microsoft" in txt


def test_a_below_bar_publisher_is_held_with_a_stated_reason(tmp_path, capsys):
    """Default bar is A. A B-grade outlet is 'usually reliable', not 'completely reliable' — the
    pack decides whether that clears, and the refusal must say why rather than being silent."""
    v = _vault_with(tmp_path)
    _src(v, "sources/b", "MISP galaxy")          # graded B in the fixture registry
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "0 cleared" in out and "below the A bar" in out


def test_a_pack_can_lower_the_bar_to_b(tmp_path, capsys):
    """okcti-test: bar=A clears 18 held source pages, bar=B clears 84. That is a trust decision
    belonging to the pack, so it is configuration rather than a hardcoded engine constant."""
    v = _vault_with(tmp_path, SCHEMA + "review_autoverify:\n  source_self_grade: B\n")
    _src(v, "sources/b", "MISP galaxy")
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out


def test_an_ungraded_publisher_holds_the_page(tmp_path, capsys):
    v = _vault_with(tmp_path)
    _src(v, "sources/u", "Some Blog")
    m = _load(v)
    assert m.main([]) == 0
    assert "not in source_registry" in capsys.readouterr().out


def test_a_source_page_with_no_publisher_holds(tmp_path, capsys):
    v = _vault_with(tmp_path)
    _src(v, "sources/n")
    m = _load(v)
    assert m.main([]) == 0
    assert "no publisher" in capsys.readouterr().out


def test_self_grading_can_be_disabled_by_the_pack(tmp_path, capsys):
    """`none` restores the pre-#549 behaviour: a source page is graded by what it cites, which for
    a primary document is nothing — so it stays held."""
    v = _vault_with(tmp_path, SCHEMA + "review_autoverify:\n  source_self_grade: none\n")
    _src(v, "sources/a", "Microsoft")
    m = _load(v)
    assert m.main([]) == 0
    assert "0 cleared" in capsys.readouterr().out


def test_non_source_pages_are_unaffected(tmp_path, capsys):
    """An actor page publishing under an A-grade outlet must NOT self-clear — its claim still has
    to be corroborated by what it cites. Self-grading applies to documents, not assertions."""
    v = _vault_with(tmp_path)
    p = v / "wiki" / "entities" / "x.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: malware\nname: N\npublisher: Microsoft\nneeds_review: true\n---\n\nb\n",
                 encoding="utf-8")
    m = _load(v)
    assert m.main([]) == 0
    # A non-source page must NOT be graded by its own `publisher` -- self-grading applies to
    # documents, not assertions. Uses `malware` so the assertion is about self-grading and not
    # about the always-publish list (okengine#554).
    assert "0 cleared" in capsys.readouterr().out


# --- mutation-gate hardening (okengine#549 registration, #552) -------------
# These pin behaviour the module always had but nothing asserted: boundary constants in
# _source_page's path guards, _basis's grammar/truncation, the main loop's skip-vs-stop
# semantics, and the frontmatter splice. Every one corresponds to a mutant that survived.

def test_an_overlong_path_part_returns_none_instead_of_raising(tmp_path):
    """The guard exists so a legacy prose citation cannot crash the nightly lane. What is
    observable is that an overlong ref yields None rather than an OSError — the exact byte
    boundary is not, because the `except OSError` fallback returns None as well (see the
    equivalent-mutant dispositions in mutation/survivors.json)."""
    v = _vault_with(tmp_path)
    m = _load(v)
    _src(v, "sources/normal", "Microsoft", flagged=False)
    assert m._source_page("sources/normal") is not None
    assert m._source_page("sources/" + "s" * 300) is None


def test_an_overlong_key_is_rejected_before_touching_the_filesystem(tmp_path):
    """A legacy prose citation can exceed PATH_MAX; resolving it crashed the whole nightly lane.
    The 4096-byte ceiling is the guard, and it is one byte from being untested."""
    v = _vault_with(tmp_path)
    m = _load(v)
    deep = "sources/" + "/".join("d" * 100 for _ in range(41))     # > 4096 bytes, parts all legal
    assert len(deep.encode()) > 4096
    assert m._source_page(deep) is None


def test_an_ambiguous_basename_resolves_to_nothing(tmp_path):
    """Exactly one hit resolves; two is ambiguous and must NOT silently pick the first — that
    would attribute a page's evidence to whichever shard sorted first."""
    v = _vault_with(tmp_path)
    m = _load(v)
    _src(v, "sources/2024/dup", "Microsoft", flagged=False)
    _src(v, "sources/2025/dup", "Microsoft", flagged=False)
    assert m._source_page("sources/nowhere/dup") is None
    _src(v, "sources/2024/solo", "Microsoft", flagged=False)
    assert m._source_page("sources/nowhere/solo") is not None


def test_basis_is_singular_for_one_source_and_plural_for_two(tmp_path):
    v = _vault_with(tmp_path)
    m = _load(v)
    assert m._basis({"A": ["Microsoft"]}) == "1 A-grade source: Microsoft"
    assert m._basis({"B": ["MISP galaxy", "URLhaus"]}).startswith("2 B-grade sources: ")


def test_basis_names_at_most_four_sources_but_counts_them_all(tmp_path):
    """The count and the name list are deliberately different lengths — a basis that claimed
    '5 sources' while listing 5 would have no truncation, and one that said '4' would lie."""
    v = _vault_with(tmp_path)
    m = _load(v)
    out = m._basis({"B": ["b1", "b2", "b3", "b4", "b5"]})
    assert out.startswith("5 B-grade sources: ")
    assert "b4" in out and "b5" not in out


def test_a_skipped_page_does_not_stop_the_scan(tmp_path, capsys):
    """Each `continue` in the scan loop is a SKIP, never a STOP. If any became a break, one
    structural or unflagged file early in sort order would silently end the run and leave the
    rest of the vault unprocessed — a silent no-op that looks like a clean pass."""
    v = _vault_with(tmp_path)
    (v / "wiki" / "sources").mkdir(parents=True, exist_ok=True)
    (v / "wiki" / "sources" / "_skip.md").write_text("---\ntype: source\n---\n", encoding="utf-8")
    (v / "wiki" / "sources" / "aaa-no-frontmatter.md").write_text("no frontmatter\n", encoding="utf-8")
    _src(v, "sources/aab-unflagged", "Microsoft", flagged=False)
    _src(v, "sources/zzz-clearable", "Microsoft")            # sorts LAST
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out


def test_a_held_page_does_not_stop_the_scan_and_is_counted(tmp_path, capsys):
    """Same for the held branch: a held page must not end the run, and the held tally must
    increment by exactly one per page."""
    v = _vault_with(tmp_path)
    _src(v, "sources/aaa-held", "Unknown Outlet")             # ungraded -> held, sorts first
    _src(v, "sources/aab-held2", "Unknown Outlet")
    _src(v, "sources/zzz-clear", "Microsoft")
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "1 cleared on evidence" in out and "2 held for human review" in out, out
    # these are `source` pages, not an always-publish type, so nothing takes the unverified branch
    assert "0 published unverified" in out, out


def test_only_the_flag_line_is_replaced_and_the_body_survives(tmp_path):
    """The stamp substitutes the needs_review line exactly once and splices the body back from
    the frontmatter match end. A wrong count or offset silently eats page content."""
    v = _vault_with(tmp_path)
    p = v / "wiki" / "sources" / "x.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: source\npublisher: Microsoft\nneeds_review: true\n---\n\n"
                 "# Heading\n\nneeds_review: true\n\nbody text\n", encoding="utf-8")
    m = _load(v)
    assert m.main([]) == 0
    out = p.read_text()
    assert out.count("review_status: auto-verified") == 1
    assert "# Heading" in out and "body text" in out
    assert "needs_review: true" in out.split("---", 2)[2]      # the BODY occurrence is untouched


# --- second mutation pass (okengine#552) ----------------------------------

def test_the_pack_can_disable_the_lane_entirely(tmp_path, capsys):
    v = _vault_with(tmp_path, SCHEMA + "review_autoverify:\n  enabled: false\n")
    _src(v, "sources/a", "Microsoft")
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "disabled by schema" in out
    assert '"wakeAgent": false' in out and '"wakeAgent": true' not in out
    assert "auto-verified" not in (v / "wiki" / "sources" / "a.md").read_text()


def test_required_fields_drops_only_the_type_key(tmp_path):
    """`type` is always present by construction, so it is excluded from the missing-field check;
    every OTHER declared required field must survive the filter."""
    v = _vault_with(tmp_path)
    m = _load(v)
    # Built from YAML, not Python literals: a parsed "type" is NOT the interned literal
    # (`str(f) is "type"` is False), so an identity comparison here behaves differently from
    # equality — which a literal-only fixture cannot detect.
    schema = yaml.safe_load("types:\n  actor: {required: [type, name, aliases]}\n")
    assert m._required_fields(schema, "actor") == ["name", "aliases"]
    assert m._required_fields(schema, "absent") == []


def test_a_missing_required_field_holds_the_page(tmp_path, capsys):
    v = _vault_with(tmp_path, SCHEMA + "types:\n  source: {required: [type, name]}\n")
    _src(v, "sources/a", "Microsoft")            # no `name`
    m = _load(v)
    assert m.main([]) == 0
    assert "missing required: name" in capsys.readouterr().out


@pytest.mark.parametrize("ref", ["/sources/abs", "sources/../escape", "sources/./here",
                                 "sources/nothing-at-all"])
def test_unresolvable_refs_return_none(tmp_path, ref):
    """Absolute paths, traversal segments and plain misses each return None. Nothing pinned
    these guards, so an `or` collapsing to `and` disabled all of them at once."""
    v = _vault_with(tmp_path)
    m = _load(v)
    _src(v, "sources/real", "Microsoft", flagged=False)
    assert m._source_page(ref) is None


def test_grade_evidence_skips_bad_refs_without_abandoning_the_rest(tmp_path):
    """The skip in the evidence loop is a SKIP: a non-string ref or an unresolvable one must not
    stop the page's remaining citations from being graded."""
    v = _vault_with(tmp_path)
    m = _load(v)
    _src(v, "sources/good", "Microsoft", flagged=False)
    graded = m._grade_evidence({"sources": [42, "sources/missing", "sources/good"]},
                               {"Microsoft": "A"})
    assert graded == {"A": ["Microsoft"]}


def test_an_ungraded_or_unnamed_publisher_contributes_nothing(tmp_path):
    """Both a grade AND a label are required — either alone must not enter the tally."""
    v = _vault_with(tmp_path)
    m = _load(v)
    _src(v, "sources/nopub", "", flagged=False)
    assert m._grade_evidence({"sources": ["sources/nopub"]}, {"Microsoft": "A"}) == {}
    assert m._grade_evidence({"sources": ["Unknown Outlet"]}, {"Microsoft": "A"}) == {}


def test_basis_stays_singular_below_and_plural_above_one(tmp_path):
    v = _vault_with(tmp_path)
    m = _load(v)
    assert "1 A-grade source:" in m._basis({"A": ["x"]})
    assert "3 A-grade sources:" in m._basis({"A": ["x", "y", "z"]})


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
def test_every_exit_path_emits_wake_agent_false(tmp_path, capsys, argv):
    """The lane is no_agent: emitting wakeAgent=true would wake a model on every nightly run."""
    v = _vault_with(tmp_path)
    _src(v, "sources/a", "Microsoft")
    m = _load(v)
    assert m.main(argv) == 0
    assert '"wakeAgent": false' in capsys.readouterr().out


def test_missing_wiki_emits_wake_agent_false(tmp_path, capsys):
    m = _load(tmp_path / "absent")
    assert m.main([]) == 1
    out = capsys.readouterr().out
    assert '"wakeAgent": false' in out and '"wakeAgent": true' not in out


def test_absent_registry_emits_wake_agent_false(tmp_path, capsys):
    """Asserted on its OWN captured output: sharing capsys with another run let the first run's
    `false` satisfy the assertion no matter what this exit path emitted."""
    v = _vault_with(tmp_path, "types:\n  source: {required: [type]}\n")
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "no source_registry" in out
    assert '"wakeAgent": false' in out and '"wakeAgent": true' not in out


def test_dry_run_is_announced_and_writes_nothing(tmp_path, capsys):
    v = _vault_with(tmp_path)
    p = _src(v, "sources/a", "Microsoft")
    before = p.read_text()
    m = _load(v)
    assert m.main(["--dry-run"]) == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert p.read_text() == before
    m2 = _load(v)
    assert m2.main([]) == 0
    assert "[dry-run]" not in capsys.readouterr().out


def test_structural_files_are_skipped_individually(tmp_path, capsys):
    """Underscore, INDEX and .bak are three independent skip reasons — an `or` collapsing to
    `and` would let all three through."""
    v = _vault_with(tmp_path)
    d = v / "wiki" / "sources"
    d.mkdir(parents=True, exist_ok=True)
    body = "---\ntype: source\npublisher: Microsoft\nneeds_review: true\n---\n\nb\n"
    for n in ("_a.md", "INDEX.md", "c.bak.md"):
        (d / n).write_text(body, encoding="utf-8")
    m = _load(v)
    assert m.main([]) == 0
    assert "0 cleared" in capsys.readouterr().out


def test_a_status_sorting_after_tombstoned_is_not_treated_as_tombstoned(tmp_path, capsys):
    """`== "tombstoned"` must be equality, not an ordering — `>= ` would sweep in every status
    that sorts later (e.g. 'withdrawn') and hold pages for the wrong reason."""
    v = _vault_with(tmp_path)
    p = v / "wiki" / "sources" / "w.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: source\npublisher: Microsoft\nstatus: withdrawn\n"
                 "needs_review: true\n---\n\nb\n", encoding="utf-8")
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "1 cleared" in out and "tombstoned" not in out


def test_a_type_sorting_after_source_does_not_self_grade(tmp_path, capsys):
    """Self-grading applies to `type: source` exactly. `>=` would sweep in 'trend', 'vendor',
    'tool' — every type sorting later — and let them clear from their own publisher."""
    v = _vault_with(tmp_path)
    p = v / "wiki" / "entities" / "t.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: vendor\npublisher: Microsoft\nneeds_review: true\n---\n\nb\n",
                 encoding="utf-8")
    m = _load(v)
    assert m.main([]) == 0
    assert "0 cleared" in capsys.readouterr().out


def test_a_publisher_above_the_bar_clears_it(tmp_path, capsys):
    """`own <= bar` is an ordering, not equality: with the bar at B an A-grade publisher must
    still clear. `==` would hold every source better than the configured bar."""
    v = _vault_with(tmp_path, SCHEMA + "review_autoverify:\n  source_self_grade: B\n")
    _src(v, "sources/a", "Microsoft")            # A, bar is B
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out


def test_more_corroboration_than_the_bar_still_clears(tmp_path, capsys):
    """`>=` is a floor, not an exact match — three B-grade publishers must clear a 2xB bar."""
    v = _vault_with(tmp_path, SCHEMA + "types:\n  actor: {required: [type]}\n"
                    + "  # third B so >= 2 is strictly more than == 2\n")
    (v / "schema.yaml").write_text(
        (v / "schema.yaml").read_text().replace("  URLhaus: {reliability: B}",
                                                "  URLhaus: {reliability: B}\n  ThirdWire: {reliability: B}"),
        encoding="utf-8")
    for n, pub in (("b1", "MISP galaxy"), ("b2", "URLhaus"), ("b3", "ThirdWire")):
        _src(v, f"sources/{n}", pub, flagged=False)
    p = v / "wiki" / "entities" / "x.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: actor\nneeds_review: true\nsources:\n"
                 "  - sources/b1\n  - sources/b2\n  - sources/b3\n---\n\nb\n", encoding="utf-8")
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out


def test_a_required_field_sorting_after_type_is_kept(tmp_path):
    """The filter is inequality, not ordering: `url` sorts AFTER `type` and must still be
    required. `<` would silently drop every field later in the alphabet."""
    v = _vault_with(tmp_path)
    m = _load(v)
    assert m._required_fields({"types": {"a": {"required": ["type", "url", "name"]}}}, "a") \
        == ["url", "name"]


def test_enabled_accepts_only_a_real_false(tmp_path, capsys):
    """`is not False` is identity: a pack writing `enabled: 0` has NOT disabled the lane, and
    `!= False` / `> False` would silently treat that integer as off."""
    v = _vault_with(tmp_path, SCHEMA + "review_autoverify:\n  enabled: 0\n")
    _src(v, "sources/a", "Microsoft")
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "disabled by schema" not in out and "1 cleared" in out


def test_a_traversal_ref_cannot_escape_the_wiki(tmp_path):
    """The `..` guard is load-bearing: without it this ref resolves to a real file OUTSIDE the
    sources tree and its publisher would be credited as evidence."""
    v = _vault_with(tmp_path)
    m = _load(v)
    (v / "wiki" / "escape.md").write_text(
        "---\ntype: source\npublisher: Microsoft\n---\n\nb\n", encoding="utf-8")
    assert (v / "wiki" / "escape.md").is_file()
    assert m._source_page("sources/../escape") is None


def test_a_source_page_that_vanishes_mid_scan_is_skipped_not_fatal(tmp_path, monkeypatch):
    """A reshelve can delete a cited page between resolve and read. That must skip the ref, not
    abandon the remaining citations."""
    v = _vault_with(tmp_path)
    m = _load(v)
    _src(v, "sources/gone", "MITRE ATT&CK", flagged=False)
    _src(v, "sources/ok", "Microsoft", flagged=False)
    real = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "gone.md":
            raise OSError("vanished")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    assert m._grade_evidence({"sources": ["sources/gone", "sources/ok"]},
                             {"Microsoft": "A", "MITRE ATT&CK": "A"}) == {"A": ["Microsoft"]}


def test_an_unreadable_page_does_not_end_the_vault_scan(tmp_path, monkeypatch, capsys):
    v = _vault_with(tmp_path)
    _src(v, "sources/aaa-unreadable", "Microsoft")
    _src(v, "sources/zzz-clearable", "Microsoft")
    m = _load(v)
    real = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "aaa-unreadable.md":
            raise OSError("unreadable")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out


def test_a_unique_basename_hit_is_returned_and_two_are_not(tmp_path):
    v = _vault_with(tmp_path)
    m = _load(v)
    _src(v, "sources/2024/only", "Microsoft", flagged=False)
    got = m._source_page("sources/elsewhere/only")
    assert got is not None and got.name == "only.md"


def test_exactly_one_flag_line_is_replaced(tmp_path):
    """count=1 is exact: with the flag duplicated in frontmatter, ONE line is stamped and the
    other is left. count=0 or 2 would rewrite both.

    NOTE: this pins current behaviour, and that behaviour is arguably a latent bug — the
    surviving `needs_review: true` re-flags the page on the next run. Duplicated frontmatter keys
    are malformed input, so it is out of scope for #549; recorded here so the next reader sees it
    was measured rather than missed."""
    v = _vault_with(tmp_path)
    p = v / "wiki" / "sources" / "d.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: source\npublisher: Microsoft\nneeds_review: true\n"
                 "needs_review: true\n---\n\nbody\n", encoding="utf-8")
    m = _load(v)
    assert m.main([]) == 0
    head = p.read_text().split("---")[1]
    assert head.count("review_status: auto-verified") == 1
    assert head.count("needs_review: true") == 1


def test_the_stamped_page_keeps_exactly_one_frontmatter_block(tmp_path):
    """head is group(1) — the frontmatter INTERIOR. group(0) would re-embed the delimiters and
    produce a page with a doubled `---` fence."""
    v = _vault_with(tmp_path)
    p = _src(v, "sources/g", "Microsoft")
    m = _load(v)
    assert m.main([]) == 0
    assert p.read_text().count("---") == 2


# --- okengine#554: actors always publish; confidence + consensus are DERIVED -------------------
def test_actor_with_no_qualifying_evidence_still_publishes(tmp_path, capsys):
    """The model: attribution is published and the confidence field carries the uncertainty. The
    old bar held 874 of 1209 actor pages waiting for evidence to cross a threshold, which gates
    publication on the very doubt `attribution_confidence` exists to express."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "c" / "cephalus.md"
    _page(page, {"type": "actor", "name": "Cephalus", "needs_review": True,
                 "attribution_confidence": "suspected", "sources": ["Nobody In Particular"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert "needs_review" not in fm, "an actor page must never be held for a human"
    # published, but NOT claimed as verified: no graded publisher backs it, so there is no basis to
    # cite and nothing was verified (okengine#563 — this used to stamp `auto-verified` with an empty
    # `auto_verified_basis`, which is the same dishonesty `consensus: 0` below exists to avoid)
    assert fm["review_status"] == "unverified-no-evidence"
    assert "auto_verified_basis" not in fm
    assert fm["consensus"] == 0, "no graded publisher backs it -- say so honestly"
    # the lane's own lean is preserved, NOT invented from absent evidence
    assert fm["attribution_confidence"] == "suspected"


def test_non_actor_variant_cannot_escape_via_actor_always_publish(tmp_path, capsys):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "t" / "terminalfix.md"
    _page(
        page,
        {"type": "actor", "title": "TerminalFix", "needs_review": True,
         "actor_type": "cybercriminal", "sources": ["Microsoft"]},
        "A new ClickFix variant, dubbed TerminalFix, tricks users into running commands.",
    )
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["needs_review"] is True
    assert "review_status" not in fm
    assert "actor evidence defines a clickfix variant" in capsys.readouterr().out


def test_explicitly_unvalidated_actor_cannot_escape_always_publish(tmp_path, capsys):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "u" / "unvalidated.md"
    _page(
        page,
        {"type": "actor", "title": "Unvalidated", "needs_review": True,
         "actor_identity_validated": False, "sources": ["Microsoft"]},
        "Unvalidated is a named threat actor.",
    )
    assert _load(v).main([]) == 0
    assert _fm(page)["needs_review"] is True
    assert "explicitly unvalidated" in capsys.readouterr().out


def test_single_B_source_derives_moderate_with_consensus_one(tmp_path):
    """MISP galaxy alone is the corpus's dominant shape: 789 of 857 actor pages. One B-grade
    publisher is real but uncorroborated evidence -> `moderate`, consensus 1."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-b.md"
    _page(page, {"type": "actor", "name": "APT B", "needs_review": True,
                 "attribution_confidence": "unverified", "sources": ["MISP galaxy"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["attribution_confidence"] == "moderate", "B x1 derives moderate"
    assert fm["consensus"] == 1
    assert "MISP galaxy" in fm["auto_verified_basis"]


def test_two_distinct_B_publishers_raise_the_band(tmp_path):
    """Corroboration is the axis that separates a lone claim from a supported one."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-bb.md"
    _page(page, {"type": "actor", "name": "APT BB", "needs_review": True,
                 "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["attribution_confidence"] == "high", "B x2 outranks B x1"
    assert fm["consensus"] == 2


def test_an_A_source_derives_confirmed(tmp_path):
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-a.md"
    _page(page, {"type": "actor", "name": "APT A", "needs_review": True,
                 "attribution_confidence": "low", "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["attribution_confidence"] == "confirmed", "A is completely reliable -> confirmed"
    assert fm["consensus"] == 1


def test_consensus_counts_publishers_not_pages(tmp_path):
    """Two articles from ONE outlet are one voice, so `consensus` must not reach 2.

    The dedup is enforced in _grade_evidence, which keys by publisher -- _consensus is only a count
    over labels that are already distinct. An earlier version of this docstring claimed to pin
    _consensus's own set(); it does not, and swapping that set() for a plain sum leaves every test
    green. Said plainly so the next reader does not mistake this for coverage it lacks: the
    property is real and enforced, just one layer up.
    """
    v = _vault(tmp_path)
    for n in ("one", "two"):
        _page(v / "wiki" / "sources" / f"{n}.md",
              {"type": "source", "publisher": "MISP galaxy", "published": "2026-01-01"})
    page = v / "wiki" / "entities" / "a" / "apt-dup.md"
    _page(page, {"type": "actor", "name": "APT Dup", "needs_review": True,
                 "sources": ["sources/one", "sources/two"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["consensus"] == 1, "same publisher twice is ONE voice, not corroboration"
    assert fm["attribution_confidence"] == "moderate", "so it must not reach the B x2 band"


def test_a_conflict_still_holds_an_actor(tmp_path, capsys):
    """always-publish drops the EVIDENCE-BAR hold only. Genuine disagreement between sources must
    still escalate -- 0 of 1209 actor pages carry one today, and that escape hatch stays."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-x.md"
    _page(page, {"type": "actor", "name": "APT X", "needs_review": True,
                 "conflicts": ["RU per vendor A, CN per vendor B"], "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm.get("needs_review") is True, "a recorded conflict must still hold the page"
    assert "consensus" not in fm


def test_derived_confidence_replaces_rather_than_duplicates(tmp_path):
    """A second attribution_confidence key would make the frontmatter ambiguous and let
    last-writer-wins decide silently."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "a" / "apt-once.md"
    _page(page, {"type": "actor", "name": "APT Once", "needs_review": True,
                 "attribution_confidence": "unverified", "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    assert page.read_text().count("attribution_confidence:") == 1
    assert _fm(page)["attribution_confidence"] == "confirmed"


def test_non_actor_types_keep_the_evidence_bar(tmp_path, capsys):
    """always_publish_types is a narrow list. A malware page with no qualifying evidence must NOT
    start auto-publishing as a side effect."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "m" / "thing.md"
    _page(page, {"type": "malware", "name": "Thing", "needs_review": True,
                 "sources": ["Nobody In Particular"]})
    assert _load(v).main([]) == 0
    assert _fm(page).get("needs_review") is True


# --- okengine#563: never claim a verification that did not happen --------------------------------
def test_no_evidence_publishes_but_does_not_claim_auto_verified(tmp_path):
    """An always-publish type with zero graded evidence must still LEAVE the human queue -- that is
    the point of always_publish -- but must not be stamped `auto-verified`.

    Before this, such a page got `review_status: auto-verified` with `consensus: 0` and an EMPTY
    `auto_verified_basis`: a verification claim the lane's own basis field admitted was hollow. 50
    records on one live vault carried it.
    """
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "n" / "no-evidence.md"
    _page(page, {"type": "actor", "name": "NoEvidence", "needs_review": True})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert "needs_review" not in fm, "must still leave the human queue"
    assert fm["review_status"] == "unverified-no-evidence", fm
    assert "auto_verified_basis" not in fm, "an empty basis must never be emitted"
    assert "auto_verified_at" not in fm, "no verification happened, so nothing was verified at a time"
    assert fm["consensus"] == 0
    assert fm["review_checked_at"]


def test_ungraded_source_also_publishes_unverified(tmp_path):
    """Same rule when sources exist but none grade: the arithmetic found nothing, so there is no
    basis to cite and no verification to claim."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "u" / "ungraded.md"
    _page(page, {"type": "actor", "name": "Ungraded", "needs_review": True,
                 "sources": ["Some Blog Nobody Graded"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["review_status"] == "unverified-no-evidence", fm
    assert "auto_verified_basis" not in fm


def test_auto_verified_is_never_stamped_with_an_empty_basis(tmp_path):
    """THE INVARIANT, asserted over every page the lane touches rather than one shape: if a page
    claims `auto-verified`, it must carry a non-empty basis proving why. This is the detector -- it
    fails for any future path that reintroduces a hollow verification claim."""
    v = _vault(tmp_path)
    shapes = {
        "graded":   {"type": "actor", "name": "Graded", "sources": ["Microsoft"]},
        "ungraded": {"type": "actor", "name": "Ungraded", "sources": ["Nobody Graded This"]},
        "none":     {"type": "actor", "name": "NoneAtAll"},
        "two_b":    {"type": "actor", "name": "TwoB", "sources": ["MISP galaxy", "URLhaus"]},
    }
    pages = {}
    for key, fm in shapes.items():
        p = v / "wiki" / "entities" / key[0] / f"{key}.md"
        _page(p, {**fm, "needs_review": True})
        pages[key] = p
    assert _load(v).main([]) == 0
    for key, p in pages.items():
        fm = _fm(p)
        if fm.get("review_status") == "auto-verified":
            assert fm.get("auto_verified_basis"), (
                f"{key}: claims auto-verified with an empty/absent basis — a hollow verification")
    # and the fixture must actually exercise both branches, or the loop above is vacuous
    got = {_fm(p).get("review_status") for p in pages.values()}
    assert got == {"auto-verified", "unverified-no-evidence"}, got


def test_an_existing_hollow_claim_is_repaired_without_needs_review(tmp_path, capsys):
    """SELF-HEALING: a page already stamped `auto-verified` never carries needs_review again, so
    without a second entry condition the 50 hollow claims on the live vault would persist forever
    and every deployment would need a one-off repair script. The lane must pick them up itself."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "h" / "hollow.md"
    _page(page, {"type": "actor", "name": "Hollow", "review_status": "auto-verified",
                 "consensus": 0, "auto_verified_basis": "", "auto_verified_at": "2026-08-06T02:00:00Z"})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["review_status"] == "unverified-no-evidence", fm
    assert "auto_verified_basis" not in fm
    assert "auto_verified_at" not in fm, "the stale verification timestamp must go with the claim"
    assert fm["review_checked_at"]
    assert "needs_review" not in fm, "repair must not push the page back into a human queue"


def test_repair_promotes_to_verified_when_evidence_has_since_landed(tmp_path):
    """If sources arrived after the hollow stamp, the repair pass grades them properly rather than
    flattening the page to unverified."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "l" / "landed.md"
    _page(page, {"type": "actor", "name": "Landed", "review_status": "auto-verified",
                 "consensus": 0, "auto_verified_basis": "", "sources": ["Microsoft"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["review_status"] == "auto-verified"
    assert "Microsoft" in fm["auto_verified_basis"]
    assert fm["consensus"] == 1


def test_a_sound_auto_verified_page_is_left_alone(tmp_path):
    """The repair condition keys on an EMPTY basis, so a properly verified page must not be
    rewritten -- otherwise every nightly run churns the whole corpus."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "s" / "sound.md"
    # `attribution_confidence` included: with re-entry the lane keeps the field's PRESENCE
    # consistent with policy, so a page missing a band its evidence supports is NOT yet sound and
    # is legitimately rewritten. The anti-churn property under test is about a fully consistent page.
    _page(page, {"type": "actor", "name": "Sound", "review_status": "auto-verified",
                 "consensus": 1, "auto_verified_basis": "1 A-grade source: Microsoft",
                 "auto_verified_at": "2026-08-01T00:00:00Z", "sources": ["Microsoft"],
                 "attribution_confidence": "confirmed"})
    before = page.read_text()
    assert _load(v).main([]) == 0
    assert page.read_text() == before, "an already-sound page must not be rewritten"


def test_repair_does_not_duplicate_keys(tmp_path):
    """The repair path appends a stamp after stripping the stale keys; a missed strip would leave
    the page carrying BOTH claims, and YAML last-writer-wins would hide it."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "d" / "dup.md"
    _page(page, {"type": "actor", "name": "Dup", "review_status": "auto-verified",
                 "consensus": 0, "auto_verified_basis": "", "auto_verified_at": "2026-08-06T02:00:00Z"})
    assert _load(v).main([]) == 0
    head = page.read_text().split("---")[1]
    assert head.count("review_status:") == 1, head
    assert head.count("consensus:") == 1, head
    assert "auto-verified" not in head, head


def test_evidence_landing_after_an_unverified_stamp_is_re_graded(tmp_path):
    """Order-of-operations gap found by running the lanes end-to-end rather than trusting units:
    if autoverify stamps `unverified-no-evidence` and source_normalize LATER lifts body citations
    into `sources:`, the page must be re-graded. Otherwise it says "no evidence" forever while
    citing an A-grade publisher."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "l" / "late.md"
    _page(page, {"type": "actor", "name": "Late", "needs_review": True})
    m = _load(v)
    assert m.main([]) == 0
    assert _fm(page)["review_status"] == "unverified-no-evidence"

    # evidence arrives afterwards (what source_normalize does)
    txt = page.read_text().replace("review_status:", "sources:\n- Microsoft\nreview_status:", 1)
    page.write_text(txt, encoding="utf-8")

    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["review_status"] == "auto-verified", fm
    assert "Microsoft" in fm["auto_verified_basis"]


def test_a_still_unevidenced_page_is_not_rewritten_every_run(tmp_path):
    """Re-entry must not mean re-stamping: rewriting only to move `review_checked_at` would churn
    the corpus nightly and break the idempotence the drains rely on."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "s" / "stillnone.md"
    _page(page, {"type": "actor", "name": "StillNone", "needs_review": True})
    m = _load(v)
    assert m.main([]) == 0
    once = page.read_text()
    assert m.main([]) == 0
    assert page.read_text() == once, "a still-unevidenced page must not be rewritten"


def test_operator_evidence_refs_are_graded_like_sources(tmp_path):
    """okengine#563: operator_evidence_refs is a second evidence channel. Reading only `sources`
    left it uncounted on 1299 entity pages — a page published `moderate`/consensus 1 while holding
    two distinct B-grade publishers, which is 2xB -> `high`, consensus 2."""
    v = _vault(tmp_path)
    _page(v / "wiki" / "sources" / "op" / "rec.md",
          {"type": "source", "publisher": "URLhaus", "url": "https://example.invalid/r"})
    page = v / "wiki" / "entities" / "a" / "anon.md"
    _page(page, {"type": "actor", "name": "Anon", "needs_review": True,
                 "sources": ["MISP galaxy"],                       # 1xB
                 "operator_evidence_refs": ["sources/op/rec"]})     # + a DIFFERENT B publisher
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["consensus"] == 2, fm
    assert fm["attribution_confidence"] == "high", fm
    assert "URLhaus" in fm["auto_verified_basis"] and "MISP galaxy" in fm["auto_verified_basis"]


def test_operator_evidence_from_the_same_publisher_is_not_corroboration(tmp_path):
    """Distinctness is by PUBLISHER, not by record: two records from one originator are one voice.
    Without this, a page citing an aggregator's many records would self-corroborate to `high`."""
    v = _vault(tmp_path)
    for i in (1, 2):
        _page(v / "wiki" / "sources" / "op" / f"r{i}.md",
              {"type": "source", "publisher": "MISP galaxy", "url": f"https://example.invalid/{i}"})
    page = v / "wiki" / "entities" / "s" / "same.md"
    _page(page, {"type": "actor", "name": "Same", "needs_review": True,
                 "operator_evidence_refs": ["sources/op/r1", "sources/op/r2"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["consensus"] == 1, "two records from one publisher are one voice"
    assert fm["attribution_confidence"] == "moderate", fm


def test_aggregator_carried_record_grades_by_originator_not_aggregator(tmp_path):
    """A record an aggregator merely CARRIED is owned by its originator: `publisher` is the
    originator and `retrieved_via` the aggregator. Grading must read `publisher`, so the aggregator
    is never treated as a source in its own right."""
    v = _vault(tmp_path)
    _page(v / "wiki" / "sources" / "priv" / "rec.md",
          {"type": "source", "publisher": "Microsoft", "retrieved_via": "SOME-AGGREGATOR",
           "url": "https://example.invalid/x"})
    page = v / "wiki" / "entities" / "c" / "carried.md"
    _page(page, {"type": "actor", "name": "Carried", "needs_review": True,
                 "operator_evidence_refs": ["sources/priv/rec"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert "Microsoft" in fm["auto_verified_basis"], fm
    assert "AGGREGATOR" not in fm["auto_verified_basis"], "the carrier must never be cited as source"


def test_re_flagged_page_does_not_keep_a_stale_stamp(tmp_path):
    """A page can carry needs_review AND an earlier stamp: an enrichment lane re-raises the flag
    after this one has already cleared it. The flagged path used to substitute only over the
    needs_review line, leaving DUPLICATE consensus/auto_verified_basis keys — and YAML last-wins
    kept the STALE pair, so the lane logged the correct basis while the page kept the old one.

    Caught on a real page (nation-state actor re-flagged by an enrichment lane), not in fixtures."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "r" / "reflagged.md"
    _page(page, {"type": "actor", "name": "Reflagged", "needs_review": True,
                 # the earlier, now-stale stamp — deliberately AFTER needs_review in key order
                 "review_status": "auto-verified", "consensus": 1,
                 "auto_verified_basis": "1 B-grade source: MISP galaxy",
                 "auto_verified_at": "2026-08-04T02:45:41Z",
                 "sources": ["MISP galaxy", "URLhaus"]})          # evidence has since grown to 2xB
    assert _load(v).main([]) == 0
    head = page.read_text().split("---")[1]
    assert head.count("consensus:") == 1, head
    assert head.count("auto_verified_basis:") == 1, head
    fm = _fm(page)
    assert fm["consensus"] == 2, f"stale consensus survived: {fm}"
    assert "URLhaus" in fm["auto_verified_basis"], f"stale basis survived: {fm}"
    assert "needs_review" not in fm


def test_a_well_formed_but_stale_stamp_is_re_graded(tmp_path):
    """The gap that shipped with the second evidence channel: a stamp can be well-formed and still
    STALE. Entry conditions keyed only on BROKEN stamps, so 1299 already-stamped pages could never
    pick up the widened grading rule — the fix was live and unreachable. Caught on the live fleet
    mid-roll, not in fixtures."""
    v = _vault(tmp_path)
    _page(v / "wiki" / "sources" / "op" / "rec.md",
          {"type": "source", "publisher": "URLhaus", "url": "https://example.invalid/r"})
    page = v / "wiki" / "entities" / "s" / "stale.md"
    _page(page, {"type": "actor", "name": "Stale",
                 # a VALID stamp from before the second channel was graded
                 "review_status": "auto-verified", "consensus": 1,
                 "auto_verified_basis": "1 B-grade source: MISP galaxy",
                 "auto_verified_at": "2026-08-04T02:45:41Z",
                 "sources": ["MISP galaxy"],
                 "operator_evidence_refs": ["sources/op/rec"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["consensus"] == 2, f"stale stamp never re-graded: {fm}"
    assert "URLhaus" in fm["auto_verified_basis"], fm
    assert fm["attribution_confidence"] == "high", fm


def test_a_settled_corpus_is_not_rewritten(tmp_path):
    """Re-entering every stamped page must not mean rewriting them: without the material-change
    check the lane would rewrite the whole corpus nightly just to move a timestamp."""
    v = _vault(tmp_path)
    page = v / "wiki" / "entities" / "s" / "settled.md"
    _page(page, {"type": "actor", "name": "Settled", "needs_review": True, "sources": ["Microsoft"]})
    m = _load(v)
    assert m.main([]) == 0
    once = page.read_text()
    assert m.main([]) == 0 and m.main([]) == 0
    assert page.read_text() == once, "a settled page must not be rewritten on later runs"


# --- okengine#563: an enrichment quarantine outranks evidence grade -------------------------------
def test_hold_when_field_outranks_even_A_grade_evidence(tmp_path, capsys):
    """Grading answers "are the citations good?", never "is this the right entity?". A lane that
    flags identity ambiguity is asserting something evidence cannot resolve, so it must win — and
    it must win for an ALWAYS-PUBLISH type too, which is exactly the shape that flapped: the
    enrichment lane raised the flag, this lane cleared it on 2xA evidence, and the page's state
    depended on which ran last."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {hold_when_fields: [enrichment_quarantined]}\n")
    page = v / "wiki" / "entities" / "a" / "ambiguous.md"
    _page(page, {"type": "actor", "name": "Ambig", "needs_review": True,
                 "sources": ["Microsoft", "MITRE ATT&CK"],      # 2xA — would clear on evidence
                 "enrichment_quarantined": True})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert fm["needs_review"] is True, "an identity quarantine must not be cleared by evidence"
    assert "review_status" not in fm
    assert "enrichment_quarantined" in capsys.readouterr().out


def test_hold_when_fields_is_pack_declared_not_hardcoded(tmp_path):
    """The engine ships the MECHANISM only. With nothing declared, the same page clears — so no
    lane-specific or vendor-specific key is baked into the engine."""
    v = _vault(tmp_path)                                    # no hold_when_fields in schema
    page = v / "wiki" / "entities" / "a" / "ambiguous.md"
    _page(page, {"type": "actor", "name": "Ambig", "needs_review": True,
                 "sources": ["Microsoft"], "enrichment_quarantined": True})
    assert _load(v).main([]) == 0
    assert _fm(page)["review_status"] == "auto-verified"


def test_a_falsy_hold_field_does_not_hold(tmp_path):
    """The flag is only a hold when TRUTHY — a page carrying the field as false/absent is normal."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {hold_when_fields: [enrichment_quarantined]}\n")
    page = v / "wiki" / "entities" / "c" / "clean.md"
    _page(page, {"type": "actor", "name": "Clean", "needs_review": True,
                 "sources": ["Microsoft"], "enrichment_quarantined": False})
    assert _load(v).main([]) == 0
    assert _fm(page)["review_status"] == "auto-verified"


def test_a_soundly_stamped_page_newly_held_is_reported_accurately(tmp_path, capsys):
    """Re-entry covers every stamped page, so a sound stamp can meet a NEW refusal. Reporting that
    as a "stale claim with no basis" would misdescribe it — and its existing stamp is left alone
    rather than being torn off on the strength of another lane's flag."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {hold_when_fields: [enrichment_quarantined]}\n")
    page = v / "wiki" / "entities" / "s" / "sound.md"
    _page(page, {"type": "actor", "name": "Sound", "review_status": "auto-verified", "consensus": 1,
                 "auto_verified_basis": "1 A-grade source: Microsoft",
                 "auto_verified_at": "2026-08-04T00:00:00Z",
                 "sources": ["Microsoft"], "enrichment_quarantined": True})
    before = page.read_text()
    assert _load(v).main([]) == 0
    out = capsys.readouterr().out
    assert "now-held" in out and "stale-claim" not in out, out
    assert page.read_text() == before, "the existing stamp must be left in place"


# --- okengine#563: an analytic judgment governs its claim's confidence ----------------------------
def test_confidence_is_not_derived_when_a_judgment_governs_the_subject(tmp_path, capsys):
    """Evidence counting answers "how many graded publishers cite this page?" — never "how confident
    are we in the claim?". Treating the first as the second published an actor at `high` off two
    CATALOGUE listings while the assessment analysing that very association said `moderate`. Both
    rendered on one screen, disagreeing."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {defer_confidence_to_types: [assessment]}\n")
    _page(v / "wiki" / "assessments" / "a1.md",
          {"type": "assessment", "name": "A1", "subject": "entities/a/anon",
           "confidence": 0.65, "confidence_band": "moderate"})
    page = v / "wiki" / "entities" / "a" / "anon.md"
    _page(page, {"type": "actor", "name": "Anon", "needs_review": True,
                 "sources": ["MISP galaxy", "URLhaus"]})      # 2xB -> would derive `high`
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert "attribution_confidence" not in fm, f"derived a confidence the assessment governs: {fm}"
    assert fm["consensus"] == 2, "the evidence count itself is still reported"
    assert "deferred to an analytic judgment" in capsys.readouterr().out


def test_a_wikilink_subject_also_matches(tmp_path):
    """`subject` is written both as a bare path and as a wikilink across the corpus."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {defer_confidence_to_types: [assessment]}\n")
    _page(v / "wiki" / "assessments" / "a1.md",
          {"type": "assessment", "name": "A1", "subject": "[[entities/a/anon]]"})
    page = v / "wiki" / "entities" / "a" / "anon.md"
    _page(page, {"type": "actor", "name": "Anon", "needs_review": True,
                 "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    assert "attribution_confidence" not in _fm(page)


def test_an_ungoverned_page_still_derives_confidence(tmp_path):
    """The positive half — deferral must not become a blanket suppression."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {defer_confidence_to_types: [assessment]}\n")
    page = v / "wiki" / "entities" / "u" / "ungoverned.md"
    _page(page, {"type": "actor", "name": "U", "needs_review": True,
                 "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["attribution_confidence"] == "high"


def test_deferral_is_off_until_the_pack_names_the_types(tmp_path):
    """The engine ships the mechanism only — it must not assume what an assessment is called."""
    v = _vault(tmp_path)                                   # no defer_confidence_to_types
    _page(v / "wiki" / "assessments" / "a1.md",
          {"type": "assessment", "name": "A1", "subject": "entities/a/anon"})
    page = v / "wiki" / "entities" / "a" / "anon.md"
    _page(page, {"type": "actor", "name": "Anon", "needs_review": True,
                 "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    assert _fm(page)["attribution_confidence"] == "high"


def test_a_stale_derived_confidence_is_removed_when_a_judgment_appears(tmp_path):
    """A page stamped BEFORE the assessment existed must not keep the contradicting value — that is
    exactly the state the live vault was in."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {defer_confidence_to_types: [assessment]}\n")
    _page(v / "wiki" / "assessments" / "a1.md",
          {"type": "assessment", "name": "A1", "subject": "entities/a/anon"})
    page = v / "wiki" / "entities" / "a" / "anon.md"
    _page(page, {"type": "actor", "name": "Anon", "needs_review": True,
                 "attribution_confidence": "high", "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    assert "attribution_confidence" not in _fm(page)


def test_a_settled_page_is_re_entered_to_drop_a_governed_confidence(tmp_path):
    """The deferral is useless without this. A page already stamped and otherwise unchanged is
    skipped by the material-change check BEFORE deferral is considered, so every previously-derived
    value would survive — 3319 pages carry the field, and on the live vault only 1 page changed
    until the stale value was itself treated as a material change (then 535 did)."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {defer_confidence_to_types: [assessment]}\n")
    _page(v / "wiki" / "assessments" / "a1.md",
          {"type": "assessment", "name": "A1", "subject": "entities/a/settled"})
    page = v / "wiki" / "entities" / "a" / "settled.md"
    # a complete, self-consistent prior stamp: nothing about basis/consensus/state has changed
    _page(page, {"type": "actor", "name": "Settled", "review_status": "auto-verified",
                 "consensus": 2, "auto_verified_basis": "2 B-grade sources: MISP galaxy, URLhaus",
                 "auto_verified_at": "2026-08-04T00:00:00Z",
                 "attribution_confidence": "high",
                 "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    fm = _fm(page)
    assert "attribution_confidence" not in fm, f"stale governed confidence survived: {fm}"
    assert fm["consensus"] == 2 and fm["review_status"] == "auto-verified"


def test_the_corpus_settles_after_the_deferral_pass(tmp_path):
    """Once dropped there is nothing left to change, so the lane must go quiet — otherwise it
    rewrites the same pages every night."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {defer_confidence_to_types: [assessment]}\n")
    _page(v / "wiki" / "assessments" / "a1.md",
          {"type": "assessment", "name": "A1", "subject": "entities/a/settled"})
    page = v / "wiki" / "entities" / "a" / "settled.md"
    _page(page, {"type": "actor", "name": "Settled", "review_status": "auto-verified",
                 "consensus": 2, "auto_verified_basis": "2 B-grade sources: MISP galaxy, URLhaus",
                 "auto_verified_at": "2026-08-04T00:00:00Z",
                 "attribution_confidence": "high", "sources": ["MISP galaxy", "URLhaus"]})
    m = _load(v)
    assert m.main([]) == 0
    once = page.read_text()
    assert m.main([]) == 0
    assert page.read_text() == once, "the lane must settle, not rewrite every run"


def test_deferral_only_applies_to_a_judgment_about_the_same_claim(tmp_path):
    """A judgment is scoped to ONE claim. An identity-scope or motivation assessment says nothing
    about attribution, so deferring to it suppresses a confidence nothing else supplies — that hit
    57 pages on the live vault, 53 of them identity-scope."""
    schema = (SCHEMA + "review_autoverify:\n  defer_confidence_to_types: [assessment]\n"
              "  defer_confidence_kinds: [actor-country-linkage]\n")
    v = _vault(tmp_path, schema)
    _page(v / "wiki" / "assessments" / "attr.md",
          {"type": "assessment", "name": "A", "assessment_kind": "actor-country-linkage",
           "subject": "entities/a/governed"})
    _page(v / "wiki" / "assessments" / "scope.md",
          {"type": "assessment", "name": "S", "assessment_kind": "actor-identity-scope",
           "subject": "entities/a/unrelated"})
    gov = v / "wiki" / "entities" / "a" / "governed.md"
    unr = v / "wiki" / "entities" / "a" / "unrelated.md"
    for p in (gov, unr):
        _page(p, {"type": "actor", "name": p.stem, "needs_review": True,
                  "sources": ["MISP galaxy", "URLhaus"]})
    assert _load(v).main([]) == 0
    assert "attribution_confidence" not in _fm(gov), "the same-claim judgment must govern"
    assert _fm(unr)["attribution_confidence"] == "high", (
        "a judgment about a DIFFERENT claim must not suppress attribution confidence")


def test_a_wrongly_suppressed_confidence_is_restored(tmp_path):
    """Symmetry: the field's presence tracks policy in BOTH directions, so a value removed by an
    over-broad deferral comes back once the scope is corrected."""
    schema = (SCHEMA + "review_autoverify:\n  defer_confidence_to_types: [assessment]\n"
              "  defer_confidence_kinds: [actor-country-linkage]\n")
    v = _vault(tmp_path, schema)
    _page(v / "wiki" / "assessments" / "scope.md",
          {"type": "assessment", "name": "S", "assessment_kind": "actor-identity-scope",
           "subject": "entities/a/lost"})
    page = v / "wiki" / "entities" / "a" / "lost.md"
    _page(page, {"type": "actor", "name": "Lost", "review_status": "auto-verified",
                 "consensus": 2, "auto_verified_basis": "2 B-grade sources: MISP galaxy, URLhaus",
                 "auto_verified_at": "2026-08-06T00:00:00Z",
                 "sources": ["MISP galaxy", "URLhaus"]})     # band was stripped by the over-broad rule
    assert _load(v).main([]) == 0
    assert _fm(page)["attribution_confidence"] == "high"


def test_a_lane_set_confidence_without_graded_evidence_does_not_churn(tmp_path):
    """The writing lane's own `attribution_confidence` is deliberately preserved when there is no
    graded evidence. Treating its presence as a mismatch loops forever — nothing in this lane would
    remove it — so the corpus is rewritten every single run. Caught on the live vault: run 2 still
    rewrote 48 pages instead of settling."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {defer_confidence_to_types: [assessment]}\n")
    page = v / "wiki" / "entities" / "l" / "laneset.md"
    _page(page, {"type": "actor", "name": "LaneSet", "needs_review": True,
                 "attribution_confidence": "suspected"})      # no sources at all
    m = _load(v)
    assert m.main([]) == 0
    once = page.read_text()
    assert _fm(page)["attribution_confidence"] == "suspected", "lane-set value must be preserved"
    assert m.main([]) == 0 and m.main([]) == 0
    assert page.read_text() == once, "must settle, not rewrite every run"


def test_a_stamp_with_no_basis_that_no_longer_clears_is_named_a_stale_claim(tmp_path, capsys):
    """The counterpart to the soundly-stamped page above. This one asserts `auto-verified` while
    recording NOTHING that could be checked, and it does not clear the bar on re-entry either — so
    it is a claim with no basis, and it has to be reported as that rather than folded into the
    human queue count (it carries no needs_review flag, so counting it as held overstates the
    queue) or into "now-held" (which describes a stamp that WAS sound)."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {hold_when_fields: [enrichment_quarantined]}\n")
    page = v / "wiki" / "entities" / "h" / "hollow.md"
    _page(page, {"type": "actor", "name": "Hollow", "review_status": "auto-verified",
                 "auto_verified_at": "2026-08-04T00:00:00Z", "sources": ["Microsoft"],
                 "enrichment_quarantined": True})
    assert _load(v).main([]) == 0
    out = capsys.readouterr().out
    assert "stale-claim" in out and "still stamped auto-verified with no basis" in out, out
    assert "now-held" not in out, out


# --- _governed_subjects reads the whole vault, so every unreadable shape has to pass through it ---

def test_governed_subject_scan_survives_every_unreadable_page(tmp_path, capsys):
    """The scan is ONE pass over the corpus. A page it cannot read must cost only that page: an
    exception here takes down the deferral map, and the lane then derives a confidence for every
    subject a judgment already governs — the exact over-publication okengine#563 was about."""
    v = _vault(tmp_path, SCHEMA + "review_autoverify: {defer_confidence_to_types: [assessment]}\n")

    # a directory that matches the glob, so reading it raises IsADirectoryError
    (v / "wiki" / "assessments" / "a-directory.md").mkdir(parents=True)
    # machinery pages, skipped by name before they are read at all
    _page(v / "wiki" / "assessments" / "_scratch.md",
          {"type": "assessment", "name": "S", "subject": "entities/a/skipped"})
    (v / "wiki" / "assessments" / ".hidden.md").write_text(
        "---\ntype: assessment\nsubject: entities/a/hidden\n---\n", encoding="utf-8")
    # no frontmatter at all
    (v / "wiki" / "assessments" / "raw.md").write_text(
        "# not a record\n[[entities/a/raw]]\n", encoding="utf-8")
    # a real judgment whose subject list mixes a usable ref with junk
    _page(v / "wiki" / "assessments" / "mixed.md",
          {"type": "assessment", "name": "M", "subject": [42, None, "entities/a/governed"]})
    # a wikilink subject whose first bracketed candidate is empty: the empty one is skipped and the
    # scan keeps reading the same string rather than stopping at the first useless candidate
    _page(v / "wiki" / "assessments" / "links.md",
          {"type": "assessment", "name": "L", "subject": "[[ ]] see also [[entities/a/linked]]"})

    for slug in ("skipped", "hidden", "raw", "governed", "linked"):
        _page(v / "wiki" / "entities" / "a" / f"{slug}.md",
              {"type": "actor", "name": slug, "needs_review": True,
               "sources": ["MISP galaxy", "URLhaus"]})

    assert _load(v).main([]) == 0
    for slug in ("governed", "linked"):
        assert "attribution_confidence" not in _fm(v / "wiki" / "entities" / "a" / f"{slug}.md"), (
            f"{slug}: the usable ref beside the junk still governs its subject")
    for slug in ("skipped", "hidden", "raw"):
        assert _fm(v / "wiki" / "entities" / "a" / f"{slug}.md")["attribution_confidence"] == "high", (
            f"{slug}: an unreadable or name-skipped page governs nothing")
