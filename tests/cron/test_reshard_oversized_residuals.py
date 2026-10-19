"""Regression for #243: already-split buckets must sweep residual direct files."""
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
    return mod


def test_already_split_bucket_sweeps_below_threshold_residual(tmp_path):
    bucket = tmp_path / "wiki" / "entities" / "c"
    (bucket / "v").mkdir(parents=True)
    (bucket / "v" / "cve-existing.md").write_text("---\ntype: vulnerability\n---\n")
    (bucket / "cve-residual.md").write_text("---\ntype: vulnerability\n---\n")

    mod = _load(tmp_path)
    mapping = mod._build_map("entities", "second-letter", 500)
    assert mapping == {
        "entities/c/cve-residual": "entities/c/v/cve-residual",
    }


def test_unsplit_bucket_below_threshold_is_left_alone(tmp_path):
    bucket = tmp_path / "wiki" / "entities" / "c"
    bucket.mkdir(parents=True)
    (bucket / "cve-small.md").write_text("---\ntype: vulnerability\n---\n")

    mod = _load(tmp_path)
    assert mod._build_map("entities", "second-letter", 500) == {}


def test_long_frontmatter_page_is_split_to_the_seat_the_drain_keeps(tmp_path):
    """okengine#993: the resharder read frontmatter from a 4000-char window, so a source whose
    envelope ran past it parsed as {} and was split to the `00` day bucket. The reshelve drain
    reads the whole envelope, computes the published day, and moved it back to the month bucket;
    the next reshard split it to `00` again. Live, three sources on one vault cycled daily."""
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n"
        "    sources: {strategy: by-date, date_field: published, reshard_by: day}\n"
        "types:\n  source: {}\n", encoding="utf-8")
    bucket = tmp_path / "wiki" / "sources" / "2026" / "04"
    (bucket / "03").mkdir(parents=True)                       # the month bucket is already split
    (bucket / "03" / "other.md").write_text("---\ntype: source\npublished: 2026-04-03\n---\n")
    raw = "".join(f"- raw/market/2026-04-11-a-long-capture-name-{i:04d}.md\n" for i in range(120))
    (bucket / "open-model-consortium.md").write_text(
        f"---\ntype: source\npublished: 2026-04-11\nraw:\n{raw}---\nBody.\n", encoding="utf-8")
    assert len((bucket / "open-model-consortium.md").read_text()) > 4000

    mod = _load(tmp_path)
    okf = sys.modules["okf_migrate"]
    mapping = mod._build_map("sources", "day", 500)
    assert mapping == {"sources/2026/04/open-model-consortium":
                       "sources/2026/04/11/open-model-consortium"}

    # The seat the resharder picks is one the drain leaves alone.
    seat = bucket / "11" / "open-model-consortium.md"
    seat.parent.mkdir()
    (bucket / "open-model-consortium.md").rename(seat)
    drain_moves, _ = okf.build_map(tmp_path, "sources")
    assert drain_moves == {}
