import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "repair_daily_brief_freshness.py"
spec = importlib.util.spec_from_file_location("repair_daily_brief_freshness", MOD)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def _page(path: Path, frontmatter: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\nBody\n")


def test_repairs_poisoned_source_from_canonical_path(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "sources" / "2026" / "07" / "06" / "report.md"
    _page(page, "type: source\npublished: queued for review at 2026-07-06+00:00\n")
    repaired, changes = m.repair_page(page, wiki, "2026-07-28")
    assert "published: '2026-07-06'" in repaired
    assert changes == ["published=2026-07-06"]


def test_repairs_day_zero_source_from_raw_path(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "sources" / "2026" / "07" / "00" / "report.md"
    _page(page, "type: source\npublished: 2026-07T18:25Z\n"
                "raw: raw/incidents/2026-07-06-report.md\n")
    repaired, _ = m.repair_page(page, wiki, "2026-07-28")
    assert "published: '2026-07-06'" in repaired


def test_repairs_or_removes_poisoned_lifecycle_values(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "concepts" / "a" / "bad.md"
    _page(page, "type: concept\ncreated: '{{now}}'\nlast_updated: '2026-07-23T12:00:00Z'\n"
                "updated: \"z-no-date\"\n")
    repaired, changes = m.repair_page(page, wiki, "2026-07-28")
    assert "created: '2026-07-23'" in repaired
    assert "\nupdated:" not in repaired
    assert changes == ["created=2026-07-23", "removed invalid updated"]


def test_does_not_rewrite_valid_current_activity(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "sources" / "2026" / "07" / "29" / "valid.md"
    _page(page, "type: source\npublished: '2026-07-29T12:00:00Z'\n")
    original = page.read_text()
    repaired, changes = m.repair_page(page, wiki, "2026-07-28")
    assert repaired == original
    assert changes == []


def test_date_parsing_and_recovery_fallbacks(tmp_path):
    assert m.parse_date(datetime(2026, 7, 1, tzinfo=timezone.utc)) == date(2026, 7, 1)
    assert m.parse_date(date(2026, 7, 2)) == date(2026, 7, 2)
    assert m.parse_date("2026-07-03") == date(2026, 7, 3)
    assert m.parse_date("2026-07-03T10:00:00Z") == date(2026, 7, 3)
    assert m.parse_date(3) is None
    assert m.parse_date("") is None
    assert m.parse_date("not-a-date") is None
    assert m._date_in("bad 2026-99-99 then 2026_0704") == date(2026, 7, 4)
    assert m._date_in("none") is None

    wiki = tmp_path / "wiki"
    lifecycle = wiki / "sources" / "report.md"
    _page(lifecycle, "type: source\ncreated: 2026-06-02\n")
    assert m.source_date(lifecycle, wiki, {"created": "2026-06-02"}) == date(2026, 6, 2)

    raw_list = wiki / "sources" / "raw-list.md"
    _page(raw_list, "type: source\n")
    assert m.source_date(
        raw_list, wiki, {"raw": ["prefix", "raw/2026/06/03/item.md"]}
    ) == date(2026, 6, 3)

    future = wiki / "sources" / "2099" / "01" / "02" / "future.md"
    _page(future, "type: source\n")
    assert m.source_date(future, wiki, {}) == date(2099, 1, 2)

    past_path = wiki / "sources" / "2026" / "06" / "05" / "past.md"
    _page(past_path, "type: source\n")
    assert m.source_date(past_path, wiki, {}) == date(2026, 6, 5)

    unstructured = wiki / "sources" / "no-date.md"
    _page(unstructured, "type: source\n")
    stamp = datetime(2026, 6, 4, tzinfo=timezone.utc).timestamp()
    unstructured.touch()
    import os
    os.utime(unstructured, (stamp, stamp))
    assert m.source_date(unstructured, wiki, {}) == date(2026, 6, 4)


def test_non_mapping_frontmatter_and_main_dry_run_apply(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    scalar = wiki / "concepts" / "scalar.md"
    _page(scalar, "- item\n")
    original = scalar.read_text()
    assert m.repair_page(scalar, wiki, "2026-07-28") == (original, [])

    page = wiki / "entities" / "bad.md"
    _page(page, "type: entity\ncreated: z-invalid\n")
    monkeypatch.setattr(
        sys,
        "argv",
        ["repair_daily_brief_freshness", str(tmp_path), "--since", "2026-07-28"],
    )
    assert m.main() == 0
    assert "would repair 1 pages" in capsys.readouterr().out
    assert "z-invalid" in page.read_text()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "repair_daily_brief_freshness",
            str(tmp_path),
            "--since",
            "2026-07-28",
            "--apply",
        ],
    )
    assert m.main() == 0
    assert "repaired 1 pages" in capsys.readouterr().out
    assert "z-invalid" not in page.read_text()
