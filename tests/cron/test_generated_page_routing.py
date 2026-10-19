"""okengine#933 / #920 detector: engine-written generated pages route through `generated_page`
and declare the base-schema generated-view type.

A module that builds a page's frontmatter literal (`---` then `type: ...`) under `scripts/cron/`
or a first-party extension is a generated-page writer. Each must write through
`generated_page.write` / `generated_page.with_id`, or it drops the page id on every rewrite
(#933), and each literal type must be the base-schema `dashboard` type, or the report counts as
schema drift and can be aliased into a knowledge type (#920). Exemptions carry their reason.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BASE_TYPES = set(yaml.safe_load((REPO / "config" / "base-schema.yaml").read_text())["types"])
GENERATED_TYPE = "dashboard"

# A frontmatter literal that opens a page and names its type, in the shapes the writers use.
_FRONTMATTER_TYPE = re.compile(
    r'"---",\s*"type:\s*([A-Za-z_-]+)'          # ["---", "type: dashboard", ...]
    r'|---\\ntype:\s*([A-Za-z_-]+)'              # "---\ntype: dashboard\n..."
    r'|^\s*(?:L|lines)\.append\("type:\s*([A-Za-z_-]+)"\)'
    r'|^\s*"type:\s*([A-Za-z_-]+)(?:\\n)?",?\s*$',
    re.MULTILINE)
_ROUTED = re.compile(r"\bgenerated_page\.(?:write|with_id)\(")

#: Modules that build page frontmatter but are not generated-page writers the rule governs.
EXEMPT = {
    "scripts/cron/build_index_tree.py": "writes only INDEX.md, INDEX-pNN.md, BUNDLE.md, HEALTH.md "
                                        "(structural, id-exempt in generated_page.is_exempt)",
    "scripts/cron/build_hot_set.py": "writes only HOT.md (reserved, id-exempt)",
    "scripts/cron/feed_fetch.py": "lands raw feed captures under raw/, not generated wiki pages",
}


def literal_types(source: str) -> set[str]:
    return {next(group for group in match.groups() if group)
            for match in _FRONTMATTER_TYPE.finditer(source)}


def violations(rel: str, source: str) -> list[str]:
    """Why ``source`` (module ``rel``) breaks the generated-page contract; empty when it holds."""
    types = literal_types(source)
    if not types or rel in EXEMPT:
        return []
    found = []
    if not _ROUTED.search(source):
        found.append(f"{rel}: builds page frontmatter but never writes through generated_page "
                     "(the page loses its id on every rewrite, okengine#933)")
    for bad in sorted(types - {GENERATED_TYPE}):
        found.append(f"{rel}: generated page declares type {bad!r}; use the base-schema "
                     f"{GENERATED_TYPE!r} type (okengine#920)")
    return found


def _engine_modules() -> list[Path]:
    return sorted([*(REPO / "scripts" / "cron").glob("*.py"),
                   *(REPO / "extensions").glob("*/*.py")])


def test_every_engine_generated_page_writer_is_routed_and_typed():
    problems = [problem for path in _engine_modules()
                for problem in violations(path.relative_to(REPO).as_posix(),
                                          path.read_text(encoding="utf-8"))]
    assert not problems, "\n".join(problems)


def test_the_detector_actually_finds_the_writers():
    writers = [path for path in _engine_modules()
               if literal_types(path.read_text(encoding="utf-8"))]
    assert len(writers) >= 40, f"only {len(writers)} writers detected; the pattern has gone blind"


@pytest.mark.parametrize("rel", sorted(EXEMPT))
def test_exemptions_are_still_needed(rel):
    assert literal_types((REPO / rel).read_text(encoding="utf-8")), f"{rel} no longer needs an exemption"


def test_generated_type_is_a_base_schema_type():
    assert GENERATED_TYPE in BASE_TYPES


# ── negative fixtures: the pre-fix shapes are rejected ─────────────────────────────────────

LEGACY_BACKLINK_DRAIN = '''
    lines = [
        "---",
        "type: operational",
        "generated_by: source_backlink_drain.py",
        "---",
    ]
    out.write_text("\\n".join(lines), encoding="utf-8")
'''

LEGACY_BARE_NAME = '''
            (OUT_DIR / "bare-name-link-normalize.md").write_text(
                f"---\\ntype: report\\ntitle: Bare-name link normalization\\n---\\n\\n", encoding="utf-8")
'''

LEGACY_DASHBOARD = '''
    L = ["---", "type: dashboard", 'title: "Fleet health"', "---", ""]
    out.write_text("\\n".join(L), encoding="utf-8")
'''


def test_legacy_backlink_drain_is_rejected_for_both_defects():
    found = violations("scripts/cron/source_backlink_drain.py", LEGACY_BACKLINK_DRAIN)
    assert any("okengine#933" in item for item in found)
    assert any("'operational'" in item and "okengine#920" in item for item in found)


def test_legacy_report_type_is_rejected():
    found = violations("scripts/cron/normalize_bare_name_links.py", LEGACY_BARE_NAME)
    assert any("'report'" in item for item in found)


def test_unrouted_dashboard_writer_is_rejected():
    found = violations("scripts/cron/fleet_health.py", LEGACY_DASHBOARD)
    assert found == ["scripts/cron/fleet_health.py: builds page frontmatter but never writes through "
                     "generated_page (the page loses its id on every rewrite, okengine#933)"]


def test_routed_dashboard_writer_passes():
    routed = LEGACY_DASHBOARD.replace('out.write_text("\\n".join(L), encoding="utf-8")',
                                      'generated_page.write(out, "\\n".join(L))')
    assert violations("scripts/cron/fleet_health.py", routed) == []


def test_modules_without_page_frontmatter_are_ignored():
    assert violations("scripts/cron/x.py", 'print("type: dashboard is a word")\n') == []
