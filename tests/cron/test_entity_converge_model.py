"""Entity-converge clustering and merge policy (okengine#462, tranche T1).

`scripts/cron/entity_converge.py` decides which entity pages are the SAME entity
and which survives the merge. Both halves are consequential and irreversible in
practice: a false merge collapses two genuinely different actors into one page,
while a missed merge leaves the duplicate canonicals that the #54 partition-dup
class is made of.

The clustering rule is deliberately conservative -- only *all-pairs-strong*
components merge, so a "bridge"-shaped component (A~B, B~C, but A and C unrelated)
stays unresolved rather than chaining unrelated entities together. That property is
the main thing these tests pin, because it is the one a naive refactor would break.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"
sys.path.insert(0, str(CRON))
spec = importlib.util.spec_from_file_location("entity_converge_model", CRON / "entity_converge.py")
ec = importlib.util.module_from_spec(spec)
sys.modules["entity_converge_model"] = ec
spec.loader.exec_module(ec)


# ── _values: the identity keys a page contributes ────────────────────────────

def test_values_uses_name_then_title_then_nothing():
    primary, keys = ec._values({"name": "Acme Corp"})
    assert primary and primary in keys

    primary_t, _ = ec._values({"title": "Acme Corp"})
    assert primary_t == primary, "title is the fallback for name"

    assert ec._values({})[0] == ""


def test_values_normalises_and_includes_aliases():
    _, keys = ec._values({"name": "ACME  Corp", "aliases": ["Acme-Inc", "acme inc"]})
    assert len(keys) >= 2


def test_values_tolerates_a_scalar_alias_value():
    _, keys = ec._values({"name": "Acme", "aliases": "Acme Inc"})
    assert keys, "a scalar aliases value must not crash identity extraction"


def test_short_keys_are_dropped_as_too_weak_to_match_on():
    """A two-character alias would match half the corpus; the minimum length is what
    stops an accidental mass-merge."""
    _, keys = ec._values({"name": "AB"})
    assert all(len(k) >= ec._MIN_KEY for k in keys)


# ── _strong: the pairwise match rule ─────────────────────────────────────────

def test_identical_primary_names_are_a_strong_match():
    assert ec._strong({"name": "Acme Corp"}, {"name": "acme  corp"})


def test_two_shared_aliases_are_a_strong_match():
    a = {"name": "Alpha Group", "aliases": ["Shared One", "Shared Two"]}
    b = {"name": "Beta Group", "aliases": ["Shared One", "Shared Two"]}
    assert ec._strong(a, b)


def test_a_single_shared_alias_is_not_enough():
    """One coincidental overlap must not merge two entities."""
    a = {"name": "Alpha Group", "aliases": ["Shared One"]}
    b = {"name": "Beta Group", "aliases": ["Shared One"]}
    assert not ec._strong(a, b)


def test_unrelated_pages_do_not_match():
    assert not ec._strong({"name": "Alpha Group"}, {"name": "Beta Group"})


# ── clusters: all-pairs-strong only ──────────────────────────────────────────

def test_two_pages_with_the_same_name_cluster():
    records = {"entities/a/acme.md": {"name": "Acme Corp"},
               "entities/a/acme-corp.md": {"name": "Acme  Corp"}}
    assert ec.clusters(records) == [sorted(records)]


def test_unrelated_pages_do_not_cluster():
    records = {"entities/a/alpha.md": {"name": "Alpha Group"},
               "entities/b/beta.md": {"name": "Beta Group"}}
    assert ec.clusters(records) == []


def test_a_bridge_shaped_component_stays_unresolved():
    """A~B and B~C but NOT A~C: merging would chain two unrelated entities through a
    shared middle page, so the whole component is left alone for review."""
    records = {
        "entities/a/a.md": {"name": "Shared Name"},
        "entities/b/b.md": {"name": "Shared Name", "aliases": ["Other Handle Xy", "Second Alias Q"]},
        "entities/c/c.md": {"aliases": ["Other Handle Xy", "Second Alias Q"]},
    }
    assert ec.clusters(records) == [], "bridge-shaped components must not merge"


def test_an_all_pairs_strong_triple_clusters():
    records = {f"entities/a/{n}.md": {"name": "Same Entity Name"} for n in ("x", "y", "z")}
    assert ec.clusters(records) == [sorted(records)]


def test_empty_input_clusters_to_nothing():
    assert ec.clusters({}) == []


# ── _grounded / choose_winner: which page survives ───────────────────────────

def test_grounded_counts_only_canonical_source_page_refs():
    assert ec._grounded({"sources": ["sources/2026/a", "sources/2026/b"]}) == 2
    assert ec._grounded({"sources": ["https://example.com/x"]}) == 0, "a bare URL is not a page"
    assert ec._grounded({"sources": ["MITRE ATT&CK"]}) == 0, "a provenance label is not a page"
    assert ec._grounded({}) == 0
    assert ec._grounded({"sources": "sources/2026/a"}) == 1, \
        "a scalar sources value is promoted, matching the other scalar-tolerant helpers"


def test_winner_prefers_a_valid_type_then_grounding(tmp_path):
    (tmp_path / "schema.yaml").write_text("types:\n  actor: {required: [type]}\n", encoding="utf-8")
    records = {
        "entities/a/typed.md": {"type": "actor", "sources": []},
        "entities/a/untyped.md": {"type": "not-a-type", "sources": ["sources/2026/a"]},
    }
    assert ec.choose_winner(tmp_path, sorted(records), records) == "entities/a/typed.md"


def test_winner_prefers_more_grounding_when_types_tie(tmp_path):
    (tmp_path / "schema.yaml").write_text("types:\n  actor: {required: [type]}\n", encoding="utf-8")
    records = {
        "entities/a/thin.md": {"type": "actor", "sources": []},
        "entities/a/grounded.md": {"type": "actor",
                                   "sources": ["sources/2026/a", "sources/2026/b"]},
    }
    assert ec.choose_winner(tmp_path, sorted(records), records) == "entities/a/grounded.md"


def test_winner_deprioritises_a_page_still_flagged_for_review(tmp_path):
    (tmp_path / "schema.yaml").write_text("types:\n  actor: {required: [type]}\n", encoding="utf-8")
    records = {
        "entities/a/clean.md": {"type": "actor"},
        "entities/a/flagged.md": {"type": "actor", "needs_review": True},
    }
    assert ec.choose_winner(tmp_path, sorted(records), records) == "entities/a/clean.md"


def test_winner_counts_scalar_alias_as_one_not_string_length(tmp_path):
    """A legacy scalar alias cannot beat a richer list merely because its string is longer."""
    (tmp_path / "schema.yaml").write_text("types:\n  actor: {required: [type]}\n", encoding="utf-8")
    records = {
        "entities/a/rich.md": {"type": "actor", "aliases": ["One", "Two", "Three"]},
        "entities/a/scalar.md": {"type": "actor", "aliases": "LongScalarAlias"},
    }
    assert ec.choose_winner(tmp_path, sorted(records), records) == "entities/a/rich.md"


# ── _union: additive field merge ─────────────────────────────────────────────

def test_union_merges_additive_fields_without_duplicates():
    winner = {"name": "Acme", "aliases": ["Acme Corp"]}
    losers = [{"aliases": ["ACME CORP", "Acme Inc"]}]
    merged = ec._union(winner, losers, ["entities/a/loser.md"])
    lowered = [str(a).casefold() for a in merged["aliases"]]
    assert len(lowered) == len(set(lowered)), "case-insensitive dedup"
    assert "acme inc" in lowered, "a loser's unique alias is carried over"


def test_union_preserves_the_winners_scalar_fields():
    winner = {"name": "Acme", "type": "actor", "status": "active"}
    merged = ec._union(winner, [{"name": "Other", "status": "draft"}], ["entities/a/l.md"])
    assert merged["name"] == "Acme" and merged["status"] == "active"


def test_union_tolerates_scalar_values_in_additive_fields():
    merged = ec._union({"aliases": "One"}, [{"aliases": "Two"}], ["entities/a/l.md"])
    lowered = [str(a).casefold() for a in merged["aliases"]]
    assert "one" in lowered and "two" in lowered
