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
