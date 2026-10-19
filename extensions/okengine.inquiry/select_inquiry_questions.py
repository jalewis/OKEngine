#!/usr/bin/env python3
"""select_inquiry_questions.py — okengine.inquiry:inquiry-analyze wake-gate.

Picks the standing inquiry question(s) that most need an assessment, and hands the model the
evidence the inquiry's own terms collected for it. Deterministic; the model does the judging.

Why this lane exists (okengine#854): an inquiry drove COLLECTION only. Hundreds of records came in,
a few dozen compiled into source pages, and nothing ever turned the question into a judgment — the
dossier read "Assessments 0" indefinitely. Every analysis lane in the engine either picked its own
field (lacuna: the densest cluster in the whole vault) or reviewed assessments that already existed
(adversarial-evidence-review), so a directional question steered nothing.

What gets selected, in order:
  1. a question with NO assessment yet, once its inquiry has `analyze_min_sources` compiled sources
     (below that, a first judgment would be extrapolation from a handful of articles);
  2. a question whose assessment's CITED evidence moved: a page it cites was rewritten after it,
     or a page in its `sources` no longer exists. Read from okengine.reevaluation's dependency
     sidecar (`wiki/.reevaluation-edges.json`) — this lane is that index's consumer for
     assessments, as predictions regrade is for predictions. The CHE loop: changed evidence
     selects the affected proposition;
  3. a question with `analyze_min_new` sources compiled for its inquiry since the assessment was
     last written — "evidence for THIS question arrived", not "any recent source exists".
Ties break on the inquiry's `opened` date, then slug, so selection is stable run to run.

Identity: the selector names the page (`inquiry.assessment_slug(question)`); the model never
does. A rerun updates the one page per question instead of creating a second spelling (#54).

Evidence starts from the inquiry's attributed, COMPILED sources (inquiry_lib.evidence_by_term).
Only pages containing captured content attested by local raw provenance enter the analysis pool;
discovery snippets, failed captures and unknown content remain explicit collection gaps.
Newest-compiled first, sources new since the last assessment marked, capped at
`analyze_max_evidence` — the model can read any listed page in full through the read tools.

Completion is per-selected-item with a receipt: `insufficient-evidence` is a CORRECT outcome for a
question the collected material does not bear on, and must not be graded as a failed run.

Env: WIKI_PATH (/opt/vault) · OKENGINE_SELECTION_MANIFEST ·
     OKENGINE_INQUIRY_ANALYZE_BATCH (1) · OKENGINE_INQUIRY_ANALYZE_MIN_SOURCES (5) ·
     OKENGINE_INQUIRY_ANALYZE_MIN_NEW (3) · OKENGINE_INQUIRY_ANALYZE_MAX_EVIDENCE (25)
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "cron"))
import inquiry_lib as lib  # noqa: E402
from source_content import source_state  # noqa: E402
from selection_manifest import write_selection_manifest  # noqa: E402

BATCH = int(os.environ.get("OKENGINE_INQUIRY_ANALYZE_BATCH", "1"))
MIN_SOURCES = int(os.environ.get("OKENGINE_INQUIRY_ANALYZE_MIN_SOURCES", "5"))
MIN_NEW = int(os.environ.get("OKENGINE_INQUIRY_ANALYZE_MIN_NEW", "3"))
MAX_EVIDENCE = int(os.environ.get("OKENGINE_INQUIRY_ANALYZE_MAX_EVIDENCE", "25"))
EXCERPT_CHARS = 400
LANE = "okengine.inquiry:inquiry-analyze"
ASSESSMENTS = "assessments"


def manifest_path() -> Path:
    """Runner-owned in deployment (OKENGINE_SELECTION_MANIFEST is set on the generated job);
    the default only serves a manual run."""
    home = Path(os.environ.get("HERMES_HOME", str(lib.VAULT / ".hermes-data")))
    return home / "cron-plus" / "selections" / f"{LANE}.json"


@dataclass
class Candidate:
    inquiry: lib.Inquiry
    question: lib.Question
    slug: str
    existing: tuple[Path, dict] | None
    sources: list[str]
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)

    @property
    def rank(self) -> tuple:
        # Unassessed questions first; then evidence the judgment RESTS ON having moved (a cited
        # source changed or vanished) ahead of merely new evidence; then oldest inquiry.
        return (self.existing is not None, -(len(self.gone) + len(self.changed)),
                -len(self.new), self.inquiry.opened or "9999", self.inquiry.slug,
                self.question.key)


EDGES = ".reevaluation-edges.json"


def cited_by(wiki: Path) -> dict[str, dict[str, list[str]]] | None:
    """assessment page -> {cited page: [via field, ...]}, inverted from okengine.reevaluation's
    dependency sidecar. None when the sidecar is absent or unreadable: cited-evidence change is
    then UNDETECTABLE, which the digest says rather than implying nothing changed."""
    try:
        edges = json.loads((wiki / EDGES).read_text(encoding="utf-8")).get("edges")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(edges, dict):
        return None
    out: dict[str, dict[str, list[str]]] = {}
    for cited, rows in edges.items():
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("page"), str):
                out.setdefault(row["page"], {})[cited] = list(row.get("via") or [])
    return out


def moved_evidence(wiki: Path, fm: dict,
                   cites: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """(changed, gone) among the pages an assessment cites.

    CHANGED: the cited page was rewritten after the assessment was (write-path `last_updated`
    timestamps; ISO-8601 UTC strings order correctly as text). GONE: a page named in `sources` no
    longer exists. Only `sources` counts for GONE because the write path resolved it when the
    assessment was written, so its absence now is a real removal; a body link may never have
    resolved. Both conditions clear themselves on reassessment: the rewrite makes the assessment
    newer, and the contract rejects an update that still cites a missing source. The inquiry page
    is excluded: editing the question is not evidence about its answer. (Self-references need no
    guard: the edge index drops them, and a page is never newer than itself.)
    """
    since = str(fm.get("last_updated") or "").strip()
    changed: list[str] = []
    gone: list[str] = []
    for cited, via in sorted(cites.items()):
        if cited.startswith(f"{lib.NS}/"):
            continue
        target = wiki / f"{cited}.md"
        if not target.is_file():
            if "sources" in via:
                gone.append(cited)
            continue
        stamp = str(lib.split_frontmatter(target)[0].get("last_updated") or "").strip()
        if since and stamp > since:
            changed.append(cited)
    return changed, gone


def existing_assessments(wiki: Path) -> dict[str, tuple[Path, dict]]:
    """slug -> (path, frontmatter), wherever the partition strategy filed the page. Located by
    stem, never by a hand-built shard path (#54)."""
    out: dict[str, tuple[Path, dict]] = {}
    root = wiki / ASSESSMENTS
    if not root.is_dir():
        return out
    for page in sorted(root.rglob("*.md")):
        if page.name.startswith(("_", ".")) or page.name.upper().startswith("INDEX"):
            continue
        fm, _ = lib.split_frontmatter(page)
        if str(fm.get("type") or "") == "assessment":
            out.setdefault(page.stem, (page, fm))
    return out


def _date(fm: dict, *fields: str) -> str:
    for name in fields:
        value = str(fm.get(name) or "").strip()
        if value:
            return value[:10]
    return ""


def source_meta(wiki: Path, ref: str) -> tuple[dict, str]:
    return lib.split_frontmatter(wiki / f"{ref}.md")


def candidates(wiki: Path, inquiries: list[lib.Inquiry], ledger: dict,
               dependencies: dict[str, dict[str, list[str]]] | None = None) -> list[Candidate]:
    compiled = lib.compiled_sources(wiki, ledger)
    assessed = existing_assessments(wiki)
    dependencies = dependencies or {}
    created: dict[str, str] = {}
    states: dict[str, str] = {}
    out: list[Candidate] = []
    for inquiry in inquiries:
        if not inquiry.is_open:
            continue
        by_term = lib.evidence_by_term(wiki, inquiry, ledger, compiled)
        sources = sorted({r for e in by_term.values() for r in e["sources"]}, reverse=True)
        for ref in sources:
            if ref not in created:
                fm, body = source_meta(wiki, ref)
                created[ref] = _date(fm, "created", "published")
                states[ref] = source_state(wiki.parent, fm, body)
        excluded = {ref: states[ref] for ref in sources if states[ref] != "captured"}
        sources = [ref for ref in sources if ref not in excluded]
        for question in inquiry.standing_questions:
            slug = inquiry.assessment_slug(question)
            existing = assessed.get(slug)
            if existing is None:
                if len(sources) >= MIN_SOURCES:
                    out.append(Candidate(inquiry, question, slug, None, sources, excluded=excluded))
                continue
            since = _date(existing[1], "last_updated", "as_of")
            new = [r for r in sources if created.get(r) and (not since or created[r] > since)]
            page = existing[0].relative_to(wiki).as_posix()[:-3]
            changed, gone = moved_evidence(wiki, existing[1], dependencies.get(page, {}))
            if len(new) >= MIN_NEW or changed or gone:
                out.append(Candidate(inquiry, question, slug, existing, sources, new,
                                     changed, gone, excluded))
    return sorted(out, key=lambda c: c.rank)


_MARKUP = re.compile(r"<[^>\n]{1,40}>")


def _excerpt(body: str) -> str:
    """Whitespace-collapsed opening of a source body. Search-service highlight markup (`<b>` around
    matched terms) is stripped: it is the collector's emphasis, not the author's, and handing it to
    the model marks the query terms as the important words."""
    text = " ".join(_MARKUP.sub("", body).split())
    return text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")


def render(wiki: Path, picked: list[Candidate]) -> list[str]:
    lines: list[str] = []
    for n, cand in enumerate(picked, 1):
        inq = cand.inquiry
        lines += ["", f"## {n}. `{cand.slug}`", "",
                  f"- Inquiry: [[{lib.NS}/{inq.slug}]] — {inq.title or inq.question}",
                  f"- QUESTION TO ASSESS: {cand.question.text}",
                  f"- Write exactly this page: `{ASSESSMENTS}/{cand.slug}` "
                  "(logical key; the write tool files it)",
                  f"- Set `inquiry: {inq.slug}` and `subject_ref: \"[[{lib.NS}/{inq.slug}]]\"`."]
        if cand.existing:
            path, fm = cand.existing
            rel = path.relative_to(wiki).as_posix()[:-3]
            lines += [f"- MODE: UPDATE [[{rel}]] (last written "
                      f"{_date(fm, 'last_updated', 'as_of') or 'undated'}). Reassess because:"]
            lines += [f"  - CITED SOURCE REMOVED: [[{ref}]] no longer exists — drop it from "
                      "`sources` and re-examine what rested on it" for ref in cand.gone]
            lines += [f"  - CITED SOURCE CHANGED since this assessment: [[{ref}]] — re-read it"
                      for ref in cand.changed]
            if cand.new:
                lines.append(f"  - {len(cand.new)} new source(s) compiled for this inquiry "
                             "(marked NEW below)")
            lines += [f"  - current claim: {str(fm.get('claim') or '').strip()[:300]}",
                      f"  - current confidence: {fm.get('confidence')} "
                      f"({fm.get('confidence_band')})",
                      "  - Record the belief change under `## Revisions` (see the prompt)."]
        else:
            lines.append("- MODE: CREATE — no assessment answers this question yet.")
        if cand.excluded:
            counts = {state: list(cand.excluded.values()).count(state)
                      for state in sorted(set(cand.excluded.values()))}
            lines += ["", f"- Collection gap: {len(cand.excluded)} attributed source(s) excluded "
                      f"from evidence: {json.dumps(counts)}. "
                      "Capture/hydrate these leads; their absence is not evidence against a hypothesis."]
        new = set(cand.new)
        shown = ([r for r in cand.sources if r in new] +
                 [r for r in cand.sources if r not in new])[:MAX_EVIDENCE]
        lines += ["", f"### Evidence ({len(shown)} of {len(cand.sources)} attributed source(s); "
                      "captured content only; newest first, NEW = since the last assessment)", "",
                  "Captured does not mean appraised: inspect the article content and its limits.", ""]
        for ref in shown:
            fm, body = source_meta(wiki, ref)
            tag = "NEW " if ref in new else ""
            meta = " · ".join(str(x) for x in (
                fm.get("publisher"), _date(fm, "published"),
                f"reliability {fm.get('reliability')}" if fm.get("reliability") else "",
                f"credibility {fm.get('credibility')}" if fm.get("credibility") else "") if x)
            lines += [f"- {tag}[[{ref}]] — {str(fm.get('title') or ref).strip()}"
                      + (f" ({meta})" if meta else ""),
                      f"  > {_excerpt(body)}"]
    return lines


def assessments_available() -> tuple[bool | None, str]:
    """Whether the governing schema defines the `assessment` type this lane writes.

    okengine.inquiry and okengine.assessments are enabled independently. Without assessments, a
    woken lane would spend a model run producing pages the write path must reject, every day, while
    the dossier looked healthy. (True, "") / (False, why) / (None, why) — None is UNKNOWN (the
    schema could not be read), never a pass: the lane proceeds and the write path's contract stays
    the backstop, but the digest says the check did not run."""
    try:
        import schema_lib  # noqa: PLC0415 — staged beside selection_manifest; repo: scripts/cron
        types = schema_lib.merged_schema(lib.VAULT, ASSESSMENTS).get("types") or {}
    except Exception as exc:  # noqa: BLE001 — any failure here is "could not check", reported
        return None, f"governing schema unreadable ({type(exc).__name__}: {exc})"
    if "assessment" in types:
        return True, ""
    return False, ("the governing schema has no `assessment` type — enable okengine.assessments "
                   "(`framework extensions enable <pack> okengine.assessments`)")


def main(argv: list[str] | None = None) -> int:
    inquiries, errors = lib.load_inquiries()
    for error in errors:
        print(f"WARN  {error}")
    available, why = assessments_available()
    if available is False:
        print("=== inquiry-analyze wake-gate ===")
        print(f"  → SKIP: standing questions cannot be answered here: {why}. Collection and the "
              "dossier still run.")
        print(json.dumps({"wakeAgent": False}))
        return 0
    if available is None:
        print(f"WARN  assessment type UNVERIFIED — {why}; proceeding, the write path's output "
              "contract still rejects a type this vault does not define")
    dependencies = cited_by(lib.WIKI)
    picked = candidates(lib.WIKI, inquiries, lib.load_ledger(), dependencies)[:max(BATCH, 0)]
    print("=== inquiry-analyze wake-gate ===")
    print(f"  vault: {lib.VAULT}")
    print(f"  open inquiries: {sum(1 for i in inquiries if i.is_open)} · thresholds: "
          f"first assessment >= {MIN_SOURCES} sources, reassess >= {MIN_NEW} new or any "
          "cited source changed/removed")
    if dependencies is None:
        print(f"  WARN  {EDGES} absent or unreadable — cited-source changes are UNDETECTABLE "
              "this run (enable okengine.reevaluation); only new evidence can trigger a "
              "reassessment")
    if not picked:
        print("  → SKIP: no standing question has enough attributed evidence to assess or "
              "reassess; discovery snippets, capture failures and unknown content do not "
              "count toward the evidence floor. Check collection/hydration gaps.")
        print(json.dumps({"wakeAgent": False}))
        return 0

    selected = [c.slug for c in picked]
    # Receipt-enforced: the runner verifies the model's receipt against this manifest
    # (OKENGINE_SELECTION_MANIFEST names it on the generated job).
    manifest = write_selection_manifest(selected, manifest_path())
    print(f"=== {len(picked)} question(s) selected (max {BATCH} per run) — process IN ORDER ===")
    print("\n".join(render(lib.WIKI, picked)))
    receipt = {"api": 1, "lane_id": manifest["lane_id"],
               "contract_digest": manifest["contract_digest"],
               "input_digest": manifest["input_digest"],
               "items": [{"key": key,
                          "disposition": "<accepted|updated|insufficient-evidence|"
                                         "deferred-for-review|failed>",
                          "writes": [{"path": "wiki/assessments/<path the write tool returned>.md",
                                      "sha256": "sha256:<current-file-hash>"}],
                          "reason": "<required unless accepted or updated>"} for key in selected]}
    print()
    print("FINAL RESPONSE CONTRACT (MANDATORY): return ONLY this fenced receipt, one item per "
          "selected key; drop `writes` from items you did not write.")
    print("```okengine-receipt")
    print(json.dumps(receipt, indent=2))
    print("```")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
