"""Behavioral contracts for tools/policy_plane.py that its critical mutation campaign showed
were unpinned (okengine#612): document validation and composition edges, digest semantics,
capability evaluation for append bodies and rule lookup, audit scoping, waiver status, and
CLI dispatch."""
from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
POLICY = REPO / "tools" / "policy_plane.py"


def _load(name: str = "policy_plane_mutation"):
    spec = importlib.util.spec_from_file_location(name, POLICY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pp = _load()


def _rule(rule_id: str, **overrides) -> dict:
    rule = {
        "id": rule_id, "owner": "engine", "description": f"{rule_id} rule",
        "severity": "warning", "applies_to": {}, "enforcement": ["write"],
        "evaluator": "field-capability", "remediation": f"fix {rule_id}",
        "override": "tighten-only", "verified_by": ["write"],
    }
    rule.update(overrides)
    return rule


def _document(*rules: dict, **extra) -> dict:
    return {"schema_version": 1, "rules": list(rules), **extra}


def _write(path: Path, document: dict) -> Path:
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _capability(**overrides) -> dict:
    capability = {"rule_id": "cap", "operations": ["append", "update"], "paths": ["**"],
                  "update_fields": ["*"], "body": "append-only"}
    capability.update(overrides)
    return capability


def _policy(capability: dict, *rules: dict) -> dict:
    return {"rules": list(rules) or [_rule("cap")], "capabilities": {"actor": capability}}


# --- documents ---------------------------------------------------------------------------------

def test_malformed_yaml_is_a_policy_error(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("rules: [unclosed\n", encoding="utf-8")
    with pytest.raises(pp.PolicyError, match="cannot load policy"):
        pp.load_document(path)


def test_validate_document_source_is_keyword_only():
    parameters = inspect.signature(pp.validate_document).parameters
    assert parameters["source"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["document"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_extra_rule_keys_are_not_reported_missing():
    assert pp.validate_document(_document(_rule("r", notes="free text"))) == []


def test_capabilities_are_validated_against_the_documents_rules():
    errors = pp.validate_document(_document(
        _rule("r"), capabilities={"actor": {"rule_id": "elsewhere", "body": "deny"}}))
    assert errors == ["capability actor: unknown rule_id 'elsewhere'"]


# --- composition and digest --------------------------------------------------------------------

def test_a_later_document_may_strengthen_a_rules_severity(tmp_path):
    base = _write(tmp_path / "base.yaml", _document(_rule("r", severity="warning")))
    tighter = _write(tmp_path / "tighter.yaml", _document(_rule("r", severity="reject")))
    composed = pp.compose_documents([base, tighter])
    assert composed["rules"][0]["severity"] == "reject"


def test_digest_reflects_rule_content():
    first = {"schema_version": 1, "rules": [_rule("r")], "capabilities": {}, "waivers": []}
    second = {**first, "rules": [_rule("r", description="changed")]}
    assert pp.policy_digest(first) != pp.policy_digest(second)


def test_effective_policy_without_a_vault_uses_wiki_path(tmp_path, monkeypatch):
    discover = create_autospec(pp.discover_documents, return_value=[])
    monkeypatch.setattr(pp, "discover_documents", discover)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    pp.effective_policy()
    assert discover.call_args.args[0] == tmp_path


# --- capability evaluation ---------------------------------------------------------------------

def test_md_suffix_is_stripped_before_exact_scope_match():
    assert pp._path_in_scopes("entities/acme.md", ["entities/acme"]) is True
    assert pp._path_in_scopes("entities/acme.md", ["wiki/entities/acme"]) is True


def test_finding_cites_the_capabilitys_own_rule_not_a_neighbor():
    rules = (_rule("z-rule", remediation="z remedy"), _rule("a-rule", remediation="a remedy"))
    result = pp.evaluate_capability(_policy(_capability(rule_id="a-rule"), *rules), "actor",
                                    "patch", "entities/x", body_change="none")
    assert result["rule_id"] == "a-rule"
    assert result["remediation"] == "a remedy"


def test_append_body_is_denied_for_a_body_deny_capability():
    result = pp.evaluate_capability(_policy(_capability(body="deny")), "actor", "append",
                                    "entities/x", body_change="append")
    assert result is not None and "body mutation is denied" in result["message"]


def test_append_body_is_allowed_for_an_append_only_capability():
    assert pp.evaluate_capability(_policy(_capability()), "actor", "append", "entities/x",
                                  body_change="append") is None


def test_incomplete_envelope_without_identity_names_an_unknown_subject():
    result = pp.validate_importer_envelope({"connector_id": "c"})
    assert result["subject"] == "<unknown>"
    present = pp.validate_importer_envelope({"source_native_id": "n-1"})
    assert present["subject"] == "n-1"


def test_coverage_report_schema_version_is_one():
    assert pp.coverage({"rules": [], "digest": "d"})["schema_version"] == 1


# --- audit -------------------------------------------------------------------------------------

def test_a_type_scoped_rule_skips_pages_of_another_type():
    rule = _rule("meta", evaluator="source-metadata-completeness",
                 applies_to={"type": "aaa"}, enforcement=["audit"], verified_by=["audit"])
    assert pp._audit_rule_page(rule, "sources/x.md", {"type": "zzz"}, {}) is None
    assert pp._audit_rule_page(rule, "sources/x.md", {"type": "aaa"}, {}) is not None


def test_audit_replays_only_the_last_200_policy_events(tmp_path):
    (tmp_path / ".okengine").mkdir()
    events = [json.dumps({"outcome": "reject", "n": index}) for index in range(201)]
    (tmp_path / ".okengine/policy-events.jsonl").write_text("\n".join(events) + "\n")
    replayed = pp.audit(tmp_path, {"rules": []})
    assert [event["n"] for event in replayed] == list(range(1, 201))


def test_atomic_json_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "a/b/c.json"
    pp._atomic_json(target, {"k": 1})
    assert json.loads(target.read_text()) == {"k": 1}


def test_a_waiver_expiring_this_second_is_still_active(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "utc_now", create_autospec(pp.utc_now,
                                                       return_value="2026-10-01T00:00:00Z"))
    policy = {"digest": "d", "rules": [], "waivers": [
        {"rule_id": "r", "reason": "why", "expires_at": "2026-10-01T00:00:00Z"}]}
    pp._write_dashboard(tmp_path, policy, {"rules": []}, [])
    text = (tmp_path / "wiki/operational/policy-health.md").read_text()
    assert "`r` — active; why" in text


# --- CLI ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("command", "run_audit"), [("materialize", False), ("audit", True)])
def test_materialize_and_audit_dispatch_to_materialize(tmp_path, monkeypatch, capsys,
                                                       command, run_audit):
    materialize = create_autospec(pp.materialize, return_value={"digest": "d"})
    digest = create_autospec(pp.effective_policy, return_value={"digest": "d", "rules": []})
    monkeypatch.setattr(pp, "materialize", materialize)
    monkeypatch.setattr(pp, "effective_policy", digest)
    assert pp.main([_fresh(command), "--vault", str(tmp_path)]) == 0
    materialize.assert_called_once_with(tmp_path, run_audit=run_audit)
    digest.assert_not_called()
    assert json.loads(capsys.readouterr().out) == {"digest": "d"}


def test_the_cli_runs_main_when_executed_as_a_script():
    completed = subprocess.run([sys.executable, str(POLICY), "not-a-command"],
                               capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 2
    assert "invalid choice" in completed.stderr


def test_importing_under_any_module_name_does_not_run_the_cli():
    assert callable(_load("Policy").main)


# --- second tranche: the rest of the measured survivors ----------------------------------------

def _fresh(text: str) -> str:
    """A runtime-built str, as YAML, JSON and argv deliver them: equal, never identical."""
    return "".join(list(text))


def test_a_missing_policy_file_is_a_policy_error(tmp_path):
    with pytest.raises(pp.PolicyError, match="cannot load policy"):
        pp.load_document(tmp_path / "absent.yaml")


@pytest.mark.parametrize(("function", "keyword_only"), [
    (pp.finding, {"enforcement_point", "evaluated_at"}),
    (pp.validate_importer_envelope, {"actor"}),
    (pp.materialize, {"run_audit"}),
])
def test_trailing_options_are_keyword_only(function, keyword_only):
    parameters = inspect.signature(function).parameters.values()
    assert {p.name for p in parameters if p.kind is p.KEYWORD_ONLY} == keyword_only
    assert all(p.kind is p.POSITIONAL_OR_KEYWORD for p in parameters
               if p.name not in keyword_only)


def test_capability_list_fields_must_be_string_lists():
    errors = pp.validate_capability("actor", {"rule_id": "r", "paths": "entities/**"})
    assert errors == ["capability actor.paths: must be a list of non-empty strings"]


def test_an_empty_capability_rule_id_is_refused():
    assert pp.validate_capability("actor", {"rule_id": ""}) == [
        "capability actor.rule_id: required stable rule ID"]


def test_schema_version_is_compared_numerically():
    assert pp.validate_document({"schema_version": 1.0, "rules": [_rule("r")]}) == []


def test_verification_beyond_declared_enforcement_is_not_an_error():
    assert pp.validate_document(_document(_rule("r", verified_by=["write", "ci"]))) == []


def test_a_whitespace_only_rule_description_is_refused():
    errors = pp.validate_document(_document(_rule("r", description="   ")))
    assert errors == ["policy: rules[0].description must be a non-empty string"]


def test_extra_waiver_keys_are_not_reported_missing():
    waiver = {"rule_id": "r", "owner": "o", "reason": "why", "scope": "all",
              "created_at": "2026-01-01", "expires_at": "2027-01-01", "ticket": "#1"}
    assert pp.validate_document(_document(_rule("r"), waivers=[waiver])) == []


@pytest.mark.parametrize(("page", "scope"), [
    ("entities/a:b.md", "write:entities/a:b"),
    ("entities/acme.md", "entities/acme/"),
    ("entities/acme/report.md", "entities/acme"),
    ("entities/acme/report.md", "entities/acme/"),
])
def test_scope_matching_by_prefix_directory_and_exact_page(page, scope):
    assert pp._path_in_scopes(_fresh(page), [_fresh(scope)]) is True


def test_admin_bypass_matches_a_runtime_built_actor_name():
    assert pp.evaluate_capability({"capabilities": {}}, _fresh("admin"), "update", "x") is None


@pytest.mark.parametrize("ids", [("z-rule", "m-rule"), ("a-rule", "m-rule")])
def test_finding_cites_its_rule_whatever_sorts_around_it(ids):
    rules = [_rule(_fresh(rule_id), remediation=f"{rule_id} remedy") for rule_id in ids]
    capability = _capability(rule_id=_fresh("m-rule"))
    result = pp.evaluate_capability(_policy(capability, *rules), "actor", "patch",
                                    "entities/x")
    assert result["remediation"] == "m-rule remedy"


def test_body_replacement_under_deny_reports_only_the_denial():
    result = pp.evaluate_capability(_policy(_capability(body="deny")), "actor", "update",
                                    "entities/x", body_change="replace")
    assert result["message"] == "body mutation is denied"


def test_an_audit_rule_without_remediation_uses_the_default_advice():
    rule = _rule("meta", evaluator="source-metadata-completeness", remediation="")
    result = pp._audit_rule_page(rule, "sources/x.md", {}, {})
    assert result["remediation"] == "Repair the page and rerun the policy audit."


def test_a_namespace_scoped_rule_skips_pages_in_another_namespace():
    rule = _rule("meta", evaluator="source-metadata-completeness",
                 applies_to={"namespace": "aaa"})
    assert pp._audit_rule_page(rule, "zzz/x.md", {}, {}) is None
    assert pp._audit_rule_page(rule, "aaa/x.md", {}, {}) is not None


def test_only_the_strict_type_evaluator_judges_page_types():
    rule = _rule("cap", evaluator="field-capability")
    assert pp._audit_rule_page(rule, "entities/x.md", {"type": "undeclared"}, {}) is None
    assert pp._audit_rule_page(rule, "entities/x.md", {}, {}) is None


def test_a_type_outside_its_home_namespace_is_reported(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "scripts" / "cron"))
    rule = _rule("strict", evaluator="strict-type-namespace")
    schema = {"types": {"widget": {}}, "type_namespaces": {"widget": "concepts"}}
    result = pp._audit_rule_page(rule, "entities/x.md", {"type": "widget"}, schema)
    assert result is not None
    assert result["evidence"]["expected_namespace"] == "concepts"


@pytest.mark.parametrize(("evaluator", "loads"), [
    ("strict-type-namespace", True),
    ("source-metadata-completeness", False),
])
def test_audit_loads_the_schema_only_for_strict_type_rules(tmp_path, monkeypatch,
                                                           evaluator, loads):
    load = create_autospec(pp._audit_schema, return_value={})
    monkeypatch.setattr(pp, "_audit_schema", load)
    rule = _rule("r", evaluator=_fresh(evaluator), enforcement=["audit"])
    pp.audit(tmp_path, {"rules": [rule]})
    assert load.call_count == (1 if loads else 0)


def test_schema_loading_adds_each_existing_source_once(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path
                                      if p != str(REPO / "scripts" / "cron")])
    pp._audit_schema(tmp_path)
    pp._audit_schema(tmp_path)
    assert sys.path.count(str(REPO / "scripts" / "cron")) == 1
    assert "/opt/hermes/scripts/cron" not in sys.path or Path(
        "/opt/hermes/scripts/cron").is_dir()


def test_materialized_findings_are_versioned_and_dashboard_lists_the_last_100(
        tmp_path, monkeypatch):
    policy = {"digest": "d", "rules": [], "waivers": []}
    monkeypatch.setattr(pp, "effective_policy",
                        create_autospec(pp.effective_policy, return_value=policy))
    findings = [{"outcome": "warn", "rule_id": "r", "subject": f"s{index:03d}",
                 "message": "m"} for index in range(101)]
    monkeypatch.setattr(pp, "audit", create_autospec(pp.audit, return_value=findings))
    pp.materialize(tmp_path, run_audit=True)
    stored = json.loads((tmp_path / ".okengine/policy-findings.json").read_text())
    assert stored["schema_version"] == 1
    dashboard = (tmp_path / "wiki/operational/policy-health.md").read_text()
    rows = [line for line in dashboard.splitlines() if line.startswith("| warn |")]
    assert len(rows) == 100
    assert "`s000`" not in dashboard and "`s001`" in dashboard and "`s100`" in dashboard


def test_prompt_check_requires_no_body_wording_only_for_body_denied_capabilities():
    policy = {"capabilities": {
        "denied": {"update_fields": ["title"], "body": _fresh("deny")},
        "allowed": {"update_fields": ["title"], "body": "allow"},
    }}
    assert pp.check_prompt(policy, "denied", "set title") == [
        "prompt does not state NO body for body-denied capability"]
    assert pp.check_prompt(policy, "allowed", "set title") == []


def test_validate_from_runtime_argv_validates(tmp_path, monkeypatch, capsys):
    validate = create_autospec(pp.effective_policy, return_value={"digest": "d", "rules": []})
    materialize = create_autospec(pp.materialize, return_value={})
    monkeypatch.setattr(pp, "effective_policy", validate)
    monkeypatch.setattr(pp, "materialize", materialize)
    assert pp.main([_fresh("validate"), "--vault", str(tmp_path)]) == 0
    materialize.assert_not_called()
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_materialize_result_is_printed_with_sorted_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pp, "materialize", create_autospec(
        pp.materialize, return_value={"rules": 2, "digest": "d", "findings": 0}))
    assert pp.main(["materialize", "--vault", str(tmp_path)]) == 0
    assert capsys.readouterr().out == '{"digest": "d", "findings": 0, "rules": 2}\n'


def test_unknown_capability_operations_are_refused():
    errors = pp.validate_capability("actor", {"rule_id": "r", "operations": ["fly"]})
    assert errors == ["capability actor.operations: unknown operations ['fly']"]


@pytest.mark.parametrize("version", [0, 2])
def test_any_other_schema_version_is_refused(version):
    assert pp.validate_document({"schema_version": version, "rules": []}) == [
        "policy: schema_version must be 1"]


def test_an_empty_rule_id_is_refused():
    errors = pp.validate_document(_document(_rule("")))
    assert errors == ["policy: rules[0].id must be a non-empty string"]


def test_composition_refuses_any_evaluator_change(tmp_path):
    base = _write(tmp_path / "base.yaml",
                  _document(_rule("r", evaluator="strict-type-namespace")))
    other = _write(tmp_path / "other.yaml", _document(_rule("r", evaluator="field-capability")))
    with pytest.raises(pp.PolicyError, match="changes evaluator"):
        pp.compose_documents([base, other])


def test_no_body_change_is_never_a_body_denial():
    capability = _capability(body="deny", operations=["update"])
    assert pp.evaluate_capability(_policy(capability), "actor", "update", "entities/x",
                                  body_change=_fresh("none")) is None


def test_policy_events_append_into_a_fresh_vault_repeatedly(tmp_path):
    vault = tmp_path / "new/vault"
    pp.append_event(vault, {"n": 1})
    pp.append_event(vault, {"n": 2})
    lines = (vault / ".okengine/policy-events.jsonl").read_text().splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_the_dashboard_can_be_rewritten(tmp_path):
    policy = {"digest": "d", "rules": [], "waivers": []}
    pp._write_dashboard(tmp_path, policy, {"rules": []}, [])
    pp._write_dashboard(tmp_path, policy, {"rules": []}, [])
    assert "No waivers declared." in (tmp_path / "wiki/operational/policy-health.md").read_text()


def test_digest_from_runtime_argv_prints_only_the_digest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pp, "effective_policy", create_autospec(
        pp.effective_policy, return_value={"digest": "abc", "rules": []}))
    monkeypatch.setattr(pp, "materialize", create_autospec(pp.materialize, return_value={}))
    assert pp.main([_fresh("digest"), "--vault", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "abc\n"
