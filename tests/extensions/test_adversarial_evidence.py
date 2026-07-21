from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
EXT = REPO / "extensions" / "okengine.assessments"
FIX = REPO / "tests" / "fixtures" / "adversarial_evidence"


def _load_module():
    spec = importlib.util.spec_from_file_location("adversarial_evidence", EXT / "adversarial_evidence.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fixture(name):
    return yaml.safe_load((FIX / name).read_text(encoding="utf-8"))


def _expected_absence(*, status="not-observed", coverage="partial",
                      detection="unknown", move=0.0):
    record = _fixture("resistant-corroboration.yaml")
    record["consequence"] = "medium"
    record["proposed_confidence_change"] = move
    record["adversarial_evidence"] = [{
        "evidence_kind": "expected-absence",
        "observation": "No Iran-aligned victim was identified in the reported victim set.",
        "source": "sources/2022/cybereason-strifewater",
        "observation_confidence": "high",
        "diagnosticity": "low",
        "manipulation_susceptibility": "medium",
        "source_independence": "primary-direct",
        "evidence_lineage": "cybereason-observed-victim-set",
        "claim_role": "third-party-claim",
        "deception_possible": False,
        "collection_context": "Published vendor observations; completeness is not established.",
        "alternatives": ["Vendor visibility does not cover the full victim population."],
        "expected_observation": "At least one Iran-aligned victim in a geographically indiscriminate campaign.",
        "expected_under": {
            "opportunistic-crime": "Iran-aligned victims should occasionally appear.",
            "politically-constrained-targeting": "Iran-aligned victims should be uncommon.",
        },
        "search_scope": "Cybereason's published 2021-2022 Moses Staff victim observations.",
        "opportunity_population": "Unknown; the report does not enumerate all reachable targets.",
        "absence_status": status,
        "coverage": coverage,
        "detection_probability": detection,
        "collection_bias": ["Vendor telemetry and publication selection are unknown."],
        "collection_requirement": "Compare independent, higher-coverage victim datasets.",
        "would_strengthen": ["Repeated absence across independent high-coverage datasets."],
        "would_weaken": ["A confirmed Iran-aligned victim."],
    }]
    return record


def test_manifest_and_schema_ownership_contract():
    manifest = yaml.safe_load((EXT / "extension.yaml").read_text())
    schema = yaml.safe_load((EXT / "schema" / "assessments.schema.yaml").read_text())
    assert manifest["id"] == "okengine.assessments" and manifest["core"] is False
    assert manifest["capabilities"]["network"] is False
    assert schema["owns"]["namespaces"] == ["assessments"]
    required = set(schema["field_items"]["adversarial_evidence"]["_item"]["required"])
    assert {"observation_confidence", "diagnosticity", "manipulation_susceptibility",
            "evidence_lineage", "source_independence", "deception_possible",
            "collection_context", "alternatives", "claim_role"} <= required
    item = schema["field_items"]["adversarial_evidence"]
    assert item["evidence_kind"]["enum"] == "evidence_kind"
    assert item["absence_status"]["enum"] == "absence_status"
    assert item["expected_under"]["shape"] == "dict"
    assert item["collection_bias"]["shape"] == "list"


def test_pack_declared_assessment_subtype_is_consumed(tmp_path):
    (tmp_path / "schema.yaml").write_text("assessment_types: [actor-assessment]\n")
    assert _load_module().assessment_types(tmp_path) == {"assessment", "actor-assessment"}


def test_composed_extension_enforces_item_shape_and_enums_at_write_boundary(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {}\n", encoding="utf-8")
    fragment = yaml.safe_load((EXT / "schema" / "assessments.schema.yaml").read_text())

    sl_spec = importlib.util.spec_from_file_location(
        "schema_lib_assessment_test", REPO / "scripts" / "cron" / "schema_lib.py")
    sl = importlib.util.module_from_spec(sl_spec)
    sl_spec.loader.exec_module(sl)
    composed, errors = sl.compose_schema(tmp_path, fragments=[("ext:okengine.assessments", fragment)])
    assert errors == []
    artifact = tmp_path / ".okengine" / "composed-schema.yaml"
    artifact.parent.mkdir()
    artifact.write_text(yaml.safe_dump(composed, sort_keys=False), encoding="utf-8")

    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.modules.pop("write_server", None)
    ws_spec = importlib.util.spec_from_file_location("write_server", REPO / "okengine-mcp" / "write_server.py")
    ws = importlib.util.module_from_spec(ws_spec)
    sys.modules["write_server"] = ws
    ws_spec.loader.exec_module(ws)

    good = _fixture("planted-attribution.yaml")
    assert not ws._create("assessments/good", good, "# Assessment\n").startswith("rejected:")
    bad = _fixture("planted-attribution.yaml")
    del bad["adversarial_evidence"][0]["diagnosticity"]
    result = ws._create("assessments/bad", bad, "# Assessment\n")
    assert result.startswith("rejected:") and "diagnosticity" in result
    bad_enum = _fixture("planted-attribution.yaml")
    bad_enum["adversarial_evidence"][0]["manipulation_susceptibility"] = "very-high"
    result = ws._create("assessments/bad-enum", bad_enum, "# Assessment\n")
    assert result.startswith("rejected:") and "sanctioned vocabulary" in result
    bad_absence = _expected_absence()
    bad_absence["adversarial_evidence"][0]["coverage"] = "probably-enough"
    result = ws._create("assessments/bad-absence", bad_absence, "# Assessment\n")
    assert result.startswith("rejected:") and "coverage" in result


def test_three_policy_outcomes_and_lineage_topology():
    m = _load_module()
    unrestricted = m.evaluate(_fixture("resistant-corroboration.yaml"))
    review = m.evaluate(_fixture("planted-attribution.yaml"))
    held = m.evaluate(_fixture("syndicated-lineage.yaml"))
    assert unrestricted["outcome"] == "unrestricted"
    assert review["outcome"] == "human-review" and review["human_review_required"]
    assert held["outcome"] == "capped-held" and held["maximum_recommended_move"] == 0.05
    assert held["topology"]["reports"] == 3
    assert held["topology"]["independent_lineages"] == 1
    assert "one evidentiary lineage" in " ".join(held["reasons"])


def test_unique_publishers_do_not_inflate_independence_when_lineage_is_unknown_or_shared():
    m = _load_module()
    record = _fixture("resistant-corroboration.yaml")
    record["adversarial_evidence"] += [
        dict(record["adversarial_evidence"][0], evidence_lineage="guidepost-a",
             source_independence="unknown"),
        dict(record["adversarial_evidence"][0], evidence_lineage="aggregator-b",
             source_independence="shared-lineage"),
    ]
    result = m.evaluate(record)
    assert result["topology"]["reports"] == 4
    assert result["topology"]["independent_lineages"] == 2
    assert result["topology"]["lineages"] == ["incident-b", "sensor-a"]


def test_manipulable_evidence_cannot_silently_drive_large_move():
    result = _load_module().evaluate(_fixture("planted-attribution.yaml"))
    assert result["outcome"] != "unrestricted"
    assert result["requested_move"] == 0.20
    assert result["reasons"] and result["alternatives"]


def test_possible_deception_requires_falsifiable_detail_not_blank_suspicion():
    m = _load_module()
    record = _fixture("planted-attribution.yaml")
    record["consequence"] = "low"
    record["adversarial_evidence"][0]["deception_hypothesis"] = ""
    result = m.evaluate(record)
    assert result["outcome"] == "human-review"
    assert "falsifiable hypothesis" in result["reasons"][0]


def test_incomplete_expected_absence_fails_closed_to_human_review():
    m = _load_module()
    record = _expected_absence()
    del record["adversarial_evidence"][0]["search_scope"]
    result = m.evaluate(record)
    assert result["outcome"] == "human-review"
    assert "search_scope" in result["reasons"][0]


def test_not_observed_is_collection_signal_not_negative_evidence():
    result = _load_module().evaluate(_expected_absence())
    assert result["outcome"] == "unrestricted"
    assert result["topology"]["expected_absences"] == 1
    assert result["topology"]["qualified_expected_absences"] == 0
    assert "collection signal" in result["reasons"][0]


def test_collection_gap_cannot_drive_positive_confidence_move():
    result = _load_module().evaluate(_expected_absence(status="collection-gap", move=0.12))
    assert result["outcome"] == "capped-held"
    assert result["maximum_recommended_move"] == 0.0
    assert result["topology"]["collection_gaps"] == 1
    assert "relies only on absence" in result["reasons"][0]


def test_adequately_searched_absence_can_be_evaluated_as_negative_evidence():
    m = _load_module()
    record = _expected_absence(status="searched-not-found", coverage="substantial",
                               detection="high", move=0.08)
    evidence = record["adversarial_evidence"][0]
    evidence["diagnosticity"] = "high"
    evidence["manipulation_susceptibility"] = "low"
    result = m.evaluate(record)
    assert result["outcome"] == "unrestricted"
    assert result["topology"]["qualified_expected_absences"] == 1


def test_actor_statement_is_evidence_of_speech_not_canonical_fact():
    m = _load_module()
    record = _fixture("syndicated-lineage.yaml")
    record["adversarial_evidence"] = [dict(record["adversarial_evidence"][0],
        observation="The actor claimed responsibility for a named victim.",
        claim_role="actor-statement", evidence_lineage="actor-channel",
        source_independence="primary-direct")]
    record["proposed_confidence_change"] = 0.08
    result = m.evaluate(record)
    assert result["outcome"] == "capped-held"
    assert "prove the statements occurred" in result["reasons"][0]


def test_renderer_exposes_qualifications_hypothesis_and_alternatives():
    m = _load_module()
    record = _fixture("planted-attribution.yaml")
    text = m.render_assessment(record, m.evaluate(record))
    for phrase in ("Evidence qualifications", "Authenticity", "Diagnosticity", "Manipulation",
                   "Deception hypothesis", "Competing alternatives", "human-review"):
        assert phrase in text
    assert text.startswith('<div class="assessment-review-separator" role="separator"></div>\n\n## ')


def test_renderer_exposes_expected_absence_scope_and_collection_limits():
    m = _load_module()
    record = _expected_absence()
    text = m.render_assessment(record, m.evaluate(record))
    for phrase in ("Expected-absence qualifications", "not-observed", "Expected observation",
                   "Opportunity population", "Detection probability", "Known collection bias",
                   "Compare independent, higher-coverage victim datasets"):
        assert phrase in text


def test_renderer_names_malformed_legacy_record_without_empty_table():
    m = _load_module()
    record = {"type": "assessment", "id": "x" * 300}
    text = m.render_assessment(record, m.evaluate(record), page="assessments/legacy")
    assert "Record validity:** `invalid`" in text
    assert "missing `subject, question, claim" in text
    assert "[[assessments/legacy]]" in text
    assert "No structured adversarial evidence recorded" in text
    assert "| Observation | Authenticity" not in text
    assert "x" * 141 not in text


def test_no_agent_operation_writes_analyst_dashboard(tmp_path):
    record = _fixture("planted-attribution.yaml")
    adir = tmp_path / "wiki" / "assessments"
    adir.mkdir(parents=True)
    (adir / "country-link.md").write_text(
        "---\n" + yaml.safe_dump(record, sort_keys=False) + "---\n# Assessment\n", encoding="utf-8")
    run = subprocess.run([sys.executable, str(EXT / "adversarial_evidence.py")],
                         env={**os.environ, "WIKI_PATH": str(tmp_path)},
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.strip().splitlines()[-1]) == {"wakeAgent": False}
    dashboard = (tmp_path / "wiki" / "dashboards" / "adversarial-evidence-review.md").read_text()
    assert "human-review" in dashboard and "Persian locale metadata" in dashboard


def test_no_agent_operation_reviews_pack_declared_subtype(tmp_path):
    record = _fixture("planted-attribution.yaml")
    record["type"] = "actor-assessment"
    (tmp_path / "schema.yaml").write_text("assessment_types: [actor-assessment]\n")
    adir = tmp_path / "wiki" / "assessments"
    adir.mkdir(parents=True)
    (adir / "actor.md").write_text(
        "---\n" + yaml.safe_dump(record, sort_keys=False) + "---\n", encoding="utf-8")
    run = subprocess.run([sys.executable, str(EXT / "adversarial_evidence.py")],
                         env={**os.environ, "WIKI_PATH": str(tmp_path)},
                         text=True, capture_output=True)
    assert run.returncode == 0
    dashboard = (tmp_path / "wiki" / "dashboards" / "adversarial-evidence-review.md").read_text()
    assert record["claim"] in dashboard


def test_no_agent_operation_renders_newest_lifecycle_timestamp_first(tmp_path):
    older = _fixture("planted-attribution.yaml")
    older["claim"] = "Older assessment"
    older["last_updated"] = "2026-07-17T01:00:00Z"
    newer = _fixture("planted-attribution.yaml")
    newer["claim"] = "Newer assessment"
    newer["last_updated"] = "2026-07-17T02:00:00Z"
    adir = tmp_path / "wiki" / "assessments"
    adir.mkdir(parents=True)
    # Filenames intentionally oppose lifecycle order.
    (adir / "a-older-path.md").write_text(
        "---\n" + yaml.safe_dump(older, sort_keys=False) + "---\n", encoding="utf-8")
    (adir / "z-newer-path.md").write_text(
        "---\n" + yaml.safe_dump(newer, sort_keys=False) + "---\n", encoding="utf-8")
    run = subprocess.run([sys.executable, str(EXT / "adversarial_evidence.py")],
                         env={**os.environ, "WIKI_PATH": str(tmp_path)},
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    dashboard = (tmp_path / "wiki/dashboards/adversarial-evidence-review.md").read_text()
    assert dashboard.index("Newer assessment") < dashboard.index("Older assessment")
    assert "Last updated:** `2026-07-17T02:00:00Z`" in dashboard


def test_sort_key_breaks_batch_update_ties_by_analytic_as_of():
    m = _load_module()
    common = {"last_updated": "2026-07-17T16:07:39Z", "created": "2026-07-16T04:54:19Z"}
    older = {**common, "title": "Zed", "as_of": "2026-07-16T04:54:19Z"}
    newer = {**common, "title": "Alpha", "as_of": "2026-07-17T05:08:40Z"}
    assert m._record_sort_key(newer) > m._record_sort_key(older)
