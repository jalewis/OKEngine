"""Regression: supply must count DECLARED relations, not only prose wikilinks.

Supply is the numerator of "thin supply", so undercounting it manufactures whitespace. The lane
surfaced `ai-security` as an empty market and the model rejected it from its own knowledge of the
vendors — "supply=2 is a counting artifact". A selector that hands the model false candidates
spends model time on rejection and teaches nothing.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SEL = REPO / "extensions" / "okengine.frontier-watch" / "select_whitespace.py"
pytestmark = pytest.mark.skipif(not SEL.is_file(), reason="selector absent")
yaml = pytest.importorskip("yaml")


def _load(monkeypatch, wiki: Path):
    sys.path.insert(0, str(SEL.parent))
    spec = importlib.util.spec_from_file_location("select_whitespace", SEL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "WIKI", wiki)
    return mod


def _entity(wiki: Path, name: str, front: str, body: str = "Body.") -> None:
    p = wiki / "entities" / "a" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: vendor\n{front}---\n\n{body}\n", encoding="utf-8")


def test_a_frontmatter_relation_counts_as_supply(monkeypatch, tmp_path):
    """Vendors are associated with a market in FRONTMATTER; most vendor pages have no prose at all."""
    wiki = tmp_path / "wiki"
    _entity(wiki, "vendor-a", "segment: entities/a/i/ai-security\n")
    mod = _load(monkeypatch, wiki)
    _demand, supply = mod._demand_supply()
    assert len(supply.get("ai-security", ())) == 1


def test_a_prose_wikilink_still_counts(monkeypatch, tmp_path):
    """The original signal must not be lost while adding the new one."""
    wiki = tmp_path / "wiki"
    _entity(wiki, "vendor-b", "", body="Operates in [[concepts/ai-security]].")
    mod = _load(monkeypatch, wiki)
    _demand, supply = mod._demand_supply()
    assert len(supply.get("ai-security", ())) == 1


def test_identifier_and_provenance_fields_are_not_relations(monkeypatch, tmp_path):
    """`id` holds the page's OWN slug and `sources` cites where a claim came from. Counting either
    inflates supply with pages that merely mention a name — the mirror error of undercounting."""
    wiki = tmp_path / "wiki"
    _entity(wiki, "ai-security", "id: entities/a/i/ai-security\n"
                                 "sources:\n- entities/a/i/ai-security\n")
    mod = _load(monkeypatch, wiki)
    _demand, supply = mod._demand_supply()
    assert supply.get("ai-security", set()) == set()


def test_source_pages_never_count_toward_supply(monkeypatch, tmp_path):
    """Sources are DEMAND. Letting a source page count as supply would net the metric to zero."""
    wiki = tmp_path / "wiki"
    p = wiki / "sources" / "2026" / "s.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: source\nsegment: entities/a/i/ai-security\n---\n\n"
                 "About [[concepts/ai-security]].\n", encoding="utf-8")
    mod = _load(monkeypatch, wiki)
    demand, supply = mod._demand_supply()
    assert len(demand.get("ai-security", ())) == 1
    assert supply.get("ai-security", set()) == set()


def test_a_page_whose_frontmatter_cannot_be_read_declares_no_relations(monkeypatch, tmp_path):
    """Supply is the numerator of "thin supply", so a page that yields no relations must yield
    exactly that — not an exception that stops the scan partway and leaves every market after it
    undercounted, which is how a real market gets surfaced as whitespace."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    mod = _load(monkeypatch, wiki)
    assert mod._declared_slugs("segment: [unclosed\n") == set()
    assert mod._declared_slugs("- a\n- b\n") == set(), "a sequence declares no fields"
    assert mod._declared_slugs("") == set()


def test_an_unparsable_vendor_page_does_not_hide_its_neighbours_supply(monkeypatch, tmp_path):
    """The scan is one pass over the vendors. One bad page must cost only that page's contribution."""
    wiki = tmp_path / "wiki"
    _entity(wiki, "vendor-broken", "segment: [unclosed\n")
    _entity(wiki, "vendor-good", "segment: entities/a/i/ai-security\n")
    mod = _load(monkeypatch, wiki)
    _demand, supply = mod._demand_supply()
    assert len(supply.get("ai-security", ())) == 1, supply
