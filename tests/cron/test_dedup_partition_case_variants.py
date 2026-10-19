"""okengine#883: the dedup drain must also collapse CASE-ONLY duplicate pages.

`okf_migrate.find_page` is case-insensitive (!1144) but prefers the EXACT spelling when both
exist. So once `entities/c/v/CVE-x.md` (agent-written) and `entities/c/v/cve-x.md`
(importer-written) coexist on a case-sensitive filesystem, each writer keeps updating its own
copy and the pair never converges. The drain grouped exact file stems only, so it reported 0.

Contract pinned here, on real temp-dir vaults driven through the real lane:
  * a case-only pair is detected in dry-run, and dry-run writes nothing;
  * --apply merges onto the canonical spelling with the existing merge semantics (frontmatter
    union, longest body, `needs_review` when bodies differ);
  * `[[links]]` AND bare frontmatter path refs to the removed spelling are rewritten;
  * separator variants are NOT merged (slug_identity folding is not an identity);
  * a generated/per-directory artifact (INDEX, index, Log, README ...) is never removed;
  * a second --apply is a no-op.
All CVE ids here are synthetic (year 2099).
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "scripts" / "cron"


def _load():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "dedup_partition_collisions_case", SCRIPTS / "dedup_partition_collisions.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["dedup_partition_collisions_case"] = m
    spec.loader.exec_module(m)
    m.okf_migrate._SCHEMA_CACHE.clear()
    return m


def _vault(tmp_path: Path) -> Path:
    # The live shape: entities/ by-letter with a second-letter reshard (entities/c/v/...).
    (tmp_path / "schema.yaml").write_text(
        "types:\n  vulnerability: {required: [type]}\n  concept: {required: [type]}\n"
        "  source: {required: [type]}\n"
        "partitioning:\n  namespaces:\n"
        "    entities: {strategy: by-letter, reshard_by: second-letter}\n"
        "    concepts: {strategy: by-letter, reshard_by: second-letter}\n"
        "    sources: {strategy: by-date, reshard_by: day}\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    return tmp_path


def _page(vault: Path, rel: str, fm: str, body: str = "Body.") -> Path:
    p = vault / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm.rstrip()}\n---\n\n{body}\n", encoding="utf-8")
    return p


def _fm(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8").split("---\n", 2)[1])


def _run(m, vault: Path, *args) -> str:
    with redirect_stdout(io.StringIO()) as out:
        assert m.main(["--root", str(vault), *args]) == 0
    return out.getvalue()


def _names(d: Path) -> set[str]:
    return {p.name for p in d.iterdir()}


def _cve_pair(v: Path):
    agent = _page(
        v, "entities/c/v/CVE-2099-0001",
        "id: entities:cve-2099-0001\ntype: vulnerability\ntags: [cve, kernel]\n"
        "related: ['[[entities/a/n/android]]']",
        "# CVE-2099-0001\n\nA long agent-written analysis of the synthetic flaw, with context.")
    importer = _page(
        v, "entities/c/v/cve-2099-0001",
        "id: cve:cve-2099-0001\ntype: vulnerability\ncve_id: CVE-2099-0001\ntags: [kev]\n"
        "cvss_base: 5.5",
        "Short importer body.")
    return agent, importer


# ── detection ────────────────────────────────────────────────────────────────

def test_a_case_only_pair_is_detected_in_dry_run_and_nothing_is_written(tmp_path):
    v = _vault(tmp_path)
    agent, importer = _cve_pair(v)
    before = (agent.read_text(), importer.read_text())
    out = _run(_load(), v)
    assert "DRY-RUN entities: 1 duplicate copy(ies)" in out, out
    assert "entities/c/v/CVE-2099-0001  ->  entities/c/v/cve-2099-0001" in out, out
    assert (agent.read_text(), importer.read_text()) == before, "dry-run must not write"


def test_case_pair_across_partition_directories_is_one_group(tmp_path):
    """A flat stale copy in one case and the sharded copy in another are the same page."""
    v = _vault(tmp_path)
    flat = _page(v, "concepts/ClickFix", "type: concept\ntags: [a]", "Flat copy.")
    shard = _page(v, "concepts/c/l/clickfix", "type: concept\ntags: [b]", "Sharded copy, longer.")
    _run(_load(), v, "--apply")
    assert not flat.exists() and shard.is_file()
    assert set(_fm(shard)["tags"]) == {"a", "b"}


# ── merge ────────────────────────────────────────────────────────────────────

def test_apply_merges_onto_the_lowercase_spelling_with_the_existing_merge_semantics(tmp_path):
    v = _vault(tmp_path)
    agent, importer = _cve_pair(v)
    out = _run(_load(), v, "--apply")
    assert not agent.exists(), "the non-canonical spelling is removed"
    assert _names(v / "wiki/entities/c/v") == {"cve-2099-0001.md"}
    fm = _fm(importer)
    assert set(fm["tags"]) == {"cve", "kernel", "kev"}, fm            # list union
    assert fm["cve_id"] == "CVE-2099-0001" and fm["cvss_base"] == 5.5  # importer-only scalars kept
    assert fm["related"] == ["[[entities/a/n/android]]"]              # agent-only list kept
    assert fm["needs_review"] is True, "bodies differ -> review, never a silent drop"
    assert "A long agent-written analysis" in importer.read_text()     # the longest body wins
    assert "Short importer body." not in importer.read_text()
    assert "1 removed" in out and "needs_review" in out


def test_identical_bodies_and_agreeing_scalars_are_not_flagged(tmp_path):
    v = _vault(tmp_path)
    upper = _page(v, "entities/a/r/ArXiv", "type: vulnerability\nid: entities:arxiv", "Same.")
    lower = _page(v, "entities/a/r/arxiv", "type: vulnerability\nid: entities:arxiv", "Same.")
    _run(_load(), v, "--apply")
    assert not upper.exists()
    assert "needs_review" not in _fm(lower)


def test_without_an_all_lowercase_spelling_the_fewest_capitals_win(tmp_path):
    v = _vault(tmp_path)
    many = _page(v, "concepts/m/a/MacOS-Stealers", "type: concept", "x")
    few = _page(v, "concepts/m/a/macOS-stealers", "type: concept", "x")
    _run(_load(), v, "--apply")
    assert few.is_file() and not many.exists()


def test_capital_count_ties_break_lexically(tmp_path):
    v = _vault(tmp_path)
    a = _page(v, "concepts/m/a/Macos-stealers", "type: concept", "x")
    b = _page(v, "concepts/m/a/macos-Stealers", "type: concept", "x")
    _run(_load(), v, "--apply")
    # "Macos-stealers" < "macos-Stealers" (uppercase sorts first in code-point order)
    assert a.is_file() and not b.exists()


def test_the_canonical_seat_wins_even_when_it_holds_the_other_spelling(tmp_path):
    """Lowercase spelling only at a stale flat path; a capitalized copy already sits in the
    declared shard. The survivor lands IN the shard under the canonical spelling, not back at
    the flat path find_page's exact-spelling preference would have picked."""
    v = _vault(tmp_path)
    flat = _page(v, "entities/qwen3", "type: vulnerability", "flat")
    shard = _page(v, "entities/q/w/Qwen3", "type: vulnerability", "sharded, longer")
    out = _run(_load(), v, "--apply")
    seat = v / "wiki/entities/q/w/qwen3.md"
    assert seat.is_file() and not flat.exists() and not shard.exists(), out
    assert _names(v / "wiki/entities/q/w") == {"qwen3.md"}


def test_a_hard_linked_loser_that_is_the_destination_is_never_unlinked(tmp_path):
    """On a case-insensitive filesystem the capitalized loser IS the destination file. Model
    that with a hard link: removing the loser would delete the merged page."""
    v = _vault(tmp_path)
    flat = _page(v, "entities/qwen3", "type: vulnerability", "flat")
    seat = v / "wiki/entities/q/w/qwen3.md"
    seat.parent.mkdir(parents=True)
    alias = v / "wiki/entities/q/w/Qwen3.md"
    alias.write_text("---\ntype: vulnerability\n---\n\nsharded, longer\n")
    os.link(alias, seat)                        # Qwen3.md and qwen3.md are one inode
    m = _load()
    move_map, _review, removed = m.dedup_namespace(v, "entities", apply=True)
    assert "entities/q/w/Qwen3" in move_map
    assert alias.exists() and seat.is_file() and not flat.exists()
    assert removed == 1                         # only the flat copy


def test_only_one_live_spelling_beside_a_tombstone_is_left_alone(tmp_path):
    v = _vault(tmp_path)
    live = _page(v, "entities/f/o/foo", "type: vulnerability\nstatus: active")
    tomb = _page(v, "entities/f/o/Foo", "type: vulnerability\nstatus: tombstoned\n"
                 "superseded_by: entities/x/y/other")
    out = _run(_load(), v, "--apply")
    assert live.is_file() and tomb.is_file()
    assert "blocked" in out


# ── links ────────────────────────────────────────────────────────────────────

def test_links_and_frontmatter_refs_to_the_removed_spelling_are_rewritten(tmp_path):
    v = _vault(tmp_path)
    _cve_pair(v)
    note = _page(
        v, "concepts/k/e/kernel-bugs",
        "type: concept\nrelated: ['[[entities/c/v/CVE-2099-0001]]']\n"
        "subject: entities/c/v/CVE-2099-0001\nother: entities/c/v/CVE-2099-00012",
        "See [[entities/c/v/CVE-2099-0001|the bug]] and [[entities/c/v/CVE-2099-0001#fix]].")
    out = _run(_load(), v, "--apply")
    text = note.read_text()
    assert "CVE-2099-0001]]" not in text and "CVE-2099-0001|" not in text and "CVE-2099-0001#" not in text
    assert "[[entities/c/v/cve-2099-0001]]" in text
    assert "[[entities/c/v/cve-2099-0001|the bug]]" in text
    assert "[[entities/c/v/cve-2099-0001#fix]]" in text
    fm = _fm(note)
    assert fm["subject"] == "entities/c/v/cve-2099-0001", "a bare frontmatter ref is rewritten"
    assert fm["other"] == "entities/c/v/CVE-2099-00012", "a longer path is not touched"
    assert "4 link(s) rewritten" in out, out


def test_dry_run_counts_bare_refs_without_writing(tmp_path):
    v = _vault(tmp_path)
    _cve_pair(v)
    note = _page(v, "concepts/k/e/note", "type: concept\nsubject: entities/c/v/CVE-2099-0001")
    before = note.read_text()
    out = _run(_load(), v)
    assert "1 link(s) rewritten" in out and note.read_text() == before


# ── negative space ───────────────────────────────────────────────────────────

def test_separator_variants_are_never_merged(tmp_path):
    v = _vault(tmp_path)
    pages = [
        _page(v, "entities/i/p/ip-10-1-11-1", "type: vulnerability", "one host"),
        _page(v, "entities/i/p/ip-10-11-1-1", "type: vulnerability", "another host"),
        _page(v, "entities/a/g/agent-tesla", "type: vulnerability", "a"),
        _page(v, "entities/a/g/agent_tesla", "type: vulnerability", "b"),
        _page(v, "entities/a/g/Agent_Tesla", "type: vulnerability", "b, capitalized"),
    ]
    out = _run(_load(), v, "--apply")
    # Only the true case pair (agent_tesla / Agent_Tesla) collapses; nothing else is touched.
    assert all(p.exists() for p in pages[:4]), out
    assert not pages[4].exists()
    assert "1 duplicate copy(ies)" in out


def test_a_character_fold_is_not_a_case_fold(tmp_path):
    """str.casefold() maps 'ß' to 'ss': a character fold, not case. Those stay distinct."""
    v = _vault(tmp_path)
    a = _page(v, "entities/s/t/straße", "type: vulnerability", "a")
    b = _page(v, "entities/s/t/strasse", "type: vulnerability", "b")
    c = _page(v, "entities/s/t/STRASSE", "type: vulnerability", "c")
    _run(_load(), v, "--apply")
    assert a.exists() and b.exists() and not c.exists()


@pytest.mark.parametrize("names", [
    ("INDEX.md", "index.md"),
    ("INDEX.md", "Index.md"),
    ("index.md", "Index.md"),
    ("INDEX-p02.md", "Index-p02.md"),
    ("Log.md", "log.md"),
    ("LOG.md", "Log.md"),
    ("README.md", "Readme.md"),
    ("_Scaffold.md", "_scaffold.md"),
])
def test_generated_artifact_case_variants_are_never_removed(tmp_path, names):
    v = _vault(tmp_path)
    d = v / "wiki/entities/c/v"
    d.mkdir(parents=True)
    for n in names:
        (d / n).write_text(f"---\ntype: vulnerability\n---\n\n{n} generated\n")
    out = _run(_load(), v, "--apply")
    assert _names(d) == set(names), out
    assert "0 duplicate copy(ies)" in out


def test_index_in_case_variant_directories_is_never_removed(tmp_path):
    """The okcti shape: sources/2026/July/INDEX.md beside sources/2026/july/INDEX.md."""
    v = _vault(tmp_path)
    for d in ("July", "july"):
        p = v / "wiki/sources/2026" / d / "INDEX.md"
        p.parent.mkdir(parents=True)
        p.write_text(f"# {d}\n")
    out = _run(_load(), v, "--apply")
    assert (v / "wiki/sources/2026/July/INDEX.md").is_file()
    assert (v / "wiki/sources/2026/july/INDEX.md").is_file()
    assert "0 duplicate copy(ies)" in out


def test_indexing_content_pages_are_still_content(tmp_path):
    """The reserved-name fold must not swallow real pages whose name merely starts 'index'."""
    v = _vault(tmp_path)
    a = _page(v, "concepts/i/n/Indexing-attacks", "type: concept", "a")
    b = _page(v, "concepts/i/n/indexing-attacks", "type: concept", "b")
    _run(_load(), v, "--apply")
    assert b.exists() and not a.exists()


# ── idempotency ──────────────────────────────────────────────────────────────

def test_second_apply_is_a_no_op(tmp_path):
    v = _vault(tmp_path)
    _cve_pair(v)
    _page(v, "concepts/k/e/note", "type: concept\nsubject: entities/c/v/CVE-2099-0001",
          "[[entities/c/v/CVE-2099-0001]]")
    m = _load()
    assert "1 removed" in _run(m, v, "--apply")
    snapshot = {p: p.read_text() for p in (v / "wiki").rglob("*.md")}
    out = _run(m, v, "--apply")
    assert "0 duplicate copy(ies), 0 removed, 0 link(s) rewritten, 0 needs_review" in out, out
    assert {p: p.read_text() for p in (v / "wiki").rglob("*.md")} == snapshot


# ── the rule, directly ───────────────────────────────────────────────────────

@pytest.mark.parametrize("stems, expected", [
    (["CVE-2099-0001", "cve-2099-0001"], "cve-2099-0001"),
    (["cve-2099-0001", "CVE-2099-0001"], "cve-2099-0001"),
    (["ClickFix", "clickfix", "CLICKFIX"], "clickfix"),
    (["MacOS-Stealers", "macOS-stealers"], "macOS-stealers"),
    (["macos-Stealers", "Macos-stealers"], "Macos-stealers"),
    (["solo"], "solo"),
])
def test_canonical_stem_rule(stems, expected):
    assert _load()._canonical_stem(stems) == expected


@pytest.mark.parametrize("a, b, same", [
    ("CVE-2099-0001", "cve-2099-0001", True),
    ("ip-10-1-11-1", "ip-10-11-1-1", False),
    ("agent-tesla", "agent_tesla", False),
    ("straße", "STRASSE", False),
    ("İstanbul", "i̇stanbul", False),       # 'İ'.lower() expands to two code points: not folded
    ("Élan", "élan", True),
])
def test_case_key_folds_case_only(a, b, same):
    m = _load()
    assert (m._case_key(a) == m._case_key(b)) is same


def test_same_file_is_false_when_a_path_has_vanished(tmp_path):
    """A loser removed by a concurrent lane is not the destination; the unlink then just
    reports its own failure through the existing path."""
    m = _load()
    present = tmp_path / "present.md"
    present.write_text("x")
    assert m._same_file(tmp_path / "gone.md", present) is False
    assert m._same_file(present, present) is True


def test_an_exact_name_group_off_its_seat_still_resolves_through_write_key(tmp_path):
    """The case-variant seat rule applies only when a live copy holds the declared seat in
    another spelling. Two same-spelling copies that are both off the seat keep the existing
    contract: write_key's answer, the deepest existing copy."""
    v = _vault(tmp_path)
    flat = _page(v, "entities/zeta", "type: vulnerability", "flat")
    deep = _page(v, "entities/zz/zeta", "type: vulnerability", "deeper, longer")
    _run(_load(), v, "--apply")
    assert deep.is_file() and not flat.exists()
    assert not (v / "wiki/entities/z/e/zeta.md").exists()
