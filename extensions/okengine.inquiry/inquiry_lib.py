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

import json
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
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,78}[a-z0-9]\Z")


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
class Question:
    """A standing question an inquiry wants ANSWERED, as opposed to a term it wants COLLECTED.

    `key` is the question's stable identity: it names the one assessment page that answers it
    (`assessment_slug`), so rewording the question text updates that page instead of spawning a
    second one — the okengine#54 duplicate-page loop, prevented at the identity rather than
    cleaned up after."""
    key: str
    text: str


_KEY_CHARS = re.compile(r"[^a-z0-9]+")
MAX_KEY = 40
_KEY = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?\Z")


def question_key(text: str) -> str:
    """Kebab-case identity for a question declared without an explicit `key`."""
    return _KEY_CHARS.sub("-", text.lower()).strip("-")[:MAX_KEY].strip("-")


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
    questions: list[Question] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def standing_questions(self) -> list[Question]:
        """What the analysis lane answers: the declared `questions`, else the headline
        `question` itself. An inquiry always has at least one thing to answer."""
        return self.questions or [Question(key="main", text=self.question)]

    def assessment_slug(self, question: Question) -> str:
        """The ONE page that answers `question`. The selector hands this to the model rather
        than letting it name the page, so a rerun updates instead of duplicating."""
        return f"{self.slug}-{question.key}"


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


def normalize_questions(raw: Any) -> tuple[list[Question], list[str]]:
    """Coerce a declared `questions:` value into Questions. Returns (questions, errors).

    Absent is fine (the headline question stands in). Otherwise the same strictness as terms:
    a question with no text, or two questions sharing a key, is an error — two questions on
    one key would be answered by one assessment page, and one of them silently never would."""
    if raw is None:
        return [], []
    items = raw if isinstance(raw, list) else [raw]
    out: list[Question] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            errors.append(f"questions[{i}]: must be a string or a mapping, "
                          f"got {type(item).__name__}")
            continue
        text = str(item.get("question") or "").strip()
        if not text:
            errors.append(f"questions[{i}]: `question` is required and must be non-empty")
            continue
        key = str(item.get("key") or "").strip() or question_key(text)
        if not _KEY.match(key):
            errors.append(f"questions[{i}]: key {key!r} must be lowercase kebab-case, "
                          f"at most {MAX_KEY} characters")
            continue
        if key in seen:
            errors.append(f"questions[{i}]: duplicate question key {key!r} — "
                          "two questions on one key are answered by one assessment")
            continue
        seen.add(key)
        out.append(Question(key=key, text=text))
    return out, errors


# Shard segments are only legal after `concepts/`: `entities/x` is a different namespace, not a
# sharded concept, and must be rejected rather than read as the concept `x`.
_FIELD_REF = re.compile(r"^(?:\[\[)?(?:concepts/(?:[a-z0-9][a-z0-9-]*/)*)?([a-z0-9][a-z0-9-]*)"
                        r"(?:[#|][^\]]*)?(?:\]\])?\Z")


def normalize_fields(raw: Any) -> tuple[list[str], list[str]]:
    """Coerce a declared `fields:` value into concept slugs. Returns (slugs, errors).

    `fields` names the concept fields this inquiry bears on, for okengine.lacuna to map. It is a
    DECLARATION, not an inference: on the first live vault the inquiry's evidence linked no concept
    at all, so deriving fields from evidence would nominate nothing. Accepts `slug`,
    `concepts/<shard>/slug` or `[[concepts/…]]`; anything else is an error, never a skip."""
    if raw is None:
        return [], []
    items = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    errors: list[str] = []
    for i, item in enumerate(items):
        m = _FIELD_REF.match(str(item).strip()) if isinstance(item, str) else None
        if not m:
            errors.append(f"fields[{i}]: {item!r} is not a concept reference "
                          "(slug, concepts/<shard>/<slug> or [[concepts/…]])")
            continue
        if m.group(1) not in out:
            out.append(m.group(1))
    return out, errors


def concept_page(wiki: Path, slug: str) -> Path | None:
    """The concept page for `slug`, wherever the partition filed it (never a hand-built path)."""
    root = wiki / "concepts"
    if not root.is_dir():
        return None
    return next(iter(sorted(root.rglob(f"{slug}.md"))), None)


def field_errors(inquiries: list["Inquiry"], wiki: Path) -> list[str]:
    """A declared field with no concept page is a question lacuna can never map, and it fails
    silently: the nomination is dropped and the operator reads an idle lacuna as "nothing to
    find". Same reachability class as a missing connector. Closed inquiries are exempt."""
    out: list[str] = []
    for inq in inquiries:
        if inq.status == "closed":
            continue
        for slug in inq.fields:
            if concept_page(wiki, slug) is None:
                out.append(f"{NS}/{inq.slug}: field {slug!r} has no concept page — create "
                           f"concepts/**/{slug}.md or remove it from `fields`")
    return out


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
        questions, question_errors = normalize_questions(fm.get("questions"))
        page_errors.extend(question_errors)
        fields, field_errs = normalize_fields(fm.get("fields"))
        page_errors.extend(field_errs)
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
            opened=str(fm.get("opened") or ""), title=str(fm.get("title") or ""), fm=fm,
            questions=questions, fields=fields))
    return inquiries, errors


# ── evidence provenance: which inquiry term collected which record ──────────
#
# A connector record says nothing about the query that fetched it, and one record is often
# returned by several terms, so provenance cannot live on the record or on the compiled source
# page as a scalar field. It lives here instead: `inquiry-collect` writes a ledger mapping each
# archived record DIRECTORY (`raw/connectors/<archive>/records/<native-id>`) to every
# `<inquiry>::<term>` that returned it, and a source page joins back through the `raw:` path the
# compile lane already carries (it must — `select_raw_batch` reads `raw:` to know what is done).
#
# Deliberately NOT a frontmatter field the compile agent is asked to carry. That lane is a model,
# and a model-carried field is dropped the day the model changes (okengine#194 lost the whole
# provenance block that way). The join is deterministic and multi-valued; a field would be
# neither.
#
# A source compiled from a whole search-RESPONSE page (`raw/connectors/<archive>/raw/<digest>`)
# cannot be attributed: that page holds every result of one query, so it maps to no one record.
# It is left unattributed, never guessed.

RECORD_DIR = re.compile(r"^(raw/connectors/[^/]+/records/[^/]+)/")


def ledger_path() -> Path:
    """Resolved at call time so a patched VAULT (tests, a second vault) is always honored."""
    return VAULT / ".okengine/inquiry/evidence-ledger.json"


def load_ledger(path: Path | None = None) -> dict:
    """`{"records": {record_dir: {"connector", "native_id", "terms": {term_id: {first_seen,
    last_seen}}}}}`. Absent or corrupt reads as empty: a truncated ledger must not wedge
    collection, and the next run re-attributes whatever its terms still return."""
    try:
        data = json.loads((path or ledger_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"records": {}}
    if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
        return {"records": {}}
    return data


def save_ledger(ledger: dict, path: Path | None = None) -> None:
    """Atomic replace: the dossier reads this file while a collect run may be writing it."""
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def term_id(slug: str, term_key: str) -> str:
    """The one spelling of a term's identity, shared by collect state, ledger and dossier."""
    return f"{slug}::{term_key}"


def attribute(ledger: dict, record_dir: str, *, connector: str, native_id: str,
              term: str, today: str) -> bool:
    """Record that `term` returned the record at `record_dir`. True when this term had never
    returned it before — the NEW-evidence count, as opposed to the connector's returned-this-run
    count, which re-counts the same capped result page every day."""
    entry = ledger.setdefault("records", {}).setdefault(
        record_dir, {"connector": connector, "native_id": native_id, "terms": {}})
    terms = entry.setdefault("terms", {})
    seen = terms.get(term)
    if seen:
        seen["last_seen"] = today
        return False
    terms[term] = {"first_seen": today, "last_seen": today}
    return True


def records_by_term(ledger: dict) -> dict[str, set[str]]:
    """term id -> the record directories it has ever returned."""
    out: dict[str, set[str]] = {}
    for record_dir, entry in (ledger.get("records") or {}).items():
        for term in (entry.get("terms") or {}):
            out.setdefault(term, set()).add(record_dir)
    return out


def record_dir_of(raw: Any) -> str | None:
    """The per-record archive directory a source page's `raw:` points into, or None. `raw:` is
    a string or a list; the first per-record path wins."""
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    for value in values:
        m = RECORD_DIR.match(str(value).strip().lstrip("/"))
        if m:
            return m.group(1)
    return None


def compiled_sources(wiki: Path, ledger: dict) -> dict[str, list[str]]:
    """record dir -> the source pages compiled from it (refs without `.md`). Only records the
    ledger knows are kept; the walk is confined to the sources namespace, the only place a
    compiled record lands (select_raw_batch reads the same root)."""
    known = set(ledger.get("records") or {})
    out: dict[str, list[str]] = {}
    root = wiki / "sources"
    if not known or not root.is_dir():
        return out
    for page in sorted(root.rglob("*.md")):
        if page.name.startswith(("_", ".")) or page.name.upper().startswith("INDEX"):
            continue
        fm, _ = split_frontmatter(page)
        record_dir = record_dir_of(fm.get("raw"))
        if record_dir in known:
            out.setdefault(record_dir, []).append(page.relative_to(wiki).as_posix()[:-3])
    return out


def evidence_by_term(wiki: Path, inquiry: Inquiry, ledger: dict,
                     compiled: dict[str, list[str]] | None = None) -> dict[str, dict]:
    """term key -> {"records": n collected, "sources": [compiled source refs]}.

    THE question a downstream analysis lane asks — "what evidence does this inquiry have?" —
    answered from one place, so the dossier and any lane reasoning over an inquiry cannot
    disagree about it. Pass `compiled` to reuse one source walk across several inquiries."""
    if compiled is None:
        compiled = compiled_sources(wiki, ledger)
    by_term = records_by_term(ledger)
    out: dict[str, dict] = {}
    for term in inquiry.terms:
        dirs = by_term.get(term_id(inquiry.slug, term.key), set())
        refs = sorted({ref for d in dirs for ref in compiled.get(d, [])})
        out[term.key] = {"records": len(dirs), "sources": refs}
    return out


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
