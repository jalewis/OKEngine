"""check_implausible_dates: directional plausibility bounds on page date fields.

The module had no tests until okengine#991 touched its `_FULLDATE` anchor; these pin the
behavioral contract the mutation target scores against: backward fields may not be future,
forward fields may be future but not implausibly far, and no field may predate FLOOR_YEAR.
"""
import importlib.util
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
TODAY = date(2026, 6, 15)


def _mod(monkeypatch=None, vault=None):
    if monkeypatch is not None and vault is not None:
        monkeypatch.setenv("WIKI_PATH", str(vault))
    spec = importlib.util.spec_from_file_location(
        "check_implausible_dates", REPO / "scripts" / "cron" / "check_implausible_dates.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _page(path: Path, frontmatter: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("frontmatter", [
    "published: 2026-06-15",          # today
    "published: 2026-06-17",          # inside the two-day clock-skew slack
    "published: 1990-01-01",          # exactly the floor year
    "published: 2026",                # a bare current year
    "resolves_by: 2041-01-01",        # today.year + FORWARD_MAX_YEARS is still allowed
    "horizon: 12 months",             # not a date field
    "published: unknown",             # not date-like
    "published: 2026-02-30",          # malformed: left to the schema check
    "published:",                     # empty value
])
def test_plausible_values_raise_no_issue(frontmatter):
    assert _mod().classify_implausible(frontmatter, TODAY) == []


@pytest.mark.parametrize("frontmatter, expected", [
    ("published: 1970-01-01", "published=1970-01-01: implausible year 1970 (before 1990)"),
    ("resolves_by: 1025-05-01", "resolves_by=1025-05-01: implausible year 1025 (before 1990)"),
    ("published: 1989", "published=1989: implausible year 1989 (before 1990)"),
    ("published: 2026-06-18", "published=2026-06-18: future date on a backward-looking field"),
    ("made_on: 2027", "made_on=2027: future year on a backward-looking field"),
    ("resolves_by: 2042-01-01", "resolves_by=2042-01-01: implausibly far-future (year 2042)"),
    ('published: "2026-07-01"', "published=2026-07-01: future date on a backward-looking field"),
])
def test_implausible_values_are_reported(frontmatter, expected):
    assert _mod().classify_implausible(frontmatter, TODAY) == [expected]


def test_a_trailing_newline_never_turns_a_full_date_into_a_bare_year():
    """okengine#991: `_FULLDATE` anchors with \\Z, so a newline-suffixed value is not a full date."""
    m = _mod()
    assert m._FULLDATE.match("2026-06-18\n") is None
    assert m._FULLDATE.match("2026-06-18")


def test_issues_are_reported_in_field_order():
    fm = "updated: 2030-01-01\npublished: 1900-01-01"
    assert _mod().classify_implausible(fm, TODAY) == [
        "published=1900-01-01: implausible year 1900 (before 1990)",
        "updated=2030-01-01: future date on a backward-looking field",
    ]


def test_audit_files_skips_underscore_pages_and_pages_without_frontmatter(tmp_path):
    m = _mod()
    bad = _page(tmp_path / "bad.md", "published: 1970-01-01")
    hidden = _page(tmp_path / "_index.md", "published: 1970-01-01")
    plain = tmp_path / "plain.md"
    plain.write_text("no frontmatter\n", encoding="utf-8")
    missing = tmp_path / "missing.md"
    assert m.audit_files([bad, hidden, plain, missing], TODAY) == [
        ("bad.md", "published=1970-01-01: implausible year 1970 (before 1990)")]


def test_audit_vault_scans_only_the_declared_namespaces(tmp_path, monkeypatch):
    m = _mod(monkeypatch, tmp_path)
    wiki = tmp_path / "wiki"
    _page(wiki / "sources" / "a" / "bad.md", "published: 1970-01-01")
    _page(wiki / "sources" / "_skip.md", "published: 1970-01-01")
    _page(wiki / "sources" / "x.bak.1" / "old.md", "published: 1970-01-01")
    _page(wiki / "concepts" / "fine.md", "published: 2020-01-01")
    _page(wiki / "other" / "ignored.md", "published: 1970-01-01")
    assert m.audit_vault(TODAY) == [
        ("wiki/sources/a/bad.md", "published=1970-01-01: implausible year 1970 (before 1990)")]


def test_check_mode_fails_on_a_violation_and_passes_when_clean(tmp_path, capsys):
    m = _mod()
    bad = _page(tmp_path / "bad.md", "published: 1970-01-01")
    good = _page(tmp_path / "good.md", "published: 2020-01-01")
    assert m.main(["--check", "--paths", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "1 implausible date(s)" in err and "bad.md: published=1970-01-01" in err
    assert m.main(["--check", "--paths", str(good)]) == 0
    assert "content dates plausible" in capsys.readouterr().err
    assert m.main(["--check"]) == 0


def test_report_mode_prints_a_table_or_a_clean_line(tmp_path, monkeypatch, capsys):
    m = _mod(monkeypatch, tmp_path)
    assert m.main([]) == 0
    assert "No implausible date-field years" in capsys.readouterr().out
    _page(tmp_path / "wiki" / "entities" / "e.md", "published: 1970-01-01")
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "1 implausible date field(s)" in out
    assert "| `wiki/entities/e.md` | published=1970-01-01: implausible year 1970" in out
