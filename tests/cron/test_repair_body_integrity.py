import importlib.util
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "repair_body_integrity.py"


def _load():
    spec = importlib.util.spec_from_file_location("repair_body_integrity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: entity\nname: Acme\n---\n" + body)


def test_repairs_heading_merges_sections_and_dedupes_source_date():
    module = _load()
    body = (
        "# Acme\n\n"
        "## ## Recent activity\n\n"
        "- 2026-07-15 — [[sources/a]] first wording\n"
        "- 2026-07-15 — [[sources/b]] distinct\n\n"
        "## Recent activity\n\n"
        "- 2026-07-15 — [[sources/a]] repeated with different wording\n"
        "- 2026-07-16 — [[sources/a]] next-day update\n\n"
        "## Notes\n\nKeep this.\n"
    )

    repaired, stats = module.repair_body(body)

    assert repaired.count("## Recent activity") == 1
    assert "## ##" not in repaired
    assert repaired.count("[[sources/a]]") == 2
    assert "[[sources/b]]" in repaired
    assert "## Notes\n\nKeep this." in repaired
    assert stats == {
        "malformed_headings": 1,
        "duplicate_sections": 1,
        "duplicate_entries": 1,
    }


def test_dry_run_is_non_mutating_and_apply_is_bounded(tmp_path):
    module = _load()
    for name in ("a", "b"):
        _page(
            tmp_path / "wiki" / "entities" / f"{name}.md",
            f"# {name}\n\n## ## Notes\n\n- item\n",
        )
    first = tmp_path / "wiki" / "entities" / "a.md"
    before = first.read_text()

    assert module.main(["--vault", str(tmp_path), "--limit", "1"]) == 0
    assert first.read_text() == before

    assert module.main(["--vault", str(tmp_path), "--limit", "1", "--apply"]) == 0
    assert "## ##" not in first.read_text()
    assert "## ##" in (tmp_path / "wiki" / "entities" / "b.md").read_text()


def test_frontmatter_is_preserved_verbatim(tmp_path):
    module = _load()
    path = tmp_path / "wiki" / "entities" / "a.md"
    path.parent.mkdir(parents=True)
    prefix = "---\ntype: entity\naliases: ['A', 'B'] # keep formatting\n---\n"
    path.write_text(prefix + "# A\n\n## ## Notes\n\nText.\n")

    assert module.main(["--vault", str(tmp_path), "--apply"]) == 0

    assert path.read_text().startswith(prefix)


def test_fenced_headings_and_bullets_are_preserved_verbatim():
    module = _load()
    body = (
        "# Example\n\n"
        "```markdown\n"
        "## ## Recent activity\n"
        "- 2026-07-15 — [[sources/a]] repeated\n"
        "- 2026-07-15 — [[sources/a]] repeated\n"
        "```\n\n"
        "## Notes\n\nKeep.\n"
    )

    repaired, stats = module.repair_body(body)

    assert repaired == body
    assert stats == {
        "malformed_headings": 0,
        "duplicate_sections": 0,
        "duplicate_entries": 0,
    }


def test_bullet_blocks_prose_and_tilde_fences_cover_boundary_paths():
    module = _load()
    lines = [
        "- same item",
        "  continuation",
        "- SAME   ITEM",
        "",
        "Prose after list",
        "~~~text",
        "- repeated-looking fenced item",
        "~~~",
        "* final",
    ]
    result = module._dedupe_bullets(lines)
    assert result.count("- same item") == 1
    assert "Prose after list" in result
    assert "- repeated-looking fenced item" in result
    assert module._bullet_count(result) == 3


def test_heading_cleanup_preamble_spacing_and_duplicate_merge():
    module = _load()
    assert module._clean_heading("###   Name ") == "Name"
    repaired, stats = module.repair_body(
        "\nPreamble\n\n## One\ntext\n\n## one\nmore\n\n## Two\n- x\n"
    )
    assert repaired.startswith("\nPreamble\n\n## One")
    assert "text\n\nmore" in repaired
    assert stats["duplicate_sections"] == 1


def test_repair_page_rejects_unreadable_missing_frontmatter_and_unchanged(tmp_path, monkeypatch):
    module = _load()
    no_fm = tmp_path / "no.md";no_fm.write_text("# no frontmatter\n")
    assert module.repair_page(no_fm) == (None, {})
    clean = tmp_path / "clean.md";_page(clean, "# Clean\n\n## Notes\ntext\n")
    repaired, stats = module.repair_page(clean)
    assert repaired is None and stats == {
        "malformed_headings": 0, "duplicate_sections": 0, "duplicate_entries": 0
    }
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError()) if self == clean else original(self, *a, **k))
    assert module.repair_page(clean) == (None, {})


def test_candidates_missing_wiki_and_skipped_namespaces(tmp_path):
    module = _load()
    assert module.candidates(tmp_path) == []
    keep = tmp_path/"wiki/entities/keep.md";_page(keep, "## ## Notes\n")
    skip = tmp_path/"wiki/dashboards/skip.md";_page(skip, "## ## Notes\n")
    assert module.candidates(tmp_path) == [keep]


def test_main_skips_clean_page_and_reports_zero(tmp_path, capsys):
    module = _load()
    _page(tmp_path/"wiki/entities/clean.md", "# Clean\n\n## Notes\ntext\n")
    assert module.main(["--vault", str(tmp_path)]) == 0
    assert "would repair 0 page(s)" in capsys.readouterr().out


def test_unterminated_and_mismatched_fences_and_compact_duplicate_sections():
    module = _load()
    assert module._dedupe_bullets(["```", "- fenced"]) == ["```", "- fenced"]
    assert module._bullet_count(["```", "- fenced"]) == 0
    repaired, stats = module.repair_body(
        "## One\ntext\n## one\nmore\n## Two\n```\n~~~\n## not-a-heading\n"
    )
    assert "text\n\nmore" in repaired
    assert "## not-a-heading" in repaired
    assert stats["duplicate_sections"] == 1


def test_limit_must_be_positive():
    module = _load()
    with pytest.raises(SystemExit):
        module.main(["--limit", "0"])
