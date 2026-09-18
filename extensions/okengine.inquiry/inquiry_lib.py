#!/usr/bin/env python3
"""inquiry_lib.py — okengine.inquiry: load, normalize and contract-check inquiry pages.

The SINGLE source of what an inquiry means. Both lanes (collect, dossier) and the deploy-time
gate (framework validate -> check_inquiries) read inquiries through here, so "what counts as a
valid inquiry" cannot drift between the thing that runs one and the thing that admits one —
the invariant-at-one-boundary-only failure this repo keeps re-learning.

Term normalization is the one piece of leniency. An inquiry may write terms the short way:

    terms: [AI alignment, superintelligence risk]

or the long way, when a term needs its own knobs:

    terms:
      - query: AI alignment
        label: alignment
        params: {source_tier: "2"}

`normalize_terms` coerces the first into the second, so every consumer sees one shape and the
simple case stays simple. A term with no `query` is an ERROR, not a skip: silently dropping it
would make an inquiry quietly collect less than it declares, which is the failure mode the
dossier's DRY reporting exists to surface.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
NS = "inquiry"
OPEN_STATUSES = ("open",)
STATUSES = ("open", "paused", "closed")

# Line-anchored: a fenced ```yaml block inside a body must never be mistaken for frontmatter
# (okengine#349 — the unanchored form leaked a body block into a page's fields).
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)", re.S)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,78}[a-z0-9]$")


@dataclass
class Term:
    query: str
    label: str = ""
    params: dict[str, str] = field(default_factory=dict)
    since: str = ""

    @property
    def key(self) -> str:
        """Stable identity for state/dossier keying. The LABEL when given (an operator can
        rename the query text without losing the term's collection history), else the query."""
        return self.label or self.query


@dataclass
class Inquiry:
    slug: str
    path: Path
    question: str
    status: str
    terms: list[Term]
    connector: str = ""
    collection_params: dict[str, str] = field(default_factory=dict)
    min_source_tier: str = ""
    exclude_domains: list[str] = field(default_factory=list)
    opened: str = ""
    title: str = ""
    fm: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


def split_frontmatter(p: Path) -> tuple[dict, str]:
    """Return (frontmatter, body). Unreadable or frontmatter-less pages yield ({}, text)."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), text[m.end():]


def normalize_terms(raw: Any) -> tuple[list[Term], list[str]]:
    """Coerce a declared `terms:` value into Terms. Returns (terms, errors).

    A bare scalar becomes a one-term list — `terms: AI alignment` is a typo-shaped but
    unambiguous declaration, and the field_shapes `list` coercion at the write path already
    splits a comma string, so accepting the scalar here keeps the two boundaries agreeing.
    """
    errors: list[str] = []
    if raw is None:
        return [], ["terms: required"]
    items = raw if isinstance(raw, list) else [raw]
    if not items:
        return [], ["terms: declared but empty — an inquiry with no terms collects nothing"]
    out: list[Term] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if isinstance(item, str):
            item = {"query": item}
        if not isinstance(item, dict):
            errors.append(f"terms[{i}]: must be a string or a mapping, got {type(item).__name__}")
            continue
        query = str(item.get("query") or "").strip()
        if not query:
            errors.append(f"terms[{i}]: `query` is required and must be non-empty")
            continue
        params = item.get("params") or {}
        if not isinstance(params, dict):
            errors.append(f"terms[{i}].params: must be a mapping")
            params = {}
        term = Term(query=query,
                    label=str(item.get("label") or "").strip(),
                    params={str(k): str(v) for k, v in params.items()},
                    since=str(item.get("since") or "").strip())
        if term.key in seen:
            errors.append(f"terms[{i}]: duplicate term key {term.key!r} — "
                          "two terms with the same key share one collection history")
            continue
        seen.add(term.key)
        out.append(term)
    return out, errors


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def load_inquiries(wiki: Path | None = None) -> tuple[list[Inquiry], list[str]]:
    """Read every inquiry page. Returns (inquiries, errors); errors name the page."""
    wiki = wiki or WIKI
    root = wiki / NS
    inquiries: list[Inquiry] = []
    errors: list[str] = []
    if not root.is_dir():
        return inquiries, errors
    for p in sorted(root.rglob("*.md")):
        if p.name.startswith(("_", ".")) or p.name.upper().startswith("INDEX"):
            continue
        fm, _ = split_frontmatter(p)
        if str(fm.get("type") or "") != "inquiry":
            continue
        where = f"{NS}/{p.stem}"
        page_errors: list[str] = []
        question = str(fm.get("question") or "").strip()
        if not question:
            page_errors.append("question: required and must be non-empty")
        status = str(fm.get("status") or "").strip()
        if status not in STATUSES:
            page_errors.append(f"status: must be one of {', '.join(STATUSES)} (got {status!r})")
        if not _SLUG.match(p.stem):
            page_errors.append(f"slug {p.stem!r}: must be lowercase kebab-case")
        terms, term_errors = normalize_terms(fm.get("terms"))
        page_errors.extend(term_errors)
        params = fm.get("collection_params") or {}
        if not isinstance(params, dict):
            page_errors.append("collection_params: must be a mapping")
            params = {}
        errors.extend(f"{where}: {e}" for e in page_errors)
        inquiries.append(Inquiry(
            slug=p.stem, path=p, question=question, status=status, terms=terms,
            connector=str(fm.get("connector") or "").strip(),
            collection_params={str(k): str(v) for k, v in params.items()},
            min_source_tier=str(fm.get("min_source_tier") or "").strip(),
            exclude_domains=_as_list(fm.get("exclude_domains")),
            opened=str(fm.get("opened") or ""), title=str(fm.get("title") or ""), fm=fm))
    return inquiries, errors


def connector_errors(inquiries: list[Inquiry], connector_ids: set[str]) -> list[str]:
    """The half a schema cannot express: a declared inquiry must have reachable INGRESS.

    An inquiry that names a connector nothing provides is a question the vault will never
    gather evidence for, and it fails silently — the lane skips it, the dossier stays empty,
    and the operator reads the empty dossier as "nothing is happening in this field" rather
    than "I misspelled the connector". Fail it at deploy instead.

    A CLOSED inquiry is exempt: its collection is deliberately over, and a deployment that
    retires a connector should not be blocked by the archived questions that once used it.
    """
    out: list[str] = []
    for inq in inquiries:
        if inq.status == "closed":
            continue
        if not inq.connector:
            out.append(f"{NS}/{inq.slug}: no `connector:` declared — "
                       "the inquiry states a question with no way to collect evidence for it")
            continue
        if inq.connector not in connector_ids:
            known = ", ".join(sorted(connector_ids)) or "(none discovered)"
            out.append(f"{NS}/{inq.slug}: connector {inq.connector!r} is not among the "
                       f"discovered source connectors: {known}")
    return out
