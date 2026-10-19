"""trends-refresh TOPIC fallback: source tags whose SHARE of the window rose.

On a vault whose entity lanes mint one entity per source, the entity-mention signal never fires
(one live vault: 0 movers every week since August while ~3,000 sources/6 weeks arrived). The
fallback nominates tags instead — by share, not count, and only when >= 2 publishers carry them.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"
NOW = "2026-06-26"            # window: 2026-05-15..06-26; prior: 2026-04-03..05-15
RECENT, PRIOR = "2026-06-10", "2026-04-20"


def _src(root: Path, name: str, day: str, tags=(), publisher: str = "", extra: str = "") -> None:
    tag_lines = "".join(f"  - {t}\n" for t in tags)
    p = root / "wiki" / "sources" / day[:4] / day[5:7] / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: source\npublished: '{day}'\n"
                 + (f"publisher: {publisher}\n" if publisher else "")
                 + (f"tags:\n{tag_lines}" if tags else "") + extra + "---\nbody\n")


@pytest.fixture
def gate(tmp_path, monkeypatch):
    def load(**env):
        monkeypatch.setenv("WIKI_PATH", str(tmp_path))
        monkeypatch.setenv("TREND_NOW", NOW)
        for key in ("TREND_TAG_EXCLUDE", "TREND_TAG_FALLBACK"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.syspath_prepend(str(CRON))
        sys.modules.pop("select_trend_deltas", None)
        return importlib.import_module("select_trend_deltas")
    yield load, tmp_path
    sys.modules.pop("select_trend_deltas", None)


def _corpus(root: Path) -> None:
    """15 recent + 10 prior sources, with the tag shapes the fallback judges."""
    for i in range(10):
        _src(root, f"prior-{i}", PRIOR, tags=["flat"] * (i < 6) + ["rl"] * (i < 1),
             publisher="A")
    for i in range(10):
        tags = (["rl"] if i < 5 else []) + (["flat"] if i < 6 else []) \
            + (["cs.xx"] if i < 5 else [])
        _src(root, f"recent-{i}", RECENT, tags=tags, publisher="AB"[i % 2])
    for i in range(5):  # one outlet's house taxonomy: plenty of hits, a single publisher
        _src(root, f"house-{i}", RECENT, tags=["house"], publisher="C")
    for i in range(5):  # a retired source never votes
        _src(root, f"ghost-{i}", RECENT, tags=["ghost"], publisher=f"P{i}",
             extra="status: tombstoned\n")


def _run(mod, capsys) -> tuple[str, dict]:
    assert mod.main() == 0
    out = capsys.readouterr().out
    return out, json.loads(out.strip().splitlines()[-1])


def test_a_tag_whose_share_rose_across_publishers_wakes_the_lane(gate, capsys):
    load, root = gate
    _corpus(root)
    out, verdict = _run(load(), capsys)
    assert verdict == {"wakeAgent": True}
    assert "## topic `rl` — 5 recent (33.3%) vs 1 prior (10.0%), 3.3x share, 2 publishers" in out
    assert "[[sources/2026/06/recent-0]]" in out, "candidates arrive with their grounding sources"


def test_count_artifacts_are_not_trends(gate, capsys):
    """NEGATIVE: flat share, one outlet's house tag, and retired sources never nominate."""
    load, root = gate
    _corpus(root)
    out, _ = _run(load(), capsys)
    for tag in ("flat", "house", "ghost"):
        assert f"topic `{tag}`" not in out, tag


def test_a_doubled_feed_volume_alone_is_not_a_trend(gate, capsys):
    """NEGATIVE: twice the sources and twice the tag is the same share — the case raw counts
    get wrong."""
    load, root = gate
    for i in range(10):
        _src(root, f"prior-{i}", PRIOR, tags=["vol"] * (i < 5), publisher="A")
    for i in range(20):
        _src(root, f"recent-{i}", RECENT, tags=["vol"] * (i < 10), publisher="AB"[i % 2])
    out, verdict = _run(load(), capsys)
    assert verdict == {"wakeAgent": False} and "no rising entity or topic" in out


def test_the_pack_exclude_pattern_removes_taxonomy_codes(gate, capsys):
    load, root = gate
    _corpus(root)
    out, _ = _run(load(), capsys)
    assert "topic `cs.xx`" in out, "the engine ships no exclusions of its own"
    out, _ = _run(load(TREND_TAG_EXCLUDE=r"^[a-z]+\.[a-z]+$"), capsys)
    assert "topic `cs.xx`" not in out and "topic `rl`" in out
    assert "pack exclude set" in out


def test_the_fallback_can_be_turned_off(gate, capsys):
    load, root = gate
    _corpus(root)
    out, verdict = _run(load(TREND_TAG_FALLBACK="0"), capsys)
    assert verdict == {"wakeAgent": False} and "topic fallback: off" in out


def test_entity_movers_still_take_precedence(gate, capsys):
    load, root = gate
    _corpus(root)
    ent = root / "wiki" / "entities" / "r" / "riser.md"
    ent.parent.mkdir(parents=True)
    ent.write_text("---\ntype: model\nsources:\n" + "".join(
        f"- '[[sources/2026/06/recent-{i}]]'\n" for i in range(3)) + "---\n")
    out, verdict = _run(load(TREND_MIN_MOVERS="1"), capsys)
    assert verdict == {"wakeAgent": True}
    assert "entities/r/riser" in out and "rising TOPICS" not in out


def test_malformed_and_undated_pages_are_skipped(gate, capsys):
    load, root = gate
    sdir = root / "wiki" / "sources" / "2026" / "06"
    sdir.mkdir(parents=True)
    (sdir / "no-frontmatter.md").write_text("just text\n")
    (sdir / "bad-yaml.md").write_text("---\npublished: '2026-06-10'\ntags: [unclosed\n---\n")
    (sdir / "list-yaml.md").write_text("---\n- published: '2026-06-10'\n---\n")
    (sdir / "_draft.md").write_text("---\npublished: '2026-06-10'\ntags: [rl]\n---\n")
    (sdir / "INDEX.md").write_text("---\npublished: '2026-06-10'\ntags: [rl]\n---\n")
    (sdir / "undated.md").write_text("---\ntags: [rl]\n---\n")
    _src(root, "old", "2025-01-01", tags=["rl"], publisher="A")
    _src(root, "str-tag", RECENT, tags=(), publisher="", extra="tags: solo\nsource_tags: ['']\n")
    (sdir / "vanished.md").symlink_to(sdir / "gone.md")  # listed, unreadable: skipped
    mod = load()
    assert mod._source_meta("no frontmatter at all\n") == (set(), "")
    assert mod._source_meta("---\n- a\n---\n") == (set(), "")
    assert mod._source_meta("---\ntags: solo\n---\n") == ({"solo"}, "")
    movers, n_this, n_prior = mod.tag_movers(mod._today())
    assert movers == [] and n_this == 2 and n_prior == 0, "bad-yaml + str-tag are dated in-window"


def test_no_sources_directory_is_a_clean_skip(gate, capsys):
    load, root = gate
    (root / "wiki").mkdir()
    out, verdict = _run(load(), capsys)
    assert verdict == {"wakeAgent": False}


# The entity path predates this file and was only ever run as a subprocess (tests/test_trends.py),
# which coverage cannot see in a non-package scripts/ tree — so it read as "100%" while untested.
# Importing the selector in-process above made it visible; these pin its branches.

def test_today_defaults_to_the_real_date_and_bad_dates_parse_to_none(gate, monkeypatch):
    load, _root = gate
    mod = load()
    monkeypatch.delenv("TREND_NOW")
    assert mod._today() == mod.date.today()
    assert mod._parse_date("not-a-date") is None


def test_source_dates_fall_back_to_the_folder_month_and_skip_the_rest(gate):
    load, root = gate
    sdir = root / "wiki" / "sources"
    for rel, body in {
        "2026/05/folder-dated.md": "---\ntype: source\n---\n",        # no `published`
        "2026/13/impossible-month.md": "---\ntype: source\n---\n",    # folder month invalid
        "loose/undatable.md": "---\ntype: source\n---\n",             # no date anywhere
        "2026/05/_draft.md": "---\npublished: '2026-05-01'\n---\n",
        "2026/05/INDEX.md": "---\npublished: '2026-05-01'\n---\n",
    }.items():
        (sdir / rel).parent.mkdir(parents=True, exist_ok=True)
        (sdir / rel).write_text(body)
    (sdir / "2026" / "05" / "gone.md").symlink_to(sdir / "missing.md")  # unreadable
    dates = load().source_dates()
    assert dates == {"2026/05/folder-dated": __import__("datetime").date(2026, 5, 15)}


def test_scan_entities_reads_refs_and_skips_what_it_cannot_use(gate):
    load, root = gate
    edir = root / "wiki" / "entities" / "a"
    edir.mkdir(parents=True)
    (edir / "typed.md").write_text("---\ntype: model\n---\nsee [[sources/2026/06/x]]\n")
    (edir / "untyped.md").write_text("no frontmatter, cites [[sources/2026/06/y]]\n")
    (edir / "no-refs.md").write_text("---\ntype: model\n---\nnothing cited\n")
    (edir / "_draft.md").write_text("---\ntype: model\n---\n[[sources/2026/06/z]]\n")
    (edir / "INDEX.md").write_text("[[sources/2026/06/z]]\n")
    (edir / "gone.md").symlink_to(edir / "missing.md")
    assert sorted(load().scan_entities()) == [("a/typed", "model", ["2026/06/x"]),
                                              ("a/untyped", "?", ["2026/06/y"])]


def test_the_entity_window_counts_prior_refs_and_ignores_unresolved_ones(gate, capsys):
    """An entity with more prior than recent citations is not rising; an unresolved ref never
    counts. Only the riser (3 recent, 0 prior) nominates."""
    load, root = gate
    for i in range(4):
        _src(root, f"r{i}", RECENT, publisher="A")
        _src(root, f"p{i}", PRIOR, publisher="A")
    _src(root, "ancient", "2025-01-10", publisher="A")  # older than both windows: not counted
    edir = root / "wiki" / "entities" / "e"
    edir.mkdir(parents=True)
    (edir / "riser.md").write_text("---\ntype: model\n---\n" + "".join(
        f"[[sources/2026/06/r{i}]]\n" for i in range(3)) + "[[sources/2026/06/unknown]]\n")
    (edir / "faller.md").write_text("---\ntype: model\n---\n" + "".join(
        f"[[sources/2026/04/p{i}]]\n" for i in range(4)) + "[[sources/2026/06/r3]]\n"
        + "[[sources/2025/01/ancient]]\n")
    out, verdict = _run(load(TREND_MIN_MOVERS="1"), capsys)
    assert verdict == {"wakeAgent": True}
    assert "entities/e/riser" in out and "entities/e/faller" not in out
