"""okengine#546 — a declared confidence SCALE is self-documenting, so only the top of it flags.

`review_on_change_fields` flagged EVERY change to a listed field. Measured on okcti-test that
produced 264 queue rows for `attribution_confidence`, of which 233 were hedges or mid-scale values
and **120 were downgrades** — the flag fired when an agent became MORE cautious, which is backwards
for a guard whose purpose is catching a claim laundered upward.

The rule under test: for a field with a pack-DECLARED ORDERED ENUM, flag only when the value is set
to the TOP of that scale on a page that shows no evidence for it. Hedges, downgrades, lateral moves
and evidenced top-of-scale assertions do not flag. Fields with no declared ordering keep the old
flag-on-any-change behaviour.

The vocabulary is entirely pack-declared — the engine ships none of it, so these fixtures invent
their own scale rather than reusing a CTI one.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"

# A pack-declared ordered scale, most-certain first, plus an alias and an UNORDERED review field.
SCHEMA = (
    "types:\n"
    "  widget: {required: [type]}\n"
    "  source: {required: [type]}\n"
    "enums:\n"
    "  certainty: [locked, firm, tentative, guessed, unknown]\n"
    "field_enums:\n"
    "  certainty: {enum: certainty}\n"
    "value_aliases:\n"
    "  certainty: {solid: firm}\n"
    "review:\n"
    "  review_on_change_fields: [certainty, owner_note]\n"
    "partitioning:\n"
    "  namespaces: {widget: {strategy: flat}}\n"
)


def _load(wiki_path: Path, schema: str = SCHEMA):
    os.environ["WIKI_PATH"] = str(wiki_path)
    os.environ["OKENGINE_MCP_WRITE_DATE"] = "2026-08-04"
    os.environ["OKENGINE_BASE_SCHEMA"] = str(REPO / "config" / "base-schema.yaml")
    (wiki_path / "wiki").mkdir(parents=True, exist_ok=True)
    (wiki_path / "wiki" / "schema.yaml").write_text(schema)
    spec = importlib.util.spec_from_file_location("write_server", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m


def _flags(m, fm, prev=None):
    return m._review_flags(m._safe("widget/w.md"), fm, prev)


# --- the declared scale ----------------------------------------------------

def test_the_ordered_enum_is_read_from_the_pack_not_the_engine(tmp_path):
    """The engine ships no confidence vocabulary. A pack declares its own scale and its own field
    name, and both are resolved through the same field_enums -> enums indirection every other
    consumer uses."""
    m = _load(tmp_path)
    # the FULL governing schema — governing_policy returns only the review/permissions subset
    pol = m._governing(m._safe("widget/w.md"))
    assert m._ordered_enum(pol, "certainty") == ["locked", "firm", "tentative", "guessed", "unknown"]
    assert m._ordered_enum(pol, "owner_note") == []          # no declared ordering
    assert m._ordered_enum(pol, "absent_field") == []


def test_declared_value_aliases_are_applied(tmp_path):
    """okcti maps `medium` -> `moderate`; an alias spelling must not read as a different level."""
    m = _load(tmp_path)
    # the FULL governing schema — governing_policy returns only the review/permissions subset
    pol = m._governing(m._safe("widget/w.md"))
    assert m._enum_norm(pol, "certainty", "SOLID") == "firm"
    assert m._enum_norm(pol, "certainty", "Firm") == "firm"
    assert m._enum_norm(pol, "certainty", None) == ""


def test_write_normalization_composes_base_aliases_and_preserves_ambiguous_prose(tmp_path):
    m = _load(tmp_path)
    page = m._safe("widget/w.md")
    normalized, _ = m._normalize_drift(
        {"type": "widget", "estimative_probability": "Probable"}, page)
    assert normalized["estimative_probability"] == "likely"

    ambiguous, _ = m._normalize_drift(
        {"type": "widget", "estimative_probability": "possible"}, page)
    assert ambiguous["estimative_probability"] == "possible"


def test_an_inline_enum_list_also_resolves(tmp_path):
    m = _load(tmp_path, SCHEMA.replace("  certainty: {enum: certainty}\n",
                                       "  certainty: [a, b, c]\n"))
    # the FULL governing schema — governing_policy returns only the review/permissions subset
    pol = m._governing(m._safe("widget/w.md"))
    assert m._ordered_enum(pol, "certainty") == ["a", "b", "c"]


# --- what no longer flags ---------------------------------------------------

@pytest.mark.parametrize("value", ["firm", "tentative", "guessed", "unknown"])
def test_a_hedged_or_mid_scale_value_never_flags(tmp_path, value):
    """233 of okcti's 264 rows were exactly this: a value below the top of the scale, which is
    self-documenting — the reader sees the hedge."""
    m = _load(tmp_path)
    assert _flags(m, {"type": "widget", "certainty": value}) == []


def test_a_downgrade_never_flags(tmp_path):
    """120 of the 264 were DOWNGRADES. Flagging an agent for becoming more cautious is backwards
    for a guard that exists to catch a claim being laundered upward."""
    m = _load(tmp_path)
    assert _flags(m, {"type": "widget", "certainty": "unknown"},
                  {"type": "widget", "certainty": "locked"}) == []


def test_an_unchanged_value_never_flags(tmp_path):
    m = _load(tmp_path)
    assert _flags(m, {"type": "widget", "certainty": "locked"},
                  {"type": "widget", "certainty": "locked"}) == []


def test_the_top_of_the_scale_with_evidence_does_not_flag(tmp_path):
    """The assertion is exactly as strong as its citation. With one, there is nothing for a human
    to add that the evidence does not already say."""
    m = _load(tmp_path)
    assert _flags(m, {"type": "widget", "certainty": "locked",
                      "sources": ["sources/2026/08/a"]}) == []


def test_an_alias_for_a_mid_level_does_not_flag(tmp_path):
    m = _load(tmp_path)
    assert _flags(m, {"type": "widget", "certainty": "solid"}) == []


def test_a_source_page_at_the_top_does_not_flag(tmp_path):
    """A `source` page IS the primary document — it cites nothing by construction and its
    authority is its own publisher. Demanding a citation from it is the category error #549 fixed;
    measured, it was all 23 of the otherwise-remaining flags."""
    m = _load(tmp_path)
    assert m._review_flags(m._safe("sources/s.md"),
                           {"type": "source", "certainty": "locked", "publisher": "X"}) == []


# --- what still flags -------------------------------------------------------

def test_the_top_of_the_scale_without_evidence_flags(tmp_path):
    """The one case worth a human: a claim asserted at maximum certainty with nothing behind it."""
    m = _load(tmp_path)
    out = _flags(m, {"type": "widget", "certainty": "locked"})
    assert len(out) == 1
    assert "top of the declared scale" in out[0] and "no citation" in out[0]


def test_an_empty_sources_list_is_not_evidence(tmp_path):
    m = _load(tmp_path)
    assert _flags(m, {"type": "widget", "certainty": "locked", "sources": []}) != []
    assert _flags(m, {"type": "widget", "certainty": "locked", "sources": ["  "]}) != []


@pytest.mark.parametrize("cited", [{"sources": "sources/a"}, {"source": "sources/a"},
                                   {"source": ["sources/a"]}])
def test_either_citation_key_counts_as_evidence(tmp_path, cited):
    m = _load(tmp_path)
    assert _flags(m, {"type": "widget", "certainty": "locked", **cited}) == []


def test_an_unordered_review_field_still_flags_on_any_change(tmp_path):
    """Only a DECLARED ORDERED scale gets the new treatment. A field with no ordering has no
    notion of 'the top', so flag-on-any-change remains the only safe rule for it."""
    m = _load(tmp_path)
    out = _flags(m, {"type": "widget", "owner_note": "anything"})
    assert len(out) == 1 and "owner_note" in out[0]


def test_the_escalation_flag_survives_a_pack_with_no_declared_enum(tmp_path):
    """Same field name, no `enums:` block — the pack gets the conservative old behaviour rather
    than silently losing the guard."""
    m = _load(tmp_path, SCHEMA.replace("enums:\n  certainty: [locked, firm, tentative, guessed, unknown]\n", "")
                              .replace("field_enums:\n  certainty: {enum: certainty}\n", ""))
    out = _flags(m, {"type": "widget", "certainty": "guessed"})
    assert len(out) == 1 and "set/changed review field" in out[0]


# --- mutation hardening (okengine#552 diff-scoped gate) --------------------

def test_a_field_enum_naming_a_missing_vocabulary_yields_no_scale(tmp_path):
    """`isinstance(ref, list) and ref` — an `or` here would iterate a STRING, returning its
    characters as if they were scale levels. A typo'd enum name must degrade to 'no declared
    ordering', not to a scale of single letters."""
    m = _load(tmp_path, SCHEMA.replace("  certainty: {enum: certainty}\n",
                                       "  certainty: {enum: no_such_vocabulary}\n"))
    pol = m._governing(m._safe("widget/w.md"))
    assert m._ordered_enum(pol, "certainty") == []
    # and the field therefore keeps flag-on-any-change rather than silently gaining a scale
    out = _flags(m, {"type": "widget", "certainty": "firm"})
    assert len(out) == 1 and "set/changed review field" in out[0]


def test_an_empty_declared_enum_yields_no_scale(tmp_path):
    m = _load(tmp_path, SCHEMA.replace("  certainty: [locked, firm, tentative, guessed, unknown]\n",
                                       "  certainty: []\n"))
    pol = m._governing(m._safe("widget/w.md"))
    assert m._ordered_enum(pol, "certainty") == []


def test_an_unchanged_value_is_compared_by_equality_not_identity(tmp_path):
    """`fm[k] == prev[k]` must be equality: YAML-parsed strings are not the interned literal, so
    `is` would treat an untouched value as a change and re-flag every backfill."""
    m = _load(tmp_path)
    same = "".join(["loc", "ked"])          # equal to "locked" but a distinct object
    assert _flags(m, {"type": "widget", "certainty": same},
                  {"type": "widget", "certainty": "locked"}) == []


def test_the_source_exemption_is_equality_not_ordering(tmp_path):
    """`type == "source"` must be equality. `<=` would exempt every type sorting at or before
    'source' — including `actor`, the most claim-bearing type there is."""
    m = _load(tmp_path, SCHEMA + "types:\n  actor: {required: [type]}\n")
    out = m._review_flags(m._safe("entities/a.md"), {"type": "actor", "certainty": "locked"})
    assert len(out) == 1 and "top of the declared scale" in out[0]


def test_the_source_exemption_matches_a_parsed_type_string(tmp_path):
    """`is` would fail for a YAML-parsed 'source', re-flagging every source page."""
    m = _load(tmp_path)
    parsed = yaml.safe_load("type: source\n")["type"]
    assert m._review_flags(m._safe("sources/s.md"),
                           {"type": parsed, "certainty": "locked"}) == []


def test_each_skip_in_the_review_field_loop_is_a_skip_not_a_stop(tmp_path):
    """Every `continue` in the review_on_change_fields loop must SKIP that field, never end the
    loop — a break would silently stop evaluating the remaining configured fields."""
    m = _load(tmp_path, SCHEMA.replace(
        "  review_on_change_fields: [certainty, owner_note]\n",
        "  review_on_change_fields: [certainty, owner_note]\n  # order matters for the skip test\n"))
    # `certainty` hits a skip (mid-scale), `owner_note` must STILL be evaluated after it
    out = _flags(m, {"type": "widget", "certainty": "firm", "owner_note": "changed"})
    assert len(out) == 1 and "owner_note" in out[0]
    # same when certainty skips via the evidence branch
    out = _flags(m, {"type": "widget", "certainty": "locked",
                     "sources": ["sources/a"], "owner_note": "changed"})
    assert len(out) == 1 and "owner_note" in out[0]


def test_a_source_page_skip_still_evaluates_later_fields(tmp_path):
    m = _load(tmp_path)
    out = m._review_flags(m._safe("sources/s.md"),
                          {"type": "source", "certainty": "locked", "owner_note": "changed"})
    assert len(out) == 1 and "owner_note" in out[0]


def test_an_unset_review_field_skips_without_ending_the_loop(tmp_path):
    """The first `continue` (field absent or unchanged) must not stop later fields either."""
    m = _load(tmp_path)
    out = _flags(m, {"type": "widget", "owner_note": "changed"})       # certainty absent
    assert len(out) == 1 and "owner_note" in out[0]


def test_a_non_list_vocabulary_is_not_iterated_as_a_scale(tmp_path):
    """`isinstance(ref, list) and ref` — with `or`, a non-list truthy vocabulary would be ITERATED
    and its elements returned as scale levels. A malformed `enums:` entry must degrade to
    no-ordering rather than to nonsense whose "top" is an arbitrary character or key.

    Called directly rather than through a fixture schema: schema composition coerces a string enum
    into a character list before this function ever sees it (a separate schema_lib defect), which
    would mask the behaviour under test.
    """
    m = _load(tmp_path)
    for bad in ("notalist", {"a": 1}, 5):
        pol = {"field_enums": {"certainty": {"enum": "certainty"}}, "enums": {"certainty": bad}}
        assert m._ordered_enum(pol, "certainty") == [], bad
    # an inline non-list spec takes the same path without the enums indirection
    assert m._ordered_enum({"field_enums": {"certainty": {"a": 1}}}, "certainty") == []


def test_flagging_one_field_still_evaluates_the_next(tmp_path):
    """The `continue` after appending the top-of-scale flag is a SKIP: a break would stop the loop
    at the first flagged field and silently drop every later configured review field."""
    m = _load(tmp_path)
    out = _flags(m, {"type": "widget", "certainty": "locked", "owner_note": "changed"})
    assert len(out) == 2
    assert any("top of the declared scale" in f for f in out)
    assert any("owner_note" in f for f in out)
