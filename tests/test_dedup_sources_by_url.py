"""Retiring URL-duplicate source pages safely (okengine#516).

16,064 pairs of source pages share a url on one deployment — the same document twice — and the
id-index cannot see them, because the two copies have different titles and so different ids.

The dedup's value is entirely in its restraint, so that is what these pin:

* the survivor is chosen by EVIDENCE, never by filename. The `-link` half is smaller ~99% of
  the time but LARGER in 45 of 4,922 measured pairs, so a filename rule would destroy content;
* losers are TOMBSTONED, never deleted — provenance survives and the id-index understands it;
* the merge is strictly ADDITIVE: a loser may contribute a field the survivor lacks, never
  replace one it has, and never identity;
* a group with malformed frontmatter is skipped whole, mirroring `_tombstone`, which refuses
  rather than silently wiping it;
* BOTH reference forms are repointed — `[[wikilinks]]` and bare paths. Fixing only wikilinks is
  how a previous migration left bare frontmatter references dangling.
"""
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("yaml")
import yaml  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "dedup_sources_by_url.py"


def _load():
    spec = importlib.util.spec_from_file_location("dedup_sources_by_url", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["dedup_sources_by_url"] = m
    spec.loader.exec_module(m)
    return m


def _src(vault: Path, rel: str, *, url, body="Body.\n", **fm):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"type": "source", "url": url, "published": "2026-06-01"}
    data.update(fm)
    p.write_text("---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n" + body,
                 encoding="utf-8")
    return p


def _fm(p: Path) -> dict:
    m = _load()
    fm, _body, _raw = m.read_page(p)
    return fm or {}


def test_survivor_is_the_substantive_page_not_the_shorter_name(tmp_path):
    """The 45-case guard: the page with content wins even when its name is longer."""
    m = _load()
    url = "https://example.test/story"
    thin = _src(tmp_path, "sources/2026/06/story.md", url=url, body="Stub.\n")
    fat = _src(tmp_path, "sources/2026/06/story-link.md", url=url,
               body="A much longer body with real extractable content. " * 8)
    result = m.plan(tmp_path)
    assert len(result["actions"]) == 1
    act = result["actions"][0]
    assert act["survivor"] == "sources/2026/06/story-link", (
        "the substantive page must win even though it is the '-link' one")
    assert [r for r, _ in act["losers"]] == ["sources/2026/06/story"]
    assert thin.is_file() and fat.is_file()


def test_repeated_dataset_record_keeps_newest_snapshot_not_lexical_hash(tmp_path):
    """Repeated structured records can share a URL/body but differ in observation state."""
    m = _load()
    url = "https://apt.example/showcard?u=stable-record"
    common = {
        "dataset": "example_actor_data",
        "dataset_record_id": "426",
        "retrieved_via": "structured-dataset",
    }
    _src(
        tmp_path,
        "sources/private/dataset/actor/zzzz-older.md",
        url=url,
        body="Same record body.\n",
        collection_timestamp="2026-07-19T08:00:09+00:00",
        reliability="C",
        **common,
    )
    newest = _src(
        tmp_path,
        "sources/private/dataset/actor/aaaa-newer.md",
        url=url,
        body="Same record body.\n",
        collection_timestamp="2026-08-09T08:00:08+00:00",
        reliability="F",
        **common,
    )

    action = m.plan(tmp_path)["actions"][0]
    assert action["survivor"] == "sources/private/dataset/actor/aaaa-newer"
    assert newest.is_file()


def test_governed_writer_loader_fails_closed_on_corrupt_installation(tmp_path, monkeypatch):
    m = _load()
    real_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: False
                        if path.name == "catalog.yaml" else real_is_file(path))
    monkeypatch.setattr(m.sys, "path", [])
    monkeypatch.setattr(m.importlib.util, "spec_from_file_location", lambda *_args: None)
    with pytest.raises(RuntimeError, match="cannot load governed write server"):
        m._load_governed_writer(tmp_path)


def test_freshness_skips_missing_and_invalid_values_and_normalizes_naive_time():
    m = _load()
    expected = datetime(2026, 8, 9, 8, 0, 8, tzinfo=timezone.utc).timestamp()
    assert m._freshness({
        "collection_timestamp": "not-a-date",
        "retrieved_at": datetime(2026, 8, 9, 8, 0, 8),
    }) == expected
    assert m._freshness({}) == 0.0


def test_static_article_still_prefers_evidence_over_ingest_recency(tmp_path):
    m = _load()
    url = "https://example.test/static-article"
    substantive = _src(
        tmp_path,
        "sources/older.md",
        url=url,
        body="Substantive evidence. " * 20,
        retrieved_at="2026-07-01T00:00:00Z",
    )
    _src(
        tmp_path,
        "sources/newer.md",
        url=url,
        body="Stub.\n",
        retrieved_at="2026-08-01T00:00:00Z",
    )
    action = m.plan(tmp_path)["actions"][0]
    assert action["survivor"] == "sources/older"
    assert substantive.is_file()


def test_losers_are_tombstoned_never_deleted(tmp_path):
    m = _load()
    url = "https://example.test/x"
    keep = _src(tmp_path, "sources/2026/06/keep.md", url=url, body="Long body. " * 20)
    drop = _src(tmp_path, "sources/2026/06/drop.md", url=url, body="s\n")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert drop.is_file(), "a duplicate must never be unlinked"
    fm = _fm(drop)
    assert fm["status"] == "tombstoned"
    assert fm["superseded_by"] == "sources/2026/06/keep"
    assert "516" in str(fm.get("tombstone_reason", ""))
    assert _fm(keep).get("status") != "tombstoned"


def test_merge_is_additive_and_never_overwrites(tmp_path):
    m = _load()
    url = "https://example.test/m"
    keep = _src(tmp_path, "sources/2026/06/keep.md", url=url, body="Long. " * 20,
                publisher="Real Publisher")
    _src(tmp_path, "sources/2026/06/drop.md", url=url, body="s\n",
         publisher="Wrong Publisher", tags=["useful"])
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    fm = _fm(keep)
    assert fm["publisher"] == "Real Publisher", "an existing field must never be replaced"
    assert fm["tags"] == ["useful"], "a field the survivor lacked may be contributed"


def test_identity_fields_are_never_merged(tmp_path):
    m = _load()
    url = "https://example.test/i"
    keep = _src(tmp_path, "sources/2026/06/keep.md", url=url, body="Long. " * 20,
                id="sources:keep")
    _src(tmp_path, "sources/2026/06/drop.md", url=url, body="s\n", id="sources:drop")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert _fm(keep)["id"] == "sources:keep"


def test_a_group_with_malformed_frontmatter_is_skipped_whole(tmp_path):
    """Mirrors _tombstone: refuse rather than risk wiping frontmatter we cannot parse."""
    m = _load()
    url = "https://example.test/bad"
    good = _src(tmp_path, "sources/2026/06/good.md", url=url, body="Long. " * 20)
    bad = tmp_path / "wiki" / "sources" / "2026" / "06" / "bad.md"
    bad.write_text("---\ntype: source\nurl: [unclosed\n---\nBody.\n", encoding="utf-8")
    result = m.plan(tmp_path)
    assert result["actions"] == [], "a malformed member must stop the whole group"
    assert len(result["malformed"]) == 1
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert _fm(good).get("status") != "tombstoned"


def test_dry_run_changes_nothing(tmp_path):
    m = _load()
    url = "https://example.test/d"
    _src(tmp_path, "sources/2026/06/keep.md", url=url, body="Long. " * 20)
    drop = _src(tmp_path, "sources/2026/06/drop.md", url=url, body="s\n")
    before = drop.read_text()
    assert m.main(["--vault", str(tmp_path)]) == 0
    assert drop.read_text() == before


def test_both_wikilinks_and_bare_paths_are_repointed(tmp_path):
    """Fixing only [[wikilinks]] is how bare frontmatter references were left dangling."""
    m = _load()
    url = "https://example.test/ref"
    _src(tmp_path, "sources/2026/06/keep.md", url=url, body="Long. " * 20)
    _src(tmp_path, "sources/2026/06/drop.md", url=url, body="s\n")
    ref = tmp_path / "wiki" / "concepts" / "c.md"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("---\ntype: concept\nsubject: sources/2026/06/drop\n---\n"
                   "See [[sources/2026/06/drop]] for detail.\n", encoding="utf-8")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    text = ref.read_text()
    assert "[[sources/2026/06/keep]]" in text, "wikilink must repoint"
    assert "subject: sources/2026/06/keep" in text, "bare path reference must repoint too"
    assert "drop" not in text


def test_already_tombstoned_pages_are_ignored(tmp_path):
    """Re-running must be a no-op, so this is safe to repeat."""
    m = _load()
    url = "https://example.test/t"
    _src(tmp_path, "sources/2026/06/keep.md", url=url, body="Long. " * 20)
    drop = _src(tmp_path, "sources/2026/06/drop.md", url=url, body="s\n")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    first = drop.read_text()
    assert m.plan(tmp_path)["actions"] == [], "a retired page must not re-enter a group"
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert drop.read_text() == first


def test_a_lone_page_is_not_touched(tmp_path):
    m = _load()
    solo = _src(tmp_path, "sources/2026/06/solo.md", url="https://example.test/solo")
    before = solo.read_text()
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert solo.read_text() == before


def test_selection_is_deterministic_across_runs(tmp_path):
    """Two identical pages must still resolve the same way, or repeated runs would flap."""
    m = _load()
    url = "https://example.test/same"
    _src(tmp_path, "sources/2026/06/aaa.md", url=url, body="Same. " * 10)
    _src(tmp_path, "sources/2026/06/bbb.md", url=url, body="Same. " * 10)
    first = m.plan(tmp_path)["actions"][0]["survivor"]
    second = m.plan(tmp_path)["actions"][0]["survivor"]
    assert first == second


def test_survivor_score_counts_scalar_relationship_as_one_not_string_length():
    m = _load()
    scalar = m._score(
        "sources/scalar", {"entities": "[[entities/a/very-long-relationship-name]]"},
        "same body", None)
    two_real_refs = m._score(
        "sources/list", {"entities": ["entities/a/one", "entities/b/two"]},
        "same body", None)
    assert scalar[1] == 1
    assert two_real_refs[1] == 2
    assert two_real_refs > scalar

def test_a_placeholder_url_is_not_an_identity(tmp_path):
    """The bug a live dry-run caught before this ever ran with --apply.

    1,495 source pages on one deployment carry a `url` that is a PLACEHOLDER rather than an
    address — `UNKNOWN`, literal `null`, blanks. Those compare EQUAL to each other, so treating
    any non-empty string as identity grouped 14+ unrelated Huntress articles into one
    "duplicate" set and would have retired all but one of them. A placeholder is the ABSENCE
    of identity and must behave exactly like a missing url.
    """
    m = _load()
    for name, bad in (("a", "UNKNOWN"), ("b", "UNKNOWN"), ("c", "null"), ("d", "N/A")):
        _src(tmp_path, f"sources/2026/06/{name}.md", url=bad, body=f"Distinct body {name}. " * 5)
    assert m.plan(tmp_path)["actions"] == [], "placeholder urls must never group"
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    for name in "abcd":
        fm = _fm(tmp_path / "wiki" / "sources" / "2026" / "06" / f"{name}.md")
        assert fm.get("status") != "tombstoned", f"{name} was retired on a placeholder url"


def test_a_relative_or_schemeless_url_is_not_an_identity(tmp_path):
    m = _load()
    for name, bad in (("a", "example.test/x"), ("b", "example.test/x"), ("c", "/local/path")):
        _src(tmp_path, f"sources/2026/06/{name}.md", url=bad, body=f"Body {name}. " * 5)
    assert m.plan(tmp_path)["actions"] == [], "only http(s) URLs carry identity"


def test_apply_routes_converge_and_tombstone_through_the_governed_boundary(tmp_path):
    m = _load()
    url = "https://example.test/governed"
    keep = _src(tmp_path, "sources/keep.md", url=url, body="Long. " * 20)
    drop = _src(tmp_path, "sources/drop.md", url=url, body="short", useful="fact")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    log = (tmp_path / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "mcp-write converge sources/keep" in log
    assert "mcp-write tombstone sources/drop" in log
    assert _fm(keep)["useful"] == "fact"
    assert _fm(drop)["status"] == "tombstoned"


def test_an_existing_status_is_replaced_not_duplicated(tmp_path):
    m = _load()
    url = "https://example.test/status"
    _src(tmp_path, "sources/2026/06/keep.md", url=url, body="Long. " * 20)
    _src(tmp_path, "sources/2026/06/drop.md", url=url, body="s\n", status="active")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    text = (tmp_path / "wiki" / "sources" / "2026" / "06" / "drop.md").read_text()
    assert text.count("status:") == 1, "the old status must be replaced, not duplicated"
    assert "status: tombstoned" in text


def test_the_reference_counter_counts_repoints_not_matches(tmp_path):
    """A number you cannot reconcile against the diff is worse than no number.

    make_rewriter matches EVERY [[sources/...]] link and returns it unchanged when that page
    is not being moved, so counting matches reported thousands of "references repointed" for
    an edit that touched a handful of lines.
    """
    m = _load()
    url = "https://example.test/count"
    _src(tmp_path, "sources/2026/06/keep.md", url=url, body="Long. " * 20)
    _src(tmp_path, "sources/2026/06/drop.md", url=url, body="s\n")
    _src(tmp_path, "sources/2026/06/other.md", url="https://example.test/other")
    ref = tmp_path / "wiki" / "concepts" / "c.md"
    ref.parent.mkdir(parents=True, exist_ok=True)
    # ONE link to the retired page, THREE to pages that are not moving
    ref.write_text("---\ntype: concept\n---\n"
                   "[[sources/2026/06/drop]] [[sources/2026/06/other]] "
                   "[[sources/2026/06/other]] [[sources/2026/06/keep]]\n", encoding="utf-8")
    actions = m.plan(tmp_path)["actions"]
    stats = m.apply_actions(tmp_path, actions)
    assert stats["link_rewrites"] == 1, (
        f"exactly one reference actually moved, got {stats['link_rewrites']}")
    assert stats["link_files"] == 1


def test_parser_and_frontmatter_edge_cases(tmp_path):
    m = _load()
    missing = tmp_path / "missing.md"
    assert m.read_page(missing) == (None, "", "")
    plain = tmp_path / "plain.md"
    plain.write_text("body", encoding="utf-8")
    assert m.read_page(plain) == (None, "body", "body")
    scalar = tmp_path / "scalar.md"
    scalar.write_text("---\nvalue\n---\nbody", encoding="utf-8")
    assert m.read_page(scalar)[0] is None
    assert m.plan(tmp_path)["actions"] == []


def test_plan_skips_non_sources_and_tolerates_canonical_failure(tmp_path, monkeypatch):
    m = _load()
    _src(tmp_path, "sources/a.md", url="https://example.test/x", type="concept")
    _src(tmp_path, "sources/b.md", url="https://example.test/x", body="one")
    _src(tmp_path, "sources/c.md", url="https://example.test/x", body="two")
    fake = type("OKF", (), {"canonical_key": staticmethod(
        lambda *_: (_ for _ in ()).throw(RuntimeError("schema")))})()
    monkeypatch.setattr(m, "_load_okf_migrate", lambda: fake)
    assert len(m.plan(tmp_path)["actions"]) == 1


def test_additive_merge_refuses_empty_and_identity_values():
    m = _load()
    assert m._additive_merge({"title": "keep"}, {
        "id": "x", "title": "replace", "empty": "", "none": None,
        "list": [], "dict": {}, "useful": 3,
    }) == {"useful": 3}


def test_apply_refuses_unreadable_pages_and_rewrite_reads(tmp_path, monkeypatch):
    m = _load()
    (tmp_path / "wiki").mkdir()
    actions = [{"survivor": "missing", "losers": [], "survivor_fm": {}}]
    assert m.apply_actions(tmp_path, actions)["tombstoned"] == 0

    keep = _src(tmp_path, "sources/keep.md", url="https://x.test", body="long")
    drop = _src(tmp_path, "sources/drop.md", url="https://x.test", body="short",
                tombstone_reason="existing")
    ref = _src(tmp_path, "sources/ref.md", url="https://y.test")
    act = [{"survivor": "sources/keep", "losers": [
        ("sources/missing", {}), ("sources/drop", {"tombstone_reason": "existing"})]}]
    real_read = Path.read_text

    def selective_read(path, *args, **kwargs):
        if path == ref:
            raise OSError("unreadable")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", selective_read)
    stats = m.apply_actions(tmp_path, act)
    assert stats["tombstoned"] == 0, "unverified references must stop retirement"
    assert stats["errors"] and "cannot verify/rewrite references" in stats["errors"][0]
    assert "existing" in drop.read_text(encoding="utf-8")
    assert keep.exists()


def test_apply_refuses_retirement_when_reference_rewrite_fails(tmp_path, monkeypatch):
    m = _load()
    _src(tmp_path, "sources/keep.md", url="https://x.test", body="long " * 20)
    drop = _src(tmp_path, "sources/drop.md", url="https://x.test", body="short")
    ref = tmp_path / "wiki" / "concepts" / "ref.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("[[sources/drop]]\n", encoding="utf-8")
    actions = m.plan(tmp_path)["actions"]
    real_write = Path.write_text

    def selective_write(path, *args, **kwargs):
        if path == ref:
            raise OSError("read-only")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", selective_write)
    stats = m.apply_actions(tmp_path, actions)
    assert stats["tombstoned"] == 0
    assert stats["link_files"] == 0
    assert stats["errors"] == [
        "concepts/ref: reference rewrite failed: read-only",
    ]
    assert _fm(drop).get("status") != "tombstoned"


def test_apply_continues_when_tombstone_write_refuses(tmp_path, monkeypatch):
    m = _load()
    _src(tmp_path, "sources/keep.md", url="https://x.test")
    _src(tmp_path, "sources/drop.md", url="https://x.test")
    writer = type("Writer", (), {
        "_converge": staticmethod(lambda *_args: "converged"),
        "_tombstone": staticmethod(lambda *_args: "refused: policy"),
    })()
    monkeypatch.setattr(m, "_load_governed_writer", lambda _vault: writer)
    stats = m.apply_actions(tmp_path, [{"survivor": "sources/keep",
        "losers": [("sources/drop", {})], "survivor_fm": {}}])
    assert stats["tombstoned"] == 0
    assert stats["errors"] and "governed tombstone failed" in stats["errors"][0]


def test_converge_refusal_leaves_group_and_references_untouched(tmp_path, monkeypatch):
    m = _load()
    _src(tmp_path, "sources/keep.md", url="https://x.test", body="long " * 20)
    drop = _src(tmp_path, "sources/drop.md", url="https://x.test", body="short")
    ref = tmp_path / "wiki" / "concepts" / "ref.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("[[sources/drop]]\n", encoding="utf-8")
    tombstones = []
    writer = type("Writer", (), {
        "_converge": staticmethod(lambda *_args: "refused: ownership"),
        "_tombstone": staticmethod(lambda *_args: tombstones.append(_args) or "tombstoned"),
    })()
    monkeypatch.setattr(m, "_load_governed_writer", lambda _vault: writer)

    stats = m.apply_actions(tmp_path, m.plan(tmp_path)["actions"])
    assert stats["errors"] and "governed converge failed" in stats["errors"][0]
    assert not tombstones
    assert "[[sources/drop]]" in ref.read_text(encoding="utf-8")
    assert _fm(drop).get("status") != "tombstoned"


def test_references_are_repaired_before_governed_tombstone(tmp_path, monkeypatch):
    m = _load()
    _src(tmp_path, "sources/keep.md", url="https://x.test", body="long " * 20)
    _src(tmp_path, "sources/drop.md", url="https://x.test", body="short")
    ref = tmp_path / "wiki" / "concepts" / "ref.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("[[sources/drop]]\n", encoding="utf-8")

    def tombstone(*_args):
        assert "[[sources/keep]]" in ref.read_text(encoding="utf-8")
        return "tombstoned"

    writer = type("Writer", (), {
        "_converge": staticmethod(lambda *_args: "converged"),
        "_tombstone": staticmethod(tombstone),
    })()
    monkeypatch.setattr(m, "_load_governed_writer", lambda _vault: writer)
    stats = m.apply_actions(tmp_path, m.plan(tmp_path)["actions"])
    assert stats["tombstoned"] == 1 and not stats["errors"]


def test_cli_missing_vault_limit_and_long_listing(tmp_path, monkeypatch, capsys):
    m = _load()
    assert m.main(["--vault", str(tmp_path)]) == 2
    (tmp_path / "wiki").mkdir()
    actions = [{"url": f"https://x.test/{i}", "survivor": f"sources/k{i}",
                "losers": [(f"sources/d{i}", {})]} for i in range(7)]
    monkeypatch.setattr(m, "plan", lambda _v: {"actions": actions, "groups": 7,
        "skipped_malformed": 0, "malformed": []})
    assert m.main(["--vault", str(tmp_path), "--limit", "7"]) == 0
    assert "and 1 more" in capsys.readouterr().out


def test_cli_fails_loud_when_governed_apply_is_incomplete(tmp_path, monkeypatch, capsys):
    m = _load()
    (tmp_path / "wiki").mkdir()
    monkeypatch.setattr(m, "plan", lambda _vault: {
        "actions": [], "groups": 0, "skipped_malformed": 0, "malformed": [],
    })
    monkeypatch.setattr(m, "apply_actions", lambda *_args: {
        "tombstoned": 0,
        "merged": 0,
        "link_files": 0,
        "link_rewrites": 0,
        "errors": ["sources/x: governed tombstone failed: refused"],
    })
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 1
    assert "governed tombstone failed" in capsys.readouterr().err


def test_cli_bounds_reported_apply_errors(tmp_path, monkeypatch, capsys):
    m = _load()
    (tmp_path / "wiki").mkdir()
    monkeypatch.setattr(m, "plan", lambda _vault: {
        "actions": [], "groups": 0, "skipped_malformed": 0, "malformed": [],
    })
    monkeypatch.setattr(m, "apply_actions", lambda *_args: {
        "tombstoned": 0,
        "merged": 0,
        "link_files": 0,
        "link_rewrites": 0,
        "errors": [f"failure {index}" for index in range(23)],
    })
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 1
    stderr = capsys.readouterr().err
    assert "ERROR: failure 19" in stderr
    assert "failure 20" not in stderr
    assert "ERROR: … and 3 more" in stderr
