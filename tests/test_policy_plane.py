"""Canonical policy plane and the #283 source-quality vertical slice."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
POLICY = REPO / "tools" / "policy_plane.py"
WRITE = REPO / "okengine-mcp" / "write_server.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _policy():
    return _load("policy_plane_test", POLICY)


def test_catalog_validates_and_has_complete_enforcement_coverage():
    p = _policy()
    effective = p.effective_policy(REPO)
    assert len(effective["rules"]) >= 7
    assert len(effective["digest"]) == 64
    assert not p.validate_document(p.load_document(REPO / "config" / "policy" / "catalog.yaml"))
    rows = p.coverage(effective)["rules"]
    assert rows and all(row["covered"] for row in rows), rows


def test_policy_digest_is_independent_of_deployment_paths(tmp_path):
    p = _policy()
    first = tmp_path / "host" / "catalog.yaml"
    second = tmp_path / "container" / "catalog.yaml"
    first.parent.mkdir()
    second.parent.mkdir()
    content = (REPO / "config" / "policy" / "catalog.yaml").read_text()
    first.write_text(content)
    second.write_text(content)
    host_policy = p.compose_documents([first])
    container_policy = p.compose_documents([second])
    assert host_policy["sources"] != container_policy["sources"]
    assert host_policy["rules"][0]["source"] != container_policy["rules"][0]["source"]
    assert host_policy["digest"] == container_policy["digest"]


@pytest.mark.parametrize("mutation,fragment", [
    (("rules", 0, "id", "engine-authenticated-writer"), "duplicate rule ID"),
    (("rules", 0, "evaluator", "magic-yaml"), "evaluator unknown"),
    (("rules", 0, "enforcement", ["telepathy"]), "unknown targets"),
])
def test_catalog_rejects_duplicate_unknown_evaluator_and_target(mutation, fragment):
    p = _policy()
    doc = p.load_document(REPO / "config" / "policy" / "catalog.yaml")
    _, index, key, value = mutation
    if key == "id":
        doc["rules"][1]["id"] = value
    else:
        doc["rules"][index][key] = value
    assert any(fragment in error for error in p.validate_document(doc))


def test_composition_refuses_forbidden_override_and_invalid_waiver(tmp_path):
    p = _policy()
    base = REPO / "config" / "policy" / "catalog.yaml"
    override = tmp_path / "policy.yaml"
    override.write_text(yaml.safe_dump({
        "schema_version": 1,
        "rules": [{
            "id": "source-quality-fields-only", "owner": "pack", "description": "weaker",
            "severity": "warning", "applies_to": {}, "enforcement": ["write"],
            "evaluator": "field-capability", "remediation": "none", "override": "tighten-only",
            "verified_by": ["write"],
        }],
        "capabilities": {}, "waivers": [],
    }))
    with pytest.raises(p.PolicyError, match="non-overridable"):
        p.compose_documents([base, override])

    waiver = tmp_path / "waiver.yaml"
    waiver.write_text(yaml.safe_dump({
        "schema_version": 1, "rules": [], "capabilities": {},
        "waivers": [{"rule_id": "engine-policy-digest", "owner": "x", "reason": "x",
                     "scope": "x", "created_at": "2026-07-18T00:00:00Z",
                     "expires_at": "2026-07-19T00:00:00Z"}],
    }))
    with pytest.raises(p.PolicyError, match="does not permit waivers"):
        p.compose_documents([base, waiver])


def test_source_quality_capability_decision_table():
    p = _policy()
    policy = p.effective_policy(REPO)
    actor = "cron:source-quality-backfill"
    assert p.evaluate_capability(policy, actor, "update", "sources/2026/x", "source",
                                 ["reliability", "credibility"], "none") is None
    cases = [
        ("create", "sources/x", "source", ["reliability"], "none"),
        ("update", "entities/x", "source", ["reliability"], "none"),
        ("update", "sources/x", "actor", ["reliability"], "none"),
        ("update", "sources/x", "source", ["publisher"], "none"),
        ("update", "sources/x", "source", ["reliability"], "replace"),
        ("append", "sources/x", "source", [], "append"),
    ]
    for operation, path, page_type, fields, body in cases:
        result = p.evaluate_capability(policy, actor, operation, path, page_type, fields, body)
        assert result and result["rule_id"] == "source-quality-fields-only"
        assert result["outcome"] == "reject" and result["remediation"]


def test_candidate_capability_requires_complete_evidence_bundle_atomically():
    p = _policy()
    policy = p.effective_policy(REPO)
    actor = "cron:candidate"
    policy["capabilities"][actor] = {
        "rule_id": "source-quality-fields-only",
        "operations": ["update"], "paths": ["procedures/**"],
        "types": ["threat-procedure"],
        "update_fields": ["attack_techniques", "mapping_evidence", "confidence", "alternatives"],
        "required_fields": ["attack_techniques", "mapping_evidence", "confidence", "alternatives"],
        "protected_fields": ["reviewed_by", "result"], "body": "deny",
    }
    complete = ["attack_techniques", "mapping_evidence", "confidence", "alternatives"]
    assert p.evaluate_capability(policy, actor, "update", "procedures/p", "threat-procedure",
                                 complete, "none") is None
    rejected = p.evaluate_capability(
        policy, actor, "update", "procedures/p", "threat-procedure",
        ["attack_techniques", "confidence", "alternatives"], "none")
    assert rejected["rule_id"] == "source-quality-fields-only"
    assert rejected["outcome"] == "reject"
    assert rejected["evidence"]["missing_fields"] == ["mapping_evidence"]

    policy["capabilities"][actor]["required_fields"] = ["not-allowed"]
    errors = p.validate_capability(actor, policy["capabilities"][actor])
    assert any("required fields are not allowed" in error for error in errors)


@pytest.fixture
def governed_vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("""\
okf: {required: [type]}
types:
  source: {required: [type, source_kind, publisher, published]}
strict_types: false
permissions:
  default: {create: true, update: true, delete: false}
""")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_POLICY_CATALOG", str(REPO / "config" / "policy" / "catalog.yaml"))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_NOW", "2026-07-18T12:34:56Z")
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-18")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    sys.modules.pop("write_server", None)
    module = _load("write_server", WRITE)
    result = module._create("sources/2026/07/example", {
        "type": "source", "source_kind": "article", "publisher": "Example",
        "published": "2026-07-17", "url": "https://example.test/report",
    }, "# Example\n\nCaptured article.\n")
    assert result.startswith("created"), result
    path = tmp_path / "wiki" / "sources" / "2026" / "07" / "example.md"
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:source-quality-backfill")
    return module, tmp_path, path


def test_source_quality_two_field_patch_allowed(governed_vault):
    module, _, path = governed_vault
    result = module._update("sources/2026/07/example",
                            {"reliability": "A", "credibility": 1}, None)
    assert result.startswith("updated"), result
    fm, body = module._read_page(path)
    assert fm["reliability"] == "A" and fm["credibility"] == 1
    assert body == "# Example\n\nCaptured article.\n"


@pytest.mark.parametrize("field,value", [
    ("type", "concept"), ("id", "sources:forged"), ("publisher", "Forged"),
    ("published", "2026-07-18"), ("published_at", "2026-07-18T00:00:00Z"),
    ("url", "https://evil.test"), ("raw_capture", "changed"), ("status", "tombstoned"),
    ("lifecycle", "deleted"), ("tlp", "RED"), ("confidence", "confirmed"),
    ("maintained_by", ["other"]), ("discovered_by", "other"),
])
def test_source_quality_protected_fields_rejected_atomically(governed_vault, field, value):
    module, root, path = governed_vault
    before = path.read_bytes()
    result = module._update("sources/2026/07/example", {field: value}, None)
    assert result.startswith("rejected: policy[source-quality-fields-only]"), result
    assert field in result
    assert path.read_bytes() == before
    events = (root / ".okengine" / "policy-events.jsonl").read_text()
    assert '"rule_id": "source-quality-fields-only"' in events


def test_source_quality_body_and_other_write_lanes_rejected_atomically(governed_vault):
    module, _, path = governed_vault
    operations = [
        lambda: module._update("sources/2026/07/example", {"reliability": "A"}, "replacement"),
        lambda: module._patch("sources/2026/07/example", "publisher: Example", "publisher: Forged"),
        lambda: module._append_section("sources/2026/07/example", "Notes", "extra"),
        lambda: module._tombstone("sources/2026/07/example", "bad"),
        lambda: module._converge("sources/2026/07/example", {"type": "source",
                                  "publisher": "Forged", "published": "2026-07-17",
                                  "source_kind": "article"}, ""),
    ]
    for operation in operations:
        before = path.read_bytes()
        result = operation()
        assert "policy[source-quality-fields-only]" in str(result), result
        assert path.read_bytes() == before


def test_unknown_bound_job_fails_closed(governed_vault, monkeypatch):
    module, _, path = governed_vault
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:undeclared-job")
    before = path.read_bytes()
    result = module._update("sources/2026/07/example", {"reliability": "A"}, None)
    assert "policy[engine-authenticated-writer]" in result
    assert path.read_bytes() == before


def test_malformed_runtime_capability_fails_closed_without_exception():
    p = _policy()
    policy = p.effective_policy(REPO)
    policy["capabilities"]["extension:broken"] = {"operations": ["update"]}
    result = p.evaluate_capability(
        policy, "extension:broken", "update", "sources/x", "source", ["reliability"])
    assert result and result["rule_id"] == "engine-authenticated-writer"
    assert "capability_errors" in result["evidence"]


def test_prompt_contract_and_importer_envelope():
    p = _policy()
    policy = p.effective_policy(REPO)
    prompts = json.loads((REPO / "templates" / "pack" / "skeleton" / "crons" /
                          "engine-template-prompts.json").read_text())
    assert p.check_prompt(policy, "cron:source-quality-backfill",
                          prompts["source-quality-backfill"]) == []
    bad = p.validate_importer_envelope({"source_native_id": "x"})
    assert bad and bad["rule_id"] == "engine-importer-envelope"


def test_source_quality_job_has_no_bypass_writer_or_file_tool():
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    job = next(item for item in jobs if item["name"] == "source-quality-backfill")
    assert job["enabled_toolsets"] == ["okengine-write-source-quality", "okengine"]


def test_audit_materializes_structured_findings_and_cockpit_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_POLICY_CATALOG", str(REPO / "config" / "policy" / "catalog.yaml"))
    source = tmp_path / "wiki" / "sources" / "x.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\ntype: source\n---\n# Missing metadata\n")
    result = _policy().materialize(tmp_path, run_audit=True)
    assert result["findings"] == 1
    findings = json.loads((tmp_path / ".okengine" / "policy-findings.json").read_text())
    assert findings["findings"][0]["rule_id"] == "engine-source-metadata-complete"
    dashboard = (tmp_path / "wiki" / "operational" / "policy-health.md").read_text()
    assert "# Policy health" in dashboard and "engine-source-metadata-complete" in dashboard
