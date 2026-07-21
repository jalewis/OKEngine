#!/usr/bin/env python3
"""Deterministic adversarial-evidence policy and analyst review renderer (#244)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml

OUTCOMES = ("unrestricted", "capped-held", "human-review")
REQUIRED_FIELDS = ("subject", "question", "claim", "status", "as_of", "confidence",
                   "confidence_band", "proposed_confidence_change", "consequence",
                   "alternatives", "adversarial_evidence")
ABSENCE_REQUIRED_FIELDS = ("expected_observation", "expected_under", "search_scope",
                           "opportunity_population", "absence_status", "coverage",
                           "detection_probability", "collection_bias", "collection_requirement",
                           "would_strengthen", "would_weaken")
_FM = re.compile(r"\A---[ \t]*\n(.*?)\n---(?:\n|\Z)", re.S)
_UTC_SECOND = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _record_sort_key(record: dict) -> tuple[int, str, str, str, str]:
    """Newest durable lifecycle timestamp first; legacy records remain visible last."""
    timestamps = tuple(
        value if _UTC_SECOND.fullmatch(value := str(record.get(field) or "")) else ""
        for field in ("last_updated", "as_of", "created")
    )
    identity = str(record.get("title") or record.get("claim") or record.get("id") or "")
    return (1 if any(timestamps) else 0, *timestamps, identity)


def _lineages(items: list[dict]) -> set[str]:
    """Return only lineages asserted to be independent evidentiary origins.

    A unique publisher or lineage label is not itself evidence of independence. Aggregators and
    curated guideposts may expose distinct labels while repeating the same underlying reporting.
    Unknown and shared-lineage records therefore remain visible in the report count but cannot
    inflate the independent-lineage count.
    """
    return {
        str(e.get("evidence_lineage") or "").strip()
        for e in items
        if e.get("source_independence") in ("primary-direct", "independent-origin")
        and str(e.get("evidence_lineage") or "").strip()
    }


def _absence_qualification(item: dict) -> tuple[list[str], bool]:
    """Return missing qualification fields and whether an expected absence is evidentiary.

    Merely not observing something is not negative evidence. It becomes potentially evidentiary
    only after a declared search with enough coverage and detection probability that the missing
    observation would be surprising. A collection gap remains useful, but only as a collection
    requirement.
    """
    missing = []
    for field in ABSENCE_REQUIRED_FIELDS:
        value = item.get(field)
        if value is None or value == "":
            missing.append(field)
        elif field in {"expected_under", "would_strengthen", "would_weaken"} and not value:
            missing.append(field)
    qualified = (
        not missing
        and item.get("absence_status") == "searched-not-found"
        and item.get("coverage") in {"substantial", "near-complete"}
        and item.get("detection_probability") in {"medium", "high"}
    )
    return missing, qualified


def evaluate(record: dict, *, large_move: float = 0.10, cap: float = 0.05) -> dict:
    """Return an explainable advisory; never mutates assessment confidence."""
    items = record.get("adversarial_evidence") or []
    try:
        move = float(record.get("proposed_confidence_change") or 0.0)
    except (TypeError, ValueError):
        return {
            "outcome": "human-review", "requested_move": 0.0,
            "maximum_recommended_move": None, "human_review_required": True,
            "reasons": ["proposed confidence change is not numeric"],
            "topology": {"reports": len(items) if isinstance(items, list) else 0,
                         "independent_lineages": 0, "lineages": []},
            "alternatives": [str(a) for a in (record.get("alternatives") or [])],
        }
    consequence = str(record.get("consequence") or "low")
    reasons: list[str] = []
    alternatives = [str(a) for a in (record.get("alternatives") or []) if str(a).strip()]
    valid_items = items if isinstance(items, list) else []
    absence_items = [e for e in valid_items if isinstance(e, dict)
                     and e.get("evidence_kind") == "expected-absence"]
    absence_checks = [(e, *_absence_qualification(e)) for e in absence_items]
    qualified_absences = [e for e, _missing, qualified in absence_checks if qualified]
    collection_gaps = [e for e in absence_items if e.get("absence_status") == "collection-gap"]
    topology = {
        "reports": len(valid_items),
        "independent_lineages": len(_lineages(valid_items)),
        "lineages": sorted(_lineages(valid_items)),
        "expected_absences": len(absence_items),
        "qualified_expected_absences": len(qualified_absences),
        "collection_gaps": len(collection_gaps),
    }
    force_zero_cap = False

    if not isinstance(items, list) or not items:
        reasons.append("assessment has no structured adversarial evidence")
        outcome = "human-review"
    else:
        incomplete_absences = [(e, missing) for e, missing, _qualified in absence_checks if missing]
        deceptive = [e for e in items if isinstance(e, dict) and e.get("deception_possible") is True]
        incomplete_deception = [e for e in deceptive
                                if not str(e.get("deception_hypothesis") or "").strip()
                                or not (e.get("alternatives") or [])]
        if incomplete_absences:
            fields = sorted({field for _item, missing in incomplete_absences for field in missing})
            reasons.append("expected-absence evidence lacks required qualification fields: " +
                           ", ".join(fields))
            outcome = "human-review"
        elif incomplete_deception:
            reasons.append("possible deception lacks a falsifiable hypothesis or competing alternative")
            outcome = "human-review"
        else:
            resistant = [e for e in items if isinstance(e, dict)
                         and (e.get("evidence_kind") != "expected-absence"
                              or e in qualified_absences)
                         and e.get("manipulation_susceptibility") == "low"
                         and e.get("diagnosticity") == "high"
                         and e.get("source_independence") in ("primary-direct", "independent-origin")]
            highly_manipulable = [e for e in items if isinstance(e, dict)
                                  and e.get("manipulation_susceptibility") == "high"]
            actor_statements = [e for e in items if isinstance(e, dict)
                                and e.get("claim_role") == "actor-statement"]
            one_lineage = topology["independent_lineages"] < 2
            only_unqualified_absence = bool(absence_items) and len(absence_items) == len(items) \
                and not qualified_absences

            if move > 0 and only_unqualified_absence:
                reasons.append("positive confidence move relies only on absence that was not "
                               "established by an adequately covered, detectable search")
                outcome = "capped-held"
                force_zero_cap = True
            elif consequence == "high" and move > 0 and (one_lineage or not resistant):
                reasons.append("high-consequence increase lacks two lineages and resistant corroboration")
                outcome = "human-review"
            elif move > large_move and highly_manipulable and not resistant:
                reasons.append("large increase rests on highly manipulable evidence without resistant corroboration")
                outcome = "capped-held"
            elif move > 0 and one_lineage and len(items) > 1:
                reasons.append("multiple reports resolve to one evidentiary lineage, not independent corroboration")
                outcome = "capped-held"
            elif move > 0 and actor_statements and len(actor_statements) == len(items):
                reasons.append("actor-controlled statements prove the statements occurred, not their allegations")
                outcome = "capped-held"
            elif move == 0 and absence_items and not qualified_absences:
                reasons.append("absence is retained as a collection signal, not confidence-bearing "
                               "negative evidence")
                outcome = "unrestricted"
            elif move == 0:
                reasons.append("no confidence increase requested; evidence qualifications retained")
                outcome = "unrestricted"
            else:
                reasons.append("independent, diagnostic, manipulation-resistant evidence supports the requested move")
                outcome = "unrestricted"

    applied_cap = 0.0 if force_zero_cap else cap if outcome == "capped-held" and move > cap else None
    return {
        "outcome": outcome,
        "requested_move": move,
        "maximum_recommended_move": applied_cap,
        "human_review_required": outcome == "human-review",
        "reasons": reasons,
        "topology": topology,
        "alternatives": alternatives,
    }


def render_assessment(record: dict, result: dict, *, page: str | None = None) -> str:
    """Render qualifications, topology, and alternatives for an analyst."""
    heading = str(record.get("claim") or record.get("title") or record.get("id") or "Assessment")
    if len(heading) > 140:
        heading = heading[:137].rstrip() + "…"
    missing = [field for field in REQUIRED_FIELDS if field not in record]
    # The marker is emitted for every record, including the first. Cockpit turns it into a strong
    # visual boundary and styles the following heading as the record header. Other Markdown
    # readers still get a semantic separator before each assessment.
    lines = ['<div class="assessment-review-separator" role="separator"></div>', "",
             f"## {heading}", ""]
    if page:
        lines += [f"- **Record:** [[{page}]]"]
    lines += [f"- **Record validity:** `{'invalid' if missing else 'valid'}`" +
              (f" — missing `{', '.join(missing)}`" if missing else ""),
             f"- **Last updated:** `{record.get('last_updated') or record.get('as_of') or 'not supplied'}`",
             "", f"- **Policy outcome:** `{result['outcome']}`",
             f"- **Requested confidence move:** {result['requested_move']:+.2f}",
             f"- **Evidence topology:** {result['topology']['reports']} evidence records / "
             f"{result['topology']['independent_lineages']} independent lineages",
             f"- **Reason:** {'; '.join(result['reasons'])}", ""]
    if result["topology"].get("expected_absences"):
        lines += [f"- **Expected absences:** {result['topology']['expected_absences']} recorded / "
                  f"{result['topology']['qualified_expected_absences']} qualified as negative evidence / "
                  f"{result['topology']['collection_gaps']} collection gaps", ""]
    if result.get("maximum_recommended_move") is not None:
        lines.append(f"- **Recommended cap:** +{result['maximum_recommended_move']:.2f}")
        lines.append("")
    evidence = record.get("adversarial_evidence") or []
    lines += ["### Evidence qualifications", ""]
    if not evidence:
        lines += ["_No structured adversarial evidence recorded._", ""]
    else:
        lines += ["| Kind | Observation | Authenticity | Diagnosticity | Manipulation | Lineage | Deception |",
                  "|---|---|---|---|---|---|---|"]
    for e in evidence:
        if not isinstance(e, dict):
            continue
        obs = str(e.get("observation") or "").replace("|", "\\|")
        lines.append(f"| {e.get('evidence_kind', 'observed')} | {obs} | "
                     f"{e.get('observation_confidence', '—')} | "
                     f"{e.get('diagnosticity', '—')} | {e.get('manipulation_susceptibility', '—')} | "
                     f"{e.get('evidence_lineage', '—')} | "
                     f"{'possible' if e.get('deception_possible') else 'not indicated'} |")
        if e.get("deception_hypothesis"):
            lines.append(f"\n_Deception hypothesis:_ {e['deception_hypothesis']}\n")
    absences = [e for e in evidence if isinstance(e, dict)
                and e.get("evidence_kind") == "expected-absence"]
    if absences:
        lines += ["", "### Expected-absence qualifications", ""]
    for index, e in enumerate(absences, 1):
        expected_under = e.get("expected_under") or {}
        lines += [f"#### {index}. {e.get('observation') or 'Expected observation was absent'}", "",
                  f"- **Status:** `{e.get('absence_status', 'not-supplied')}`",
                  f"- **Expected observation:** {e.get('expected_observation', 'Not supplied')}",
                  f"- **Search scope:** {e.get('search_scope', 'Not supplied')}",
                  f"- **Opportunity population:** {e.get('opportunity_population', 'Not supplied')}",
                  f"- **Coverage:** `{e.get('coverage', 'not-supplied')}`",
                  f"- **Detection probability:** `{e.get('detection_probability', 'not-supplied')}`",
                  f"- **Collection requirement:** {e.get('collection_requirement', 'Not supplied')}", "",
                  "**Expected under competing hypotheses:**", ""]
        if isinstance(expected_under, dict) and expected_under:
            lines += [f"- **{name}:** {expectation}" for name, expectation in expected_under.items()]
        else:
            lines += ["- Not supplied"]
        lines += ["", "**Known collection bias:**", ""]
        lines += [f"- {item}" for item in (e.get("collection_bias") or ["None recorded"])]
        lines += ["", "**Would strengthen:**", ""]
        lines += [f"- {item}" for item in (e.get("would_strengthen") or ["Not supplied"])]
        lines += ["", "**Would weaken:**", ""]
        lines += [f"- {item}" for item in (e.get("would_weaken") or ["Not supplied"])]
    lines += ["", "### Competing alternatives", ""]
    lines += [f"- {a}" for a in result.get("alternatives") or ["None recorded — review required."]]
    return "\n".join(lines) + "\n"


def _load(path: Path) -> dict | None:
    try:
        match = _FM.match(path.read_text(encoding="utf-8"))
        data = yaml.safe_load(match.group(1)) if match else None
        return data if isinstance(data, dict) else None
    except (OSError, yaml.YAMLError):
        return None


def assessment_types(vault: Path) -> set[str]:
    """Generic type plus pack-declared domain assessment subtypes."""
    out = {"assessment"}
    try:
        schema = yaml.safe_load((vault / "schema.yaml").read_text(encoding="utf-8")) or {}
        declared = schema.get("assessment_types") or []
        if isinstance(declared, list):
            out.update(str(v) for v in declared if str(v).strip())
    except (OSError, yaml.YAMLError):
        pass
    return out


def main() -> int:
    vault = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
    assessments = vault / "wiki" / "assessments"
    out = vault / "wiki" / "dashboards" / "adversarial-evidence-review.md"
    records = []
    accepted_types = assessment_types(vault)
    for path in sorted(assessments.rglob("*.md")) if assessments.is_dir() else []:
        record = _load(path)
        if record and record.get("type") in accepted_types:
            records.append((record, path))
    rows = []
    for record, path in sorted(records, key=lambda item: _record_sort_key(item[0]), reverse=True):
        result = evaluate(record,
                          large_move=float(os.environ.get("OKENGINE_ASSESSMENTS_LARGE_CONFIDENCE_MOVE", "0.10")),
                          cap=float(os.environ.get("OKENGINE_ASSESSMENTS_MANIPULABLE_EVIDENCE_CAP", "0.05")))
        rows.append(render_assessment(
            record, result, page=str(path.relative_to(vault / "wiki").with_suffix(""))))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("---\ntype: dashboard\nid: dashboard:adversarial-evidence-review\n"
                   "title: Adversarial evidence review\n---\n\n# Adversarial evidence review\n\n" +
                   ("\n".join(rows) if rows else "_No assessment records found._\n"), encoding="utf-8")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
