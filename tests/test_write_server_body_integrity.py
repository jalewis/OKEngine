"""Body-integrity guard on the write path (okengine#462, tranche T1).

Two structural defects the write path refuses to let an agent introduce:

  `## ##`  a malformed heading (an agent passing an already-prefixed section name)
  `## Referenced by` and friends -- headings the READ MCP generates from the live
           backlink graph. Authoring one into a canonical page freezes derived
           state into prose and makes readers see the panel twice.

The guard is deliberately *differential*: it compares the proposed body against
the previous one so legacy pages carrying these defects stay editable, and only a
NEWLY introduced one (including a second copy) is rejected. Both directions are
asserted, since a guard that refuses to let a bad page be repaired is as harmful
as one that lets a bad page in.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf:\n  required: [type]\nstrict_types: false\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module


DERIVED = "## Referenced by\n\n- [[a/b]]\n"


# ── _body_integrity_counts ────────────────────────────────────────────────────

def test_counts_malformed_headings_and_derived_panels(ws):
    counts = ws._body_integrity_counts
    bad, panels = counts("## ## Section\n\ntext\n")
    assert bad == 1 and not panels

    bad, panels = counts(DERIVED)
    assert bad == 0 and panels["referenced by"] == 1


def test_counts_are_case_insensitive_and_cumulative(ws):
    bad, panels = ws._body_integrity_counts("## Referenced By\n\n## REFERENCED BY\n")
    assert panels["referenced by"] == 2, "a second copy is also an introduction"


def test_empty_and_none_bodies_count_as_clean(ws):
    for empty in ("", None):
        bad, panels = ws._body_integrity_counts(empty)
        assert bad == 0 and not panels


def test_defects_inside_fenced_code_blocks_are_ignored(ws):
    """A fenced block is an example, not structure -- documentation must be able to
    SHOW a derived heading without tripping the guard."""
    counts = ws._body_integrity_counts
    for fence in ("```", "~~~", "````"):
        body = f"{fence}\n## Referenced by\n## ## malformed\n{fence}\n"
        bad, panels = counts(body)
        assert bad == 0 and not panels, fence


def test_a_longer_closing_fence_closes_and_a_shorter_one_does_not(ws):
    counts = ws._body_integrity_counts
    # closed by a longer run of the same marker -> the heading after it counts
    bad, panels = counts("```\ncode\n`````\n## Referenced by\n")
    assert panels["referenced by"] == 1

    # a different marker does not close the fence -> still inside, ignored
    bad, panels = counts("```\ncode\n~~~\n## Referenced by\n")
    assert not panels


def test_only_h2_derived_headings_are_counted(ws):
    _, panels = ws._body_integrity_counts("### Referenced by\n# Referenced by\n")
    assert not panels, "the derived panels are H2; other levels are ordinary prose"


# ── _body_integrity_reject: differential behaviour ────────────────────────────

def test_newly_introduced_malformed_heading_is_rejected(ws):
    reason = ws._body_integrity_reject("# Page\n", "# Page\n\n## ## Notes\n")
    assert reason and "malformed" in reason and "plain section name" in reason


def test_newly_introduced_derived_panel_is_rejected(ws):
    reason = ws._body_integrity_reject("# Page\n", "# Page\n\n" + DERIVED)
    assert reason and "reader-derived panel heading(s)" in reason
    assert "referenced by" in reason
    assert "computed and must not be authored" in reason


def test_a_legacy_defect_can_still_be_edited(ws):
    """Carrying an existing defect through an unrelated edit must NOT be blocked --
    otherwise a page with a legacy panel could never be repaired."""
    legacy = "# Page\n\n" + DERIVED
    assert ws._body_integrity_reject(legacy, legacy + "\nMore prose.\n") is None


def test_removing_a_legacy_defect_is_allowed(ws):
    legacy = "# Page\n\n" + DERIVED
    assert ws._body_integrity_reject(legacy, "# Page\n\nClean now.\n") is None


def test_adding_a_second_copy_of_an_existing_panel_is_rejected(ws):
    legacy = "# Page\n\n" + DERIVED
    reason = ws._body_integrity_reject(legacy, legacy + DERIVED)
    assert reason and "referenced by" in reason


def test_clean_bodies_pass(ws):
    assert ws._body_integrity_reject("# Page\n", "# Page\n\n## Analysis\n\ntext\n") is None
