"""dedup-partition-collisions link-rewrite count must reflect ACTUAL rewrites, not re.subn's
match count.

Regression: make_rewriter's repl returns any link NOT in move_map unchanged, but re.subn counts
every `[[ns/…]]` match as a substitution — so the tally added untouched links too. It reported
~19,675 "links rewritten" for 2 entities when only 6 actually changed.
"""
import importlib.util
import sys
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
