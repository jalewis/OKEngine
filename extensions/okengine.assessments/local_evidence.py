#!/usr/bin/env python3
"""Deterministic local-only evidence resolution for CHE producers (#328)."""
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
        origin.endswith(":operator_evidence_refs") and fm.get("local_only") is True
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


def resolve(vault: Path, subject_ref: str, question_kind: str, *,
            aliases: Iterable[str] = (), authority_ids: Iterable[str] = (),
            embedded_article_fields: Iterable[str] = ("repository_recent_articles",),
            policy_id: str = CONTRACT_VERSION,
            index: LocalIndex | None = None) -> dict[str, Any]:
    """Resolve declared evidence already in the vault; never access the network."""
    index, subject_key = index or build_index(vault), _ref(subject_ref)
    subject = index.pages.get(subject_key)
    base = {"contract": CONTRACT_VERSION, "policy_id": policy_id,
            "subject_ref": subject_key, "question_kind": question_kind}
    if not subject:
        return {**base, "status": "failed", "resolved": [], "held_alias_matches": [],
                "unresolved": [{"reference": subject_key, "origin": "subject",
                                "reason": "subject-not-local"}], "malformed": index.malformed,
                "searched": {"local_pages": len(index.pages), "declared_references": 0},
                "snapshot_digest": _digest({"pages": sorted(index.pages)})}
    fm, _body, subject_digest = subject
    title = str(fm.get("title") or subject_key.rsplit("/", 1)[-1]).strip()
    aliases = tuple(dict.fromkeys(str(x).strip() for x in
                    [*aliases, *_values(fm.get("aliases"))] if str(x).strip()))
    references: list[tuple[str, str]] = []
    malformed: list[dict[str, str]] = list(index.malformed)
    for field in REFERENCE_FIELDS:
        for value in _values(fm.get(field)):
            if isinstance(value, str) and value.strip():
                references.append((value.strip(), f"{subject_key}:{field}"))
            elif isinstance(value, dict):
                ref = next((str(value.get(k) or "").strip() for k in
                            ("source_ref", "path", "url", "id") if value.get(k)), "")
                (references if ref else malformed).append(
                    (ref, f"{subject_key}:{field}") if ref else
                    {"reference": repr(value), "reason": "malformed-reference"})
            else:
                malformed.append({"reference": repr(value), "reason": "malformed-reference"})
    for aid in authority_ids:
        references.append((str(aid), f"{subject_key}:authority_ids"))
    for path, (afm, _abody, _digest_value) in index.pages.items():
        if afm.get("type") != "assessment" or _ref(str(
                afm.get("subject_ref") or afm.get("subject") or "")) != subject_key:
            continue
        for value in _values(afm.get("sources")):
            if isinstance(value, str):
                references.append((value, f"{path}:sources"))
        for evidence in _values(afm.get("adversarial_evidence")):
            if isinstance(evidence, dict) and evidence.get("source"):
                references.append((str(evidence["source"]), f"{path}:adversarial_evidence"))
    resolved, unresolved, seen = [], [], set()
    for reference, origin in sorted(references, key=lambda row: (row[0].casefold(), row[1])):
        paths, reason = index.lookup(reference)
        if not paths:
            unresolved.append({"reference": reference, "origin": origin, "reason": reason})
        for path in paths:
            if path not in seen:
                resolved.append(_item(index, path, reference, origin, title, aliases)); seen.add(path)
    for article_field in embedded_article_fields:
      for article in _values(fm.get(str(article_field))):
        if not isinstance(article, dict):
            malformed.append({"reference": repr(article), "reason": "malformed-embedded-article"}); continue
        url = str(article.get("url") or "").strip()
        local = index.urls.get(_url(url), []) if url else []
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
    snapshot = _digest({"subject": subject_digest,
                        "evidence": [(x["artifact"], x["artifact_digest"]) for x in resolved],
                        "unresolved": unresolved, "policy": policy_id, "question": question_kind})
    return {**base, "subject_title": title, "status": "resolved", "resolved": resolved,
            "unresolved": unresolved,
            "held_alias_matches": [x for x in resolved if x["identity_transfer"] == "required"],
            "malformed": malformed, "searched": {"local_pages": len(index.pages),
            "declared_references": len(references), "resolved_artifacts": len(resolved),
            "unresolved_references": len(unresolved)}, "snapshot_digest": snapshot}
