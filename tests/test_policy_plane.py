"""Canonical policy plane and the #283 source-quality vertical slice."""
from __future__ import annotations

import importlib.util
import asyncio
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


def test_cli_validate_dispatches_directly_to_policy_validation(monkeypatch, tmp_path, capsys):
    p = _policy()
    calls = []
    monkeypatch.setattr(
        p, "effective_policy",
        lambda vault: calls.append(("validate", vault)) or {"digest": "d", "rules": []},
    )
    monkeypatch.setattr(
        p, "materialize",
        lambda *_args, **_kwargs: calls.append(("materialize", None)) or {},
    )

    assert p.main(["validate", "--vault", str(tmp_path)]) == 0
    assert calls == [("validate", tmp_path)]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "digest": "d", "rules": 0}


def test_default_catalog_path_is_repo_relative(monkeypatch):
    p = _policy()
    monkeypatch.delenv("OKENGINE_POLICY_CATALOG", raising=False)
    assert p.engine_catalog_path() == REPO / "config" / "policy" / "catalog.yaml"


def test_catalog_validates_and_has_complete_enforcement_coverage():
    p = _policy()
    effective = p.effective_policy(REPO)
    assert len(effective["rules"]) >= 7
    assert len(effective["digest"]) == 64
    assert not p.validate_document(p.load_document(REPO / "config" / "policy" / "catalog.yaml"))
    rows = p.coverage(effective)["rules"]
    assert rows and all(row["covered"] for row in rows), rows


def test_disabled_extension_policy_grant_is_not_composed(tmp_path):
    """Negative fixture: files remain installed after disable, but authority is revoked."""
    p = _policy()
    ext = tmp_path / "extensions" / "demo.policy"
    ext.mkdir(parents=True)
    (ext / "extension.yaml").write_text(yaml.safe_dump({
        "id": "demo.policy", "kind": "operation", "version": "0.1.0",
        "name": "Demo policy", "requires": {"engine": ">=0.3.0"},
        "trust": "in-gateway", "capabilities": {"read": ["wiki/**"],
                                                   "write": ["dashboards/**"]},
    }), encoding="utf-8")
    policy_file = ext / "policy.yaml"
    policy_file.write_text(yaml.safe_dump({
        "schema_version": 1, "waivers": [],
        "rules": [{"id": "demo-policy-write", "owner": "demo.policy",
                   "description": "Demo writer grant", "severity": "reject",
                   "applies_to": {"operation": "update"}, "enforcement": ["write"],
                   "evaluator": "field-capability", "remediation": "Disable the extension",
                   "override": "forbidden", "verified_by": ["write"]}],
        "capabilities": {"extension:demo.policy": {
            "rule_id": "demo-policy-write", "operations": ["update"],
            "paths": ["sources/**"], "body": "allow",
        }},
    }), encoding="utf-8")
    state = tmp_path / ".okengine" / "extensions.yaml"
    state.parent.mkdir()

    # Installed but not enabled is no grant. Enabling adds the same document.
    assert policy_file not in p.discover_documents(tmp_path)
    state.write_text(yaml.safe_dump({"enabled": {"demo.policy": {}}}), encoding="utf-8")
    enabled = p.effective_policy(tmp_path)
    assert policy_file in p.discover_documents(tmp_path)
    assert "extension:demo.policy" in enabled["capabilities"]

    # Disable edits only enabled-state; a bare filesystem glob would leave the grant live.
    state.write_text(yaml.safe_dump({"enabled": {}, "disabled": ["demo.policy"]}),
                     encoding="utf-8")
    disabled = p.effective_policy(tmp_path)
    assert policy_file.is_file() and policy_file not in p.discover_documents(tmp_path)
    assert "extension:demo.policy" not in disabled["capabilities"]
    assert enabled["digest"] != disabled["digest"]


def test_policy_composition_fails_on_invalid_enabled_state(tmp_path):
    p = _policy()
    state = tmp_path / ".okengine" / "extensions.yaml"
    state.parent.mkdir()
    state.write_text("enabled: [not-a-map]\n", encoding="utf-8")
    with pytest.raises(p.PolicyError, match="'enabled' must be a mapping"):
        p.discover_documents(tmp_path)


def test_policy_discovery_fails_loud_when_shared_discovery_module_is_missing(
        tmp_path, monkeypatch):
    p = _policy()
    real_is_file = Path.is_file

    def missing_discovery(path):
        if path.name == "extension_discovery.py":
            return False
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", missing_discovery)
    with pytest.raises(p.PolicyError, match="extension discovery unavailable"):
        p.discover_documents(tmp_path)


def test_policy_discovery_uses_runtime_scripts_when_installed_package_has_no_helpers(
        tmp_path, monkeypatch):
    p = _policy()
    runtime = tmp_path / "data"
    scripts = runtime / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "extension_discovery.py").write_text("# runtime helper\n", encoding="utf-8")
    monkeypatch.setenv("OKENGINE_DATA", str(runtime))
    real_is_file = Path.is_file

    def installed_without_source_helpers(path):
        source_helper = Path(p.__file__).resolve().parents[1] / "scripts" / "extension_discovery.py"
        return False if path == source_helper else real_is_file(path)

    monkeypatch.setattr(Path, "is_file", installed_without_source_helpers)
    monkeypatch.setitem(sys.modules, "extension_discovery", type("Discovery", (), {
        "resolve_for_pack": staticmethod(lambda _vault: ({}, [])),
    }))
    assert p.discover_documents(tmp_path) == [p.engine_catalog_path()]


def test_policy_discovery_adds_shared_scripts_path_when_absent(tmp_path):
    p = _policy()
    scripts_dir = str(REPO / "scripts")
    while scripts_dir in sys.path:
        sys.path.remove(scripts_dir)
    sys.modules.pop("extension_discovery", None)
    p.discover_documents(tmp_path)
    assert scripts_dir in sys.path


def test_core_extension_policy_follows_explicit_disable(tmp_path, monkeypatch):
    """Core default-on policy is composed until the operator explicitly disables it."""
    p = _policy()
    # Force lazy discovery to load its module, then substitute a disposable engine root.
    p.discover_documents(tmp_path)
    import extension_discovery

    engine = tmp_path / "engine"
    ext = engine / "extensions" / "okengine.policy-sample"
    ext.mkdir(parents=True)
    (ext / "extension.yaml").write_text(yaml.safe_dump({
        "id": "okengine.policy-sample", "kind": "operation", "version": "0.1.0",
        "name": "Core policy", "core": True, "requires": {"engine": ">=0.3.0"},
        "trust": "in-gateway", "capabilities": {"read": ["wiki/**"],
                                                   "write": ["dashboards/**"]},
    }), encoding="utf-8")
    policy_file = ext / "policy.yaml"
    policy_file.write_text("schema_version: 1\nrules: []\ncapabilities: {}\nwaivers: []\n",
                           encoding="utf-8")
    monkeypatch.setattr(extension_discovery, "ENGINE_ROOT", engine)
    pack = tmp_path / "pack"
    pack.mkdir()
    assert policy_file in p.discover_documents(pack)
    state = pack / ".okengine" / "extensions.yaml"
    state.parent.mkdir()
    state.write_text("enabled: {}\ndisabled: [okengine.policy-sample]\n", encoding="utf-8")
    assert policy_file not in p.discover_documents(pack)


def test_source_quality_actor_exposes_only_dedicated_score_tool(monkeypatch):
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:source-quality-backfill")
    writer = _load("write_server_source_quality_tools", WRITE)
    assert [tool.name for tool in asyncio.run(writer.mcp.list_tools())] == [
        "score_source",
    ]


@pytest.mark.parametrize(("actor", "expected"), [
    ("cron:raw-backfill", {"converge_source"}),
    ("cron:entity-backfill", {"converge_entity"}),
    ("cron:concept-backfill", {"converge_concept"}),
    ("cron:page-quality-enrich",
     {"update_entity", "patch_entity", "append_to_section", "converge_entity"}),
])
def test_backfill_actors_expose_only_contract_operations(monkeypatch, actor, expected):
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    writer = _load("write_server_" + actor.replace(":", "_"), WRITE)
    assert {tool.name for tool in asyncio.run(writer.mcp.list_tools())} == expected


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


def test_policy_digest_is_canonical_for_key_order_and_unicode():
    p = _policy()
    left = {
        "schema_version": 1,
        "rules": [{"id": "r", "description": "café", "z": 2, "a": 1}],
        "capabilities": {"writer": {"paths": ["sources/**"], "body": "deny"}},
        "waivers": [],
    }
    right = {
        "waivers": [],
        "capabilities": {"writer": {"body": "deny", "paths": ["sources/**"]}},
        "rules": [{"a": 1, "z": 2, "description": "café", "id": "r"}],
        "schema_version": 1,
    }
    assert p.policy_digest(left) == p.policy_digest(right)
    assert p.policy_digest(left) != p.policy_digest({**right, "schema_version": 2})


def test_severity_order_is_an_explicit_non_ambiguous_ratchet():
    p = _policy()
    assert p._SEVERITY_RANK == {
        "info": 0, "warning": 1, "review": 2, "reject": 3}
    for lower, higher in zip(
            ("info", "warning", "review"),
            ("warning", "review", "reject"), strict=True):
        assert p._SEVERITY_RANK[lower] < p._SEVERITY_RANK[higher]


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
    # Exercise the real source-quality capability together with its declared
    # scheduler output contract.  Authenticated jobs now fail closed when the
    # contract inventory is absent, so this governed-lane fixture must provide
    # the same inventory that deployment stages for the write server.
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(REPO / "config" / "engine-crons.json"))
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


def test_source_quality_rejects_legacy_source_missing_required_field(
        governed_vault):
    module, _, path = governed_vault
    text = path.read_text()
    path.write_text(text.replace("published: '2026-07-17'\n", "").replace(
        "published: 2026-07-17\n", ""))
    before = path.read_bytes()
    result = module._update("sources/2026/07/example",
                            {"reliability": "B", "credibility": 3}, None)
    assert result == "rejected: type 'source' is missing required field(s): published"
    assert path.read_bytes() == before


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


def test_wildcard_field_capability_allows_contract_governed_candidate_fields():
    p = _policy()
    policy = p.effective_policy(REPO)
    policy["capabilities"]["cron:contracted"] = {
        "rule_id": "engine-authenticated-writer",
        "operations": ["create"],
        "paths": ["briefings/**"],
        "types": ["briefing"],
        "update_fields": ["*"],
        "body": "allow",
    }

    result = p.evaluate_capability(
        policy, "cron:contracted", "create", "briefings/daily-2026-09-10",
        "briefing", ["type", "title", "published"], "replace",
    )

    assert result is None


def test_prompt_contract_and_importer_envelope():
    p = _policy()
    policy = p.effective_policy(REPO)
    prompts = json.loads((REPO / "templates" / "pack" / "skeleton" / "crons" /
                          "engine-template-prompts.json").read_text())
    source_ref = prompts["source-quality-backfill"]["prompt_file"]
    source_prompt = (REPO / "templates/pack/skeleton" / source_ref).read_text()
    assert p.check_prompt(policy, "cron:source-quality-backfill", source_prompt) == []
    bad = p.validate_importer_envelope({"source_native_id": "x"})
    assert bad and bad["rule_id"] == "engine-importer-envelope"


def test_source_quality_job_has_no_bypass_writer_or_file_tool():
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    job = next(item for item in jobs if item["name"] == "source-quality-backfill")
    assert job["enabled_toolsets"] == ["okengine", "okengine-write-source-quality"]
    assert "file" not in job["enabled_toolsets"] and "okengine-write" not in job["enabled_toolsets"]


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


def test_policy_document_validation_remaining_malformed_shapes():
    p=_policy()
    errors=p.validate_document({
        "schema_version":0,"rules":[None,{"id":"","enforcement":["write"],
        "verified_by":["ci"],"severity":"bad","evaluator":"bad","override":"bad",
        "applies_to":[],"owner":"","description":"","remediation":""}],
        "capabilities":"bad","waivers":"bad"},source="x")
    text="\n".join(errors)
    for fragment in ("schema_version","must be a mapping","verified_by lacks","capabilities must",
                     "waivers must","severity","applies_to"):
        assert fragment in text
    errors=p.validate_document({"schema_version":1,"rules":[],"capabilities":{},
                                "waivers":[None,{"rule_id":"x"}]})
    assert any("waivers[0] must" in x for x in errors)
    assert any("waivers[1] missing" in x for x in errors)


def test_policy_composition_override_and_waiver_error_paths(tmp_path):
    p=_policy()
    base=p.load_document(REPO/"config/policy/catalog.yaml")
    rule=dict(base["rules"][0]);rule["override"]="tighten-only"
    first={**base,"rules":[rule],"capabilities":{}}
    a=tmp_path/"a.yaml";a.write_text(yaml.safe_dump(first))
    def variant(**changes):
        row={**rule,**changes};doc={"schema_version":1,"rules":[row],"capabilities":{},"waivers":[]}
        path=tmp_path/(str(len(list(tmp_path.iterdir())))+".yaml");path.write_text(yaml.safe_dump(doc));return path
    with pytest.raises(p.PolicyError,match="changes evaluator"):
        p.compose_documents([a,variant(evaluator="policy-digest")])
    with pytest.raises(p.PolicyError,match="weakens severity"):
        p.compose_documents([a,variant(severity="info")])
    capability=next(iter(base["capabilities"].values()))
    matching={**next(row for row in base["rules"] if row["id"]==capability["rule_id"]),
              "override":"tighten-only"}
    dup={"schema_version":1,"rules":[matching],"capabilities":{"actor":capability},"waivers":[]}
    b=tmp_path/"dup1.yaml";c=tmp_path/"dup2.yaml"
    b.write_text(yaml.safe_dump(dup));c.write_text(yaml.safe_dump(dup))
    with pytest.raises(p.PolicyError,match="duplicate capability"):
        p.compose_documents([b,c])
    unknown={**first,"waivers":[{"rule_id":"absent","owner":"o","reason":"r","scope":"s",
        "created_at":"x","expires_at":"y"}]}
    u=tmp_path/"unknown.yaml";u.write_text(yaml.safe_dump(unknown))
    with pytest.raises(p.PolicyError,match="unknown rule"):
        p.compose_documents([u])


def test_policy_audit_dashboard_prompt_and_cli_edges(tmp_path, monkeypatch, capsys):
    p=_policy();monkeypatch.setenv("OKENGINE_POLICY_CATALOG",str(REPO/"config/policy/catalog.yaml"))
    wiki=tmp_path/"wiki";wiki.mkdir()
    (wiki/"plain.md").write_text("no frontmatter")
    (wiki/"bad.md").write_text("---\n[bad\n---\n")
    events=tmp_path/".okengine/policy-events.jsonl";events.parent.mkdir()
    events.write_text("{bad\n"+json.dumps({"outcome":"pass"})+"\n"+
                      json.dumps({"outcome":"warn","rule_id":"x"})+"\n")
    findings=p.audit(tmp_path,p.effective_policy(tmp_path))
    assert sum(x.get("rule_id")=="engine-page-quality-review" for x in findings)==2
    assert any(x.get("rule_id")=="x" for x in findings)
    policy=p.effective_policy(tmp_path);policy["waivers"]=[
      {"rule_id":"x","reason":"temporary","expires_at":"9999-01-01"},
      {"rule_id":"y","reason":"old","expires_at":"2000-01-01"}]
    p._write_dashboard(tmp_path,policy,p.coverage(policy),[])
    dashboard=(wiki/"operational/policy-health.md").read_text()
    assert "active" in dashboard and "EXPIRED" in dashboard and "No active findings" in dashboard
    assert p.check_prompt(policy,"missing","x")==["no capability for missing"]
    actor=next(iter(policy["capabilities"]))
    assert p.check_prompt(policy,actor,"")
    assert p.main(["validate","--vault",str(tmp_path)])==0
    assert p.main(["digest","--vault",str(tmp_path)])==0
    assert p.main(["materialize","--vault",str(tmp_path)])==0
    monkeypatch.setattr(p,"effective_policy",lambda *_:(_ for _ in ()).throw(p.PolicyError("bad")))
    assert p.main(["validate","--vault",str(tmp_path)])==1
    assert "policy invalid" in capsys.readouterr().err


def test_policy_helpers_preserve_exact_boundaries_and_artifacts(tmp_path, monkeypatch, capsys):
    p = _policy()

    # A lexical prefix is not a scope match; only a complete path component is.
    assert p._path_in_scopes("sources/item.md", ["wiki/sources/**"])
    assert not p._path_in_scopes("sources/item.mdx", ["wiki/sources/item"])
    assert not p._path_in_scopes("sources-private/item.md", ["sources/**"])
    assert p._path_in_scopes("entities/a/acme.md", ["capability:wiki/entities/**"])

    event = {"outcome": "warn", "message": "café", "rule_id": "r"}
    p.append_event(tmp_path, event)
    event_path = tmp_path / ".okengine" / "policy-events.jsonl"
    assert event_path.read_text(encoding="utf-8") == (
        json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")

    target = tmp_path / "state" / "value.json"
    p._atomic_json(target, {"z": "café", "a": 1})
    assert target.read_text(encoding="utf-8") == '{\n  "a": 1,\n  "z": "café"\n}\n'
    assert not target.with_suffix(".json.tmp").exists()

    policy = {
        "digest": "d" * 64,
        "rules": [{"id": "r", "owner": "o", "enforcement": ["write"],
                   "verified_by": ["write"]}],
        "waivers": [
            {"rule_id": "r", "reason": "live", "expires_at": "9999-01-01"},
            {"rule_id": "r", "reason": "old", "expires_at": "2000-01-01"},
        ],
    }
    findings = [{"outcome": "warn", "rule_id": "r", "subject": "a|b",
                 "message": "m|n"}]
    p._write_dashboard(tmp_path, policy, p.coverage(policy), findings)
    dashboard = (tmp_path / "wiki" / "operational" / "policy-health.md").read_text()
    assert "Rules: **1** · fully covered: **1** · findings: **1** · waivers: **2**" in dashboard
    assert "| warn | `r` | `a|b` | m\\|n |" in dashboard
    assert "`r` — active; live" in dashboard
    assert "`r` — EXPIRED; old" in dashboard

    monkeypatch.setattr(p, "materialize", lambda vault, *, run_audit: {
        "vault": str(vault), "run_audit": run_audit})
    assert p.main(["audit", "--vault", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["run_audit"] is True


def test_materialize_default_and_dashboard_gap_are_not_boolean_invertible(
        tmp_path, monkeypatch):
    """Pin two policy-plane booleans that survived the authoritative campaign."""
    p = _policy()
    policy = {
        "digest": "d" * 64,
        "rules": [{
            "id": "gap", "owner": "o", "enforcement": ["write"],
            "verified_by": [],
        }],
        "capabilities": {},
        "waivers": [],
    }
    monkeypatch.setattr(p, "effective_policy", lambda _vault: policy)
    monkeypatch.setattr(
        p, "audit",
        lambda *_args, **_kwargs: pytest.fail(
            "materialize() must not audit unless run_audit=True"),
    )

    result = p.materialize(tmp_path)
    assert result == {"digest": "d" * 64, "rules": 1, "findings": 0}
    assert not (tmp_path / ".okengine" / "policy-findings.json").exists()
    assert not (tmp_path / "wiki" / "operational" / "policy-health.md").exists()

    cov = p.coverage(policy)
    assert cov["rules"][0]["covered"] is False
    p._write_dashboard(tmp_path, policy, cov, [])
    dashboard = (tmp_path / "wiki" / "operational" / "policy-health.md").read_text()
    assert "| `gap` | write | none | gap |" in dashboard
    assert "| `gap` | write | none | covered |" not in dashboard


def test_capability_rejection_reports_every_independent_boundary():
    p = _policy()
    capability = {
        "rule_id": "r", "operations": ["update"], "paths": ["sources/**"],
        "types": ["source"], "update_fields": ["title", "required"],
        "required_fields": ["required"], "protected_fields": ["id"],
        "body": "append-only",
    }
    policy = {
        "rules": [{"id": "r", "severity": "reject", "remediation": "narrow it"}],
        "capabilities": {"writer": capability},
    }
    result = p.evaluate_capability(
        policy, "writer", "create", "entities/a/acme", "actor",
        ["id", "extra"], "replace")
    assert result is not None
    assert result["message"].split("; ") == [
        "operation 'create' is not allowed",
        "path is outside allowed scopes",
        "page type 'actor' is not allowed",
        "protected fields would change",
        "fields exceed the update allowlist",
        "required candidate fields are missing",
        "body replacement exceeds append-only authority",
    ]
    assert result["remediation"] == "narrow it"
    assert result["evidence"] == {
        "offending_fields": ["extra", "id"],
        "missing_fields": ["required"],
        "allowed_operations": ["update"],
        "allowed_paths": ["sources/**"],
        "allowed_types": ["source"],
        "allowed_fields": ["title", "required"],
        "required_fields": ["required"],
        "body": "append-only",
    }


def test_append_only_body_mode_uses_value_equality_not_string_identity():
    p = _policy()
    capability = {
        "rule_id": "r", "operations": ["update"], "paths": ["**"],
        "types": [], "update_fields": [], "required_fields": [],
        "protected_fields": [], "body": "append-only",
    }
    policy = {"rules": [{"id": "r", "severity": "reject"}],
              "capabilities": {"writer": capability}}
    dynamically_built_replace = "".join(["re", "place"])
    result = p.evaluate_capability(
        policy, "writer", "update", "x", body_change=dynamically_built_replace)
    assert result is not None
    assert result["message"] == "body replacement exceeds append-only authority"


def test_source_audit_requires_both_source_namespace_and_exact_source_type(tmp_path):
    p = _policy()
    wiki = tmp_path / "wiki" / "sources"
    wiki.mkdir(parents=True)
    (wiki / "actor.md").write_text("---\ntype: actor\n---\n")
    (tmp_path / "wiki" / "elsewhere").mkdir()
    (tmp_path / "wiki" / "elsewhere" / "source.md").write_text(
        "---\ntype: source\n---\n")
    assert p.audit(tmp_path, {}) == []


def test_prompt_contract_checks_each_field_and_body_instruction():
    p = _policy()
    policy = {"capabilities": {"writer": {
        "update_fields": ["alpha", "beta"], "body": "deny"}}}
    assert p.check_prompt(policy, "writer", "alpha") == [
        "prompt does not name allowed field beta",
        "prompt does not state NO body for body-denied capability",
    ]
    assert p.check_prompt(policy, "writer", "alpha beta NO body") == []


def test_cli_default_vault_honors_runtime_environment(monkeypatch, tmp_path, capsys):
    p = _policy()
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    observed = []
    monkeypatch.setattr(p, "effective_policy", lambda vault: (
        observed.append(vault) or {"digest": "d", "rules": []}))
    assert p.main(["validate"]) == 0
    assert observed == [tmp_path]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "digest": "d", "rules": 0}


def test_policy_last_small_branch_edges(tmp_path):
    p=_policy()
    rule={
      "id":"x","owner":"o","description":"d","severity":"warning","applies_to":{},
      "enforcement":"bad","evaluator":"field-capability","remediation":"r",
      "override":"waivable","verified_by":["write"]}
    assert any("enforcement must" in e for e in p.validate_document(
      {"schema_version":1,"rules":[rule],"capabilities":{},"waivers":[]}))
    invalid=tmp_path/"invalid.yaml";invalid.write_text("schema_version: 0\nrules: []\n")
    with pytest.raises(p.PolicyError):
        p.compose_documents([invalid])
    policy={"rules":[rule],"capabilities":{"a":{
      "rule_id":"x","operations":["update"],"paths":["**"],"types":[],
      "update_fields":[],"required_fields":[],"protected_fields":[],"body":"append-only"}}}
    result=p.evaluate_capability(policy,"a","update","x","",[],"replace")
    assert "append-only" in result["message"]
    wiki=tmp_path/"wiki/sources";wiki.mkdir(parents=True)
    (wiki/"complete.md").write_text("---\ntype: source\npublisher: P\npublished: 2026-01-01\n---\n")
    assert p.audit(tmp_path,policy)==[]
    waiver={"rule_id":"x","owner":"o","reason":"r","scope":"s",
            "created_at":"2026-01-01","expires_at":"2027-01-01"}
    valid_rule={**rule,"enforcement":["write"]}
    doc={"schema_version":1,"rules":[valid_rule],"capabilities":{},"waivers":[waiver,dict(waiver)]}
    path=tmp_path/"waivable.yaml";path.write_text(yaml.safe_dump(doc))
    assert len(p.compose_documents([path])["waivers"])==2
