#!/usr/bin/env python3
"""review_autoverify — deterministic, evidence-graded clearing of `needs_review` (okengine#313).

The review latch is a one-way flag: the enforced write path lets agents RAISE `needs_review` but
never clear it, so every galaxy/ATT&CK-seeded entity waits for a human — which does not scale to a
thousand-actor roster, and it presents authoritatively-sourced pages (Microsoft, MITRE, CISA…) as
"unverified drafts" indefinitely.

This lane is the scalable upgrade path, and it is deliberately NOT an agent: it clears the flag by
**pure arithmetic over the pack's Admiralty `source_registry`** (schema.yaml, `{name:
{reliability: A..F}}`). No LLM judgment participates, so an agent still cannot launder its own
claims into verified status — the clearing rule is auditable math, and every clear stamps its
basis.

Evidence counting (per page `sources:` entry):
  - a vault source PAGE (path resolves under wiki/) grades by its `publisher` frontmatter looked
    up in the registry — distinct by page;
  - a PROSE string grades by exact registry match (how the no_agent ATT&CK/MISP importers cite) —
    distinct by registry name.

Default bar (pack-overridable via schema `review_autoverify:`): **1×A or 2×B** — one authoritative
source verifies alone; two independent B-grade sources corroborate. A MISP-only seed (single B)
stays flagged until a second source lands.

Refusals — a page is NEVER auto-cleared when anything else is wrong (the flag may exist BECAUSE of
it): `conflicts:` present, a Grounding-check failure in the body, or a missing schema-required
field. Those stay in the human queue. A tombstoned page is different: retirement makes its review
request moot, so the lane drops that request with an auditable lifecycle stamp.

A cleared page gets, in place of the flag:
    review_status: auto-verified
    auto_verified_basis: "1 A-grade source: Microsoft"
    auto_verified_at: '<UTC ISO>'

An always-publish type with NO graded evidence still leaves the human queue, but must not claim a
verification that did not happen -- it is stamped honestly instead, and never carries an empty
`auto_verified_basis`:
    review_status: unverified-no-evidence
    consensus: 0
    review_checked_at: '<UTC ISO>'
Humans can re-raise `needs_review` any time; the review workflow is unchanged for everything else.

Env: WIKI_PATH (vault root, default /opt/vault). `--dry-run` reports without writing.
Pure script (no_agent): always emits {"wakeAgent": false}.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
import engine_package  # noqa: E402  — makes `okengine` importable when this file is
engine_package.ensure()  # loaded BY PATH by an external consumer (okpacks-library#89)
from okengine.actor_identity import actor_identity_error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
_NEEDS_RE = re.compile(r"^needs_review:[ \t]*[Tt]rue[ \t]*$", re.M)
# same failure signal the cockpit review view keys on
_GROUNDING_FAIL = re.compile(
    r"##[ \t]+Grounding check.*?(unsupported|not[- ]found|not in source|contradict)", re.S | re.I)


def _frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _registry(schema: dict) -> dict[str, str]:
    out = {}
    reg = schema.get("source_registry")
    for k, v in (reg.items() if isinstance(reg, dict) else []):
        r = str((v or {}).get("reliability") or "").strip().upper()
        if r:
            out[str(k).strip()] = r
    return out


# Judgment-shaped page types are NEVER evidence-cleared: their needs_review guards an analytic
# JUDGMENT (an assessment's claim, a prediction's framing), not the quality of the citations —
# an A-grade source can support a judgment a human still has to review. Schema-tunable via
# review_autoverify.exempt_types (replaces this default).
_JUDGMENT_TYPES = {"assessment", "proposition", "prediction", "hypothesis", "forecast"}


def _policy(schema: dict) -> dict:
    cfg = schema.get("review_autoverify")
    cfg = cfg if isinstance(cfg, dict) else {}
    exempt = cfg.get("exempt_types")
    always = cfg.get("always_publish_types")
    self_grade = cfg.get("source_self_grade", "A")
    hold_fields = cfg.get("hold_when_fields")
    defer_types = cfg.get("defer_confidence_to_types")
    defer_kinds = cfg.get("defer_confidence_kinds")
    return {"enabled": cfg.get("enabled", True) is not False,
            # Judgment types whose page, when it names this page as its `subject`, GOVERNS the
            # confidence of that claim -- so this lane must not also derive one.
            #
            # Evidence counting answers "how many graded publishers cite this page?". It cannot
            # answer "how confident are we in the claim?", and treating the first as the second
            # produces confident-sounding numbers from sources that never addressed the claim: an
            # actor published `high` off two CATALOGUE listings while the assessment analysing the
            # very same association -- weighing lineage, independence and alternatives -- said
            # `moderate`. Both appeared on one screen, disagreeing.
            #
            # The analytic product wins. The pack names the governing types, so the engine ships
            # no assumption about what an assessment is called.
            "defer_confidence_to_types": ({str(x) for x in defer_types}
                                          if isinstance(defer_types, list) else set()),
            # WHICH judgments govern. A judgment is scoped to ONE claim: an identity-scope or
            # motivation assessment says nothing about attribution, so deferring to it suppresses a
            # confidence nothing else supplies. Deferring on type ALONE did exactly that to 57 pages
            # (53 of them identity-scope). Empty = any kind, which is the type-only behaviour.
            "defer_confidence_kind_field": str(cfg.get("defer_confidence_kind_field")
                                               or "assessment_kind"),
            "defer_confidence_kinds": ({str(x) for x in defer_kinds}
                                       if isinstance(defer_kinds, list) else set()),
            # Frontmatter fields that HOLD a page whenever truthy, whatever its evidence grade.
            #
            # Another lane can flag a page for something citation quality cannot answer -- an
            # enrichment lane detecting that a page's identity is ambiguous (an upstream record whose
            # theme contradicts the page's own classification). Grading answers "are the citations
            # good?", never "is this the right entity?", so a well-sourced page can still be a
            # conflated one. Without this the two lanes FIGHT: the enrichment lane raises the flag,
            # this lane clears it on evidence, and the page's state depends on which ran last --
            # non-deterministic corpus output, which is worse than either policy.
            #
            # The same principle already governs `exempt_types` (an A-grade source can support a
            # judgment a human must still review) and `conflicts` (disagreement escalates rather
            # than silently averaging). The engine ships the MECHANISM; the pack names the fields,
            # so no lane-specific or vendor-specific key appears here.
            "hold_when_fields": ([str(x) for x in hold_fields]
                                 if isinstance(hold_fields, list) else []),
            "a_sources": int(cfg.get("a_sources", 1)),
            "b_sources": int(cfg.get("b_sources", 2)),
            # A `source` page IS the primary document — it cites nothing by construction, so the
            # cited-evidence arithmetic can never clear it (okengine#549: 282 held on okcti-test, 81
            # of them published by an outlet the registry ALREADY grades A or B). Such a page is
            # graded by its OWN `publisher`. The bar is a single grade, not a count: a primary
            # document cannot corroborate itself, so "2xB" is unreachable and must not be required.
            # Pack-overridable; `null`/`none` disables self-grading entirely.
            "source_self_grade": (None if self_grade in (None, "", "none")
                                  else str(self_grade).strip().upper()),
            # Types that are NEVER held for a human. An actor page publishes whatever its evidence
            # supports and says so in `attribution_confidence` + `consensus`; the reader judges. The old
            # model held 874 of 1209 actor pages waiting for evidence to cross a bar, which is backwards --
            # the confidence field IS the uncertainty signal, so gating on it publishes nothing and tells
            # the reader nothing (okengine#554).
            #
            # `conflicts` still wins: a page with a recorded conflict is held regardless, so genuine
            # disagreement between sources escalates rather than silently averaging.
            "always_publish_types": ({str(x) for x in always} if isinstance(always, list)
                                     else {"actor"}),
            "exempt_types": {str(x) for x in exempt} if isinstance(exempt, list)
                            else set(_JUDGMENT_TYPES)}


def _self_grade(fm: dict, registry: dict[str, str]) -> tuple[str | None, str]:
    """(grade, publisher) for a page that IS a source, from its own `publisher` field."""
    pub = str(fm.get("publisher") or "").strip()
    return (registry.get(pub) if pub else None), pub


def _required_fields(schema: dict, ptype: str) -> list[str]:
    spec = (schema.get("types") or {}).get(ptype)
    req = (spec or {}).get("required") if isinstance(spec, dict) else None
    return [str(f) for f in req if str(f) != "type"] if isinstance(req, list) else []


def _source_page(ref: str) -> Path | None:
    """Resolve a sources: entry to a vault page (direct path, else unique basename)."""
    if not isinstance(ref, str) or "://" in ref:
        return None
    key = ref.strip().removesuffix(".md")
    # Only vault source-page identifiers belong on the filesystem path. A few
    # legacy pages contain prose citations with a slash in them; treating those
    # as paths can exceed NAME_MAX and crash the entire nightly lane.
    if not key.startswith("sources/"):
        return None
    parts = Path(key).parts
    if (
        not parts
        or Path(key).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or any(len(part.encode("utf-8")) > 255 for part in parts)
        or len(key.encode("utf-8")) > 4096
    ):
        return None
    p = WIKI / (key + ".md")
    try:
        if p.is_file():
            return p
        hits = [
            h for h in WIKI.rglob(Path(key).name + ".md")
            if not h.name.startswith(("_", "."))
        ]
    except OSError:
        # Malformed/overlong legacy references are ungraded evidence, not a
        # reason to abort verification for every other page.
        return None
    return hits[0] if len(hits) == 1 else None


def _grade_evidence(fm: dict, registry: dict[str, str]) -> dict[str, list[str]]:
    """{'A': [distinct source labels...], 'B': [...]} for the page's cited evidence."""
    graded: dict[str, set[str]] = {}
    srcs = fm.get("sources")
    srcs = list(srcs) if isinstance(srcs, list) else []
    # `operator_evidence_refs` is a SECOND evidence channel and grades exactly like `sources`
    # (okengine#563). It points at operator-held source RECORDS — e.g. data an aggregator carried,
    # whose page still names the ORIGINATOR in `publisher` (`retrieved_via` names the aggregator).
    # Reading only `sources` left that evidence uncounted on 1299 entity pages of one live vault:
    # Anonymous Sudan published `moderate`/consensus 1 while actually holding two distinct B-grade
    # publishers (a galaxy import and a malware-reference project) — i.e. 2xB, `high`, consensus 2.
    #
    # Grading is by PUBLISHER, so a record's confidentiality (local_only/export_policy) does not
    # leak here: the basis names the originator, which is not the restricted part.
    op = fm.get("operator_evidence_refs")
    srcs += list(op) if isinstance(op, list) else []
    for ref in srcs:
        if not isinstance(ref, str):
            continue
        page = _source_page(ref)
        if page is not None:
            try:
                src_text = page.read_text(encoding="utf-8", errors="replace")
            except OSError:      # page vanished mid-scan (reshelve/curation race) — skip this ref
                continue
            pub = str(_frontmatter(src_text).get("publisher") or "").strip()
            grade, label = registry.get(pub), pub or None
        else:
            grade, label = registry.get(ref.strip()), ref.strip()
        # distinct by PUBLISHER (registry name), never by page: two articles from the same outlet
        # are one voice, not independent corroboration — 2xB means two DIFFERENT B-grade publishers.
        if grade and label:
            graded.setdefault(grade, set()).add(label)
    return {g: sorted(v) for g, v in graded.items()}


_SUBJ_LINK = re.compile(r"\[\[([^\]|#]+)")


def _governed_subjects(types: set[str], kind_field: str = "", kinds: set[str] | None = None) -> set[str]:
    """Vault-relative page keys (no .md) that a judgment page of one of `types` names as its
    `subject` — i.e. pages whose claim-confidence an analytic product already governs.

    Built in ONE pass and only when the pack has named governing types, so a deployment that has
    not opted in pays nothing. `subject` may be a bare path or a wikilink; both resolve here
    because a citation is written both ways across the corpus.
    """
    out: set[str] = set()
    if not types:
        return out
    for p in WIKI.rglob("*.md"):
        if p.name.startswith(("_", ".")):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _FM_RE.match(text)
        if not m:
            continue
        fm = _frontmatter(text)
        if not isinstance(fm, dict) or str(fm.get("type") or "") not in types:
            continue
        if kinds and str(fm.get(kind_field) or "") not in kinds:
            continue                      # a judgment about a DIFFERENT claim does not govern this one
        subj = fm.get("subject")
        for raw in ([subj] if isinstance(subj, str) else (subj if isinstance(subj, list) else [])):
            if not isinstance(raw, str):
                continue
            for cand in (_SUBJ_LINK.findall(raw) or [raw]):
                key = cand.strip().removesuffix(".md")
                if key:
                    out.add(key)
    return out


def _consensus(graded: dict[str, list[str]]) -> int:
    """How many DISTINCT publishers support this page.

    _grade_evidence already keys by publisher, so two articles from one outlet are one voice --
    that is the whole point of the count. Read it as "N independent publishers support this page",
    NOT "N sources were checked and agreed": the attribution claim is not structured per-source, so
    nothing verifies that they assert the SAME thing. That simplification is safe while the corpus
    records zero conflicts (0 of 1209 actor pages), and the `conflicts` field remains the escape
    hatch -- a page carrying one is still held (okengine#554).
    """
    # _grade_evidence already keys by publisher, so these labels are distinct on arrival; the set
    # is belt-and-braces against a future caller that hands over raw labels, NOT the thing enforcing
    # one-voice-per-outlet. That rule lives in _grade_evidence and is tested there.
    return len({label for grade in ("A", "B") for label in graded.get(grade, [])})


# Admiralty grade + corroboration -> the reader-facing confidence band. A is "completely reliable"
# and its credibility digit (1 confirmed / 2 probably true) is already folded into the grade the
# registry assigns, so any A clears to `confirmed`; corroboration then separates a lone A2 from two
# agreeing ones via `consensus`, which is published alongside. B is a usually-reliable outlet, so a
# single B is `moderate` and two independent Bs raise it to `high`. Nothing below B can carry an
# attribution on its own -- it becomes a lean, not a finding.
_BANDS = (("A", 2, "confirmed"), ("A", 1, "confirmed"),
          ("B", 2, "high"), ("B", 1, "moderate"))


def _derive_confidence(graded: dict[str, list[str]], consensus: int) -> str | None:
    """The confidence band this page's evidence supports, or None for no attribution.

    None means exactly what the model says it should: no evidence, therefore no attribution -- not
    a hedge, and never a hold. A page with weaker-than-B evidence still publishes; the caller
    records `suspected` for an inference-only lean.
    """
    for grade, floor, band in _BANDS:
        if len(graded.get(grade, [])) >= floor and consensus >= floor:
            return band
    return None


def _basis(graded: dict[str, list[str]]) -> str:
    parts = []
    for g in ("A", "B"):
        if graded.get(g):
            names = ", ".join(graded[g][:4])
            parts.append(f"{len(graded[g])} {g}-grade source{'s' if len(graded[g]) != 1 else ''}: {names}")
    return "; ".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = ap.parse_args(argv)

    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1
    schema = schema_lib.merged_schema(VAULT)
    registry = _registry(schema)
    pol = _policy(schema)
    if not pol["enabled"]:
        print("review-autoverify: disabled by schema review_autoverify.enabled — no-op")
        print(json.dumps({"wakeAgent": False}))
        return 0
    if not registry:
        print("review-autoverify: no source_registry in the governing schema — UNDETECTABLE, "
              "nothing can be graded (not a pass); add reliability grades to enable")
        print(json.dumps({"wakeAgent": False}))
        return 0

    cleared = held = unverified = stale_claims = deferred = dropped = 0
    governed = _governed_subjects(pol['defer_confidence_to_types'],
                                  pol['defer_confidence_kind_field'],
                                  pol['defer_confidence_kinds'])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for p in sorted(WIKI.rglob("*.md")):
        if p.name.startswith(("_", ".")) or p.name.upper().startswith("INDEX") or ".bak" in p.name:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _FM_RE.match(text)
        if not match:
            continue
        fm = _frontmatter(text)
        flagged = _NEEDS_RE.search(match.group(1)) and fm.get("needs_review") is True
        # SELF-HEALING (okengine#563): a page already stamped `auto-verified` never carries
        # needs_review again, so an earlier hollow claim -- `auto-verified` with an empty/absent
        # basis -- would persist forever without this second entry condition. Re-stamping it here
        # means every vault repairs itself on the next nightly run, instead of needing a one-off
        # script per deployment.
        # Two shapes re-enter: a hollow `auto-verified` (the claim being corrected), and a page
        # previously stamped `unverified-no-evidence` -- because evidence can land AFTER that stamp
        # (source_normalize lifting body citations into `sources:` is exactly that case), and
        # without this the page would keep saying "no evidence" forever while citing an A-grade
        # publisher. Re-entry alone is not a rewrite: see the material-change check below, which
        # keeps a still-unevidenced page from being re-stamped every night.
        _rs = str(fm.get("review_status") or "")
        # Re-enter ANY page this lane has already stamped, not just the broken ones. A stamp can be
        # perfectly well-formed and still STALE: evidence lands afterwards, or the grading rule
        # itself widens (adding `operator_evidence_refs` as a second channel left 1299 already-
        # stamped pages permanently reporting the old, narrower answer — the fix shipped but could
        # never reach them). Re-entry is cheap and does NOT mean rewriting: the material-change
        # check below writes only when the recomputed basis or consensus actually differs, so a
        # settled corpus stays untouched.
        repairing = not flagged and _rs in ("auto-verified", "unverified-no-evidence")
        if not (flagged or repairing):
            continue
        rel = p.relative_to(WIKI).as_posix()

        # Retirement resolves the review request by lifecycle, not by evidence. Keeping a
        # tombstone in the human queue cannot produce a useful decision: the page is deliberately
        # no longer live, and write governance forbids resurrecting it. Clear only the queue latch
        # and leave an explicit, non-verification stamp so the disposition remains auditable.
        if flagged and str(fm.get("status") or "").strip().lower() == "tombstoned":
            stamp = ("review_status: dropped-tombstoned\n"
                     f"review_checked_at: '{now}'")
            # Unpack the single frontmatter capture rather than indexing it: the regex contract is
            # exact, and malformed future expansion must fail visibly instead of selecting a
            # different capture group.
            head, = match.groups()
            rest = text[match.end():]
            head = re.sub(r"^(review_status|auto_verified_basis|auto_verified_at|consensus"
                          r"|review_checked_at):.*$", "", head, flags=re.M)
            # Remove every duplicate latch from legacy/corrupt frontmatter. Leaving a second
            # `needs_review: true` would make the lifecycle disposition parser-dependent.
            new_head = _NEEDS_RE.sub("", head)
            new_head = "\n".join(line for line in new_head.splitlines() if line.strip())
            new_head = f"{new_head.rstrip()}\n{stamp}"
            dropped += 1
            print(f"  drop  {rel}: tombstoned lifecycle makes review moot")
            if not args.dry_run:
                p.write_text(f"---\n{new_head.rstrip()}\n---\n\n{rest.lstrip()}",
                             encoding="utf-8")
            continue

        # refusals — anything else wrong keeps the page in the human queue
        why_held = None
        ptype = str(fm.get("type") or "")
        title = str(fm.get("title") or fm.get("name") or p.stem)
        actor_contradiction = actor_identity_error(title, text) if ptype == "actor" else None
        if actor_contradiction:
            why_held = f"actor evidence defines a {actor_contradiction}, not an adversarial agent"
        elif fm.get("actor_identity_validated") is False:
            why_held = "actor identity classification is explicitly unvalidated"
        elif ptype in pol["exempt_types"]:
            why_held = f"judgment type '{fm.get('type')}' — evidence grade never clears judgment review"
        elif isinstance(fm.get("conflicts"), list) and fm["conflicts"]:
            why_held = "conflicts present"
        elif [f for f in pol["hold_when_fields"] if fm.get(f)]:
            _hf = [f for f in pol["hold_when_fields"] if fm.get(f)]
            why_held = (f"held by {', '.join(_hf)} — another lane flagged this page for something "
                        f"evidence grade cannot resolve")
        elif _GROUNDING_FAIL.search(text):
            why_held = "grounding-check failure"
        else:
            missing = [f for f in _required_fields(schema, str(fm.get("type") or ""))
                       if fm.get(f) in (None, "", [], {})]
            if missing:
                why_held = f"missing required: {', '.join(missing)}"

        graded, basis_self = {}, None
        ok = False
        if why_held is None:
            if str(fm.get("type") or "") == "source" and pol["source_self_grade"]:
                # Grade the document by who published it, not by what it cites.
                own, pub = _self_grade(fm, registry)
                if own and own <= pol["source_self_grade"]:      # 'A' <= 'A'; 'B' <= 'A' is False
                    ok, basis_self = True, f"{own}-grade publisher: {pub}"
                elif not own:
                    why_held = (f"source publisher {pub!r} is not in source_registry" if pub
                                else "source page has no publisher")
                else:
                    why_held = f"source publisher {pub} grades {own}, below the {pol['source_self_grade']} bar"
            else:
                graded = _grade_evidence(fm, registry)
                ok = (len(graded.get("A", [])) >= pol["a_sources"]
                      or len(graded.get("B", [])) >= pol["b_sources"])
        # An always-publish type never waits for a human. `conflicts` and the other refusals above
        # still hold it -- what is dropped is only the evidence-bar hold, which was gating
        # publication on the very uncertainty the confidence field exists to express.
        derived = None
        if not ok and str(fm.get("type") or "") in pol["always_publish_types"] and why_held is None:
            ok = True
            derived = _derive_confidence(graded, _consensus(graded))

        if not ok:
            if repairing:
                # Not "held for human review" -- this page carries no needs_review flag, so counting
                # it as held would overstate the queue. Two distinct outcomes reach here since
                # re-entry covers every stamped page, and calling both a "stale claim with no basis"
                # would misreport a soundly-stamped page that a new refusal now catches.
                if not fm.get("auto_verified_basis"):
                    stale_claims += 1
                    print(f"  stale-claim {rel}: still stamped auto-verified with no basis"
                          f"{f' ({why_held})' if why_held else ''}")
                else:
                    print(f"  now-held {rel}: {why_held or 'no longer clears the bar'} "
                          f"(existing stamp left in place)")
            else:
                held += 1
                if why_held:
                    print(f"  held  {rel}: {why_held}")
            continue

        basis = basis_self or _basis(graded)
        agree = _consensus(graded)
        # MATERIAL-CHANGE CHECK. A re-entered page is rewritten only when the recomputed verdict
        # actually differs from what it already carries; otherwise every nightly run would rewrite
        # the whole corpus just to move a timestamp, destroying the idempotence the drains rely on.
        if repairing:
            _same_basis = str(fm.get("auto_verified_basis") or "") == basis
            _same_count = int(fm.get("consensus") or 0) == agree
            _same_state = _rs == ("auto-verified" if basis else "unverified-no-evidence")
            # A page already carrying a derived confidence that a judgment now governs IS a material
            # change even when basis/consensus are unchanged: the stale value is the contradiction
            # being corrected. Without this the corpus keeps every previously-derived value, because
            # a settled page never reaches the deferral below (3319 pages carry this field).
            # The field's PRESENCE must match what policy now implies, in both directions: a value
            # a judgment governs must go, and one wrongly removed must come back (57 pages lost it
            # to a judgment about an unrelated claim).
            _gov = rel.removesuffix(".md") in governed
            _has_conf = bool(fm.get("attribution_confidence"))
            # Material only when this lane would ACT: add a band its evidence supports, or remove
            # one a same-claim judgment governs. NOT when a value is present without graded
            # evidence -- the writing lane's own value is deliberately preserved (see the module
            # docstring), so treating that as a mismatch loops forever: nothing here would remove it.
            _conf_change = ((not _gov) and bool(_derive_confidence(graded, agree)) and not _has_conf) \
                or (_gov and _has_conf)
            if _same_basis and _same_count and _same_state and not _conf_change:
                continue
        # Publish the corroboration count and the band the evidence supports. `derived is None`
        # means no A/B evidence at all: the page still publishes, and `attribution_confidence` is
        # left exactly as the writing lane set it rather than being invented here.
        extra = f"consensus: {agree}\n"
        if derived is None and ok and graded:
            derived = _derive_confidence(graded, agree)
        # An analytic judgment already governs this claim -- do not also derive one from citation
        # counts. Suppressing rather than adopting the judgment's band is deliberate: a judgment is
        # scoped to ONE claim (a country linkage, say) and its band is not automatically the page's
        # overall attribution confidence, so copying it would trade a visible contradiction for an
        # invisible conflation. The reader gets the assessment, which states its own scope.
        _governed_here = rel.removesuffix(".md") in governed
        if _governed_here and (derived or fm.get("attribution_confidence")):
            deferred += 1
            print(f"  defer {rel}: confidence governed by an analytic judgment — "
                  f"not deriving one from citation counts")
            derived = None
            # drop any value already on the page, not just a newly derived one: the stale value IS
            # the contradiction, and it outlives the run that produced it
            head_drop_conf = True
        else:
            head_drop_conf = bool(derived)
        if derived:
            extra += f"attribution_confidence: {derived}\n"
        # `basis` empty == the always-publish path fired with NO graded evidence at all. Publishing
        # is still right (the page leaves the human queue -- that is the point of always_publish),
        # but stamping "auto-verified" there asserts a verification that never happened, and an
        # empty auto_verified_basis is the lane's own admission of it. 50 records on one live vault
        # carried that false claim. Publish it honestly instead, and never emit an empty basis.
        if basis:
            stamp = (f"review_status: auto-verified\n"
                     f"{extra}"
                     f"auto_verified_basis: {json.dumps(basis)}\n"
                     f"auto_verified_at: '{now}'")
        else:
            stamp = (f"review_status: unverified-no-evidence\n"
                     f"{extra}"
                     f"review_checked_at: '{now}'")
        head, rest = match.group(1), text[match.end():]
        # Replace an existing attribution_confidence rather than emitting a second key -- a
        # duplicate would make the frontmatter ambiguous and the last-writer-wins silently.
        if head_drop_conf:
            head = re.sub(r"^attribution_confidence:.*$", "", head, flags=re.M)
        # Strip any PRIOR stamp keys on BOTH paths before writing the new one. The flagged path used
        # to only substitute over the needs_review line, so a page carrying needs_review AND an
        # earlier stamp ended up with DUPLICATE `consensus:`/`auto_verified_basis:` keys — and YAML
        # last-wins silently kept the STALE values, so the lane logged the right basis while the
        # page kept the old one. That shape is not hypothetical: it is any page an enrichment lane
        # re-flags after this one has already stamped it.
        head = re.sub(r"^(review_status|auto_verified_basis|auto_verified_at|consensus"
                      r"|review_checked_at):.*$", "", head, flags=re.M)
        if flagged:
            new_head = _NEEDS_RE.sub(stamp, head, count=1)
        else:
            new_head = head.rstrip() + "\n" + stamp
        new_head = "\n".join(l for l in new_head.splitlines() if l.strip())
        if basis:
            cleared += 1
            print(f"  clear {rel}: {basis}")
        else:
            unverified += 1
            print(f"  publish-unverified {rel}: no graded evidence")
        if not args.dry_run:
            p.write_text(f"---\n{new_head.rstrip()}\n---\n\n{rest.lstrip()}", encoding="utf-8")

    dropped_summary = f", {dropped} tombstoned review(s) dropped" if dropped else ""
    print(f"review-autoverify: {cleared} cleared on evidence{dropped_summary}, "
          f"{unverified} published unverified "
          f"(no graded evidence), {deferred} deferred to an analytic judgment, "
          f"{held} held for human review, "
          f"{stale_claims} stale claim(s) unresolved "
          f"(bar: {pol['a_sources']}xA or {pol['b_sources']}xB{' [dry-run]' if args.dry_run else ''})")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
