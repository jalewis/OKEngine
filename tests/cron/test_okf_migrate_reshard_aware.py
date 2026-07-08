"""Regression: okf_migrate.build_map must NOT revert a valid reshard sub-bucket.

reshard_oversized splits an oversized leaf one level deeper (sources/{y}/{m}/{DD}/slug,
entities/{L}/{2nd}/slug). build_map computed the 2-segment canonical key, saw it differ, and moved
every page straight back up — so reshard@00:45 and reshelve@02:35 churned forever and the leaf-size
invariant was never satisfied. Both crons now share okf_migrate.reshard_seg; build_map treats a
valid reshard sub-bucket as already-canonical.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "okf_migrate.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load():
    spec = importlib.util.spec_from_file_location("okf_migrate", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["okf_migrate"] = m
    spec.loader.exec_module(m)
    return m


def _vault(tmp_path):
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n"
        "    sources: {strategy: by-date, date_field: published, reshard_by: day}\n"
        "    entities: {strategy: by-letter, reshard_by: second-letter}\n"
        "types:\n  source: {}\n  entity: {}\n", encoding="utf-8")
    w = tmp_path / "wiki"
    (w / "sources" / "2026" / "07" / "03").mkdir(parents=True)
    (w / "sources" / "2026" / "07" / "03" / "breach-a.md").write_text(
        "---\ntype: source\npublished: 2026-07-03\n---\n", encoding="utf-8")     # valid reshard bucket
    (w / "sources" / "flat-misplaced.md").write_text(
        "---\ntype: source\npublished: 2026-07-05\n---\n", encoding="utf-8")      # genuinely misplaced
    (w / "entities" / "f" / "o").mkdir(parents=True)
    (w / "entities" / "f" / "o" / "foo.md").write_text(
        "---\ntype: entity\n---\n", encoding="utf-8")                             # valid reshard (2nd of foo)
    return tmp_path


def test_reshard_buckets_are_not_reverted(tmp_path):
    m = _load(); root = _vault(tmp_path)
    src_map, _ = m.build_map(root, "sources")
    assert "sources/2026/07/03/breach-a" not in src_map                # valid reshard left in place
    assert src_map.get("sources/flat-misplaced") == "sources/2026/07/flat-misplaced"  # real move still happens

    ent_map, _ = m.build_map(root, "entities")
    assert "entities/f/o/foo" not in ent_map                           # valid reshard left in place
    assert ent_map == {}                                               # nothing to move


def test_shared_reshard_seg_matches_writer(tmp_path):
    # the writer (reshard_oversized) and the drain must compute the identical segment
    m = _load()
    assert m.reshard_seg("day", "x", {"published": "2026-07-03"}) == "03"
    assert m.reshard_seg("second-letter", "foo", {}) == "o"
    assert m.reshard_seg("nope", "x", {}) is None
