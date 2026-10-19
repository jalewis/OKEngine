"""okengine#997: `is_reserved_basename` is THE reserved-name predicate — the validator's
conformance exemption, the write path's refusal, okf_migrate's minting guard and the
reserved-basenames deployment check all call it."""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tools import schema_validator as sv  # noqa: E402


@pytest.mark.parametrize("name", [
    "index.md", "INDEX.md", "Index.md", "log.md", "AGENTS.md", "README.md", "hot.md", "HEALTH.md",
    "bundle.md", ".net-x.md", ".md", "_template.md", "_review-queue.md", "INDEX-glossary.md",
    "INDEX-p02.md", "index-p02.md", "Index-P7.md", "index-p0.md",
    # every `index-*`, any case: the reader/backlinks/lint/critic skip them (QA round 2)
    "index-funds.md", "index-pricing.md", "index-p.md", "Index-glossary.md", "index-page.md",
    "index-sickness-pang-principle.md",
])
def test_reserved(name):
    assert sv.is_reserved_basename(name), name


@pytest.mark.parametrize("name", [
    "healthcare.md", "indexing.md", "reindex-funds.md", "xindex-p02.md", "a_b.md",
    "a.health.md", "dotnet-x.md", "health-publisher.md", "page-index.md", "concept-index.md",
    "logistics.md", "index.mdx.md",
])
def test_not_reserved(name):
    assert not sv.is_reserved_basename(name), name


def test_pack_reserved_files_are_case_insensitive_and_additive():
    assert sv.is_reserved_basename("Overview.md", ["overview.MD"])
    assert sv.is_reserved_basename("health.md", ["overview.md"])
    assert not sv.is_reserved_basename("overview.md")
    assert not sv.is_reserved_basename("overview.md", ())


def test_engine_generated_is_the_index_tree_and_scaffolding_only():
    for name in ("INDEX.md", "INDEX-x.md", "index-p3.md", "INDEX-P03.md", "_x.md"):
        assert sv.is_engine_generated(name), name
    for name in ("index.md", ".x.md", "Index-x.md", "index-pricing.md", "health.md"):
        assert not sv.is_engine_generated(name), name     # reserved, but not generated


def test_generated_structural_adds_only_dot_files():
    assert sv._is_generated_structural(".x.md") and sv._is_generated_structural("_x.md")
    assert not sv._is_generated_structural("index-pricing.md")


def test_validation_skips_exactly_the_reserved_names(tmp_path):
    (tmp_path / "schema.yaml").write_text("types:\n  concept: {}\nreserved_files: [Overview.md]\n")
    ns = tmp_path / "concepts"
    ns.mkdir()
    for name in ("index-p02.md", "index-pricing.md", "overview.md", "_t.md"):
        assert sv.schema_reject_reason(str(ns / name), "no frontmatter") is None, name
    assert sv.schema_reject_reason(str(ns / "pricing-index.md"), "no frontmatter") is not None
