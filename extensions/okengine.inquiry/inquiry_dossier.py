#!/usr/bin/env python3
"""inquiry_dossier.py — okengine.inquiry: render the per-inquiry dossier + index.
no_agent, deterministic, idempotent. Zero model budget.

A derived L1 `dashboard` (the #148 convention: a view, not a new page type), so no schema
fragment is needed for the output. The inquiry PAGES themselves are authored through the normal
write path; this lane never writes one.

The dossier answers three questions an operator actually has:

  1. What is this inquiry asking, and is it still open?
  2. Is the declaration WORKING — is each declared term actually returning anything?
  3. What evidence has accumulated, and does it answer the question or only decorate it?

(2) is the point. `framework validate` catches a term whose connector does not exist; nothing
catches a term whose connector exists, runs clean, and answers nothing — an inquiry that looks
healthy while collecting zero. A term with no yield for `dry_term_days` is reported DRY here, so
a misjudged query surfaces continuously instead of at the next manual review. This is the
standing detector for the failure class the extension was built to prevent.

Evidence is gathered two ways, because both directions occur in practice: the inquiry's own
`assessments:` / `predictions:` / `solutions:` lists (an author grouping known pages), and any
page carrying `inquiry: <slug>` in its frontmatter (a lane attributing what it produced). Neither
alone is complete.

Env: WIKI_PATH (/opt/vault) · OKENGINE_INQUIRY_DRY_TERM_DAYS (14)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Engine cron libs, appended so this extension's own modules keep precedence: staged one level
# up (/opt/data/scripts/ holds /opt/data/scripts/<id>/), or scripts/cron/ in a repo checkout.
_ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(_ENGINE_ROOT))
sys.path.append(str(_ENGINE_ROOT.parent / "scripts" / "cron"))
import generated_page  # noqa: E402 — keeps a regenerated page's id (okengine#933)
import inquiry_lib as lib  # noqa: E402

STATE = lib.VAULT / ".okengine/inquiry/collect-state.json"
DASH = lib.WIKI / "dashboards" / "inquiry"
DRY_DAYS = int(os.environ.get("OKENGINE_INQUIRY_DRY_TERM_DAYS", "14"))
EVIDENCE_FIELDS = ("assessments", "predictions", "solutions")
# The `assessment_kind` value that turns an assessment into a proposed remedy.
REMEDY_KIND = "remedy"
# Compiled sources listed on a dossier (newest path first); the count above is always complete.
MAX_LISTED_SOURCES = 25


def _days_since(stamp: str, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(stamp[:10])).days
    except (ValueError, TypeError):
        return None


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _bucket(fm: dict) -> str | None:
    """Which dossier section a page belongs to, or None if it is not evidence.

    A SOLUTION is not its own type (okengine#746, decided against a new type): it is an
    assessment carrying `assessment_kind: remedy`. The whole adversarial-evidence apparatus
    applies unchanged to a proposed intervention — it has evidence for and against exactly
    like a factual claim — so reusing the type was cheaper than duplicating that contract.

    The consequence is that TYPE ALONE CANNOT BUCKET A PAGE. Routing every `assessment` into
    the Assessments column would file every remedy under "is this true?" when it answers
    "would this work?", and the Solutions column would read empty forever while remedies
    existed. The kind is checked first for that reason.

    `solution` is still accepted as a type so a pack that owns one of its own is not shut out.
    """
    kind = str(fm.get("assessment_kind") or "").strip().lower()
    ptype = str(fm.get("type") or "")
    if ptype == "assessment" and kind == REMEDY_KIND:
        return "solutions"
    return {"assessment": "assessments", "prediction": "predictions",
            "solution": "solutions"}.get(ptype)


def gather_evidence(wiki: Path, inquiries: list[lib.Inquiry]) -> dict[str, dict[str, list[str]]]:
    """slug -> {field -> [page refs]}. Declared lists plus back-references, deduped."""
    found: dict[str, dict[str, list[str]]] = {
        i.slug: {f: list(lib._as_list(i.fm.get(f))) for f in EVIDENCE_FIELDS} for i in inquiries}
    slugs = {i.slug for i in inquiries}
    if not slugs or not wiki.is_dir():
        return found
    for page in sorted(wiki.rglob("*.md")):
        if page.name.startswith(("_", ".")) or page.name.upper().startswith("INDEX"):
            continue
        rel = page.relative_to(wiki).as_posix()
        if rel.startswith((f"{lib.NS}/", "dashboards/")):
            continue
        fm, _ = lib.split_frontmatter(page)
        slug = str(fm.get("inquiry") or "").strip()
        if slug not in slugs:
            continue
        bucket = _bucket(fm)
        if bucket is None:
            continue
        ref = rel[:-3] if rel.endswith(".md") else rel
        if ref not in found[slug][bucket]:
            found[slug][bucket].append(ref)
    return found


def _collected_section(term_evidence: dict[str, dict]) -> list[str]:
    """What the terms actually brought in, and how much of it the compile lane has turned into
    source pages. The gap between the two numbers is the compile backlog — shown, because an
    inquiry with hundreds of collected records and a handful of sources reads as a quiet field
    when it is really a queue."""
    records = sum(e["records"] for e in term_evidence.values())
    sources = sorted({ref for e in term_evidence.values() for ref in e["sources"]}, reverse=True)
    lines = ["## Collected evidence", ""]
    if not records:
        lines += ["_No records attributed to this inquiry yet. Attribution starts with the first "
                  "collect run that records provenance, and needs a connector that archives "
                  "per-record (`archive.enabled`)._", ""]
        return lines
    lines += [f"**{records}** record(s) collected by this inquiry's terms; **{len(sources)}** "
              "compiled into source pages so far. The rest wait on the raw→source compile lane.",
              ""]
    if sources:
        lines += [f"- [[{ref}]]" for ref in sources[:MAX_LISTED_SOURCES]]
        if len(sources) > MAX_LISTED_SOURCES:
            lines.append(f"- … and {len(sources) - MAX_LISTED_SOURCES} more")
        lines.append("")
    return lines


def nominations_path() -> Path:
    """Read by okengine.lacuna (config `inquiry_nominations_path`); resolved at call time."""
    return lib.VAULT / ".okengine/inquiry/lacuna-nominations.json"


def write_lacuna_nominations(inquiries: list[lib.Inquiry], today: date) -> int:
    """Nominate every open inquiry's declared, REAL concept fields to okengine.lacuna.

    Written in lacuna's existing nomination shape, dated today, and rewritten WHOLE every run —
    even when empty — so removing a field or closing an inquiry stops its nomination instead of
    leaving a stale row to steer discovery. A field with no concept page is not nominated (the
    validate gate reports it); lacuna maps a named field, never a dangling link."""
    rows = [{"field": slug, "nominated_at": today.isoformat(), "source": f"{lib.NS}/{inq.slug}",
             "reason": f"inquiry question: {inq.question}"}
            for inq in inquiries if inq.is_open
            for slug in inq.fields if lib.concept_page(lib.WIKI, slug) is not None]
    target = nominations_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps({"nominations": rows}, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return len(rows)


def _fields_section(inquiry: lib.Inquiry) -> list[str]:
    if not inquiry.fields:
        return []
    lines = ["## Fields (nominated to lacuna)", ""]
    for slug in inquiry.fields:
        page = lib.concept_page(lib.WIKI, slug)
        if page is None:
            lines.append(f"- `{slug}` — **no concept page**: not nominated; create it or remove "
                         "the field")
        else:
            lines.append(f"- [[{page.relative_to(lib.WIKI).as_posix()[:-3]}]]")
    return lines + [""]


def render(inquiry: lib.Inquiry, runs: dict, evidence: dict[str, list[str]], today: date,
           term_evidence: dict[str, dict] | None = None) -> str:
    term_evidence = term_evidence or {}
    lines = ["---", "type: dashboard",
             f"title: \"Inquiry: {inquiry.title or inquiry.question}\"",
             f"updated: {today.isoformat()}", "---", "",
             f"# {inquiry.title or inquiry.slug}", "",
             f"**Question.** {inquiry.question}", "",
             f"- Status: `{inquiry.status}`" + (f" · opened {inquiry.opened}" if inquiry.opened else ""),
             f"- Connector: `{inquiry.connector}`",
             f"- Page: [[{lib.NS}/{inquiry.slug}]]", ""]

    lines += ["## Terms", "",
              "| term | query | last run | last yield | new (last run) | collected | compiled "
              "| state |",
              "|---|---|---|---|---|---|---|---|"]
    dry: list[str] = []
    never: list[str] = []
    for term in inquiry.terms:
        record = runs.get(lib.term_id(inquiry.slug, term.key)) or {}
        last_run = str(record.get("last_run") or "never")
        last_yield = str(record.get("last_yield") or "")
        new = record.get("new_records")
        held = term_evidence.get(term.key) or {"records": 0, "sources": []}
        if not record:
            state = "not yet run"
        elif not record.get("ok", True):
            state = f"ERROR: {str(record.get('error') or '')[:60]}"
        elif not last_yield:
            state = "**DRY — never yielded**"
            never.append(term.key)
        else:
            age = _days_since(last_yield, today)
            if age is not None and age >= DRY_DAYS:
                state = f"**DRY — {age}d since last yield**"
                dry.append(term.key)
            else:
                state = "ok"
        lines.append(f"| `{term.key}` | {term.query} | {last_run} | {last_yield or '—'} "
                     f"| {'—' if new is None else int(new)} | {held['records']} "
                     f"| {len(held['sources'])} | {state} |")
    lines.append("")

    if never or dry:
        lines += ["> **Declared but not collecting.** "
                  "These terms are part of what this inquiry claims to gather, and they are "
                  "returning nothing. Either the query is wrong or the connector cannot reach "
                  "this material; an empty dossier section below is a collection fault, not a "
                  "finding about the field.", ""]
        for key in never:
            lines.append(f"> - `{key}` has never yielded a record.")
        for key in dry:
            lines.append(f"> - `{key}` has yielded nothing for at least {DRY_DAYS} days.")
        lines.append("")

    lines += _collected_section(term_evidence)
    lines += _fields_section(inquiry)
    lines += ["## Evidence", ""]
    labels = {"assessments": "Assessments (is it true?)",
              "predictions": "Predictions (does it resolve?)",
              "solutions": "Solutions (what would fix it?)"}
    for field in EVIDENCE_FIELDS:
        refs = evidence.get(field) or []
        lines.append(f"### {labels[field]} — {len(refs)}")
        lines.append("")
        if refs:
            lines += [f"- [[{ref}]]" for ref in sorted(refs)]
        else:
            lines.append("_None yet._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    inquiries, errors = lib.load_inquiries()
    for error in errors:
        print(f"WARN  {error}")
    if not inquiries:
        print(f"# no inquiry pages under {lib.WIKI / lib.NS} — no dossier to render")
        print(json.dumps({"wakeAgent": False, "inquiries": 0}))
        return 0

    today = datetime.now(timezone.utc).date()
    runs = (_load_state().get("terms") or {})
    evidence = gather_evidence(lib.WIKI, inquiries)
    ledger = lib.load_ledger()
    compiled = lib.compiled_sources(lib.WIKI, ledger)
    DASH.mkdir(parents=True, exist_ok=True)

    rows = []
    for inquiry in inquiries:
        term_evidence = lib.evidence_by_term(lib.WIKI, inquiry, ledger, compiled)
        # Flat namespace by declaration (see the schema fragment): the dossier path mirrors the
        # inquiry slug one-for-one, so there is no partition strategy that could re-file it and
        # no second spelling for a writer to re-create (okengine#54).
        generated_page.write(
            DASH / f"{inquiry.slug}.md",
            render(inquiry, runs, evidence[inquiry.slug], today, term_evidence))
        counts = {f: len(evidence[inquiry.slug].get(f) or []) for f in EVIDENCE_FIELDS}
        counts["sources"] = len({r for e in term_evidence.values() for r in e["sources"]})
        rows.append((inquiry, counts))

    index = ["---", "type: dashboard", "title: Inquiries",
             f"updated: {today.isoformat()}", "---", "", "# Inquiries", "",
             "Standing research questions this vault is working. Each declares the terms that "
             "collect evidence for it; adding a question is adding a page.", "",
             "| inquiry | status | terms | sources | assessments | predictions | solutions |",
             "|---|---|---|---|---|---|---|"]
    for inquiry, counts in sorted(rows, key=lambda r: (r[0].status != "open", r[0].slug)):
        index.append(f"| [[dashboards/inquiry/{inquiry.slug}]] | `{inquiry.status}` "
                     f"| {len(inquiry.terms)} | {counts['sources']} | {counts['assessments']} "
                     f"| {counts['predictions']} | {counts['solutions']} |")
    index.append("")
    (DASH / "INDEX.md").write_text("\n".join(index), encoding="utf-8")
    nominated = write_lacuna_nominations(inquiries, today)

    print(f"# rendered {len(rows)} dossier(s) into {DASH.relative_to(lib.VAULT)}; "
          f"{nominated} lacuna field nomination(s)")
    print(json.dumps({"wakeAgent": False, "inquiries": len(rows)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
