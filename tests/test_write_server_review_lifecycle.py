"""Version-locked human review on the write path (okengine#462, tranche T1).

The review lifecycle is the governed human decision surface: a reviewer claims a
request and records approve/dismiss against a SPECIFIC page version and content
hash. The version lock is the whole point -- it stops a decision made about one
version of a page silently applying to a later, different one.

These drive the real functions against a vault fixture and assert the validation
contract (structured {ok, status, error} results, not exceptions) plus the
version/hash locking that makes a decision auditable.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"

SCHEMA = "types:\n  actor: {required: [type]}\n  vendor: {required: [type]}\n"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-15")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m, tmp_path


@pytest.fixture
def subject(ws):
    """A real page under review, with its current version + content hash."""
    m, vault = ws
    m._create("entities/acme.md", "type: vendor\ntitle: Acme", "body under review")
    p = m._safe("entities/acme.md")
    fm, body, subj, version, digest = m._review_page_state(p)
    return m, p, version, digest


# ── digests and page state ────────────────────────────────────────────────────

def test_review_digest_is_a_stable_content_hash(ws):
    m, _ = ws
    assert m._review_digest("abc") == hashlib.sha256(b"abc").hexdigest()
    assert m._review_digest("abc") == m._review_digest("abc")
    assert m._review_digest("abc") != m._review_digest("abd")


def test_review_record_path_is_derived_from_the_review_id(ws):
    m, vault = ws
    path = m._review_record_path("review-123")
    assert path.parent == m._review_store()
    assert path.name == m._review_digest("review-123") + ".yaml"


def test_review_page_state_reports_version_subject_and_hash(subject):
    m, p, version, digest = subject
    fm, body, subj, v, d = m._review_page_state(p)
    assert subj == "entities/a/acme", "subject is the rel path without .md"
    assert v == version and d == digest
    assert d == m._review_digest(p.read_text(encoding="utf-8"))


def test_review_page_state_defaults_a_malformed_version_to_one(ws, tmp_path):
    m, vault = ws
    p = vault / "wiki" / "entities" / "v" / "verbose.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: vendor\nversion: not-a-number\n---\nbody\n", encoding="utf-8")
    _, _, _, version, _ = m._review_page_state(p)
    assert version == 1, "an unparseable version must not crash the review surface"


# ── _assign_review validation ─────────────────────────────────────────────────

def test_assign_requires_a_reviewer_identity(subject):
    m, p, version, digest = subject
    out = m._assign_review("entities/acme.md", "  ", version, digest)
    assert out["ok"] is False and out["status"] == 400
    assert "reviewer identity is required" in out["error"]


def test_assign_requires_a_parseable_expected_version(subject):
    m, p, version, digest = subject
    out = m._assign_review("entities/acme.md", "alice", "not-an-int", digest)
    assert out["ok"] is False and out["status"] == 400
    assert "expected page version is required" in out["error"]


def test_assign_requires_a_well_formed_expected_hash(subject):
    """A malformed hash must be refused rather than treated as 'no lock' -- otherwise
    the version lock silently degrades to no lock at all."""
    m, p, version, digest = subject
    for bad in ("", "not-a-hash", "abc123", digest[:-1], digest.upper()):
        out = m._assign_review("entities/acme.md", "alice", version, bad)
        assert out["ok"] is False and out["status"] == 400, bad
        assert "expected page hash is required" in out["error"]


def test_assign_returns_a_structured_result_not_an_exception(subject):
    m, p, version, digest = subject
    out = m._assign_review("entities/acme.md", "alice", version, digest)
    assert isinstance(out, dict) and "ok" in out and "status" in out


# ── _resolve_review validation ────────────────────────────────────────────────

def test_resolve_rejects_an_unknown_decision(subject):
    m, p, version, digest = subject
    for bad in ("maybe", "", "APPROVED-ISH", None):
        out = m._resolve_review("entities/acme.md", bad, "alice", "note", version, digest)
        assert out["ok"] is False and out["status"] == 400, bad
        assert "invalid review decision" in out["error"]


def test_resolve_accepts_only_the_declared_decisions(ws):
    m, _ = ws
    assert m._REVIEW_DECISIONS, "the decision vocabulary must be declared, not implicit"
    for decision in m._REVIEW_DECISIONS:
        assert isinstance(decision, str) and decision == decision.lower()


def test_resolve_requires_a_reviewer_identity(subject):
    m, p, version, digest = subject
    decision = sorted(m._REVIEW_DECISIONS)[0]
    out = m._resolve_review("entities/acme.md", decision, "   ", "note", version, digest)
    assert out["ok"] is False and out["status"] == 400
    assert "reviewer identity is required" in out["error"]


def test_resolve_normalises_decision_case_and_whitespace(subject):
    """A decision is matched case-insensitively, so a reviewer typing 'Approve' is not
    rejected on presentation grounds."""
    m, p, version, digest = subject
    decision = sorted(m._REVIEW_DECISIONS)[0]
    out = m._resolve_review("entities/acme.md", f"  {decision.upper()}  ",
                            "alice", "note", version, digest)
    assert "invalid review decision" not in str(out.get("error", ""))


def test_resolve_requires_a_well_formed_expected_hash(subject):
    m, p, version, digest = subject
    decision = sorted(m._REVIEW_DECISIONS)[0]
    out = m._resolve_review("entities/acme.md", decision, "alice", "note", version, "bad")
    assert out["ok"] is False and out["status"] == 400


# ── _structured_review_reasons ────────────────────────────────────────────────

def test_review_reasons_convert_flags_into_explainable_records(ws):
    m, _ = ws
    reasons = m._structured_review_reasons({}, "", ["categorical confidence on a claim"])
    assert reasons and all(isinstance(r, dict) for r in reasons)
    assert any(r.get("code") == "categorical-confidence" for r in reasons)


def test_review_reasons_recognise_the_changed_after_approval_flag(ws):
    m, _ = ws
    reasons = m._structured_review_reasons({}, "", ["page changed after approval"])
    assert any(r.get("code") == "changed-after-approval" for r in reasons)


def test_review_reasons_always_carry_at_least_one_explanation(ws):
    """Never empty: with nothing else derivable it emits a `legacy-unspecified`
    placeholder, so a queued review can always say WHY it was queued. A review
    request with no reason would not be auditable."""
    m, _ = ws
    reasons = m._structured_review_reasons({}, "")
    assert reasons == [{"code": "legacy-unspecified", "detail": "legacy needs_review flag"}]


def test_review_reasons_record_field_level_source_conflicts(ws):
    m, _ = ws
    reasons = m._structured_review_reasons(
        {"conflicts": [{"field": "origin_country"}]}, "")
    conflict = [r for r in reasons if r["code"] == "conflict"]
    assert conflict and conflict[0]["field"] == "origin_country"
    assert "sources disagree" in conflict[0]["detail"]


@pytest.mark.parametrize("conflicts", ["origin_country", 7, True])
def test_review_reasons_tolerate_malformed_scalar_conflicts(ws, conflicts):
    m, _ = ws
    assert m._structured_review_reasons({"conflicts": conflicts}, "") == [
        {"code": "legacy-unspecified", "detail": "legacy needs_review flag"}
    ]


def test_review_reasons_detect_a_failed_grounding_check_in_the_body(ws):
    m, _ = ws
    body = "## Grounding check\n\nThe claim is unsupported by the cited source.\n"
    assert any(r["code"] == "grounding" for r in m._structured_review_reasons({}, body))

    clean = "## Grounding check\n\nAll claims trace to the cited sources.\n"
    assert not any(r["code"] == "grounding" for r in m._structured_review_reasons({}, clean))


def test_review_reasons_deduplicate_flags_surfaced_by_several_guards(ws):
    m, _ = ws
    duplicated = ["categorical confidence", "categorical confidence"]
    reasons = m._structured_review_reasons({}, "", duplicated)
    assert len(reasons) == 1, "the same flag from two guards is recorded once"


def test_review_reasons_classify_degenerate_output_as_an_agent_draft(ws):
    m, _ = ws
    reasons = m._structured_review_reasons({}, "", ["degenerate: 300-word run"])
    assert any(r["code"] == "agent-draft" for r in reasons)


def test_unrecognised_flags_fall_back_to_manual(ws):
    m, _ = ws
    reasons = m._structured_review_reasons({}, "", ["something a human typed"])
    assert any(r["code"] == "manual" for r in reasons)
