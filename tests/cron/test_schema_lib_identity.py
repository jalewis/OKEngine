"""Identity rules in the baked write-path lib: alias shape, rejected aliases, stale grades, tombstones.

`scripts/cron/schema_lib.py` defines these once so the write path, importers, autoverify and the
corpus audit cannot drift apart (okengine#845, library#101, okengine#563). The write-server and
autoverify suites exercise them through their callers; these tests pin each rule directly, at its
boundaries, so the mutation target for the lib measures the rules themselves.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

MOD = Path(__file__).resolve().parents[2] / "scripts" / "cron" / "schema_lib.py"
spec = importlib.util.spec_from_file_location("schema_lib_identity", MOD)
sl = importlib.util.module_from_spec(spec)
sys.modules["schema_lib_identity"] = sl
spec.loader.exec_module(sl)


# ── alias_value_problem ───────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["Lazarus Group", "Furuno Electric Co., Ltd.", "APT 38", 38])
def test_a_single_name_has_no_problem(value):
    assert sl.alias_value_problem(value) is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_empty_alias_is_empty(value):
    assert sl.alias_value_problem(value) == "empty"


@pytest.mark.parametrize("value", ["https://example.org/actor", "HTTP://x.y", "www.example.org"])
def test_a_url_is_not_a_name(value):
    assert sl.alias_value_problem(value) == "a URL"


def test_multi_line_text_is_not_a_name():
    assert sl.alias_value_problem("Brown Fox\nalso seen as Red Fox") == "multi-line text"


def test_one_comma_is_a_name_and_two_are_a_list():
    assert sl.alias_value_problem("Acme, Inc") is None
    for listed in ("Brown Fox, Red Fox, Grey Fox", "Brown Fox, Red Fox, Grey Fox, Blue Fox"):
        assert sl.alias_value_problem(listed) == \
            "several names in one entry (send each alias separately)"


def test_the_length_limit_admits_exactly_its_own_length():
    assert sl.ALIAS_MAX_LEN == 80
    assert sl.alias_value_problem("x" * 80) is None
    assert sl.alias_value_problem("x" * 81) == "longer than 80 characters"


def test_the_length_check_measures_the_stripped_name():
    assert sl.alias_value_problem("  " + "x" * 80 + "  ") is None


# ── alias_key / rejected_alias_keys ───────────────────────────────────────────

def test_alias_key_ignores_case_spacing_and_punctuation():
    assert sl.alias_key("Brown Fox") == sl.alias_key("brown-fox") == sl.alias_key("BrownFox") \
        == "brownfox"


def test_alias_key_of_nothing_is_empty():
    assert sl.alias_key(None) == ""
    assert sl.alias_key("--") == ""


def test_rejected_alias_keys_reads_the_page_field():
    fm = {sl.REJECTED_ALIASES_FIELD: ["Brown Fox", "!!", "red-fox"]}
    assert sl.rejected_alias_keys(fm) == {"brownfox", "redfox"}


def test_rejected_alias_keys_accepts_a_scalar_and_absence():
    assert sl.rejected_alias_keys({sl.REJECTED_ALIASES_FIELD: "Brown Fox"}) == {"brownfox"}
    assert sl.rejected_alias_keys({sl.REJECTED_ALIASES_FIELD: ""}) == set()
    assert sl.rejected_alias_keys({}) == set()
    assert sl.rejected_alias_keys(None) == set()


# ── admissible_aliases ────────────────────────────────────────────────────────

def test_existing_aliases_are_kept_exactly_and_new_names_appended():
    kept, refused = sl.admissible_aliases(["https://legacy.example", "Brown Fox"], ["Red Fox"])
    assert kept == ["https://legacy.example", "Brown Fox", "Red Fox"]
    assert refused == []


def test_a_name_already_present_under_another_spelling_is_not_repeated():
    kept, refused = sl.admissible_aliases(["Brown Fox"], ["brown-fox", "Red Fox", "RED FOX"])
    assert kept == ["Brown Fox", "Red Fox"]
    assert refused == []


def test_a_malformed_addition_is_refused_with_its_reason():
    kept, refused = sl.admissible_aliases([], ["https://x.example"])
    assert kept == []
    assert refused == [("https://x.example", "a URL")]


def test_a_name_the_review_rejected_is_refused():
    kept, refused = sl.admissible_aliases([], ["Brown Fox", "Red Fox"], rejected=["brown fox"])
    assert kept == ["Red Fox"]
    assert refused == [("Brown Fox", "rejected by review for this page (`rejected_aliases`)")]


def test_an_addition_with_no_identity_is_dropped_silently():
    kept, refused = sl.admissible_aliases(["Brown Fox"], ["!!"])
    assert kept == ["Brown Fox"]
    assert refused == []


def test_scalar_and_absent_inputs_are_lists():
    assert sl.admissible_aliases("Brown Fox", "Red Fox") == (["Brown Fox", "Red Fox"], [])
    assert sl.admissible_aliases(None, "") == ([], [])


# ── removed_aliases ───────────────────────────────────────────────────────────

def test_a_dropped_alias_is_reported_once_and_stripped():
    assert sl.removed_aliases([" Brown Fox ", "brown-fox", "Red Fox"], ["Red Fox"]) == ["Brown Fox"]


def test_a_respelled_alias_is_not_a_removal():
    assert sl.removed_aliases(["Brown Fox"], ["brown-fox"]) == []


def test_malformed_and_identity_less_entries_are_never_recorded():
    assert sl.removed_aliases(["https://x.example", "!!", "Brown Fox"], []) == ["Brown Fox"]


def test_removed_aliases_accepts_scalars():
    assert sl.removed_aliases("Brown Fox", None) == ["Brown Fox"]


# ── contradicting_reliability ─────────────────────────────────────────────────

def test_a_page_grade_worse_than_its_best_cited_grade_contradicts_it():
    fm = {"reliability": "C", "auto_verified_basis": "2 B-grade sources, 1 A-grade source"}
    assert sl.contradicting_reliability(fm) == ("C", "A")


def test_an_equal_or_better_page_grade_does_not_contradict():
    assert sl.contradicting_reliability(
        {"reliability": "A", "auto_verified_basis": "1 A-grade source"}) is None
    assert sl.contradicting_reliability(
        {"reliability": "A", "auto_verified_basis": "1 B-grade source"}) is None


def test_the_grade_is_read_case_blind_from_its_first_letter():
    fm = {"reliability": " c2 ", "auto_verified_basis": "1 B-grade source"}
    assert sl.contradicting_reliability(fm) == ("C", "B")


@pytest.mark.parametrize("grade", [None, "", "Z", "7"])
def test_a_missing_or_non_admiralty_grade_never_contradicts(grade):
    assert sl.contradicting_reliability(
        {"reliability": grade, "auto_verified_basis": "1 A-grade source"}) is None


def test_a_basis_with_no_grades_never_contradicts():
    assert sl.contradicting_reliability({"reliability": "F", "auto_verified_basis": "unverified"}) \
        is None
    assert sl.contradicting_reliability({"reliability": "F"}) is None


def test_an_explicit_basis_replaces_the_pages_own():
    fm = {"reliability": "C", "auto_verified_basis": "1 D-grade source"}
    assert sl.contradicting_reliability(fm) is None
    assert sl.contradicting_reliability(fm, basis="1 A-grade source") == ("C", "A")
    assert sl.contradicting_reliability(
        {"reliability": "C", "auto_verified_basis": "1 A-grade source"}, basis="") is None


def test_no_front_matter_never_contradicts():
    assert sl.contradicting_reliability(None) is None


# ── tombstone_reason_kind ─────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", ["Different entity from Andariel", "distinct actors",
                                    "NOT THE SAME group as X", "separate identities"])
def test_a_distinctness_verdict_is_distinct(reason):
    assert sl.tombstone_reason_kind(reason) == "distinct"


@pytest.mark.parametrize("reason", ["Duplicate of lazarus-group", "merged into andariel",
                                    "same actor as X", "alias of Lazarus"])
def test_a_same_identity_reason_is_duplicate(reason):
    assert sl.tombstone_reason_kind(reason) == "duplicate"


@pytest.mark.parametrize("reason", [None, "", "retired: out of scope"])
def test_any_other_reason_has_no_kind(reason):
    assert sl.tombstone_reason_kind(reason) is None


def test_distinctness_wins_when_a_reason_says_both():
    assert sl.tombstone_reason_kind("different entity; not a duplicate") == "distinct"


# ---- okengine#887: negated distinctness is a same-identity verdict ---------------------------

@pytest.mark.parametrize("reason", [
    "Hellonet is a campaign cluster that aligns with OilRig's operations, not a separate entity.",
    "no longer a distinct actor after the 2025 rebrand",
    "Duplicate of OilRig -- not a different group",
])
def test_negated_distinctness_is_a_duplicate_not_a_distinct_verdict(reason):
    assert sl.tombstone_reason_kind(reason) == "duplicate"


def test_not_the_same_is_still_distinctness():
    assert sl.tombstone_reason_kind("not the same group as APT34") == "distinct"


def test_a_negated_phrase_does_not_hide_a_real_distinctness_verdict():
    assert sl.tombstone_reason_kind("not a separate entity from X, but a different actor from Y") \
        == "distinct"
