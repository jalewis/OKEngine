"""CLI parity and idempotent legacy review migration (#256)."""
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "framework_review.py"


def _run(vault, *args):
    return subprocess.run([sys.executable, str(CLI), str(vault), *args],
                          text=True, capture_output=True, check=False)


def test_migration_dry_run_apply_and_rerun_are_safe(tmp_path):
    wiki = tmp_path / "wiki" / "entities" / "a"
    wiki.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf: {required: [type]}\nstrict_types: false\n", encoding="utf-8")
    (wiki / "flagged.md").write_text(
        "---\ntype: actor\ntitle: Flagged\nversion: 2\nneeds_review: true\n"
        "created: 2025-01-01\n---\nClaim.\n", encoding="utf-8")
    (wiki / "signed.md").write_text(
        "---\ntype: actor\ntitle: Signed\nversion: 3\nreviewed_by: Jane\n"
        "reviewed_on: 2026-06-01\n---\nClaim.\n", encoding="utf-8")

    dry = _run(tmp_path, "--migrate")
    assert dry.returncode == 0 and "DRY RUN" in dry.stdout
    assert not (tmp_path / "wiki" / "operational" / "reviews").exists()

    first = _run(tmp_path, "--migrate", "--apply")
    assert first.returncode == 0
    assert "1 open record(s) created" in first.stdout
    assert "1 historical record(s) created" in first.stdout
    records = [yaml.safe_load(p.read_text()) for p in
               (tmp_path / "wiki" / "operational" / "reviews").glob("*.yaml")]
    assert {r["state"] for r in records} == {"open", "approved"}
    imported = next(r for r in records if r["state"] == "approved")
    assert "evidence examination is not asserted" in imported["reasons"][0]["detail"]

    second = _run(tmp_path, "--migrate", "--apply")
    assert second.returncode == 0
    assert "0 open record(s) created" in second.stdout
    assert "0 historical record(s) created" in second.stdout
    assert len(list((tmp_path / "wiki" / "operational" / "reviews").glob("*.yaml"))) == 2
