"""year_derivation_audit: how each raw file's year is derived, and the dashboard that audits it.

Runs in-process against disposable vaults. File years come from real mtimes (``os.utime``) and real
path tokens; the year index and bulk-import mtimes the selector would load at import are pointed at
per-test values on the audit module.
"""
from __future__ import annotations

import importlib.util
import os
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "year_derivation_audit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("year_derivation_audit_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Y = _load_module()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / "raw").mkdir(parents=True)
    monkeypatch.setattr(Y, "VAULT", root)
    monkeypatch.setattr(Y, "RAW", root / "raw")
    monkeypatch.setattr(Y, "SOURCES", root / "wiki" / "sources")
    monkeypatch.setattr(Y, "OUT", root / "wiki" / "dashboards" / "year-derivation-audit.md")
    monkeypatch.setattr(Y, "_YEAR_INDEX", {})
    monkeypatch.setattr(Y, "BULK_IMPORT_MTIMES", set())
    return root


def _raw(vault: Path, rel: str, year: int | None = None, *, mtime: int | None = None) -> Path:
    path = vault / "raw" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content\n", encoding="utf-8")
    if year is not None:
        mtime = int(datetime(year, 6, 1).timestamp())
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _by_rel(data: dict) -> dict[str, dict]:
    return {f["rel"]: f for f in data["files"]}


# --- scan ---------------------------------------------------------------------------------------

def test_scan_prefers_the_year_index_over_path_and_mtime(vault, monkeypatch):
    _raw(vault, "feeds/2021/a.md", 2024)
    monkeypatch.setattr(Y, "_YEAR_INDEX", {"raw/feeds/2021/a.md": 2019})

    f = _by_rel(Y.scan())["raw/feeds/2021/a.md"]

    assert (f["derived"], f["reason"]) == (2019, "index")


def test_scan_uses_the_latest_plausible_path_year(vault):
    _raw(vault, "feeds/2019/q3/2022-report-1999.md", 2025)

    f = _by_rel(Y.scan())["raw/feeds/2019/q3/2022-report-1999.md"]

    assert (f["derived"], f["reason"], f["path_years"]) == (2022, "path", [2019, 2022])
    assert f["top"] == "feeds"


def test_scan_demotes_a_bulk_import_mtime_to_the_sentinel_year(vault, monkeypatch):
    stamp = int(datetime(2024, 3, 3).timestamp())
    _raw(vault, "dump/a.md", mtime=stamp)
    monkeypatch.setattr(Y, "BULK_IMPORT_MTIMES", {stamp})

    f = _by_rel(Y.scan())["raw/dump/a.md"]

    assert (f["derived"], f["reason"], f["bulk_demoted"]) == (Y.BULK_IMPORT_SENTINEL_YEAR, "mtime-bulk-demoted", True)


def test_scan_falls_back_to_the_mtime_year(vault):
    _raw(vault, "loose.md", 2023)

    f = _by_rel(Y.scan())["raw/loose.md"]

    assert (f["derived"], f["reason"], f["top"]) == (2023, "mtime", "(root)")


def test_scan_skips_non_leaf_hidden_malformed_and_companion_files(vault):
    _raw(vault, "a/image.png")
    _raw(vault, "a/.cache/x.md")
    _raw(vault, "a\\b/x.md")
    _raw(vault, "a/doc.pdf")
    _raw(vault, "a/doc.pdf.txt")
    _raw(vault, "orphan/link.md")
    _raw(vault, "paired/link.md")
    _raw(vault, "paired/content.txt")
    (vault / "raw" / "subdir.md").mkdir()

    data = Y.scan()

    assert sorted(_by_rel(data)) == ["raw/a/doc.pdf.txt", "raw/paired/content.txt", "raw/paired/link.md"]
    assert (data["bogus_paths"], data["skipped_pdf_with_companion"], data["skipped_orphan_link_md"]) == (1, 1, 1)


def test_scan_marks_files_a_source_page_already_ingested(vault):
    _raw(vault, "feeds/a.md", 2024)
    _raw(vault, "feeds/b.md", 2024)
    src = vault / "wiki" / "sources" / "2024" / "s.md"
    src.parent.mkdir(parents=True)
    src.write_text("---\ntype: source\nraw: raw/feeds/a.md\npublisher: P\npublished: 2024-01-01\n---\n"
                   + "An accepted source page carries a real body of text. " * 3 + "\n", encoding="utf-8")
    (vault / "wiki" / "sources" / "dir-named.md").mkdir()

    files = _by_rel(Y.scan())

    assert files["raw/feeds/a.md"]["processed"] is True
    assert files["raw/feeds/b.md"]["processed"] is False


def test_scan_skips_a_raw_entry_that_vanishes_before_it_is_stat_and_keeps_walking(vault):
    # The walk is sorted, so the unreadable "a-gone.md" is reached BEFORE "b-kept.md": skipping it
    # must continue the walk, never end it.
    (vault / "raw" / "feeds").mkdir()
    (vault / "raw" / "feeds" / "a-gone.md").symlink_to(vault / "raw" / "nowhere.md")
    _raw(vault, "feeds/b-kept.md", 2024)

    assert sorted(_by_rel(Y.scan())) == ["raw/feeds/b-kept.md"]


def test_scan_reports_files_in_sorted_path_order(vault):
    for rel in ("feeds/c.md", "feeds/a.md", "archive/b.md"):
        _raw(vault, rel, 2024)

    assert [f["rel"] for f in Y.scan()["files"]] == ["raw/archive/b.md", "raw/feeds/a.md", "raw/feeds/c.md"]


# --- render -------------------------------------------------------------------------------------

def _file(rel: str, derived: int, reason: str, mtime_year: int | None = None, processed: bool = False,
          top: str = "feeds") -> dict:
    return {"rel": rel, "mtime": 0.0, "mtime_year": derived if mtime_year is None else mtime_year,
            "path_years": [], "derived": derived, "reason": reason, "bulk_demoted": False,
            "processed": processed, "top": top, "tier": 0}


def _data(files, bogus=0, pdf=0, orphan=0) -> dict:
    return {"files": files, "bogus_paths": bogus, "skipped_pdf_with_companion": pdf,
            "skipped_orphan_link_md": orphan}


def test_render_of_an_empty_scan_reports_no_risks():
    page = Y.render(_data([]))

    assert "- Total raw files scanned: **0**" in page
    assert "_None — no implausible mtime-derived years in the unprocessed set._" in page
    assert "_None — every path-derived year is within 2 of mtime year._" in page
    assert "Skipped" not in page


def test_render_lists_each_skip_class_it_saw():
    page = Y.render(_data([], bogus=2, pdf=3, orphan=4))

    assert "malformed-path guard): 2" in page
    assert "`.pdf.txt` companion): 3" in page
    assert "no sibling `content.txt`): 4" in page


def test_render_counts_only_unprocessed_files_by_reason_and_year():
    files = [_file("raw/feeds/a.md", 2024, "index"), _file("raw/feeds/b.md", 2024, "path"),
             _file("raw/feeds/c.md", 2023, "mtime"), _file("raw/feeds/d.md", 2023, "mtime", processed=True)]

    page = Y.render(_data(files))

    assert "- Already processed (raw: dedupe match): **1**" in page
    assert "- Unprocessed: **3**" in page
    assert "| 2024 | 1 | 1 | 0 | 2 |" in page
    assert "| 2023 | 0 | 0 | 1 | 1 |" in page
    assert "| `feeds` | 3 | 1 | 1 | 1 | 2024 |" in page


def test_render_flags_implausible_mtime_years_and_truncates_the_sample():
    files = [_file(f"raw/old/{i:02d}.md", 2010, "mtime", top="old") for i in range(31)]
    files.append(_file("raw/new/future.md", Y.PLAUSIBLE_MAX + 1, "mtime", top="new"))

    page = Y.render(_data(files))

    assert "## High-risk: mtime-fallback with implausible year (32)" in page
    assert "| 2010  ⚠ |" in page
    assert f"| {Y.PLAUSIBLE_MAX + 1}  ⚠ |" in page
    assert "| … | _and 2 more_ |" in page


def test_render_lists_path_years_far_from_mtime_and_truncates_the_sample():
    files = [_file(f"raw/feeds/{i:02d}-2019.md", 2019, "path", mtime_year=2025) for i in range(31)]
    files.append(_file("raw/feeds/near-2024.md", 2024, "path", mtime_year=2025))

    page = Y.render(_data(files))

    assert "## Disagreement: path year vs mtime year (>2 years apart) (31)" in page
    assert "| 2019 | 2025 | -6 | `raw/feeds/30-2019.md` |" in page
    assert "near-2024" not in page.split("## Disagreement", 1)[1].split("## Unprocessed by top-level", 1)[0]
    assert "| … | … | … | _and 1 more_ |" in page


def test_render_lists_a_short_risk_sample_without_a_truncation_row():
    files = [_file("raw/old/a.md", 2010, "mtime", top="old"),
             _file("raw/feeds/2019-b.md", 2019, "path", mtime_year=2025)]

    page = Y.render(_data(files))

    assert "| 2010 | `raw/old/a.md` |" in page
    assert "| 2019 | 2025 | -6 | `raw/feeds/2019-b.md` |" in page
    assert "_and " not in page


# --- main ---------------------------------------------------------------------------------------

def test_main_without_a_raw_dir_is_an_error_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Y, "RAW", tmp_path / "missing")

    assert Y.main() == 1
    assert "raw/ not found" in capsys.readouterr().err


def test_main_writes_the_dashboard_and_keeps_one_backup_of_the_previous_one(vault):
    _raw(vault, "feeds/2024/a.md", 2024)
    out = vault / "wiki" / "dashboards" / "year-derivation-audit.md"

    assert Y.main() == 0
    first = out.read_text()
    assert Y.main() == 0

    assert "# Year-derivation audit" in out.read_text()
    assert (out.parent / "year-derivation-audit.md.bak").read_text() == first
