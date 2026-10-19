"""okengine#842: the requires-review rule has ONE definition, used by the worklist and the reconciler."""
from __future__ import annotations

from pathlib import Path

from okengine.review_policy import (
    GROUNDING_REVIEW, legacy_review_current, requires_review, review_required_types)


def test_the_flag_requires_review():
    assert requires_review({"needs_review": True}, "", frozenset())
    assert not requires_review({"needs_review": False}, "", frozenset())


def test_a_failed_grounding_section_requires_review_regardless_of_flags():
    body = "## Grounding check\n\nclaim X: unsupported by the cited source\n"
    assert GROUNDING_REVIEW.search(body)
    assert requires_review({}, body, frozenset())


def test_a_required_type_needs_review_until_approved_or_signed_off():
    types = frozenset({"assessment"})
    assert requires_review({"type": "assessment"}, "", types)
    assert not requires_review({"type": "assessment", "review_state": "approved"}, "", types)
    signed = {"type": "assessment", "reviewed_on": "2026-09-10", "last_updated": "2026-09-01"}
    assert not requires_review(signed, "", types)


def test_a_sign_off_older_than_the_last_edit_is_stale():
    assert not legacy_review_current({"reviewed_on": "2026-09-01", "last_updated": "2026-09-10"})
    assert legacy_review_current({"reviewed_on": "2026-09-10", "last_updated": "2026-09-01"})
    assert not legacy_review_current({})


def test_required_types_prefer_the_composed_schema(tmp_path: Path):
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "composed-schema.yaml").write_text(
        "review_required_types: [prediction]\n", encoding="utf-8")
    (tmp_path / "schema.yaml").write_text("review_required_types: [briefing]\n", encoding="utf-8")
    assert review_required_types(tmp_path) == frozenset({"prediction"})


def test_required_types_fall_back_to_the_pack_schema_and_then_to_none(tmp_path: Path):
    (tmp_path / "schema.yaml").write_text("review_required_types: [briefing]\n", encoding="utf-8")
    assert review_required_types(tmp_path) == frozenset({"briefing"})
    (tmp_path / "schema.yaml").write_text("other: 1\n", encoding="utf-8")
    assert review_required_types(tmp_path) == frozenset()
    (tmp_path / "schema.yaml").write_text("[unclosed\n", encoding="utf-8")
    assert review_required_types(tmp_path) == frozenset()


def test_the_cockpit_uses_this_rule_rather_than_a_copy():
    src = (Path(__file__).resolve().parent.parent / "src/okengine/cockpit_services/page_metadata.py")
    text = src.read_text(encoding="utf-8")
    assert "return requires_review(fm, body, _review_required_types())" in text


# --- Behaviours a focused mutation campaign found unpinned (okengine#842). Each survivor was a real
# gap in what the tests asserted about the rule, not a line merely left unexecuted.

def test_a_sign_off_on_the_same_day_as_the_last_edit_is_current():
    """`>=`, not `>`: a page signed off the day it was last edited is covered by that sign-off."""
    assert legacy_review_current({"reviewed_on": "2026-09-10", "last_updated": "2026-09-10"})


def test_sign_off_currency_is_compared_by_date_not_by_timestamp():
    """Both sides are cut to the DATE. A same-day edit timestamped later than a date-only sign-off
    must not read as newer -- and a later day must, even when only the day's last digit differs."""
    assert legacy_review_current({"reviewed_on": "2026-09-10", "last_updated": "2026-09-10T23:59:00Z"})
    assert not legacy_review_current({"reviewed_on": "2026-09-10", "last_updated": "2026-09-19"})
    assert legacy_review_current({"reviewed_on": "2026-09-19T08:00:00Z", "last_updated": "2026-09-10"})


def test_the_edit_date_falls_back_from_last_updated_to_updated_to_created():
    """A page carrying only `updated`, or only `created`, is still dated -- not treated as undated."""
    assert not legacy_review_current({"reviewed_on": "2026-09-01", "updated": "2026-09-10"})
    assert not legacy_review_current({"reviewed_on": "2026-09-01", "created": "2026-09-10"})
    assert legacy_review_current({"reviewed_on": "2026-09-10", "created": "2026-09-01"})


def test_only_a_literal_true_flag_counts():
    """`is True`: YAML `needs_review: 1` is an int, and the flag is boolean. Preserved from the rule
    as it stood in the Cockpit -- extraction must not quietly widen what counts as flagged."""
    assert not requires_review({"needs_review": 1}, "", frozenset())
    assert not requires_review({"needs_review": "true"}, "", frozenset())


def test_a_rejected_review_is_not_mistaken_for_an_approval():
    """`== "approved"`, and specifically not an ordering: "rejected" sorts after "approved", so a
    `>=` would treat every rejected required-type page as cleared and drop it from review."""
    types = frozenset({"assessment"})
    for state in ("rejected", "request-changes", "open", "zzz"):
        assert requires_review({"type": "assessment", "review_state": state}, "", types), state


def test_approval_is_recognised_by_value_not_by_object_identity():
    """A state built at runtime is equal to "approved" without being the interned literal, so an
    identity check would miss it. Constructed here rather than written, to defeat interning."""
    built = "".join(["appr", "oved"])
    assert built == "approved" and built is not "approved"   # noqa: F632 -- the point of the test
    assert not requires_review({"type": "assessment", "review_state": built}, "", frozenset({"assessment"}))
