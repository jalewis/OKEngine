"""condition_recheck (okengine#547) — clear a flag whose defect has since been repaired.

The contract under test: a page clears ONLY when every recorded reason is re-testable state AND
every one now re-tests clean; anything unexplained or non-mechanical holds; and the re-test
predicates do not drift from the write-path raisers they mirror.

That last property is the load-bearing one. The predicates are copied rather than imported because
`write_server` is BAKED into the gateway image while this lane is STAGED, so a shared import would
break on the first image/stage skew. A copy is only safe with a test that fails when it diverges —
otherwise this lane silently clears pages the write path still considers broken.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "condition_recheck.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="condition_recheck absent")

SCHEMA = """\
types:
  concept: {required: [type]}
  entity: {required: [type]}
  actor: {required: [type, name]}
  prediction: {required: [type]}
"""


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("condition_recheck", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    sl = sys.modules.get("schema_lib")
    if sl is not None and hasattr(sl, "_SCHEMA_CACHE"):
        sl._SCHEMA_CACHE.clear()
    return m


def _vault(tmp_path: Path, schema: str = SCHEMA) -> Path:
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(schema, encoding="utf-8")
    return tmp_path


def _page(vault, rel, *, flagged=True, body="body\n", extra="", ptype="concept"):
    p = vault / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ntype: {ptype}\n"
    if flagged:
        fm += "needs_review: true\n"
    p.write_text(fm + extra + "---\n\n" + body, encoding="utf-8")
    return p


def _queue(vault, rows):
    q = vault / "wiki" / "_review-queue.md"
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text("---\ntitle: Review Queue\n---\n\n" +
                 "".join(f"- 2026-08-01 **{rel}.md** — {reason}\n" for rel, reason in rows),
                 encoding="utf-8")


def _record(vault, subject, detail, state="open"):
    store = vault / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{abs(hash(subject + detail))}.yaml").write_text(
        yaml.safe_dump({"version": 1, "subject": subject, "state": state,
                        "reasons": [{"code": "manual", "detail": detail}]}), encoding="utf-8")


def _flagged(p: Path) -> bool:
    return "needs_review: true" in p.read_text()


# --- the anti-drift contract ----------------------------------------------

def test_predicates_agree_with_the_write_path(tmp_path):
    """The whole design rests on this. `write_server` is baked, this lane is staged, so the
    predicates are copied — and a copy that drifts clears pages the write path still flags."""
    ws_path = REPO / "okengine-mcp" / "write_server.py"
    if not ws_path.is_file():
        pytest.skip("write_server absent")
    sys.path.insert(0, str(REPO / "okengine-mcp"))
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    # HARD import, not importorskip. The file-absent case is already handled above (public
    # snapshots ship without okengine-mcp/); past that, an import failure means a real broken
    # dependency, and skipping would turn THE anti-drift contract into a false green on exactly
    # the runs that matter -- flagged by #535's importorskip-binding guard.
    ws = importlib.import_module("write_server")

    v = _vault(tmp_path)
    wiki = v / "wiki"
    m = _load(v)
    _page(v, "concepts/target", flagged=False)
    (wiki / "entities" / "q").mkdir(parents=True, exist_ok=True)
    (wiki / "entities" / "q" / "qilin.md").write_text("---\ntype: entity\n---\n\nx\n", encoding="utf-8")
    (wiki / "entities" / "a" / "b").mkdir(parents=True, exist_ok=True)
    (wiki / "entities" / "a" / "b" / "abba.md").write_text("---\ntype: entity\n---\n\nx\n", encoding="utf-8")
    ws._wiki = lambda: wiki

    for target in ("concepts/target", "entities/qilin", "entities/abba", "concepts/missing",
                   "entities/q/qilin", "nope"):
        assert m.wikilink_resolves(wiki, target) == ws._wikilink_resolves(target), target

    bodies = [
        "see [[concepts/target]] and [[concepts/missing]]",
        "bare [[qilin]] link",
        "[[entities/qilin]] shard-resolved",
        "[[entities/abba]] second-letter shard",
        "no links at all",
        "",
        "dupe [[concepts/missing]] [[concepts/missing]]",
        "trailing [[concepts/target.md]]",
    ]
    for body in bodies:
        mine = m.unresolvable_links(wiki, "concepts", body)
        theirs = ws._unresolvable_link_flags(wiki / "concepts" / "x.md", body)
        # theirs is a single formatted message; compare the COUNT and the flagged targets it names
        assert bool(mine) == bool(theirs), body
        if theirs:
            assert f"{len(mine)} unresolvable wikilink(s)" in theirs[0], body

    for body in ("short prose.", " ".join(["w"] * 251), " ".join(["w"] * 250),
                 "```" + " ".join(["w"] * 400) + "```", " ".join(["[[x]]"] * 400), ""):
        assert m.is_degenerate(body) == bool(ws._degeneration_flags(body)), body[:40]


# --- clearing ---------------------------------------------------------------

def test_a_repaired_wikilink_clears_the_flag(tmp_path, capsys):
    """The motivating case: broken-wikilinks-drain repaired the page hours ago and nothing ever
    cleared the flag it fixed."""
    v = _vault(tmp_path)
    _page(v, "concepts/target", flagged=False)
    p = _page(v, "concepts/fixed", body="now points at [[concepts/target]]\n")
    _queue(v, [("concepts/fixed", "2 unresolvable wikilink(s): [[a]] (bare name)")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out
    txt = p.read_text()
    assert not _flagged(p)
    assert "review_status: condition-recheck-cleared" in txt and "recheck_basis:" in txt


def test_a_still_broken_wikilink_holds(tmp_path, capsys):
    v = _vault(tmp_path)
    p = _page(v, "concepts/broken", body="still [[concepts/nowhere]]\n")
    _queue(v, [("concepts/broken", "1 unresolvable wikilink(s): [[concepts/nowhere]] (no such page)")])
    m = _load(v)
    assert m.main([]) == 0
    assert "0 cleared" in capsys.readouterr().out
    assert _flagged(p)


def test_a_repaired_degenerate_page_clears(tmp_path, capsys):
    v = _vault(tmp_path)
    p = _page(v, "concepts/degen", body="A short, punctuated sentence now.\n")
    _queue(v, [("concepts/degen", "degenerate: 400-word unpunctuated run (repetition loop)")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out and not _flagged(p)


def test_a_still_degenerate_page_holds(tmp_path):
    v = _vault(tmp_path)
    p = _page(v, "concepts/degen", body=" ".join(["word"] * 400) + "\n")
    _queue(v, [("concepts/degen", "degenerate: 400-word unpunctuated run (repetition loop)")])
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)


def test_a_slug_collision_is_moot_once_the_page_exists(tmp_path, capsys):
    """The create was REJECTED, so a page existing at that path means the conflict resolved."""
    v = _vault(tmp_path)
    p = _page(v, "concepts/collided")
    _queue(v, [("concepts/collided", "slug id collision on create: concepts:x already used by entities/x.md")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out and not _flagged(p)


def test_every_reason_must_clear_not_just_one(tmp_path):
    """A page carrying two reasons clears only when BOTH re-test clean — otherwise repairing the
    easy one would clear a page whose real defect remains."""
    v = _vault(tmp_path)
    p = _page(v, "concepts/two", body="ok [[concepts/two]]\n" + " ".join(["w"] * 400))
    _queue(v, [("concepts/two", "1 unresolvable wikilink(s): [[a]] (bare name)"),
               ("concepts/two", "degenerate: 400-word unpunctuated run (repetition loop)")])
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)


# --- refusals ---------------------------------------------------------------

def test_a_page_with_no_recorded_reason_holds(tmp_path, capsys):
    """"I cannot tell why this was flagged" is not evidence that it is fixed. On okcti-test this
    is 2,245 of 2,473 flagged pages, so the conservative rule is doing most of the work."""
    v = _vault(tmp_path)
    p = _page(v, "concepts/unknown")
    _queue(v, [])
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)
    assert "no open review record" in capsys.readouterr().out


def test_a_non_mechanical_reason_holds(tmp_path, capsys):
    v = _vault(tmp_path)
    p = _page(v, "concepts/fieldloss")
    _queue(v, [("concepts/fieldloss", "field `type`: caller attempted 'x', owner value 'y' kept")])
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)
    assert "not re-testable" in capsys.readouterr().out


@pytest.mark.parametrize("extra,ptype,why", [
    ("status: tombstoned\n", "concept", "tombstoned"),
    ("conflicts:\n  - a dispute\n", "concept", "conflicts present"),
    ("", "prediction", "judgment type"),
])
def test_the_autoverify_refusals_are_honoured(tmp_path, capsys, extra, ptype, why):
    """A mechanical reason does not override a page that is wrong in another way — the flag may
    exist BECAUSE of that."""
    v = _vault(tmp_path)
    p = _page(v, "concepts/refused", extra=extra, ptype=ptype)
    _queue(v, [("concepts/refused", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)
    assert why in capsys.readouterr().out


def test_a_grounding_failure_holds(tmp_path, capsys):
    v = _vault(tmp_path)
    p = _page(v, "concepts/grounded",
              body="## Grounding check\n\nThe claim is unsupported by the cited source.\n")
    _queue(v, [("concepts/grounded", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)
    assert "grounding-check failure" in capsys.readouterr().out


def test_a_missing_required_field_holds(tmp_path, capsys):
    v = _vault(tmp_path)
    p = _page(v, "entities/noname", ptype="actor")
    _queue(v, [("entities/noname", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)
    assert "missing required" in capsys.readouterr().out


# --- reason sources ---------------------------------------------------------

def test_review_records_supplement_the_queue_log(tmp_path):
    """Records alone explain only ~200 of okcti's 2,473 flagged pages (stale post-reshard subjects,
    and `_flag` writes no record at all), so the queue log is the primary source and records are
    merged in. Merging can only add reasons, so it can only make the lane more conservative."""
    v = _vault(tmp_path)
    p = _page(v, "concepts/both", body="clean.\n")
    _queue(v, [("concepts/both", "slug id collision on create: x")])
    _record(v, "concepts/both", "field `type`: caller attempted 'x', owner value 'y' kept")
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)          # the record's non-mechanical reason holds it


def test_a_closed_record_contributes_no_reason(tmp_path):
    v = _vault(tmp_path)
    p = _page(v, "concepts/closed", body="clean.\n")
    _queue(v, [("concepts/closed", "slug id collision on create: x")])
    _record(v, "concepts/closed", "grounding check failed", state="approved")
    m = _load(v)
    assert m.main([]) == 0
    assert not _flagged(p)


def test_an_unreadable_record_is_ignored(tmp_path):
    v = _vault(tmp_path)
    store = v / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True, exist_ok=True)
    (store / "bad.yaml").write_text("state: [unclosed\n", encoding="utf-8")
    (store / "listy.yaml").write_text("- not a mapping\n", encoding="utf-8")
    (store / "nosubject.yaml").write_text("state: open\nsubject: ''\n", encoding="utf-8")
    p = _page(v, "concepts/x", body="clean.\n")
    _queue(v, [("concepts/x", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert not _flagged(p)


def test_an_absent_queue_and_store_hold_everything(tmp_path):
    v = _vault(tmp_path)
    p = _page(v, "concepts/x")
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)


# --- scanning + CLI ---------------------------------------------------------

def test_structural_and_unflagged_pages_are_skipped(tmp_path, capsys):
    v = _vault(tmp_path)
    d = v / "wiki" / "concepts"
    d.mkdir(parents=True, exist_ok=True)
    body = "---\ntype: concept\nneeds_review: true\n---\n\nclean.\n"
    for n in ("_a.md", "INDEX.md", "c.bak.md"):
        (d / n).write_text(body, encoding="utf-8")
    _page(v, "concepts/unflagged", flagged=False)
    _queue(v, [("concepts/_a", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert "0 cleared" in capsys.readouterr().out


def test_a_regex_match_with_a_non_true_parse_is_skipped(tmp_path, capsys):
    v = _vault(tmp_path)
    p = v / "wiki" / "concepts" / "dup.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: concept\nneeds_review: true\nneeds_review: false\n---\n\nclean.\n",
                 encoding="utf-8")
    _queue(v, [("concepts/dup", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert "0 cleared" in capsys.readouterr().out


def test_an_unreadable_page_does_not_end_the_scan(tmp_path, monkeypatch, capsys):
    v = _vault(tmp_path)
    _page(v, "concepts/aaa-unreadable", body="clean.\n")
    _page(v, "concepts/zzz-clear", body="clean.\n")
    _queue(v, [("concepts/aaa-unreadable", "slug id collision on create: x"),
               ("concepts/zzz-clear", "slug id collision on create: x")])
    m = _load(v)
    real = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "aaa-unreadable.md":
            raise OSError("unreadable")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out


def test_dry_run_reports_without_writing(tmp_path, capsys):
    v = _vault(tmp_path)
    p = _page(v, "concepts/x", body="clean.\n")
    before = p.read_text()
    _queue(v, [("concepts/x", "slug id collision on create: x")])
    m = _load(v)
    assert m.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "1 cleared" in out and "[dry-run]" in out
    assert p.read_text() == before


def test_the_body_survives_the_stamp(tmp_path):
    v = _vault(tmp_path)
    p = _page(v, "concepts/x", body="# Heading\n\nreal body text\n")
    _queue(v, [("concepts/x", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    txt = p.read_text()
    assert "# Heading" in txt and "real body text" in txt and txt.count("---") == 2


def test_missing_wiki_is_an_error_and_still_emits_wake_agent_false(tmp_path, capsys):
    m = _load(tmp_path / "absent")
    assert m.main([]) == 1
    out = capsys.readouterr().out
    assert '"wakeAgent": false' in out and '"wakeAgent": true' not in out


def test_a_normal_run_emits_wake_agent_false(tmp_path, capsys):
    v = _vault(tmp_path)
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert '"wakeAgent": false' in out and '"wakeAgent": true' not in out


def test_unparseable_frontmatter_is_not_treated_as_flagged(tmp_path, capsys):
    v = _vault(tmp_path)
    p = v / "wiki" / "concepts" / "broken.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: [unclosed\nneeds_review: true\n---\n\nclean.\n", encoding="utf-8")
    _queue(v, [("concepts/broken", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert "0 cleared" in capsys.readouterr().out


def test_links_outside_the_curated_namespaces_are_not_re_tested(tmp_path, capsys):
    """`sources/` forward-refs are normal, not defects — the write path only flags concepts and
    entities, so the re-test must scope identically or it would hold source pages forever."""
    v = _vault(tmp_path)
    p = _page(v, "sources/s1", body="forward ref [[concepts/not-yet]]\n")
    _queue(v, [("sources/s1", "1 unresolvable wikilink(s): [[concepts/not-yet]] (no such page)")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out and not _flagged(p)


def test_a_record_reason_that_is_not_a_mapping_is_ignored(tmp_path):
    """`reasons` entries arrive from an older writer as bare strings; a non-mapping or empty-detail
    entry must contribute nothing rather than crash the reason index."""
    v = _vault(tmp_path)
    store = v / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True, exist_ok=True)
    (store / "mixed.yaml").write_text(yaml.safe_dump(
        {"subject": "concepts/x", "state": "open",
         "reasons": ["a bare string", {"code": "manual", "detail": "   "}, {"code": "manual"}]}),
        encoding="utf-8")
    p = _page(v, "concepts/x", body="clean.\n")
    _queue(v, [("concepts/x", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert not _flagged(p)          # none of those entries is a usable reason


def test_a_page_without_frontmatter_is_not_scanned(tmp_path, capsys):
    """_frontmatter returns {} for a page with no block; the scan must skip it rather than treat
    an empty mapping as an unflagged-but-present page."""
    v = _vault(tmp_path)
    m = _load(v)
    assert m._frontmatter("no frontmatter here\n") == {}
    p = v / "wiki" / "concepts" / "bare.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("needs_review: true\n", encoding="utf-8")
    _queue(v, [("concepts/bare", "slug id collision on create: x")])
    assert m.main([]) == 0
    assert "0 cleared" in capsys.readouterr().out


# --- mutation hardening (okengine#552) ------------------------------------
# The copied predicates carry mutants that were never exercised in write_server either (it is not
# a mutation target), so this is the first time the shard-resolution logic is pinned at all.

def _entity(vault, rel, body="x\n"):
    p = vault / "wiki" / f"{rel}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: entity\n---\n\n{body}", encoding="utf-8")
    return p


def test_shard_resolution_at_each_depth(tmp_path):
    """Three resolution forms, each independently load-bearing: literal path, first-letter shard,
    second-letter reshard. Nothing pinned which depth answered which link."""
    v = _vault(tmp_path)
    w = v / "wiki"
    m = _load(v)
    _entity(v, "concepts/literal")
    _entity(v, "entities/q/qilin")
    _entity(v, "entities/a/b/abba")
    assert m.wikilink_resolves(w, "concepts/literal") is True     # literal
    assert m.wikilink_resolves(w, "entities/qilin") is True       # first-letter shard
    assert m.wikilink_resolves(w, "entities/abba") is True        # second-letter reshard
    assert m.wikilink_resolves(w, "entities/nope") is False


def test_a_single_segment_target_never_shard_resolves(tmp_path):
    """`len(parts) >= 2` is the guard: a bare name has no namespace to shard under, and treating
    parts[0] as one would resolve links the write path calls broken."""
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "entities/q/qilin")
    assert m.wikilink_resolves(v / "wiki", "qilin") is False


def test_a_non_alphanumeric_basename_is_not_sharded(tmp_path):
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "entities/_/_hidden")
    assert m.wikilink_resolves(v / "wiki", "entities/_hidden") is False


def test_a_single_character_basename_uses_only_the_first_shard(tmp_path):
    """`len(base) > 1` guards the second-letter lookup — base[1] would IndexError on a 1-char name."""
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "entities/x/x")
    assert m.wikilink_resolves(v / "wiki", "entities/x") is True


def test_a_deep_target_resolves_by_its_last_segment(tmp_path):
    """parts[-1] is the basename and parts[0] the namespace — a middle segment is neither."""
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "entities/d/deep")
    assert m.wikilink_resolves(v / "wiki", "entities/2024/deep") is True


def test_a_trailing_md_is_stripped_before_resolution(tmp_path):
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "concepts/target")
    assert m.unresolvable_links(v / "wiki", "concepts", "[[concepts/target.md]]") == []


def test_empty_and_duplicate_links_are_skipped(tmp_path):
    """The dedupe/empty skip is a SKIP: a repeated bad link is reported once, and an empty
    `[[]]` must not be reported at all."""
    v = _vault(tmp_path)
    m = _load(v)
    bad = m.unresolvable_links(v / "wiki", "concepts", "[[]] [[c/x]] [[c/x]] [[c/y]]")
    assert len(bad) == 2


@pytest.mark.parametrize("words,degen", [(250, False), (251, True)])
def test_the_degeneration_run_boundary(tmp_path, words, degen):
    v = _vault(tmp_path)
    m = _load(v)
    assert m.is_degenerate(" ".join(["w"] * words)) is degen


def test_a_reason_detail_is_truncated_in_the_held_message(tmp_path, capsys):
    v = _vault(tmp_path)
    _page(v, "concepts/x")
    _queue(v, [("concepts/x", "field `type`: " + "y" * 200)])
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "yyy" not in out                      # the detail is bucketed by prefix, not echoed whole


def test_only_the_recorded_class_is_re_tested(tmp_path, capsys):
    """A page flagged ONLY for a collision must not be held because its body happens to look
    degenerate — the lane re-tests the recorded reason, not every condition it knows."""
    v = _vault(tmp_path)
    p = _page(v, "concepts/c", body=" ".join(["word"] * 400) + "\n")
    _queue(v, [("concepts/c", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out and not _flagged(p)


def test_a_wikilink_reason_does_not_trigger_the_degeneracy_check(tmp_path, capsys):
    v = _vault(tmp_path)
    _page(v, "concepts/target", flagged=False)
    p = _page(v, "concepts/w",
              body="[[concepts/target]] " + " ".join(["word"] * 400) + "\n")
    _queue(v, [("concepts/w", "1 unresolvable wikilink(s): [[a]] (bare name)")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out and not _flagged(p)


def test_a_status_sorting_after_tombstoned_is_not_held(tmp_path, capsys):
    v = _vault(tmp_path)
    p = _page(v, "concepts/w", extra="status: withdrawn\n", body="clean.\n")
    _queue(v, [("concepts/w", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "1 cleared" in out and "tombstoned" not in out and not _flagged(p)


def test_a_required_field_sorting_after_type_is_enforced(tmp_path, capsys):
    """The filter is inequality, not ordering — `url` sorts after `type` and must still be
    required. Built from YAML because a parsed 'type' is not the interned literal."""
    v = _vault(tmp_path, "types:\n  concept: {required: [type, url]}\n")
    p = _page(v, "concepts/x", body="clean.\n")
    _queue(v, [("concepts/x", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert "missing required" in capsys.readouterr().out and _flagged(p)


def test_a_present_required_field_does_not_hold(tmp_path, capsys):
    v = _vault(tmp_path, "types:\n  concept: {required: [type, url]}\n")
    p = _page(v, "concepts/x", extra="url: http://x\n", body="clean.\n")
    _queue(v, [("concepts/x", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out and not _flagged(p)


def test_each_structural_skip_is_independent(tmp_path, capsys):
    v = _vault(tmp_path)
    d = v / "wiki" / "concepts"
    d.mkdir(parents=True, exist_ok=True)
    body = "---\ntype: concept\nneeds_review: true\n---\n\nclean.\n"
    for n in ("_u.md", "INDEX-p2.md", "b.bak.md"):
        (d / n).write_text(body, encoding="utf-8")
    _page(v, "concepts/real", body="clean.\n")
    _queue(v, [(r, "slug id collision on create: x")
               for r in ("concepts/_u", "concepts/INDEX-p2", "concepts/b.bak", "concepts/real")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared" in capsys.readouterr().out          # only the real page


def test_held_reasons_are_bucketed_and_capped(tmp_path, capsys):
    """The summary caps at eight buckets. More than that and the tail is dropped — pin the cap so
    a widened report is a deliberate change."""
    # Distinct BUCKETS, not distinct queue reasons: the key is the text before the first colon,
    # so ten "not re-testable: ..." reasons collapse to one. Varying broken-link counts give
    # genuinely different held messages ("1 unresolvable wikilink(s) remain", "2 ...", ...).
    v = _vault(tmp_path)
    rows = []
    for i in range(1, 11):
        links = " ".join(f"[[concepts/missing{i}-{k}]]" for k in range(i))
        _page(v, f"concepts/p{i}", body=links + "\n")
        rows.append((f"concepts/p{i}", "1 unresolvable wikilink(s): [[a]] (bare name)"))
    _queue(v, rows)
    m = _load(v)
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "0 cleared, 10 held" in out
    assert len([ln for ln in out.splitlines() if ln.startswith("  held ")]) == 8


def test_a_held_page_does_not_end_the_scan(tmp_path, capsys):
    v = _vault(tmp_path)
    _page(v, "concepts/aaa-held")
    _page(v, "concepts/zzz-clear", body="clean.\n")
    _queue(v, [("concepts/aaa-held", "field `x`: caller attempted"),
               ("concepts/zzz-clear", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert "1 cleared, 1 held" in capsys.readouterr().out


def test_a_broken_md_suffixed_link_is_still_reported(tmp_path):
    """Stripping `.md` must yield the target, not the empty string — an empty target is skipped,
    so a broken `[[x.md]]` link would silently vanish from the re-test."""
    v = _vault(tmp_path)
    m = _load(v)
    assert m.unresolvable_links(v / "wiki", "concepts", "[[concepts/missing.md]]") == \
        ["[[concepts/missing]] (no such page)"]


def test_the_namespace_and_basename_come_from_the_right_ends(tmp_path):
    """ns is parts[0] and base parts[-1]. A middle segment as either would resolve links that do
    not exist and miss ones that do."""
    v = _vault(tmp_path)
    w = v / "wiki"
    m = _load(v)
    _entity(v, "entities/d/deep")
    _entity(v, "middle/d/deep")
    assert m.wikilink_resolves(w, "entities/middle/deep") is True    # ns=entities, base=deep
    assert m.wikilink_resolves(w, "nope/middle/deep") is False       # ns=nope has no shard


def test_the_second_letter_shard_uses_the_second_character(tmp_path):
    """base[1] specifically — base[2] or base[0] point at different directories entirely."""
    v = _vault(tmp_path)
    w = v / "wiki"
    m = _load(v)
    _entity(v, "entities/a/b/abcd")       # a=base[0], b=base[1]
    assert m.wikilink_resolves(w, "entities/abcd") is True
    _entity(v, "entities/x/z/xyz")        # x=base[0], y=base[1] -> z is WRONG
    assert m.wikilink_resolves(w, "entities/xyz") is False


def test_a_bad_record_does_not_stop_the_record_scan(tmp_path):
    """Each skip in open_reasons is a SKIP: one unreadable, non-mapping, closed or subject-less
    record must not stop later records from contributing their reasons."""
    v = _vault(tmp_path)
    store = v / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True, exist_ok=True)
    (store / "a-bad.yaml").write_text("state: [unclosed\n", encoding="utf-8")
    (store / "b-list.yaml").write_text("- not a mapping\n", encoding="utf-8")
    (store / "c-closed.yaml").write_text(
        yaml.safe_dump({"subject": "concepts/x", "state": "approved",
                        "reasons": [{"detail": "ignored"}]}), encoding="utf-8")
    (store / "d-nosubj.yaml").write_text("state: open\nsubject: ''\n", encoding="utf-8")
    (store / "e-good.yaml").write_text(
        yaml.safe_dump({"subject": "concepts/x", "state": "open",
                        "reasons": [{"detail": "field `t`: caller attempted"}]}), encoding="utf-8")
    p = _page(v, "concepts/x", body="clean.\n")
    _queue(v, [("concepts/x", "slug id collision on create: x")])
    m = _load(v)
    assert m.main([]) == 0
    assert _flagged(p)          # the LAST record's non-mechanical reason still reached the page


def test_a_long_held_label_is_truncated_to_48_characters(tmp_path, capsys):
    """The bucket key is capped so one runaway message cannot dominate the summary."""
    v = _vault(tmp_path)
    _page(v, "concepts/x")
    m = _load(v)
    assert m.main([]) == 0
    labels = [ln.split(": ", 1)[1] for ln in capsys.readouterr().out.splitlines()
              if ln.startswith("  held ")]
    assert labels and all(len(x) <= 48 for x in labels)
    assert labels[0] == "no open review record — the flag's cause was nev"


def test_a_single_segment_target_is_rejected_even_when_a_shard_would_match(tmp_path):
    """`len(parts) >= 2` specifically: with a one-segment target there is no namespace, and
    loosening the guard would treat the name itself as one and resolve a link the write path
    considers broken."""
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "solo/s/solo")          # exists at the path a 1-part shard lookup would try
    assert m.wikilink_resolves(v / "wiki", "solo") is False


def test_the_alnum_check_looks_at_the_basename_not_a_middle_segment(tmp_path):
    """The guard inspects parts[-1][:1]. Reading a middle segment instead rejects a perfectly
    good link whose basename is alphanumeric."""
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "entities/d/deep")
    assert m.wikilink_resolves(v / "wiki", "entities/_middle/deep") is True


def test_a_non_alnum_basename_first_character_blocks_sharding(tmp_path):
    """`[:1]` is the first character only — widening it would let `_x` shard on a later char."""
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "entities/_/_ab")
    _entity(v, "entities/a/_ab")
    assert m.wikilink_resolves(v / "wiki", "entities/_ab") is False


def test_the_second_shard_needs_a_second_character(tmp_path):
    """`len(base) > 1` guards base[1]. A one-character basename must resolve by the first shard
    only; `!= 1` would send `x` down the second-letter path and IndexError."""
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "entities/x/x")
    assert m.wikilink_resolves(v / "wiki", "entities/x") is True
    _entity(v, "entities/y/y/yy")
    assert m.wikilink_resolves(v / "wiki", "entities/yy") is True


def test_only_the_first_character_gates_sharding(tmp_path):
    """`[:1]` exactly: a basename like `a_b` has an alphanumeric FIRST character and a non-alnum
    second, so widening the slice to `[:2]` would reject a link that resolves."""
    v = _vault(tmp_path)
    m = _load(v)
    _entity(v, "entities/a/a_b")
    assert m.wikilink_resolves(v / "wiki", "entities/a_b") is True


def test_an_unreadable_record_file_is_skipped(tmp_path, monkeypatch):
    """An OSError reading a record is distinct from a parse error and has its own handler — a
    permissions change on one record must not lose every later record's reasons."""
    v = _vault(tmp_path)
    store = v / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True, exist_ok=True)
    (store / "a-unreadable.yaml").write_text("subject: x\n", encoding="utf-8")
    (store / "b-good.yaml").write_text(
        yaml.safe_dump({"subject": "concepts/x", "state": "open",
                        "reasons": [{"detail": "field `t`: caller attempted"}]}), encoding="utf-8")
    p = _page(v, "concepts/x", body="clean.\n")
    _queue(v, [("concepts/x", "slug id collision on create: x")])
    m = _load(v)
    real = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "a-unreadable.yaml":
            raise OSError("permission denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    assert m.main([]) == 0
    assert _flagged(p)          # b-good's non-mechanical reason still reached the page
