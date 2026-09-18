from __future__ import annotations
# ruff: noqa: F821

import functools
import os
import re
import json
import glob
import hashlib
import hmac
import sys
import threading
import time
import datetime
from collections import Counter, defaultdict
import subprocess
import shutil
import tempfile
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse
from pathlib import Path
from typing import Any

import yaml
import markdown as md
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

_FM_SCAN_BYTES = 262_144


def _v_review_queue(box: dict) -> str:
    """Render a reusable launcher into the complete review worklist with a pack-owned type scope."""
    types = [str(v).strip() for v in (box.get("review_types") or []) if str(v).strip()]
    allowed = set(types)
    directories = [
        str(v).strip().strip("/")
        for v in (box.get("review_dirs") or [])
        if str(v).strip().strip("/")
    ]
    if directories:
        # A dashboard count scoped to known domain namespaces does not need the expensive global
        # review worklist (25k-page scan on live OKCTI). _load_dir is prewarmed and stale-while-
        # revalidate; the launcher still opens the canonical complete /api/reviews worklist.
        candidates = [row for directory in directories for row in _load_dir(directory)]
        scoped = [
            row
            for row in candidates
            if (not allowed or row.get("type") in allowed)
            and _requires_review(row, "unsupported" if row.get("_body_review_flag") else "")
        ]
    else:
        rows, _records = _review_queue_snapshot()
        scoped = [row for row in rows if not allowed or row.get("type") in allowed]
    counts = Counter(str(row.get("type") or "unknown") for row in scoped)
    scope = ",".join(types)
    detail = " · ".join(f"{_humanize(kind)} {count}" for kind, count in sorted(counts.items()))
    empty = str(box.get("empty") or "No records currently require review.")
    if not scoped:
        return f'<p class="dnote">{_esc(empty)}</p>'
    return (
        f'<a class="review-scope-launch" data-review-types="{_esc(scope)}">'
        f"<strong>{len(scoped)} awaiting review</strong>"
        f"<span>{_esc(detail)}</span><span>Open the complete filtered worklist →</span></a>"
    )


def _tid_path(row: dict | None) -> str:
    if not row:
        return ""
    return f"{row.get('_sub', '')}/{row.get('_rel') or row.get('_name', '')}".strip("/")


def _tid_ref(value) -> str:
    ref = str(value or "").strip()
    if ref.startswith("[[") and ref.endswith("]]"):
        ref = ref[2:-2].split("|", 1)[0]
    ref = ref.split("#", 1)[0].removeprefix("wiki/").lstrip("/")
    return ref[:-3] if ref.endswith(".md") else ref


def _tid_application_data() -> dict:
    """Resolve application role bindings into records and a cross-reference index."""
    application = cockpit_config().get("application") or {}
    roles = application.get("roles") if isinstance(application.get("roles"), dict) else {}
    records: dict[str, list[dict]] = {}
    index: dict[str, dict] = {}
    for role, bindings in roles.items():
        rows: list[dict] = []
        for binding in bindings if isinstance(bindings, list) else []:
            if not isinstance(binding, dict) or not binding.get("namespace"):
                continue
            for row in _load_dir(str(binding["namespace"])):
                if binding.get("type") and row.get("type") != binding["type"]:
                    continue
                rows.append(row)
                for key in (row.get("id"), _tid_path(row)):
                    if key:
                        index[_tid_ref(key)] = row
        records[role] = rows
    # These workflow records are owned by the TID pack but are downstream of the profile's required
    # role contract. Projections may consume them when present; absence remains explicit unknown.
    if application.get("profile") == "threat-informed-detection":
        for role, namespace in {
            "detection_gap": "detection-gaps",
            "defensive_decision": "defensive-decisions",
            "defensive_action": "defensive-actions",
            "defensive_outcome": "defensive-outcomes",
        }.items():
            rows = _load_dir(namespace)
            records[role] = rows
            for row in rows:
                for key in (row.get("id"), _tid_path(row)):
                    if key:
                        index[_tid_ref(key)] = row
    return {"application": application, "roles": records, "index": index}


def _tid_match(index: dict[str, dict], ref) -> dict | None:
    return index.get(_tid_ref(ref))


def _tid_link(row: dict | None, fallback: str = "Not assessed") -> str:
    if not row:
        return f'<span class="tid-missing">{_esc(fallback)}</span>'
    label = row.get("title") or row.get("claim") or row.get("id") or row.get("_name") or fallback
    path = _tid_path(row)
    return (
        f'<a class="wl" data-page="{_esc(path)}">{_esc(str(label))}</a>'
        if path
        else _esc(str(label))
    )


def _tid_state(value) -> str:
    state = str(value or "unknown").strip().lower().replace("_", "-")
    if state not in _TID_STATES:
        state = "unknown"
    label = _humanize(state)
    return f'<span class="tid-state tid-state--{_esc(state)}">{_esc(label)}</span>'


def _tid_latest(rows: list[dict]) -> dict | None:
    return (
        sorted(
            rows,
            key=lambda row: (
                str(row.get("as_of") or row.get("executed_at") or row.get("captured_at") or ""),
                _tid_path(row),
            ),
        )[-1]
        if rows
        else None
    )


def _tid_groups(data: dict) -> list[dict]:
    """Join the role graph once for all three read-only TID projections."""
    roles, index = data["roles"], data["index"]
    groups = []
    for procedure in sorted(
        roles.get("threat_procedure", []),
        key=lambda row: str(row.get("title") or row.get("id") or ""),
    ):
        pid = procedure.get("id") or _tid_path(procedure)
        requirements = [
            row
            for row in roles.get("detection_requirement", [])
            if _tid_ref(row.get("procedure_ref")) in {_tid_ref(pid), _tid_path(procedure)}
        ]
        strategies = [
            row
            for row in roles.get("detection_strategy", [])
            if any(_tid_match(index, row.get("requirement_ref")) is req for req in requirements)
        ]
        analytics = [
            row
            for row in roles.get("detection_analytic", [])
            if any(
                _tid_match(index, row.get("strategy_ref")) is strategy for strategy in strategies
            )
        ]
        deployments = [
            row
            for row in roles.get("deployment_snapshot", [])
            if any(_tid_match(index, row.get("analytic_ref")) is analytic for analytic in analytics)
        ]
        validations = [
            row
            for row in roles.get("validation_result", [])
            if any(_tid_match(index, row.get("analytic_ref")) is analytic for analytic in analytics)
        ]
        outcomes = [
            row
            for row in roles.get("defensive_outcome", [])
            if any(
                _tid_match(index, row.get("validation_ref")) is validation
                for validation in validations
            )
        ]
        coverage = [
            row
            for row in roles.get("coverage_assessment", [])
            if _tid_ref(row.get("procedure_ref")) in {_tid_ref(pid), _tid_path(procedure)}
        ]
        current_coverage = _tid_latest(
            [row for row in coverage if row.get("status") not in {"superseded", "resolved"}]
        ) or _tid_latest(coverage)
        groups.append(
            {
                "procedure": procedure,
                "actor_ref": procedure.get("actor_ref"),
                "sources": list(procedure.get("sources") or []),
                "requirements": requirements,
                "strategies": strategies,
                "analytics": analytics,
                "deployments": deployments,
                "validations": validations,
                "coverage": coverage,
                "current_coverage": current_coverage,
                "outcomes": outcomes,
            }
        )
    return groups


def _tid_node(label: str, row: dict | None, state, detail: str = "") -> str:
    detail_html = f'<div class="tid-node-detail">{_esc(detail)}</div>' if detail else ""
    return (
        f'<div class="tid-node"><div class="tid-node-label">{_esc(label)}</div>'
        f'<div class="tid-node-value">{_tid_link(row)}</div>{_tid_state(state)}'
        f"{detail_html}</div>"
    )


def _v_tid_trace(_box: dict) -> str:
    data = _tid_application_data()
    groups = _tid_groups(data)
    if not groups:
        return ""
    index = data["index"]
    rendered = []
    for group in groups:
        procedure = group["procedure"]
        requirement, strategy = _tid_latest(group["requirements"]), _tid_latest(group["strategies"])
        analytic, deployment = _tid_latest(group["analytics"]), _tid_latest(group["deployments"])
        validation, coverage = _tid_latest(group["validations"]), group["current_coverage"]
        outcome = _tid_latest(group["outcomes"])
        source_ref = group["sources"][0] if group["sources"] else ""
        source = _tid_match(index, source_ref) if source_ref else None
        if source_ref and not source:
            source_path = _tid_ref(source_ref)
            source = {
                "id": source_path.rsplit("/", 1)[-1],
                "title": _humanize(source_path.rsplit("/", 1)[-1]),
                "_sub": source_path.split("/", 1)[0] if "/" in source_path else "",
                "_rel": source_path.split("/", 1)[1] if "/" in source_path else source_path,
            }
        source_state = "present" if group["sources"] else "unknown"
        deploy_state = (
            "enabled"
            if deployment and deployment.get("enabled")
            else "disabled"
            if deployment
            else "unknown"
        )
        coverage_state = (coverage or {}).get("assessed_value") or "unassessed"
        superseded = sum(1 for row in group["coverage"] if row.get("status") == "superseded")
        actor = _tid_ref(group.get("actor_ref"))
        actor_link = (
            f'<a class="wl" data-page="{_esc(actor)}">{_esc(actor.rsplit("/", 1)[-1])}</a>'
            if actor
            else "Unlinked actor"
        )
        nodes = [
            _tid_node(
                "Source evidence",
                source,
                source_state,
                f"{len(group['sources'])} source reference(s)",
            ),
            _tid_node("Procedure", procedure, "present"),
            _tid_node("Requirement", requirement, (requirement or {}).get("status")),
            _tid_node("Strategy", strategy, (strategy or {}).get("status")),
            _tid_node(
                "Analytic",
                analytic,
                (analytic or {}).get("status"),
                str((analytic or {}).get("revision") or ""),
            ),
            _tid_node(
                "Deployment",
                deployment,
                deploy_state,
                str((deployment or {}).get("analytic_revision") or ""),
            ),
            _tid_node(
                "Validation",
                validation,
                (validation or {}).get("result"),
                str((validation or {}).get("executed_at") or ""),
            ),
            _tid_node(
                "Coverage", coverage, coverage_state, str((coverage or {}).get("as_of") or "")
            ),
            _tid_node(
                "Outcome",
                outcome,
                (outcome or {}).get("effectiveness_state"),
                str((outcome or {}).get("measured_at") or ""),
            ),
        ]
        rendered.append(
            '<article class="tid-trace-record">'
            f"<header><strong>{_tid_link(procedure)}</strong><span>{actor_link}</span>"
            f"<span>{superseded} superseded assessment(s)</span></header>"
            '<div class="tid-trace-chain">'
            + '<span class="tid-arrow" aria-hidden="true">→</span>'.join(nodes)
            + "</div>"
            "</article>"
        )
    return '<div class="tid-trace">' + "".join(rendered) + "</div>"


def _v_tid_actor_posture(_box: dict) -> str:
    groups = _tid_groups(_tid_application_data())
    actors: dict[str, list[dict]] = defaultdict(list)
    for group in groups:
        actors[_tid_ref(group.get("actor_ref")) or "unlinked"].append(group)
    rows = []
    for actor, actor_groups in sorted(actors.items()):
        counts = Counter(
            str((group.get("current_coverage") or {}).get("assessed_value") or "unassessed")
            for group in actor_groups
        )
        latest = max(
            (
                str((group.get("current_coverage") or {}).get("as_of") or "")
                for group in actor_groups
            ),
            default="",
        )
        actor_cell = (
            f'<a class="wl" data-page="{_esc(actor)}">{_esc(actor.rsplit("/", 1)[-1])}</a>'
            if actor != "unlinked"
            else "Unlinked actor"
        )
        rows.append(
            [
                actor_cell,
                str(len(actor_groups)),
                str(counts.get("validated", 0)),
                str(counts.get("partial", 0)),
                str(counts.get("gap", 0)),
                str(counts.get("unknown", 0) + counts.get("unassessed", 0)),
                str(counts.get("stale", 0) + counts.get("degraded", 0)),
                _esc(latest or "—"),
            ]
        )
    return (
        '<div class="tid-table-wrap">'
        + _html_table(
            [
                "Actor",
                "Procedures",
                "Validated",
                "Partial",
                "Gap",
                "Unknown / unassessed",
                "Stale / degraded",
                "As of",
            ],
            rows,
        )
        + "</div>"
    )


def _v_tid_facet_matrix(_box: dict) -> str:
    groups = _tid_groups(_tid_application_data())
    rows = []
    for group in groups:
        assessments = sorted(
            group["coverage"],
            key=lambda row: (str(row.get("as_of") or ""), _tid_path(row)),
            reverse=True,
        )
        if not assessments:
            rows.append(
                [
                    _tid_link(group["procedure"]),
                    _tid_state("unassessed"),
                    *[_tid_state("unknown") for _ in _TID_FACETS],
                    "—",
                ]
            )
            continue
        for assessment in assessments:
            facets = {
                item.get("facet"): item
                for item in assessment.get("facets", [])
                if isinstance(item, dict)
            }
            path = _tid_path(assessment)
            cells = []
            for name in _TID_FACETS:
                state = (facets.get(name) or {}).get("state") or "unknown"
                cells.append(
                    f'<a class="tid-facet-link" data-page="{_esc(path)}" title="{_esc((facets.get(name) or {}).get("reason") or "No basis recorded")}">{_tid_state(state)}</a>'
                )
            lifecycle = str(assessment.get("status") or "unknown")
            rows.append(
                [
                    _tid_link(group["procedure"]),
                    _tid_state(assessment.get("assessed_value") or "unknown"),
                    *cells,
                    f"{_esc(lifecycle)} · {_esc(str(assessment.get('as_of') or '—'))}",
                ]
            )
    headers = [
        "Procedure",
        "Overall",
        *[_humanize(name) for name in _TID_FACETS],
        "Assessment version",
    ]
    return '<div class="tid-table-wrap tid-facet-matrix">' + _html_table(headers, rows) + "</div>"


def _tid_refs(rows: list[dict]) -> str:
    links = [_tid_link(row) for row in rows]
    return (
        '<span class="tid-ref-list">' + "".join(links) + "</span>"
        if links
        else _tid_state("unknown")
    )


def _tid_validation_history(rows: list[dict]) -> str:
    if not rows:
        return _tid_state("unknown")
    items = []
    for row in sorted(rows, key=lambda item: str(item.get("executed_at") or ""), reverse=True):
        items.append(
            '<span class="tid-validation-history-item">'
            f"{_tid_link(row)} {_tid_state(row.get('result'))} "
            f"<code>{_esc(str(row.get('analytic_revision') or 'Unknown revision'))}</code> · "
            f"{_esc(str(row.get('executed_at') or 'Unknown time'))}</span>"
        )
    return '<span class="tid-validation-history">' + "".join(items) + "</span>"


def _v_tid_detection_dossier(_box: dict) -> str:
    data = _tid_application_data()
    roles, index = data["roles"], data["index"]
    groups = _tid_groups(data)
    procedure_by_analytic: dict[int, dict] = {}
    for group in groups:
        for analytic in group["analytics"]:
            procedure_by_analytic[id(analytic)] = group
    cards = []
    for analytic in sorted(
        roles.get("detection_analytic", []),
        key=lambda row: str(row.get("title") or row.get("id") or ""),
    ):
        group = procedure_by_analytic.get(id(analytic), {})
        strategy = _tid_match(index, analytic.get("strategy_ref"))
        requirement = _tid_match(index, (strategy or {}).get("requirement_ref"))
        deployments = [
            row
            for row in roles.get("deployment_snapshot", [])
            if _tid_match(index, row.get("analytic_ref")) is analytic
        ]
        validations = [
            row
            for row in roles.get("validation_result", [])
            if _tid_match(index, row.get("analytic_ref")) is analytic
        ]
        deployment = _tid_latest(deployments)
        current_revision = str(analytic.get("revision") or "Unknown")
        deployed_revision = str((deployment or {}).get("analytic_revision") or "Unknown")
        revision_state = (
            "validated"
            if current_revision == deployed_revision and deployment
            else "stale"
            if deployment
            else "unknown"
        )
        telemetry = [
            _tid_match(index, ref) for ref in (requirement or {}).get("telemetry_refs", [])
        ]
        telemetry = [row for row in telemetry if row]
        limitations = []
        for row in validations:
            limitations.extend(str(value) for value in (row.get("limitations") or []))
        cards.append(
            '<article class="tid-dossier">'
            f"<header><strong>{_tid_link(analytic)}</strong>{_tid_state((analytic or {}).get('status'))}</header>"
            "<dl>"
            f"<dt>Procedure</dt><dd>{_tid_link(group.get('procedure'))}</dd>"
            f"<dt>Requirement</dt><dd>{_tid_link(requirement)}</dd>"
            f"<dt>Strategy</dt><dd>{_tid_link(strategy)}</dd>"
            f"<dt>Repository</dt><dd>{_esc(str(analytic.get('repository') or 'Unknown'))} · {_esc(str(analytic.get('repository_path') or 'Unknown'))}</dd>"
            f"<dt>Repository revision</dt><dd><code>{_esc(current_revision)}</code></dd>"
            f"<dt>Deployed revision</dt><dd><code>{_esc(deployed_revision)}</code> {_tid_state(revision_state)}</dd>"
            f"<dt>Telemetry prerequisites</dt><dd>{_tid_refs(telemetry)}</dd>"
            f"<dt>Validation history</dt><dd>{_tid_validation_history(validations)}</dd>"
            f"<dt>Limitations</dt><dd>{_esc('; '.join(dict.fromkeys(limitations)) or 'None recorded')}</dd>"
            f"<dt>Dependencies</dt><dd>{_esc(', '.join(str(value) for value in (analytic.get('dependencies') or [])) or 'None recorded')}</dd>"
            "</dl></article>"
        )
    return '<div class="tid-dossiers">' + "".join(cards) + "</div>" if cards else ""


def _tid_expired(value: object) -> bool:
    raw = str(value or "").strip().replace("Z", "+00:00")
    if not raw:
        return False
    try:
        return datetime.datetime.fromisoformat(raw).astimezone(
            datetime.timezone.utc
        ) < datetime.datetime.now(datetime.timezone.utc)
    except ValueError:
        return False


def _tid_priority(rows: list[dict]) -> tuple[str, int, str | None]:
    """Return display value, weight, and an explicit vocabulary-drift reason.

    The TID pack owns this field, so the cockpit cannot launder an undeclared
    value into a zero score. Unknown values sort ahead of critical until the
    producer/schema is corrected and remain visible in the queue explanation.
    """
    values = [str(row.get("priority") or "low").strip().lower() for row in rows]
    unknown = sorted({value for value in values if value not in _TID_PRIORITY_WEIGHT})
    if unknown:
        raw = ", ".join(unknown)
        return "unknown", 500, f"unknown priority value(s): {raw} — schema/producer drift"
    value = max(values, key=_TID_PRIORITY_WEIGHT.__getitem__, default="low")
    return value, _TID_PRIORITY_WEIGHT[value], None


def _v_tid_validation_queue(_box: dict) -> str:
    data = _tid_application_data()
    roles, index = data["roles"], data["index"]
    gaps = roles.get("detection_gap", [])
    queue = []
    for analytic in roles.get("detection_analytic", []):
        validations = [
            row
            for row in roles.get("validation_result", [])
            if _tid_match(index, row.get("analytic_ref")) is analytic
        ]
        latest = _tid_latest(validations)
        linked_gaps = [
            row
            for row in gaps
            if _tid_ref(row.get("analytic_ref"))
            in {_tid_ref(analytic.get("id")), _tid_path(analytic)}
        ]
        gap_ids = {_tid_ref(row.get("id") or _tid_path(row)) for row in linked_gaps}
        linked_decisions = [
            row
            for row in roles.get("defensive_decision", [])
            if _tid_ref(row.get("gap_ref")) in gap_ids
        ]
        priority, score, priority_drift = _tid_priority(linked_gaps)
        reasons = []
        reasons.append(f"{priority} consequence")
        if priority_drift:
            reasons.append(priority_drift)
        if not latest:
            score += 80
            reasons.append("no validation result")
        elif str(latest.get("analytic_revision") or "") != str(analytic.get("revision") or ""):
            score += 60
            reasons.append("analytic revision changed")
        if latest and _tid_expired(latest.get("valid_until")):
            score += 40
            reasons.append("validation stale")
        if any(
            row.get("needs_review") or row.get("review_state") == "pending"
            for row in [*linked_gaps, *linked_decisions]
        ):
            score += 20
            reasons.append("review pending")
        queue.append((score, analytic, latest, priority, reasons))
    queue.sort(key=lambda item: (-item[0], str(item[1].get("title") or item[1].get("id") or "")))
    rows = [
        [
            _tid_link(analytic),
            _tid_state((latest or {}).get("result") or "not-run"),
            _esc(priority),
            str(score),
            _esc(" · ".join(reasons)),
            _esc(str((latest or {}).get("executed_at") or "Never")),
        ]
        for score, analytic, latest, priority, reasons in queue
    ]
    return (
        '<div class="tid-table-wrap tid-validation-queue">'
        + _html_table(
            [
                "Analytic",
                "Latest validation",
                "Consequence",
                "Priority score",
                "Inspectable reasons",
                "Executed",
            ],
            rows,
        )
        + "</div>"
        if rows
        else ""
    )


def _v_tid_gap_workbench(_box: dict) -> str:
    data = _tid_application_data()
    roles, index = data["roles"], data["index"]
    decisions = roles.get("defensive_decision", [])
    rows = []
    for gap in sorted(
        roles.get("detection_gap", []),
        key=lambda row: (
            str(row.get("status") or ""),
            str(row.get("title") or row.get("id") or ""),
        ),
    ):
        linked = [row for row in decisions if _tid_match(index, row.get("gap_ref")) is gap]
        decision = _tid_latest(linked)
        expiry = (decision or {}).get("expires_at") or gap.get("expires_at")
        expiry_label = str(expiry or "—") + (" · expired" if _tid_expired(expiry) else "")
        version = (decision or {}).get("expected_version")
        review = (decision or {}).get("review_state") or (
            "pending" if (decision or {}).get("needs_review") else "not-required"
        )
        rows.append(
            [
                _tid_link(gap),
                _tid_state(gap.get("status")),
                _esc(str(gap.get("priority") or "unknown")),
                _tid_link(decision),
                _esc(
                    str(
                        (decision or {}).get("rationale")
                        or gap.get("rationale")
                        or "No rationale recorded"
                    )
                ),
                _esc(
                    "; ".join(str(value) for value in ((decision or {}).get("alternatives") or []))
                    or "None recorded"
                ),
                _esc(
                    str(
                        (decision or {}).get("owner")
                        or (decision or {}).get("decided_by")
                        or gap.get("owner")
                        or "Unassigned"
                    )
                ),
                _esc(
                    str(
                        (decision or {}).get("decided_at")
                        or gap.get("updated")
                        or gap.get("as_of")
                        or "—"
                    )
                ),
                _esc(str(version if version is not None else "—")),
                _tid_state(review),
                _esc(expiry_label),
            ]
        )
    return (
        '<div class="tid-table-wrap tid-gap-workbench">'
        + _html_table(
            [
                "Gap",
                "Lifecycle",
                "Priority",
                "Decision",
                "Rationale",
                "Alternatives",
                "Owner",
                "Timestamp",
                "Expected version",
                "Review",
                "Expires",
            ],
            rows,
        )
        + "</div>"
        if rows
        else ""
    )
