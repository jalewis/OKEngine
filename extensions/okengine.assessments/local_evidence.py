#!/usr/bin/env python3
"""Deterministic local-only evidence resolution for CHE producers (#328).

The attestation inputs ``local_only``, ``export_policy``, ``record_checksum``,
and ``bounded_auto_accept`` are optional producer/pack conventions.  The
engine deliberately does not synthesize trust assertions; corpus_audit.py
registers these consumed fields so a nonempty source corpus with no producer
is reported as a degraded capability instead of being silently indistinguishable
from records that were evaluated and failed attestation.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

CONTRACT_VERSION = "local-evidence-resolution/v1"
REFERENCE_FIELDS = ("sources", "recent_news_refs", "mentioned_in_sources", "evidence",
                    "operator_evidence_refs")
AUTHORITY_FIELDS = ("attack_id", "authority_id", "cve_id", "ghsa_id", "osv_id")
# The attested channel resolves by EXACT page path only (okengine#980). The importer writes paths;
# an id/URL fallback let a dangling operator reference resolve to whichever record a later write
# gave that id, so a lookup-key edit on an attested record could re-point it.
OPERATOR_REFERENCE_FIELD = "operator_evidence_refs"
_FM = re.compile(r"\A---[ \t]*\n(.*?)\n---(?:\n|\Z)", re.S)


def _digest(value: Any) -> str:
    def canonical(item: Any) -> str:
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(f"Object of type {type(item).__name__} is not JSON serializable")
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=canonical).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _values(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def _ref(value: str) -> str:
    value = value.strip().removeprefix("wiki/").removesuffix(".md")
    match = re.fullmatch(r"\[\[([^]|]+)(?:\|[^]]+)?\]\]", value)
    return (match.group(1) if match else value).strip().removeprefix("wiki/").removesuffix(".md")


def _url(value: str) -> str:
    return value.strip().rstrip("/").casefold()


def _page(path: Path) -> tuple[dict[str, Any], str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = _FM.match(text)
    if not match:
        return None
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    return (fm, text[match.end():].strip()) if isinstance(fm, dict) else None


def _authority_ids(fm: dict[str, Any]) -> list[str]:
    values = [str(item).strip() for field in AUTHORITY_FIELDS
              for item in _values(fm.get(field)) if str(item).strip()]
    return sorted(set(values), key=str.casefold)


class LocalIndex:
    def __init__(self, vault: Path):
        self.wiki = vault.resolve() / "wiki"
        self.pages: dict[str, tuple[dict[str, Any], str, str]] = {}
        self.urls: dict[str, list[str]] = {}
        self.ids: dict[str, list[str]] = {}
        self.malformed: list[dict[str, str]] = []
        for path in sorted(self.wiki.rglob("*.md")) if self.wiki.is_dir() else []:
            rel = path.relative_to(self.wiki).as_posix()[:-3]
            parsed = _page(path)
            if not parsed:
                self.malformed.append({"reference": rel, "reason": "malformed-local-page"})
                continue
            fm, body = parsed
            self.pages[rel] = (fm, body, _digest({"frontmatter": fm, "body": body}))
            for item in [*_values(fm.get("id")), *_authority_ids(fm)]:
                self.ids.setdefault(str(item).casefold(), []).append(rel)
            for item in _values(fm.get("url")):
                self.urls.setdefault(_url(str(item)), []).append(rel)
        for mapping in (self.urls, self.ids):
            for key in mapping:
                mapping[key] = sorted(set(mapping[key]))

    def lookup(self, reference: str) -> tuple[list[str], str | None]:
        value = _ref(reference)
        if value.startswith(("http://", "https://")):
            found = self.urls.get(_url(value), [])
            return found, None if found else "url-not-local"
        if value in self.pages:
            return [value], None
        found = self.ids.get(value.casefold(), [])
        if found:
            return found, None
        return [], "local-page-missing" if value.startswith(
            ("sources/", "entities/", "assessments/")) else "unrecognized-reference"


def build_index(vault: Path) -> LocalIndex:
    """Build an immutable resolver index for reuse across one batch."""
    return LocalIndex(vault)


def _scope(text: str, title: str, aliases: tuple[str, ...]) -> tuple[str, str | None]:
    folded = text.casefold()
    if title and title.casefold() in folded:
        return "canonical-subject", title
    for alias in sorted(set(aliases), key=lambda item: (-len(item), item.casefold())):
        if len(alias) >= 3 and alias.casefold() in folded:
            return "alias-pending-identity-scope", alias
    return "unscoped", None


def _item(index: LocalIndex, path: str, reference: str, origin: str,
          title: str, aliases: tuple[str, ...]) -> dict[str, Any]:
    fm, body, digest = index.pages[path]
    scope, observed = _scope(body, title, aliases)
    attested = bool(
        origin.endswith(f":{OPERATOR_REFERENCE_FIELD}") and fm.get("local_only") is True
        and fm.get("export_policy") == "deny" and fm.get("retrieved_via")
        and fm.get("dataset") and fm.get("record_checksum")
    )
    return {
        "artifact": path, "artifact_digest": digest,
        "artifact_type": str(fm.get("type") or "unknown"),
        "source_id": str(fm.get("id") or "").strip() or None,
        "source_url": str(fm.get("url") or "").strip() or None,
        "source_identity": str(fm.get("publisher") or fm.get("vendor") or "").strip() or None,
        "authority_ids": _authority_ids(fm),
        "ingestion_provenance": str(fm.get("ingestion_provenance") or
                                    fm.get("source_channel") or fm.get("source_feed") or "").strip() or None,
        "evidence_access": "local-full-text" if body else "local-metadata-only",
        "evidence_lineage": str(fm.get("evidence_lineage") or "unresolved"),
        "claim_role": str(fm.get("claim_role") or "source-reporting"),
        "evidence_role": str(fm.get("evidence_role") or "").strip() or None,
        "dataset": str(fm.get("dataset") or "").strip() or None,
        "record_checksum": str(fm.get("record_checksum") or "").strip() or None,
        "retrieved_via": str(fm.get("retrieved_via") or "").strip() or None,
        "bounded_auto_accept": fm.get("bounded_auto_accept") is True,
        "authority_record_attested": attested,
        "observed_subject_label": observed, "subject_match_basis": scope,
        "identity_transfer": "required" if scope == "alias-pending-identity-scope" else "not-required",
        "published_at": fm.get("published") or fm.get("published_at") or fm.get("published_date"),
        "updated_at": fm.get("last_updated") or fm.get("updated"),
        "reference": reference, "reference_origin": origin,
    }


SELECTION_CONTRACT = "question-scoped/v1"


def _selection_policy(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """The producer declares domain vocabulary; absent policy preserves v1 behavior."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("contract") != SELECTION_CONTRACT:
        raise ValueError("unsupported evidence selection policy")
    for key in ("assessment_kinds", "current_statuses", "archived_statuses"):
        rows = value.get(key)
        if not isinstance(rows, list) or not rows or any(
                not isinstance(row, str) or not row.strip() for row in rows):
            raise ValueError(f"selection policy requires nonempty {key}")
    if set(value["current_statuses"]) & set(value["archived_statuses"]):
        raise ValueError("selection policy statuses overlap")
    tasks = value.get("collection_tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError("selection policy collection_tasks must be a mapping")
    for marker, task in tasks.items():
        if not isinstance(marker, str) or not re.fullmatch(
                r"collection-requirement:[a-z0-9]+(?:-[a-z0-9]+)*", marker):
            raise ValueError("selection policy has a malformed task marker")
        if not isinstance(task, dict):
            raise ValueError("selection policy task must be a mapping")
        for key in ("assessment_kinds", "fields", "question_kinds"):
            if not isinstance(task.get(key), list) or not task[key] or any(
                    not isinstance(row, str) or not row.strip() for row in task[key]):
                raise ValueError(f"selection policy task requires {key}")
        context = task.get("required_context")
        if not isinstance(context, dict) or not context or any(
                not isinstance(key, str) or not isinstance(item, str) or not item
                for key, item in context.items()):
            raise ValueError("selection policy task requires exact required_context")
    return value


def _known_question_kinds(question: str, policy: dict[str, Any]) -> set[str]:
    """The question kinds a policy can name: the asked question, every declared assessment family
    and every collection task's question_kinds."""
    known = {question, *policy["assessment_kinds"]}
    for task in policy.get("collection_tasks", {}).values():
        known.update(task["question_kinds"])
    return known


def _unknown_question_kind(explicit: Any, question: str, policy: dict[str, Any]) -> bool:
    """True when an evidence item carries a question_kind outside the policy's vocabulary (a typo, a
    renamed kind, a non-string). Absent (None or "") is not unknown."""
    if explicit in (None, ""):
        return False
    return not isinstance(explicit, str) or explicit not in _known_question_kinds(question, policy)


def _reference_diagnostic(reference: str, origin: str, metadata: dict[str, Any],
                          question: str, policy: dict[str, Any]) -> dict[str, Any]:
    kind, status = metadata.get("assessment_kind"), metadata.get("assessment_status")
    explicit = metadata.get("question_kind")
    scoped = kind is None or kind not in policy["assessment_kinds"] or status not in (
        policy["current_statuses"] + policy["archived_statuses"])
    reason = "direct-or-unknown-scope"
    if explicit == question:
        scoped, reason = True, "explicit-question-reference"
    elif kind in policy["assessment_kinds"] and status in policy["archived_statuses"]:
        scoped, reason = False, "archived-assessment-reference"
    elif kind in policy["assessment_kinds"] and status in policy["current_statuses"]:
        scoped = kind == question
        reason = "current-question-reference" if scoped else "unrelated-assessment-reference"
    task = policy.get("collection_tasks", {}).get(reference)
    task_match = bool(task and kind in task["assessment_kinds"] and
                      origin.rsplit(":", 1)[-1] in task["fields"] and
                      all(metadata.get("reference_context", {}).get(key) == value
                          for key, value in task["required_context"].items()))
    category = "documentary"
    if task_match:
        category = "collection-task"
        # Unknown status remains conservative; explicit relevance takes precedence.
        if status in policy["current_statuses"]:
            scoped = explicit == question or question in task["question_kinds"]
        reason = "recognized-collection-task"
    if _unknown_question_kind(explicit, question, policy):
        # FAIL CLOSED (okengine#994 [21]): an explicit question_kind is the producer's relevance
        # override, so a drifted one cannot be read as absent. That demoted a reference the producer
        # tagged as relevant to contextual. Keep it blocking and say why.
        scoped, reason = True, "unknown-question-kind"
    return {"reference": reference, "origin": origin, "category": category,
            "disposition": "blocking" if scoped else "contextual", "blocking": scoped, "reason": reason,
            "question_kind": explicit, "assessment_kind": kind, "assessment_status": status,
            "reference_context": metadata.get("reference_context", {})}


def resolve(vault: Path, subject_ref: str, question_kind: str, *,
            aliases: Iterable[str] = (), authority_ids: Iterable[str] = (),
            embedded_article_fields: Iterable[str] = ("repository_recent_articles",),
            policy_id: str = CONTRACT_VERSION,
            index: LocalIndex | None = None,
            selection_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve declared evidence already in the vault; never access the network."""
    selection_policy = _selection_policy(selection_policy)
    index, subject_key = index or build_index(vault), _ref(subject_ref)
    subject = index.pages.get(subject_key)
    base = {"contract": CONTRACT_VERSION, "policy_id": policy_id,
            "subject_ref": subject_key, "question_kind": question_kind}
    if selection_policy is not None:
        base.update(selection_policy=selection_policy, reference_diagnostics=[])
    if not subject:
        return {**base, "status": "failed", "resolved": [], "held_alias_matches": [],
                "unresolved": [{"reference": subject_key, "origin": "subject",
                                "reason": "subject-not-local"}], "malformed": index.malformed,
                "searched": {"local_pages": len(index.pages), "declared_references": 0},
                "snapshot_digest": _digest({"pages": sorted(index.pages), **(
                    {"selection_policy": selection_policy, "question": question_kind,
                     "policy": policy_id, "subject": subject_key} if selection_policy is not None else {})})}
    fm, _body, subject_digest = subject
    title = str(fm.get("title") or subject_key.rsplit("/", 1)[-1]).strip()
    aliases = tuple(dict.fromkeys(str(x).strip() for x in
                    [*aliases, *_values(fm.get("aliases"))] if str(x).strip()))
    references: list[tuple[str, str]] = []
    reference_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    malformed: list[dict[str, str]] = list(index.malformed)
    for field in REFERENCE_FIELDS:
        for value in _values(fm.get(field)):
            if isinstance(value, str) and value.strip():
                references.append((value.strip(), f"{subject_key}:{field}"))
            elif isinstance(value, dict):
                ref = next((str(value.get(k) or "").strip() for k in
                            ("source_ref", "path", "url", "id") if value.get(k)), "")
                if ref:
                    reference_metadata[(ref, f"{subject_key}:{field}")] = {
                        "question_kind": value.get("question_kind")}
                (references if ref else malformed).append(
                    (ref, f"{subject_key}:{field}") if ref else
                    {"reference": repr(value), "reason": "malformed-reference",
                     **({"origin": f"{subject_key}:{field}"} if selection_policy is not None else {})})
            else:
                malformed.append({"reference": repr(value), "reason": "malformed-reference",
                     **({"origin": f"{subject_key}:{field}"} if selection_policy is not None else {})})
    for aid in authority_ids:
        references.append((str(aid), f"{subject_key}:authority_ids"))
    for path, (afm, _abody, _digest_value) in index.pages.items():
        handles = [afm.get("subject_ref") or afm.get("subject") or ""]
        if selection_policy is not None:
            handles = [afm.get("subject_ref"), afm.get("subject")]
        if afm.get("type") != "assessment" or subject_key not in {
                _ref(str(handle or "")) for handle in handles}:
            continue
        if selection_policy is None:
            for value in _values(afm.get("sources")):
                if isinstance(value, str):
                    references.append((value, f"{path}:sources"))
            for evidence in _values(afm.get("adversarial_evidence")):
                if isinstance(evidence, dict) and evidence.get("source"):
                    references.append((str(evidence["source"]), f"{path}:adversarial_evidence"))
            continue
        context = {"assessment_kind": afm.get("assessment_kind"),
                   "assessment_status": afm.get("status")}
        for field in ("sources", "adversarial_evidence"):
            for value in _values(afm.get(field)):
                ref = value if isinstance(value, str) else (
                    next((str(value.get(k) or "").strip() for k in
                          ("source", "source_ref", "path", "url", "id") if value.get(k)), "")
                    if isinstance(value, dict) else "")
                if not isinstance(ref, str) or not ref.strip():
                    reference_metadata[(repr(value), f"{path}:{field}")] = {
                        **context, "reference_context": value if isinstance(value, dict) else {},
                        "question_kind": value.get("question_kind") if isinstance(value, dict) else None}
                    malformed.append({"reference": repr(value), "origin": f"{path}:{field}",
                                      "reason": "malformed-reference"})
                    continue
                origin = f"{path}:{field}"
                references.append((ref.strip(), origin))
                reference_metadata[(ref.strip(), origin)] = {
                    **context, "reference_context": value if isinstance(value, dict) else {},
                    "question_kind": value.get("question_kind")
                    if isinstance(value, dict) else None}
    resolved, unresolved, seen = [], [], set()
    diagnostics = []
    if selection_policy is not None:
        for entry in malformed:
            if entry["reason"] != "malformed-reference":
                continue
            origin = entry.get("origin", f"{subject_key}:evidence")
            diagnostic = _reference_diagnostic(entry["reference"], origin,
                reference_metadata.get((entry["reference"], origin), {}), question_kind, selection_policy)
            diagnostic["resolution_reason"] = "malformed-reference"
            diagnostics.append(diagnostic)
            if diagnostic["blocking"]:
                unresolved.append({"reference": entry["reference"], "origin": origin,
                                   "reason": "malformed-reference"})
    for reference, origin in _ordered_references(references, subject_key, question_kind, selection_policy, reference_metadata, index):
        diagnostic = None
        if selection_policy is not None:
            diagnostic = _reference_diagnostic(reference, origin,
                reference_metadata.get((reference, origin), {}), question_kind, selection_policy)
            diagnostics.append(diagnostic)
            if diagnostic["category"] == "collection-task":
                continue
        if origin.endswith(f":{OPERATOR_REFERENCE_FIELD}"):
            exact = _ref(reference)
            paths, reason = ([exact], None) if exact in index.pages else ([], "operator-ref-not-a-page-path")
        else:
            paths, reason = index.lookup(reference)
        if diagnostic is not None:
            if len(paths) > 1:
                paths, reason = [], "ambiguous-local-reference"
            diagnostic["resolution_reason"] = reason
            if reason == "unrecognized-reference":
                diagnostic["category"] = "contextual-provenance"
        if not paths and (diagnostic is None or diagnostic["disposition"] == "blocking"):
            unresolved.append({"reference": reference, "origin": origin, "reason": reason})
        for path in paths:
            if selection_policy is not None and index.pages[path][0].get("type") == "assessment":
                diagnostic.update(category="assessment-conclusion", resolution_reason="not-independent-evidence")
                if diagnostic["disposition"] == "blocking":
                    unresolved.append({"reference": reference, "origin": origin,
                                       "reason": "not-independent-evidence"})
                continue
            if path not in seen:
                resolved.append(_item(index, path, reference, origin, title, aliases)); seen.add(path)
    for article_field in embedded_article_fields:
      for article in _values(fm.get(str(article_field))):
        if not isinstance(article, dict):
            malformed.append({"reference": repr(article), "reason": "malformed-embedded-article"})
            if selection_policy is not None:
                origin = f"{subject_key}:{article_field}"
                diagnostic = _reference_diagnostic(repr(article), origin, {}, question_kind, selection_policy)
                diagnostic["resolution_reason"] = "malformed-embedded-article"
                diagnostics.append(diagnostic)
                unresolved.append({"reference": repr(article), "origin": origin,
                                   "reason": "malformed-embedded-article"})
            continue
        url = str(article.get("url") or "").strip()
        local = index.urls.get(_url(url), []) if url else []
        if selection_policy is not None and len(local) > 1:
            origin = f"{subject_key}:{article_field}"
            diagnostic = _reference_diagnostic(url, origin, {}, question_kind, selection_policy)
            diagnostic["resolution_reason"] = "ambiguous-local-reference"
            diagnostics.append(diagnostic)
            unresolved.append({"reference": url, "origin": origin,
                               "reason": "ambiguous-local-reference"})
            continue
        if local:
            for path in local:
                if path not in seen:
                    resolved.append(_item(index, path, url, f"{subject_key}:{article_field}", title, aliases)); seen.add(path)
            continue
        scope, observed = _scope(str(article.get("title") or ""), title, aliases)
        resolved.append({"artifact": f"{subject_key}#article:{article.get('id') or _digest(article)[7:19]}",
            "artifact_digest": _digest(article), "artifact_type": "article-reference",
            "source_id": article.get("id"), "source_url": url or None,
            "source_identity": article.get("publisher"), "authority_ids": [],
            "ingestion_provenance": "article-repository", "evidence_access": "local-metadata-only",
            "evidence_lineage": "unresolved", "claim_role": "discovery-reference",
            "observed_subject_label": observed, "subject_match_basis": scope,
            "identity_transfer": "required" if scope == "alias-pending-identity-scope" else "not-required",
            "published_at": article.get("published_at"), "updated_at": article.get("updated_at"),
            "reference": url or str(article.get("id") or ""),
            "reference_origin": f"{subject_key}:{article_field}"})
    resolved.sort(key=lambda row: row["artifact"])
    unresolved = sorted({(x["reference"], x["origin"], x["reason"]): x for x in unresolved}.values(),
                        key=lambda row: (row["reference"].casefold(), row["origin"]))
    snapshot_data = {"subject": subject_digest,
                        "evidence": [(x["artifact"], x["artifact_digest"]) for x in resolved],
                        "unresolved": unresolved, "policy": policy_id, "question": question_kind}
    if selection_policy is not None:
        base.update(reference_diagnostics=diagnostics)
        snapshot_data.update(selection_policy=selection_policy, diagnostics=diagnostics,
                             resolved=resolved, malformed=malformed, aliases=aliases)
    snapshot = _digest(snapshot_data)
    return {**base, "subject_title": title, "status": "resolved", "resolved": resolved,
            "unresolved": unresolved,
            "held_alias_matches": [x for x in resolved if x["identity_transfer"] == "required"],
            "malformed": malformed, "searched": {"local_pages": len(index.pages),
            "declared_references": len(references), "resolved_artifacts": len(resolved),
            "unresolved_references": len(unresolved)}, "snapshot_digest": snapshot}


def _ordered_references(references: list[tuple[str, str]], subject_key: str, question: str,
                        policy: dict[str, Any] | None,
                        metadata: dict[tuple[str, str], dict[str, Any]],
                        index: LocalIndex) -> list[tuple[str, str]]:
    """Keep scoped deduplication from discarding the strongest declared provenance."""
    if policy is None:
        return sorted(references, key=lambda row: (row[0].casefold(), row[1]))

    def priority(row: tuple[str, str]) -> tuple[bool, bool, str, str]:
        reference, origin = row
        diagnostic = _reference_diagnostic(reference, origin,
            metadata.get((reference, origin), {}), question, policy)
        exact_operator = (origin == f"{subject_key}:{OPERATOR_REFERENCE_FIELD}"
                          and _ref(reference) in index.pages)
        # Canonical direct operator declarations are load-bearing under this policy;
        # an assessment duplicate must never replace their exact-path provenance.
        return (not exact_operator, not diagnostic["blocking"], reference.casefold(), origin)

    return sorted(references, key=priority)
