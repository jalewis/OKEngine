"""Policy-plane evaluation, audit and CLI (okengine#462, tranche T1).

`validate_*` (covered in !721) stops a malformed policy loading; this covers what
happens once a valid policy is in force:

  evaluate_capability()          the write-time authorisation decision
  validate_importer_envelope()   provenance completeness at the importer boundary
  _path_in_scopes()              path scoping, kept dependency-free on purpose
  finding() / finding_message()  the finding record and its human rendering
  coverage()                     which rules are actually verified where declared
  audit() / main()               the scheduled sweep and its CLI

The authorisation tests assert the DENY direction hardest: an actor with no
declared capability, or one whose capability cites an unknown rule id, must be
rejected rather than quietly allowed.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "tools" / "policy_plane.py"


def load_pp():
    spec = importlib.util.spec_from_file_location("policy_plane_eval", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def pp():
    return load_pp()


def policy_with(pp, capability):
    return {
        "digest": "sha256:deadbeef",
        "rules": [{"id": "cap-1", "owner": "engine",
                   "enforcement": [sorted(pp.ENFORCEMENT_POINTS)[0]],
                   "verified_by": []}],
        "capabilities": {"importer": capability},
    }


def capability(**overrides):
    value = {
        "rule_id": "cap-1",
        "operations": ["create", "update"],
        "paths": ["sources/**"],
        "types": ["source"],
        "update_fields": ["title"],
        "required_fields": [],
        "protected_fields": ["id"],
        "body": "allow",
    }
    value.update(overrides)
    return value


# ── finding / finding_message ────────────────────────────────────────────────

def test_finding_records_the_decision_and_stamps_a_time(pp):
    f = pp.finding("rule-1", "reject", "high", "sources/x", "create", "importer",
                   "why", "how")
    assert f["rule_id"] == "rule-1" and f["outcome"] == "reject"
    assert f["evidence"] == {} and f["enforcement_point"] == "write"
    assert f["evaluated_at"], "a finding must be timestamped for the audit trail"


def test_finding_rejects_an_outcome_outside_the_vocabulary(pp):
    with pytest.raises(pp.PolicyError, match="invalid finding outcome"):
        pp.finding("rule-1", "shrug", "high", "s", "create", "a", "m", "r")


def test_finding_message_renders_offending_and_missing_fields(pp):
    result = pp.finding("rule-1", "reject", "high", "sources/x", "create", "importer",
                        "message here", "do this",
                        {"offending_fields": ["id"], "missing_fields": ["publisher"]})
    text = pp.finding_message(result)
    assert "policy[rule-1]" in text and "message here" in text
    assert "offending fields: id" in text and "missing fields: publisher" in text
    assert "Remediation: do this" in text


def test_finding_message_omits_absent_evidence(pp):
    result = pp.finding("rule-1", "warn", "low", "s", "create", "a", "m", "r")
    text = pp.finding_message(result)
    assert "offending fields" not in text and "missing fields" not in text


# ── _path_in_scopes ──────────────────────────────────────────────────────────

def test_path_scoping_matches_prefixes_globs_and_wildcards(pp):
    assert pp._path_in_scopes("sources/2026/x", ["sources/**"])
    assert pp._path_in_scopes("/sources/2026/x.md", ["wiki/sources/**"])
    assert pp._path_in_scopes("anything/at/all", ["**"])
    assert pp._path_in_scopes("entities/a/acme", ["entities/*/acme"])
    assert not pp._path_in_scopes("entities/a/acme", ["sources/**"])
    assert not pp._path_in_scopes("sources-private/x", ["sources/**"]), \
        "prefix matching must not leak across a name boundary"


# ── evaluate_capability: the authorisation decision ──────────────────────────

def test_admin_bypasses_by_identity_not_by_yaml(pp):
    assert pp.evaluate_capability({}, "admin", "create", "sources/x") is None


def test_actor_without_a_declared_capability_is_rejected(pp):
    out = pp.evaluate_capability({"rules": []}, "stranger", "create", "sources/x")
    assert out is not None and out["outcome"] == "reject"
    assert "no declared write capability" in out["message"]


def test_capability_citing_an_unknown_rule_id_is_rejected(pp):
    """A capability must trace to a declared rule, or the policy is unauditable."""
    p = policy_with(pp, capability(rule_id="does-not-exist"))
    out = pp.evaluate_capability(p, "importer", "create", "sources/x", page_type="source")
    assert out is not None and out["outcome"] == "reject"


def test_operation_outside_the_capability_is_rejected(pp):
    p = policy_with(pp, capability(operations=["update"]))
    out = pp.evaluate_capability(p, "importer", "tombstone", "sources/x", page_type="source")
    assert out is not None and out["outcome"] == "reject"


def test_path_outside_the_capability_is_rejected(pp):
    p = policy_with(pp, capability())
    out = pp.evaluate_capability(p, "importer", "create", "entities/a/acme",
                                 page_type="source")
    assert out is not None and out["outcome"] == "reject"


def test_writing_a_protected_field_is_rejected(pp):
    p = policy_with(pp, capability())
    out = pp.evaluate_capability(p, "importer", "update", "sources/x",
                                 page_type="source", changed_fields=["id"])
    assert out is not None and out["outcome"] == "reject"


def test_a_permitted_write_returns_no_finding(pp):
    p = policy_with(pp, capability())
    assert pp.evaluate_capability(p, "importer", "create", "sources/2026/x",
                                  page_type="source", changed_fields=["title"]) is None


# ── validate_importer_envelope ───────────────────────────────────────────────

def test_complete_importer_envelope_passes(pp):
    envelope = {k: "v" for k in ("connector_id", "source_native_id", "source_revision",
                                 "observed_at", "source_authority", "source_permission",
                                 "data_sensitivity", "payload")}
    assert pp.validate_importer_envelope(envelope) is None


def test_incomplete_importer_envelope_is_rejected_and_names_the_gaps(pp):
    out = pp.validate_importer_envelope({"connector_id": "c", "payload": "p"})
    assert out is not None and out["outcome"] == "reject"
    assert out["enforcement_point"] == "importer"
    assert "source_native_id" in out["evidence"]["missing_fields"]
    assert "observed_at" in out["evidence"]["missing_fields"]


def test_empty_string_values_count_as_missing_provenance(pp):
    envelope = {k: "v" for k in ("connector_id", "source_native_id", "source_revision",
                                 "observed_at", "source_authority", "source_permission",
                                 "data_sensitivity", "payload")}
    envelope["source_authority"] = ""
    out = pp.validate_importer_envelope(envelope)
    assert out is not None and "source_authority" in out["evidence"]["missing_fields"]


# ── coverage ─────────────────────────────────────────────────────────────────

def test_coverage_marks_a_rule_covered_only_when_every_point_is_verified(pp):
    points = sorted(pp.ENFORCEMENT_POINTS)[:2]
    policy = {"digest": "sha256:x", "rules": [
        {"id": "full", "owner": "engine", "evaluator": "page-quality-finding",
         "enforcement": points, "verified_by": points},
        {"id": "partial", "owner": "engine", "evaluator": "page-quality-finding",
         "enforcement": points, "verified_by": points[:1]},
        {"id": "none", "owner": "engine", "evaluator": "page-quality-finding",
         "enforcement": points, "verified_by": []},
    ]}
    rows = {r["rule_id"]: r for r in pp.coverage(policy)["rules"]}
    assert rows["full"]["covered"] is True
    assert rows["partial"]["covered"] is False, "declared-but-unverified is NOT covered"
    assert rows["none"]["covered"] is False


# ── audit + CLI ──────────────────────────────────────────────────────────────

def audit_policy():
    return {"rules": [
        {"id": "engine-source-metadata-complete", "evaluator": "source-metadata-completeness",
         "severity": "review", "enforcement": ["audit"],
         "applies_to": {"namespace": "sources", "type": "source"}, "remediation": "repair"},
        {"id": "engine-page-quality-review", "evaluator": "page-quality-finding",
         "severity": "review", "enforcement": ["audit"], "applies_to": {},
         "remediation": "classify"},
    ]}

def test_audit_flags_untyped_pages_and_incomplete_source_metadata(pp, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "sources" / "2026").mkdir(parents=True)
    (wiki / "sources" / "2026" / "thin.md").write_text(
        "---\ntype: source\n---\n# Thin\n", encoding="utf-8")
    (wiki / "untyped.md").write_text("---\ntitle: No type\n---\n# X\n", encoding="utf-8")

    results = pp.audit(tmp_path, audit_policy())
    ids = {r["rule_id"] for r in results}
    assert "engine-source-metadata-complete" in ids
    assert "engine-page-quality-review" in ids


def test_audit_ignores_operational_and_dashboard_pages(pp, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "operational").mkdir(parents=True)
    (wiki / "operational" / "note.md").write_text("---\ntitle: n\n---\n", encoding="utf-8")
    assert pp.audit(tmp_path, audit_policy()) == []


def test_audit_dispatches_strict_type_namespace_against_composed_schema(pp, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "misplaced.md").write_text(
        "---\ntype: source\n---\n# Misplaced\n", encoding="utf-8")
    (tmp_path / "schema.yaml").write_text("types: {source: {required: [type]}}\n")
    policy = {"rules": [{
        "id": "engine-strict-type-namespace", "evaluator": "strict-type-namespace",
        "severity": "reject", "enforcement": ["audit"], "applies_to": {},
        "remediation": "move it",
    }]}

    results = pp.audit(tmp_path, policy)

    assert len(results) == 1
    assert results[0]["rule_id"] == "engine-strict-type-namespace"
    assert results[0]["evidence"]["expected_namespace"] == "sources"


def test_audit_evaluator_edges_and_schema_load_failure(pp, tmp_path, monkeypatch):
    import importlib
    import sys
    from types import SimpleNamespace

    repo_cron = str(REPO / "scripts" / "cron")
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != repo_cron])
    schema = pp._audit_schema(tmp_path)
    assert isinstance(schema, dict) and repo_cron in sys.path
    monkeypatch.setattr(importlib, "import_module", lambda _name: (_ for _ in ()).throw(ImportError()))
    assert pp._audit_schema(tmp_path) == {}

    source_rule = {"id": "s", "evaluator": "source-metadata-completeness",
                   "applies_to": {"namespace": "sources", "type": "source"}}
    assert pp._audit_rule_page(source_rule, "concepts/x", {"type": "source"}, {}) is None
    assert pp._audit_rule_page(source_rule, "sources/x", {"type": "concept"}, {}) is None
    assert pp._audit_rule_page(source_rule, "sources/x", {
        "type": "source", "publisher": "p", "published": "2026-01-01"}, {}) is None

    strict = {"id": "strict", "evaluator": "strict-type-namespace", "applies_to": {}}
    monkeypatch.setitem(sys.modules, "schema_lib", SimpleNamespace(
        type_home_namespace=lambda *_a: (_ for _ in ()).throw(ValueError())))
    finding = pp._audit_rule_page(strict, "concepts/x", {"type": "unknown"}, {"types": {}})
    assert "not declared" in finding["message"]


def test_coverage_rejects_self_declared_audit_without_executable_evaluator(pp):
    policy = {"digest": "sha256:x", "rules": [{
        "id": "fictional-audit", "owner": "engine", "evaluator": "field-capability",
        "enforcement": ["audit"], "verified_by": ["audit"],
    }]}
    row = pp.coverage(policy)["rules"][0]
    assert row["covered"] is False
    assert "audit" not in row["verified_by"]


def test_audit_replays_recent_reject_and_warn_events(pp, tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    events = tmp_path / ".okengine" / "policy-events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps({"rule_id": "r", "outcome": "reject"}) + "\n"
        + json.dumps({"rule_id": "ok", "outcome": "allow"}) + "\n"
        + "{ not json\n", encoding="utf-8")

    outcomes = {r.get("outcome") for r in pp.audit(tmp_path, {})}
    assert "reject" in outcomes
    assert "allow" not in outcomes, "only reject/warn events are replayed"


def test_cli_reports_a_policy_error_without_a_traceback(pp, tmp_path, capsys):
    (tmp_path / ".okengine").mkdir(parents=True)
    (tmp_path / ".okengine" / "policy.yaml").write_text("- not a mapping\n", encoding="utf-8")
    assert pp.main(["validate", "--vault", str(tmp_path)]) == 1
    assert "policy invalid" in capsys.readouterr().err


def test_cli_digest_and_validate_report_the_engine_policy(pp, capsys):
    engine_vault = REPO
    assert pp.main(["digest", "--vault", str(engine_vault)]) == 0
    digest = capsys.readouterr().out.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", digest), digest

    assert pp.main(["validate", "--vault", str(engine_vault)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["digest"] == digest
