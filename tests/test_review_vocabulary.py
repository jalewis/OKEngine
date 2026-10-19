"""okengine#823: the words a reviewer is shown must be explained where the decision is made.

The panel rendered reason codes as bare identifiers (`legacy-unspecified`) and never said that
`approve`/`dismiss` CLEAR quarantine while the other three keep it, or that a decision is bound
to the version opened. These pin the vocabulary onto the surfaces AND pin the vocabulary to the
codes the engine actually emits -- a glossary that drifts from the emitters is jargon with extra
steps.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path

import pytest

from okengine.review_vocabulary import (
    DECISION_EFFECTS,
    REASON_GLOSSARY,
    VERSION_LOCK_NOTICE,
    explain_reason,
    explain_reasons,
)

REPO = Path(__file__).resolve().parents[1]
# Codes that reach a reviewer come from two shapes: a literal `"code": "x"` record, and the
# conditional ladder in the write path that picks a code from a flag string.
_LITERAL = re.compile(r'"code":\s*"([a-z][a-z-]*)"')


def _emitted_codes() -> set[str]:
    codes: set[str] = set()
    for root in ("src/okengine", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            codes |= set(_LITERAL.findall(path.read_text(encoding="utf-8", errors="replace")))
    from okengine.write_services import review as write_review

    source = inspect.getsource(write_review._structured_review_reasons)
    ladder = source.split("code = (", 1)[1].split(")", 1)[0]
    # The ladder alternates emitted-code literals with GUARD literals (`if "categorical" in flag`).
    # Only the former reach a reviewer, so the guards are subtracted rather than explained -- an
    # earlier version of this test demanded a glossary entry for `categorical` and `degenerate`,
    # which are substrings of a flag message and are never shown to anyone.
    guards = set(re.findall(r'if\s+"([^"]+)"\s+in\s+flag', ladder))
    codes |= {m for m in re.findall(r'"([a-z][a-z-]*)"', ladder)} - guards
    codes |= set(_reconcile_codes())
    return {c for c in codes if c}


def _reconcile_codes() -> list[str]:
    """Codes the manual reconcile script picks from a table, not a `"code": "x"` literal
    (okengine#866). Imported, not scanned: a return ladder is exactly what the literal scanner
    could not see."""
    spec = importlib.util.spec_from_file_location(
        "reconcile_review_records_vocab", REPO / "scripts" / "reconcile_review_records.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [code for _needle, code in module.REASON_CODES] + [module.DEFAULT_REASON_CODE]


def test_every_reason_code_the_engine_emits_is_explained():
    """A new code without a glossary entry ships an identifier to a human. Fail here, not there."""
    missing = sorted(_emitted_codes() - set(REASON_GLOSSARY))
    assert not missing, (
        f"reason code(s) {missing} are emitted to reviewers with no explanation; "
        "add them to okengine.review_vocabulary.REASON_GLOSSARY"
    )


def test_the_emitters_still_use_the_codes_the_glossary_claims():
    """Drift runs both ways: a renamed emitter leaves a glossary describing a code nobody sends."""
    emitted = _emitted_codes()
    for code in ("grounding", "conflict", "legacy-unspecified", "import-unvetted",
                 "categorical-confidence", "changed-after-approval", "agent-draft", "manual"):
        assert code in emitted, f"glossary explains `{code}`, which nothing emits any more"


def test_an_unregistered_code_says_so_instead_of_rendering_jargon():
    """Negative fixture: the failure mode is a bare identifier reaching a human."""
    out = explain_reason("some-new-code")
    assert out["code"] == "some-new-code"
    assert "No explanation is registered" in out["means"]
    assert out["label"] == "some-new-code"


def test_reasons_are_explained_once_per_code_in_first_seen_order():
    rows = [{"code": "manual"}, {"code": "grounding"}, {"code": "manual"}]
    assert [r["code"] for r in explain_reasons(rows)] == ["manual", "grounding"]
    assert explain_reasons([]) == [] and explain_reasons(None) == []


def test_a_repeat_mid_list_is_skipped_rather_than_ending_the_list():
    """A duplicate at the END cannot tell `continue` from `break` -- both yield the same list,
    and a mutation campaign found exactly that hole. Put the repeat in the middle, where
    stopping early would silently drop every later reason from the panel."""
    rows = [{"code": "manual"}, {"code": "manual"}, {"code": "grounding"}, {"code": "conflict"}]
    assert [r["code"] for r in explain_reasons(rows)] == ["manual", "grounding", "conflict"]


@pytest.mark.parametrize("row", [None, "", 0, {}, {"code": None}, {"code": ""}])
def test_a_malformed_reason_row_becomes_an_empty_code_not_the_string_None(row):
    """Reason rows arrive from a YAML ledger written by several producers. A row that is None,
    empty, or carries no code must degrade to the unexplained placeholder -- never to a literal
    "None" rendered at a reviewer as if it were a reason code."""
    out = explain_reasons([row])
    assert len(out) == 1
    assert out[0]["code"] == ""
    assert out[0]["label"] == "unspecified"
    assert "None" not in out[0]["code"]


@pytest.mark.parametrize("decision,quarantine,note", [
    ("approve", "cleared", False),
    ("dismiss", "cleared", True),
    ("request-changes", "kept", True),
    ("reject", "kept", True),
    ("defer", "kept", True),
])
def test_each_decision_states_its_quarantine_effect_and_note_requirement(decision, quarantine, note):
    """The fact reviewers most needed and were never shown. `reject` and `dismiss` differ here."""
    effect = DECISION_EFFECTS[decision]
    assert effect["quarantine"] == quarantine
    assert effect["note_required"] is note


def test_reject_judges_content_and_dismiss_judges_the_request():
    """The distinction that decides the legacy-flag backlog; it must be on the screen, not in a doc."""
    assert "content" in DECISION_EFFECTS["reject"]["judges"]
    assert "REQUEST" in DECISION_EFFECTS["dismiss"]["judges"]


def test_decision_effects_match_the_documented_workflow():
    """docs/human-review.md is the prose source; the vocabulary must not contradict it."""
    doc = REPO / "docs" / "human-review.md"
    if not doc.is_file():          # publish-excluded trees
        pytest.skip("human-review.md not present")
    text = doc.read_text(encoding="utf-8")
    assert "Approval and dismissal clear" in text and "quarantine" in text
    assert set(DECISION_EFFECTS) == {"approve", "request-changes", "reject", "dismiss", "defer"}


def test_the_cockpit_renders_the_vocabulary_rather_than_its_own_wording():
    js = (REPO / "okengine-cockpit" / "static" / "app.js").read_text(encoding="utf-8")
    assert "reviewGlossary(d.glossary)" in js          # worklist
    assert "reviewGlossary(d.reason_glossary)" in js   # detail panel
    assert "reviewEffects(dc.effects)" in js
    assert "dc.version_lock" in js
    # the explanation must precede the controls that act on it
    assert js.index("reviewEffects(dc.effects)") < js.index('data-decision="approve"')


def test_version_lock_notice_warns_before_the_decision_not_after_the_409():
    assert "409" in VERSION_LOCK_NOTICE and "REFUSED" in VERSION_LOCK_NOTICE


def test_every_code_the_reconcile_script_can_emit_is_explained():
    """okengine#866: four of these reached the Cockpit as bare identifiers."""
    codes = set(_reconcile_codes())
    assert {"broken-link", "id-collision", "rejected-write", "owner-conflict"} <= codes
    assert codes <= set(REASON_GLOSSARY), sorted(codes - set(REASON_GLOSSARY))
