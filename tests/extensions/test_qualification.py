import importlib.util
import datetime as dt
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

    with pytest.raises(ValueError, match="unsupported recommendation"):
        q.qualification_result(**{**kwargs, "recommendation": "invented"})
    for key in ("artifact", "reason_code"):
        malformed = [dict(examined[0])]
        malformed[0].pop(key)
        with pytest.raises(ValueError, match="examined lead requires"):
            q.qualification_result(**{**kwargs, "examined": malformed})


def test_digest_dates_and_candidate_validation_edges():
    value = {"day": dt.date(2026, 8, 4), "instant": dt.datetime(2026, 8, 4, 1, 2, 3),
             "unicode": "café"}
    assert q.digest(value).startswith("sha256:")
    with pytest.raises(TypeError, match="set is not JSON serializable"):
        q.digest({"unsupported": {1}})
    base = dict(
        artifact="sources/report", artifact_digest="sha256:" + "b" * 64,
        source_identity=None, publisher="Publisher", evidence_access="local",
        evidence_lineage="publisher:report", subject_match_basis="canonical",
        discovery_reason="named-subject",
    )
    lead = q.candidate_lead(**base)
    assert lead["publisher"] == "Publisher" and lead["retrieval_state"] == "local"
    for bad in ({**base, "artifact": ""}, {**base, "artifact_digest": "md5:bad"}):
        with pytest.raises(ValueError, match="sha256"):
            q.candidate_lead(**bad)
