#!/usr/bin/env python3
"""registry_coverage — why review-autoverify is not clearing, ranked by what it would cost to fix
(okengine#541).

`review_autoverify` (#313) clears `needs_review` by pure arithmetic over the pack's Admiralty
`source_registry`. Where the registry is fed it works — okcti-test has auto-cleared 2,198 pages.
Everywhere else it is starved, and the cause is measurable rather than mysterious: **41% of held
pages on a private market-intel deployment cite publishers that are not in the 19-entry registry at all**
(OpenAI, CrowdStrike, NIST, Bitdefender, FireEye…), and two packs ship no registry whatsoever, where
the lane prints an honest "UNDETECTABLE, nothing can be graded" and nobody acts on it.

This lane turns that into a work list. It walks the corpus, buckets every held page by the reason
`review_autoverify` refused it, and for the pages held *only* for want of a grade it ranks the
ungraded publishers by **how many held pages each one would release**. The output is a proposed
`source_registry` diff a pack author accepts in one edit.

**No grade is assigned here, by design.** The lane surfaces the decision; a human makes it. That
preserves the property #313 was built for — the clearing rule stays auditable arithmetic that an
agent cannot launder a claim through. A census that guessed grades would hand the model exactly the
laundering path the split was meant to close.

Drift: the bar, the refusal ladder and the evidence-grading all come from `review_autoverify` by
import, never re-implemented here. If the lane changes its policy (`review_autoverify.a_sources`,
`exempt_types`, the grading rule), this census follows automatically.
`tests/cron/test_registry_coverage.py` pins the two in agreement on a shared fixture.

Projections are stated per publisher and are **not additive**: two publishers can each be the sole
blocker of the same page, so grading one changes the other's number. Sum them and you will
overcount — the report says so where it prints them.

Env: WIKI_PATH (vault root, default /opt/vault) · REGISTRY_COVERAGE_TOP (25).
`--json PATH` also writes the machine form (stdout stays the cron runner's contract).
Report-only (never writes a page's frontmatter). Pure script (no_agent): emits {"wakeAgent": false}.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generated_page  # noqa: E402 — keeps a regenerated page's id (okengine#933)
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
TOP = int(os.environ.get("REGISTRY_COVERAGE_TOP", "25"))

# Two things masquerade as publishers in a `sources:` list, and they are NOT the same defect —
# reporting them together produced a scary "697 non-publishers" that was really 20 genuine artifact
# refs plus 677 ordinary source paths in wikilink form, concentrated on 58 pages of one vault.
#
#   ARTIFACT  — a vault file that is not a source at all (`log.md`, `_review-queue.md`). Something
#               upstream wrote the changelog into a citation list. Fix the writer; never grade it.
#   WIKILINK  — `[[sources/2017/…]]`: a perfectly good source path wrapped in brackets.
#               review_autoverify._source_page requires `key.startswith("sources/")`, so the bracket
#               form never resolves and the page's real evidence is invisible to the lane. Measured:
#               58 pages on that deployment, of which 5 would clear on bracket-strip alone —
#               worth fixing, but small, which is exactly why it is counted separately.
_ARTIFACT_MARKERS = ("log.md", "index.md", "readme.md", "_review-queue", "health.md", "bundle.md")


def _ref_class(label: str) -> str:
    """'artifact', 'wikilink' or 'publisher' — see the note above."""
    stripped = label.strip()
    if stripped.startswith("[[") and stripped.endswith("]]"):
        inner = stripped[2:-2].strip()
        return "artifact" if any(m in inner.lower() for m in _ARTIFACT_MARKERS) else "wikilink"
    return "artifact" if any(m in stripped.lower() for m in _ARTIFACT_MARKERS) else "publisher"


def _load_autoverify(vault: Path):
    """Import review_autoverify as a module and point it at this vault.

    Imported rather than copied: the census's whole value is that it measures the bar the LANE
    actually applies. A re-implementation would drift the first time either side is tuned, and the
    drift would be invisible — the census would confidently rank publishers against a policy nobody
    is enforcing.
    """
    path = Path(__file__).resolve().parent / "review_autoverify.py"
    spec = importlib.util.spec_from_file_location("review_autoverify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.VAULT = vault
    module.WIKI = vault / "wiki"
    return module


def _held_reason(av, page_schema, pol, text: str, fm: dict, page: Path, sources: dict):
    """The refusal `review_autoverify` would record, or None if the page fails only on evidence.

    The lane's own ladder (`review_autoverify.refusal`), not a copy: a hand-kept mirror lost the
    actor holds (contradiction, `actor_identity_validated: false`, the okengine#1014 grounding
    hold) and `hold_when_fields`, so the census ranked publishers for pages no grade could clear
    (okengine#1032 [21]).
    """
    if str(fm.get("status") or "").lower() == "tombstoned":
        return "tombstoned"   # the lane drops these before its ladder; never a grading question
    return av.refusal(fm, text, page, page_schema, pol, sources)


def _cited_labels(av, fm: dict) -> list[str]:
    """Every distinct publisher label the page cites, graded or not — the same resolution
    review_autoverify._grade_evidence performs, but keeping the ungraded ones it discards."""
    out: list[str] = []
    srcs = fm.get("sources")
    for ref in (srcs if isinstance(srcs, list) else []):
        if not isinstance(ref, str):
            continue
        page = av._source_page(ref)
        if page is not None:
            try:
                pub = str(av._frontmatter(page.read_text(encoding="utf-8", errors="replace"))
                          .get("publisher") or "").strip()
            except OSError:
                continue
        else:
            pub = ref.strip()
        if pub and pub not in out:
            out.append(pub)
    return out


def _census_self_graded(av, fm: dict, registry: dict, pol: dict, rel: str, buckets: Counter,
                        blocked_by: dict, clears_at_a: dict, clears_at_b: dict) -> None:
    """Bucket a held source page the lane grades by its OWN publisher (okengine#1042 [17]).

    An ungraded publisher is the page's whole blocker: grading it at or above the self-grade bar
    releases the page, so it is ranked like any other ungraded publisher. A page with no publisher,
    or one graded under the bar, is held for something a new grade does not fix."""
    own, pub = av._self_grade(fm, registry)
    if not pub:
        buckets["source page has no publisher"] += 1
    elif own:
        buckets["source publisher graded below the self-grade bar"] += 1
    else:
        buckets["blocked by an ungraded publisher"] += 1
        blocked_by[pub].add(rel)
        clears_at_a[pub].add(rel)                  # A is the top grade: it meets any bar
        if "B" <= pol["source_self_grade"]:
            clears_at_b[pub].add(rel)


def census(vault: Path) -> dict:
    av = _load_autoverify(vault)
    wiki = vault / "wiki"
    schema = schema_lib.merged_schema(vault)
    registry = av._registry(schema)
    pol = av._policy(schema)

    buckets: Counter = Counter()
    blocked_by: dict[str, set] = defaultdict(set)      # ungraded publisher -> pages it appears on
    clears_at_a: dict[str, set] = defaultdict(set)     # ...that would clear if graded A
    clears_at_b: dict[str, set] = defaultdict(set)     # ...that would clear if graded B
    artifact_refs: dict[str, set] = defaultdict(set)
    wikilink_refs: dict[str, set] = defaultdict(set)
    held_total = 0
    schemas: dict = {}   # governing directory -> schema, as the lane resolves it
    sources: dict = {}   # the lane's grounding-reader cache, built on the first actor that needs it

    for p in sorted(wiki.rglob("*.md")):
        if p.name.startswith(("_", ".")) or p.name.upper().startswith("INDEX") or ".bak" in p.name:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = av._FM_RE.match(text)
        if not match or not av._NEEDS_RE.search(match.group(1)):
            continue
        fm = av._frontmatter(text)
        if fm.get("needs_review") is not True:
            continue
        # An always-publish type is never held, so it is not a census subject at all. This must
        # come BEFORE the count: held_total means "pages the lane would hold", and ranking
        # publishers by pages the lane publishes regardless measures a bar nobody applies to them
        # (okengine#554). Skipping here keeps the census and the lane in agreement, which is the
        # census's entire purpose.
        # The refusal ladder runs FIRST, as in the lane: an always-publish actor that a refusal
        # holds (okengine#1014's grounding hold) is still a page the lane holds.
        page_schema = av._page_schema(p, schemas)
        reason = _held_reason(av, page_schema, pol, text, fm, p, sources)
        # The lane's own evidence step (`review_autoverify.evidence_verdict`), self-grade branch
        # included. Its hold (an ungraded or under-bar source publisher) comes before the
        # always-publish release, exactly as in the lane.
        ok, why, graded, _basis = (av.evidence_verdict(fm, registry, pol) if reason is None
                                   else (False, None, {}, None))
        if (reason is None and why is None
                and av.always_publishes(pol, page_schema, str(fm.get("type") or ""))):
            continue
        held_total += 1
        rel = p.relative_to(wiki).as_posix()

        if reason is not None:
            # The bucket is the reason's stable head, without the per-page detail.
            buckets[reason.split(":")[0].split(" — ")[0].split(" (")[0]] += 1
            continue
        if ok:
            buckets["clearable now (lane has not run)"] += 1
            continue
        if av.self_grades(fm, pol):
            _census_self_graded(av, fm, registry, pol, rel, buckets, blocked_by, clears_at_a,
                                clears_at_b)
            continue

        n_a, n_b = len(graded.get("A", [])), len(graded.get("B", []))

        labels = _cited_labels(av, fm)
        if not labels:
            buckets["no sources cited"] += 1
            continue
        ungraded = [x for x in labels if x not in registry]
        if not ungraded:
            buckets["cited, all graded, still under the bar"] += 1
            continue

        buckets["blocked by an ungraded publisher"] += 1
        for label in ungraded:
            kind = _ref_class(label)
            if kind == "artifact":
                artifact_refs[label].add(rel)
                continue
            if kind == "wikilink":
                wikilink_refs[label].add(rel)
                continue
            blocked_by[label].add(rel)
            # Would grading THIS publisher alone release the page?
            if n_a + 1 >= pol["a_sources"]:
                clears_at_a[label].add(rel)
            if n_b + 1 >= pol["b_sources"]:
                clears_at_b[label].add(rel)

    ranked = sorted(
        ({"publisher": k,
          "pages_blocked": len(v),
          "clears_if_graded_a": len(clears_at_a.get(k, ())),
          "clears_if_graded_b": len(clears_at_b.get(k, ()))}
         for k, v in blocked_by.items()),
        key=lambda r: (-r["clears_if_graded_a"], -r["pages_blocked"], r["publisher"]),
    )
    return {
        "vault": vault.name,
        "held_total": held_total,
        "registry_size": len(registry),
        "buckets": dict(buckets),
        "ranked": ranked,
        "artifact_refs": {k: len(v) for k, v in sorted(artifact_refs.items())},
        "artifact_pages": len({r for v in artifact_refs.values() for r in v}),
        "wikilink_pages": len({r for v in wikilink_refs.values() for r in v}),
        "wikilink_refs": sum(len(v) for v in wikilink_refs.values()),
        "policy": {"a_sources": pol["a_sources"], "b_sources": pol["b_sources"]},
        # The grades ALREADY set in this vault, and the types the lane exempts. Both are rendered
        # for the human who has to answer this page (okengine#823): a reviewer asked for a grade
        # with no scale, no worked examples and no statement of what a grade cannot fix has been
        # handed a question they cannot calibrate. The pack's own existing grades are the only
        # calibration that exists, so the page shows them rather than describing them.
        "registry": dict(sorted(registry.items(), key=lambda kv: (kv[1], kv[0].lower()))),
        "exempt_types": sorted(pol["exempt_types"]),
    }


# Admiralty reliability, as the engine states it (templates/pack/skeleton/prompts/
# source-quality-backfill.md ships this same baseline to the scoring lane). Rendered on the page
# because "bar: 1xA or 2xB" is unreadable to anyone who does not already know the code.
_RELIABILITY_SCALE = (
    ("A", "completely reliable"),
    ("B", "usually reliable"),
    ("C", "fairly reliable"),
    ("D", "not usually reliable"),
    ("E", "unreliable"),
    ("F", "reliability cannot be judged"),
)


def _held_split(result: dict) -> tuple[int, int, int]:
    """Held pages as (grading fixes it, grading can NEVER fix it, some other defect).

    The single "N page(s) held" headline reads as one backlog and is not one. On a live CTI vault
    it was 1,540 held of which 467 were grade-blocked, 675 were judgment types the lane exempts BY
    DESIGN, and 334 cited nothing at all -- so the page asked its reader for 1,540 pages' worth of
    grading when 3 grading decisions were the whole addressable job.
    """
    buckets = result["buckets"]
    addressable = buckets.get("blocked by an ungraded publisher", 0)
    exempt = sum(v for k, v in buckets.items() if k.startswith("judgment type"))
    return addressable, exempt, max(0, result["held_total"] - addressable - exempt)


def render(result: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ranked, buckets = result["ranked"], result["buckets"]
    pol = result["policy"]
    reach = sum(r["clears_if_graded_a"] for r in ranked[:TOP])
    L = ["---", "type: dashboard", 'title: "Registry coverage"', f"updated: {now}", "---", "",
         f"# Registry coverage — {now}", "",
         f"**{result['held_total']} page(s) held** · registry has **{result['registry_size']}** "
         f"publisher(s) · bar: {pol['a_sources']}×A or {pol['b_sources']}×B", ""]
    if not result["registry_size"]:
        L += ["> **No `source_registry` in the governing schema.** `review-autoverify` is a no-op "
              "here — nothing can be graded, so nothing can ever clear. This is UNDETECTABLE, not "
              "a pass: add `source_registry` to the pack's schema.yaml to enable the lane.", ""]
    addressable, exempt, other = _held_split(result)
    L += ["## What this page is asking you for", "",
          f"> Grade the publishers below, and the **{addressable}** page(s) waiting only on a grade "
          "are released automatically. No other page on this list is affected.", "",
          "| Of the held pages | Count | Grading releases them? |",
          "|---|---:|---|",
          f"| waiting only on an ungraded publisher | **{addressable}** | **yes — this is the job** |",
          f"| analytic judgments ({', '.join(result.get('exempt_types') or []) or 'exempt types'}) "
          f"| {exempt} | **never** — see below |",
          f"| held for some other defect | {other} | no — citation, schema or evidence defects |", ""]
    if exempt:
        L += [f"_The {exempt} judgment page(s) are exempt **by design**: an A-grade source can "
              "support a judgment a human still has to review. Grading every publisher in existence "
              "would not release one of them. They need an analyst, in the cockpit's Ops → Human "
              "review worklist._", ""]
    L += ["## Why pages are held", "", "| Reason | Pages | Grading fixes it |", "|---|---:|---|"]
    for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
        fixes = ("**yes**" if k == "blocked by an ungraded publisher"
                 else "never — needs a human judgment" if k.startswith("judgment type")
                 else "no")
        L.append(f"| {k} | {v} | {fixes} |")
    L.append("")
    if ranked:
        L += ["## What a grade is, and what it does", "",
              "`reliability` is a **NATO Admiralty letter** grading the *publishing channel* — not "
              "any single article, and not the page citing it.", "",
              "| Grade | Meaning |", "|---|---|"]
        L += [f"| **{letter}** | {meaning} |" for letter, meaning in _RELIABILITY_SCALE]
        a_n, b_n = pol["a_sources"], pol["b_sources"]
        L += ["", f"The clearing bar is **{a_n}×A or {b_n}×B**:", "",
              f"- **{a_n} A-grade source** clears a page on its own;",
              f"- **{b_n} distinct B-grade sources** clear a page together;",
              "- **C, D, E and F never clear a page**, alone or in combination.", "",
              "Grading an aggregator `C` is still worth doing: it moves the page out of "
              "*\"blocked by an ungraded publisher\"* (an unanswered question) into "
              "*\"cited, graded, still under the bar\"* (an answered one). The vault then knows "
              "the page is weakly sourced rather than unassessed.", ""]
        if result.get("registry"):
            by_grade: dict[str, list[str]] = {}
            for name, grade in result["registry"].items():
                by_grade.setdefault(grade, []).append(name)
            L += ["### Already graded in this vault — calibrate against these", "",
                  "| Grade | Publishers |", "|---|---|"]
            L += [f"| **{g}** | {', '.join('`%s`' % n for n in sorted(by_grade[g]))} |"
                  for g in sorted(by_grade)]
            L += ["", "_The question is not \"is this a good website?\" — it is: does this "
                  "publisher do primary research and publish it as the originator (**A**), report "
                  "on other people's research with editorial standards (**B**), aggregate other "
                  "people's reporting (**C**), or host whatever a user typed (**D**)?_", ""]
        L += [f"## Ungraded publishers, ranked by pages released", "",
              "_Per-publisher and **not additive** — two publishers can each be the sole blocker of "
              "the same page, so grading one changes the other's number._", "",
              "| Publisher | Pages blocked | Clears if A | Clears if B |", "|---|---|---|---|"]
        L += [f"| {r['publisher']} | {r['pages_blocked']} | {r['clears_if_graded_a']} | "
              f"{r['clears_if_graded_b']} |" for r in ranked[:TOP]]
        if len(ranked) > TOP:
            L.append(f"\n_…and {len(ranked) - TOP} more ungraded publisher(s)._")
        L += ["", "## How to answer", "",
              "Edit the `source_registry:` block in the pack's **`schema.yaml`** and fill in one "
              "letter per publisher. The key must match the publisher string below **exactly**, "
              "including case and spacing — `Wiz` and `wiz` are different publishers to the "
              "matcher. Only `reliability` is accepted. Leave a comment saying why: every existing "
              "grade has one, and the next reviewer will use yours the way you used theirs.", "",
              "Do **not** grade an entry you are unsure of. An absent grade is an honest "
              "\"unassessed\"; a wrong `A` silently verifies pages nobody checked.", "",
              "_Grades are deliberately left blank below — assigning them is the human's call, and "
              "the audit property of #313 depends on it staying that way._", "",
              "```yaml", "source_registry:"]
        L += [f"  {r['publisher']}: {{reliability: }}   # would release {r['clears_if_graded_a']} "
              f"page(s) at A" for r in ranked[:TOP]]
        L += ["```", ""]
        L.append(f"Grading the top {min(TOP, len(ranked))} would release up to **{reach}** page(s) "
                 f"at A-grade (upper bound — see the non-additive note above).\n")
        L += ["**After you answer:** the composed schema regenerates, `review-autoverify` re-runs "
              "and clears every page whose sources now meet the bar, and each cleared page is "
              "stamped with the arithmetic that cleared it (`auto_verified_basis`), so any clear "
              "traces back to your decision. This page regenerates on its own schedule — if a "
              "publisher you graded still appears here afterwards, the edit did not reach the "
              "composed schema.", ""]
    if result["artifact_refs"]:
        L += ["## Vault artifacts cited as sources — a defect, not a grading gap", "",
              f"Files that are not sources at all, sitting in a `sources:` list on "
              f"**{result['artifact_pages']}** held page(s). Something upstream wrote them there. "
              "Do not grade them; fix the writer.", "",
              "| Value | Pages |", "|---|---|"]
        L += [f"| `{k}` | {v} |" for k, v in result["artifact_refs"].items()]
        L.append("")
    if result["wikilink_pages"]:
        L += ["## Source refs in wikilink form — invisible evidence", "",
              f"**{result['wikilink_pages']}** held page(s) carry {result['wikilink_refs']} "
              "`[[sources/…]]` reference(s). `review_autoverify._source_page` requires the ref to "
              "start with `sources/`, so the bracket form never resolves and this evidence is not "
              "counted. These pages are not blocked by a missing grade — they are blocked by a "
              "link form. Grading will not help them; stripping the brackets will.", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    # A PATH, not a stdout flag: stdout is the cron runner's contract (it ends with the
    # {"wakeAgent": ...} line), so dumping the census there too makes the stream unparseable as
    # either one thing or the other.
    ap.add_argument("--json", metavar="PATH", help="also write the machine form to PATH")
    args = ap.parse_args(argv)

    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    result = census(VAULT)
    out = WIKI / "operational" / "registry-coverage.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    generated_page.write(out, render(result))

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    top = result["ranked"][:5]
    lead = ", ".join(f"{r['publisher']} ({r['clears_if_graded_a']})" for r in top) or "none"
    print(f"registry-coverage: {result['held_total']} held · registry {result['registry_size']} "
          f"publisher(s) · {len(result['ranked'])} ungraded publisher(s) blocking · top: {lead} "
          f"-> wiki/operational/registry-coverage.md")
    if not result["registry_size"]:
        print("registry-coverage: no source_registry — review-autoverify cannot clear anything here "
              "(UNDETECTABLE, not a pass)", file=sys.stderr)
    if result["artifact_pages"]:
        print(f"registry-coverage: {result['artifact_pages']} page(s) cite a vault artifact as a "
              f"source — fix the writer, do not grade it", file=sys.stderr)
    if result["wikilink_pages"]:
        print(f"registry-coverage: {result['wikilink_pages']} page(s) cite sources in [[wikilink]] "
              f"form, which _source_page cannot resolve — a link form, not a grading gap",
              file=sys.stderr)
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
