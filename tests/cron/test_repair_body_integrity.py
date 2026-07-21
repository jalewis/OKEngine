import importlib.util
from pathlib import Path

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
