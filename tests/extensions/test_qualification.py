import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "qualification", ROOT / "extensions/okengine.assessments/qualification.py")
q = importlib.util.module_from_spec(spec); spec.loader.exec_module(q)


def test_candidate_lead_is_provenance_not_support():
    lead = q.candidate_lead(artifact="sources/report", artifact_digest="sha256:" + "a" * 64,
        source_identity="Vendor", evidence_access="local-full-text", evidence_lineage="vendor:one",
        subject_match_basis="canonical-subject", discovery_reason="behavior-change-cue")
    assert lead["evidence_role"] == "candidate-lead"
    assert "claim_role" not in lead


def test_qualification_result_is_deterministic_and_rejects_role_confusion():
    examined = [{"artifact": "sources/report", "artifact_digest": "sha256:" + "a" * 64,
        "evidence_role": "candidate-lead", "outcome": "rejected",
        "reason_code": "baseline-missing", "missing_elements": ["baseline-observation"]}]
    kwargs = dict(subject_ref="entities/a/actor", dimension="behavior-capability",
        question="What changed?", policy="behavior/v2", examined=examined,
        missing_elements=["baseline-observation"], search_scope="one local source",
        recommendation="collection-required")
    assert q.qualification_result(**kwargs)["result_digest"] == q.qualification_result(**kwargs)["result_digest"]
    bad = [dict(examined[0], outcome="support")]
    with pytest.raises(ValueError):
        q.qualification_result(**{**kwargs, "examined": bad})
