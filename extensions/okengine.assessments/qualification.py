#!/usr/bin/env python3
"""Reusable candidate-evidence and claim-qualification records."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Iterable

CONTRACT = "claim-qualification/v1"
LEAD_ROLE = "candidate-lead"
OUTCOMES = {"qualified", "rejected", "unresolved"}
RECOMMENDATIONS = {"assessed", "collection-required", "searched-not-found", "not-applicable", "failed"}


def _canonical(value: Any):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                     default=_canonical).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def candidate_lead(*, artifact: str, artifact_digest: str, source_identity: str | None,
                   evidence_access: str, evidence_lineage: str, subject_match_basis: str,
                   discovery_reason: str, retrieval_state: str = "local",
                   publisher: str | None = None) -> dict[str, Any]:
    if not artifact or not artifact_digest.startswith("sha256:"):
        raise ValueError("candidate lead requires an artifact and sha256 digest")
    return {"artifact": artifact, "artifact_digest": artifact_digest,
            "source_identity": source_identity, "publisher": publisher,
            "evidence_access": evidence_access, "evidence_lineage": evidence_lineage,
            "subject_match_basis": subject_match_basis, "discovery_reason": discovery_reason,
            "retrieval_state": retrieval_state, "evidence_role": LEAD_ROLE}


def qualification_result(*, subject_ref: str, dimension: str, question: str,
                         policy: str, examined: Iterable[dict[str, Any]],
                         qualified: Iterable[dict[str, Any]] = (),
                         missing_elements: Iterable[str] = (), search_scope: str,
                         recommendation: str) -> dict[str, Any]:
    if recommendation not in RECOMMENDATIONS:
        raise ValueError(f"unsupported recommendation: {recommendation}")
    examined_rows = list(examined); qualified_rows = list(qualified)
    for row in examined_rows:
        if row.get("outcome") not in OUTCOMES or not row.get("artifact") or not row.get("reason_code"):
            raise ValueError("examined lead requires artifact, outcome, and reason_code")
    result = {"contract": CONTRACT, "subject_ref": subject_ref, "dimension": dimension,
              "question": question, "specialist_policy": policy,
              "examined": examined_rows, "qualified_observations": qualified_rows,
              "missing_elements": sorted(set(missing_elements)), "search_scope": search_scope,
              "terminal_recommendation": recommendation}
    result["result_digest"] = digest(result)
    return result
