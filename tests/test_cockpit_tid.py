from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml


pytest.importorskip("fastapi")
REPO = Path(__file__).resolve().parents[1]
APP = REPO / "okengine-cockpit" / "app.py"
CSS = (REPO / "okengine-cockpit" / "static" / "style.css").read_text()


def _load(vault: Path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_tid_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_tid_app", APP)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_tid_app"] = module
    spec.loader.exec_module(module)
    return module


def _page(root: Path, path: str, frontmatter: dict):
    target = root / "wiki" / f"{path}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\nBody.\n")


def _application(root: Path):
    roles = {
        "threat_procedure": [{"type": "threat-procedure", "namespace": "procedures"}],
        "detection_requirement": [{"type": "detection-requirement", "namespace": "detection-requirements"}],
        "detection_strategy": [{"type": "detection-strategy", "namespace": "detection-strategies"}],
        "detection_analytic": [{"type": "detection-analytic", "namespace": "detection-analytics"}],
        "deployment_snapshot": [{"type": "deployment-snapshot", "namespace": "deployment-snapshots"}],
        "validation_result": [{"type": "validation-result", "namespace": "validation-results"}],
        "coverage_assessment": [{"type": "coverage-assessment", "namespace": "coverage-assessments"}],
    }
    target = root / ".okengine" / "application.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump({"profile": "threat-informed-detection", "profile_version": "0.1.0", "bindings": {"propositions": [], "roles": roles}}, sort_keys=False))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    config = {
        "cockpit": {"tabs": ["tid"], "tab_defs": {"tid": {"label": "Detection", "boxes": [
            {"title": "Threat-to-defense trace", "span": 12, "view": "tid-trace", "empty": "No procedures imported."},
            {"title": "Actor defensive posture", "span": 12, "view": "tid-actor-posture", "empty": "No actor posture yet."},
            {"title": "Coverage facets", "span": 12, "view": "tid-facet-matrix", "empty": "No coverage assessments yet."},
            {"title": "Detection dossier", "span": 12, "view": "tid-detection-dossier"},
            {"title": "Validation queue", "span": 12, "view": "tid-validation-queue"},
            {"title": "Gap workbench", "span": 12, "view": "tid-gap-workbench"},
        ]}}}
    }
    (tmp_path / "schema.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    _application(tmp_path)
    _page(tmp_path, "sources/report", {"type": "source", "title": "Source report"})
    _page(tmp_path, "entities/s/h/shinyhunters", {"type": "actor", "title": "ShinyHunters"})
    _page(tmp_path, "procedures/voice", {"type": "threat-procedure", "id": "threat-procedure:voice", "title": "Voice phishing", "actor_ref": "entities/s/h/shinyhunters", "sources": ["sources/report"]})
    _page(tmp_path, "procedures/unassessed", {"type": "threat-procedure", "id": "threat-procedure:unassessed", "title": "Unassessed procedure", "actor_ref": "entities/s/h/shinyhunters", "sources": ["sources/report"]})
    _page(tmp_path, "detection-requirements/voice", {"type": "detection-requirement", "id": "requirement:voice", "title": "Voice requirement", "procedure_ref": "threat-procedure:voice", "status": "active"})
    _page(tmp_path, "detection-strategies/voice", {"type": "detection-strategy", "id": "strategy:voice", "title": "Voice strategy", "requirement_ref": "requirement:voice", "status": "approved"})
    _page(tmp_path, "detection-analytics/voice", {"type": "detection-analytic", "id": "analytic:voice", "title": "Voice analytic", "strategy_ref": "strategy:voice", "status": "active", "revision": "git:1111111111111111111111111111111111111111", "repository": "detection-rules", "repository_path": "rules/voice.yml", "dependencies": ["identity telemetry"]})
    _page(tmp_path, "deployment-snapshots/voice", {"type": "deployment-snapshot", "id": "deployment:voice", "analytic_ref": "analytic:voice", "analytic_revision": "git:1111111111111111111111111111111111111111", "enabled": True, "captured_at": "2026-07-19T13:10:00Z"})
    _page(tmp_path, "validation-results/voice", {"type": "validation-result", "id": "validation:voice", "analytic_ref": "analytic:voice", "analytic_revision": "git:0000000000000000000000000000000000000000", "title": "Voice validation", "result": "passed", "executed_at": "2026-07-19T13:20:00Z", "valid_until": "2026-07-19T13:21:00Z", "limitations": ["Synthetic identity only"]})
    facets = [{"facet": name, "state": "covered", "reason": f"{name} supported", "evidence_refs": [], "as_of": "2026-07-19T13:30:00Z"} for name in (
        "relevance", "observable-requirements", "telemetry-source", "telemetry-fields", "strategy", "repository-analytic", "deployed-revision", "current-validation", "signal-path")]
    facets.append({"facet": "operational-effectiveness", "state": "unknown", "reason": "No production outcome", "evidence_refs": [], "as_of": "2026-07-19T13:30:00Z"})
    _page(tmp_path, "coverage-assessments/voice-old", {"type": "coverage-assessment", "id": "coverage:voice:old", "title": "Voice old", "procedure_ref": "threat-procedure:voice", "status": "superseded", "assessed_value": "partial", "as_of": "2026-07-18T13:30:00Z", "facets": facets})
    _page(tmp_path, "coverage-assessments/voice", {"type": "coverage-assessment", "id": "coverage:voice", "title": "Voice current", "procedure_ref": "threat-procedure:voice", "status": "active", "assessed_value": "validated", "as_of": "2026-07-19T13:30:00Z", "facets": facets})
    _page(tmp_path, "defensive-outcomes/voice", {"type": "defensive-outcome", "id": "outcome:voice", "title": "Voice outcome", "validation_ref": "validation:voice", "effectiveness_state": "improved", "measured_at": "2026-07-19T13:40:00Z"})
    for number, status in enumerate(("proposed", "approved", "rejected", "deferred", "accepted-risk", "completed", "validated"), 1):
        gap_id = f"detection-gap:{status}"
        _page(tmp_path, f"detection-gaps/{status}", {"type": "detection-gap", "id": gap_id, "title": f"{status.title()} gap", "analytic_ref": "analytic:voice", "status": status, "priority": "critical" if status == "proposed" else "medium", "rationale": f"{status} rationale", "as_of": f"2026-07-{number + 10:02d}T12:00:00Z"})
        _page(tmp_path, f"defensive-decisions/{status}", {"type": "defensive-decision", "id": f"decision:{status}", "title": f"{status.title()} decision", "gap_ref": gap_id, "status": "approved", "decision": status, "rationale": f"Decision for {status}", "alternatives": ["Alternative control"], "owner": "analyst@example.test", "decided_at": f"2026-07-{number + 10:02d}T13:00:00Z", "expected_version": number, "needs_review": status == "proposed", "review_state": "pending" if status == "proposed" else "approved", "expires_at": "2026-07-18T00:00:00Z" if status in {"deferred", "accepted-risk"} else None})
    return _load(tmp_path, monkeypatch)


def test_tid_tab_renders_trace_posture_and_full_facet_matrix(vault):
    result = vault.api_tab("tid")
    boxes = {box["title"]: box for box in result["boxes"]}
    assert set(boxes) == {"Threat-to-defense trace", "Actor defensive posture", "Coverage facets",
                          "Detection dossier", "Validation queue", "Gap workbench"}
    trace = boxes["Threat-to-defense trace"]["html"]
    for stage in ("Source evidence", "Procedure", "Requirement", "Strategy", "Analytic", "Deployment", "Validation", "Coverage", "Outcome"):
        assert stage in trace
    assert "git:1111111111111111111111111111111111111111" in trace
    assert 'data-page="entities/s/h/shinyhunters"' in trace
    assert 'data-page="defensive-outcomes/voice"' in trace

    posture = boxes["Actor defensive posture"]["html"]
    assert "shinyhunters" in posture and "Validated" in posture
    assert ">2<" in posture  # current + unassessed procedure belong to the actor

    matrix = boxes["Coverage facets"]["html"]
    assert "Operational Effectiveness" in matrix
    assert "Not assessed" not in matrix
    assert "Unassessed" in matrix and "Unknown" in matrix
    assert "superseded · 2026-07-18T13:30:00Z" in matrix
    assert 'data-page="coverage-assessments/voice"' in matrix
    assert 'title="No production outcome"' in matrix


def test_incomplete_chain_is_explicit_instead_of_blank(vault):
    trace = next(box for box in vault.api_tab("tid")["boxes"] if box["title"] == "Threat-to-defense trace")["html"]
    unassessed = trace[trace.index("Unassessed procedure"):]
    assert "Not assessed" in unassessed
    assert "Unknown" in unassessed


def test_tid_layout_uses_full_cell_width_and_responsive_stacking():
    assert ".tid-facet-link .tid-state{width:100%" in CSS
    assert ".tid-node-value{width:100%" in CSS
    assert re.search(r"@media\(max-width:760px\).*\.tid-trace-chain\{flex-direction:column", CSS)
    tid_css = "\n".join(line for line in CSS.splitlines() if ".tid-" in line)
    assert "width:50%" not in tid_css


def test_application_loader_exposes_role_bindings(vault):
    application = vault.cockpit_config()["application"]
    assert application["profile"] == "threat-informed-detection"
    assert application["roles"]["coverage_assessment"][0]["namespace"] == "coverage-assessments"


def test_dossier_queue_and_gap_workbench_are_deterministic_and_complete(vault):
    boxes = {box["title"]: box["html"] for box in vault.api_tab("tid")["boxes"]}
    dossier = boxes["Detection dossier"]
    assert "rules/voice.yml" in dossier
    assert "git:1111111111111111111111111111111111111111" in dossier
    assert "git:0000000000000000000000000000000000000000" in dossier
    assert "Synthetic identity only" in dossier

    queue = boxes["Validation queue"]
    assert "critical consequence" in queue
    assert "analytic revision changed" in queue
    assert "validation stale" in queue
    assert "review pending" in queue

    workbench = boxes["Gap workbench"]
    for state in ("Proposed", "Approved", "Rejected", "Deferred", "Accepted Risk", "Completed", "Validated"):
        assert state in workbench
    assert "Alternative control" in workbench
    assert "analyst@example.test" in workbench
    assert "expired" in workbench
    assert ">1<" in workbench


def test_unknown_gap_priority_is_visible_and_ranked_for_attention(vault):
    _page(vault.VAULT, "detection-gaps/urgent", {
        "type": "detection-gap", "id": "detection-gap:urgent",
        "title": "Urgent vocabulary drift", "analytic_ref": "analytic:voice",
        "status": "proposed", "priority": "urgent",
    })

    queue = next(
        box["html"] for box in vault.api_tab("tid")["boxes"]
        if box["title"] == "Validation queue"
    )

    assert "unknown consequence" in queue
    assert "unknown priority value(s): urgent — schema/producer drift" in queue
    assert ">620<" in queue, "unknown vocabulary must rank above critical, never below low"


def test_tid_boundary_helpers_keep_incomplete_and_malformed_data_explicit(vault, monkeypatch):
    assert vault._tid_path(None) == ""
    assert vault._tid_ref("[[wiki/entities/a.md#identity|Actor]]") == "entities/a"
    assert "Unknown" in vault._tid_state("not-a-real-state")
    assert "Unknown" in vault._tid_validation_history([])
    assert vault._tid_expired("not-a-timestamp") is False
    assert vault._tid_priority([]) == ("low", 100, None)
    assert vault._tid_priority([{"priority": "HIGH"}]) == ("high", 300, None)
    priority, score, reason = vault._tid_priority([{"priority": "urgent"}])
    assert (priority, score) == ("unknown", 500)
    assert reason == "unknown priority value(s): urgent — schema/producer drift"

    application_data = vault._tid_application_data
    monkeypatch.setattr(vault, "_tid_application_data", lambda: {"roles": {}, "index": {}})
    assert vault._v_tid_trace({}) == ""
    monkeypatch.setattr(vault, "_tid_application_data", application_data)

    application = {
        "profile": "other",
        "roles": {
            "malformed": [None, {}, {"namespace": "records", "type": "wanted"}],
        },
    }
    monkeypatch.setattr(vault, "cockpit_config", lambda: {"application": application})
    monkeypatch.setattr(
        vault,
        "_load_dir",
        lambda _namespace: [
            {"type": "other", "id": "skip"},
            {"type": "wanted", "id": "keep", "_sub": "records", "_name": "keep"},
        ],
    )
    data = vault._tid_application_data()
    assert [row["id"] for row in data["roles"]["malformed"]] == ["keep"]
    assert data["index"]["keep"]["id"] == "keep"


def test_tid_missing_validation_and_assessment_metadata_paths(vault, monkeypatch):
    analytic = {
        "type": "detection-analytic",
        "id": "analytic:unvalidated",
        "title": "Unvalidated analytic",
        "revision": "git:2222222222222222222222222222222222222222",
        "_sub": "detection-analytics",
        "_name": "unvalidated",
    }
    data = {
        "roles": {"detection_analytic": [analytic], "validation_result": []},
        "index": {"analytic:unvalidated": analytic},
    }
    monkeypatch.setattr(vault, "_tid_application_data", lambda: data)
    queue = vault._v_tid_validation_queue({})
    assert "no validation result" in queue
    assert "Never" in queue

    record = {"epistemic_status": "assessed", "assessed_value": None}
    monkeypatch.setattr(vault, "_assessment_for_row", lambda _row, _spec: record)
    assert vault._assessment_bucket({}, {}) == ("__metadata_unavailable__", record)

    unknown = {"epistemic_status": "unknown", "path": "assessments/unknown"}
    monkeypatch.setattr(vault, "_assessment_for_row", lambda _row, _spec: unknown)
    assert "<strong>Unknown</strong>" in vault._assessed_value_cell({}, {})
    assert vault._assessment_terminal_for_row({}, {"kind": "another-kind"}) is None


def test_tid_existing_source_and_current_validation_avoid_false_warnings(vault, monkeypatch):
    procedure = {"id": "procedure:x", "title": "X", "_sub": "procedures", "_name": "x"}
    source = {"id": "source:x", "title": "Source", "_sub": "sources", "_name": "x"}
    group = {
        "procedure": procedure, "actor_ref": "", "sources": ["source:x"],
        "requirements": [], "strategies": [], "analytics": [], "deployments": [],
        "validations": [], "coverage": [], "current_coverage": None, "outcomes": [],
    }
    monkeypatch.setattr(vault, "_tid_application_data", lambda: {
        "roles": {}, "index": {"source:x": source}
    })
    monkeypatch.setattr(vault, "_tid_groups", lambda _data: [group])
    assert "Source" in vault._v_tid_trace({})

    analytic = {
        "id": "analytic:current", "title": "Current", "revision": "git:same",
        "_sub": "detection-analytics", "_name": "current",
    }
    validation = {
        "id": "validation:current", "analytic_ref": "analytic:current",
        "analytic_revision": "git:same", "result": "passed", "valid_until": "",
        "_sub": "validation-results", "_name": "current",
    }
    monkeypatch.setattr(vault, "_tid_application_data", lambda: {
        "roles": {
            "detection_analytic": [analytic], "validation_result": [validation],
            "detection_gap": [], "defensive_decision": [],
        },
        "index": {"analytic:current": analytic},
    })
    queue = vault._v_tid_validation_queue({})
    assert "analytic revision changed" not in queue and "validation stale" not in queue


def test_a_resharded_subject_keeps_its_standing_judgment(vault, monkeypatch):
    """A partitioned namespace re-files pages as it grows -- and back again as it shrinks.
    `entities/q/i/qilin-ransomware` became `entities/q/qilin-ransomware` on the live vault.

    An assessment records `subject:` as the path AT WRITE TIME, so a reshard silently orphaned every
    judgment pointing into the old shard: the record stayed `active` on disk while the panel showed
    "Review not run", which reads as never-assessed. Measured: 10 of 43 live attributions went dark
    between two reads hours apart, one of them a version-45 active RU judgment.

    Identity is (namespace, slug) -- shard depth is placement, not identity."""
    m = vault
    assert m._subject_key("entities/q/i/qilin-ransomware") == "entities/qilin-ransomware"
    assert m._subject_key("entities/q/qilin-ransomware") == "entities/qilin-ransomware"
    assert m._subject_key("entities/qilin-ransomware") == "entities/qilin-ransomware"
    # a wikilink and a wiki/ prefix normalise the same way
    assert m._subject_key("[[wiki/entities/q/i/qilin-ransomware.md]]") == "entities/qilin-ransomware"
    # a bare token has no namespace to scope to and must not be forced into one
    assert m._subject_key("qilin-ransomware") == "qilin-ransomware"
    assert m._subject_key("") == ""


def test_the_lookup_survives_a_shard_move_in_either_direction(vault, monkeypatch):
    """Both sides of the comparison are normalised, so it does not matter whether the record or the
    page is the one carrying the stale depth."""
    m = vault
    record = {"assessment_kind": "actor-country-linkage", "status": "active",
              "assessed_value": "RU", "epistemic_status": "assessed", "path": "assessments/q/qilin"}
    monkeypatch.setattr(m, "_assessment_subject_index",
                        lambda: {"entities/qilin-ransomware": [record]})
    spec = {"kind": "actor-country-linkage", "value_field": "assessed_value"}
    for rel in ("q/i/qilin-ransomware", "q/qilin-ransomware", "q/i/x/qilin-ransomware"):
        row = {"_sub": "entities", "_rel": rel}
        assert m._assessment_for_row(row, spec) is record, rel
        assert m._assessment_bucket(row, spec)[0] == "RU", rel
