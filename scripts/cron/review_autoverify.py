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
it): `conflicts:` present, a Grounding-check failure in the body, a missing schema-required field,
or a tombstoned status. Those stay in the human queue.

A cleared page gets, in place of the flag:
    review_status: auto-verified
    auto_verified_basis: "1 A-grade source: Microsoft"
    auto_verified_at: '<UTC ISO>'
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
    return {"enabled": cfg.get("enabled", True) is not False,
            "a_sources": int(cfg.get("a_sources", 1)),
            "b_sources": int(cfg.get("b_sources", 2)),
            "exempt_types": {str(x) for x in exempt} if isinstance(exempt, list)
                            else set(_JUDGMENT_TYPES)}


def _required_fields(schema: dict, ptype: str) -> list[str]:
    spec = (schema.get("types") or {}).get(ptype)
    req = (spec or {}).get("required") if isinstance(spec, dict) else None
    return [str(f) for f in req if str(f) != "type"] if isinstance(req, list) else []


def _source_page(ref: str) -> Path | None:
    """Resolve a sources: entry to a vault page (direct path, else unique basename)."""
    if not isinstance(ref, str) or "://" in ref:
        return None
    key = ref.strip().removesuffix(".md")
    if "/" not in key:
        return None
    p = WIKI / (key + ".md")
    if p.is_file():
        return p
    hits = [h for h in WIKI.rglob(Path(key).name + ".md") if not h.name.startswith(("_", "."))]
    return hits[0] if len(hits) == 1 else None


def _grade_evidence(fm: dict, registry: dict[str, str]) -> dict[str, list[str]]:
    """{'A': [distinct source labels...], 'B': [...]} for the page's cited evidence."""
    graded: dict[str, set[str]] = {}
    srcs = fm.get("sources")
    for ref in (srcs if isinstance(srcs, list) else []):
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

    cleared = held = 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for p in sorted(WIKI.rglob("*.md")):
        if p.name.startswith(("_", ".")) or p.name.upper().startswith("INDEX") or ".bak" in p.name:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _FM_RE.match(text)
        if not match or not _NEEDS_RE.search(match.group(1)):
            continue
        fm = _frontmatter(text)
        if fm.get("needs_review") is not True:
            continue
        rel = p.relative_to(WIKI).as_posix()

        # refusals — anything else wrong keeps the page in the human queue
        why_held = None
        if str(fm.get("type") or "") in pol["exempt_types"]:
            why_held = f"judgment type '{fm.get('type')}' — evidence grade never clears judgment review"
        elif str(fm.get("status") or "").lower() == "tombstoned":
            why_held = "tombstoned"
        elif isinstance(fm.get("conflicts"), list) and fm["conflicts"]:
            why_held = "conflicts present"
        elif _GROUNDING_FAIL.search(text):
            why_held = "grounding-check failure"
        else:
            missing = [f for f in _required_fields(schema, str(fm.get("type") or ""))
                       if fm.get(f) in (None, "", [], {})]
            if missing:
                why_held = f"missing required: {', '.join(missing)}"

        graded = _grade_evidence(fm, registry) if why_held is None else {}
        ok = (len(graded.get("A", [])) >= pol["a_sources"]
              or len(graded.get("B", [])) >= pol["b_sources"]) if why_held is None else False
        if not ok:
            held += 1
            if why_held:
                print(f"  held  {rel}: {why_held}")
            continue

        basis = _basis(graded)
        stamp = (f"review_status: auto-verified\n"
                 f"auto_verified_basis: {json.dumps(basis)}\n"
                 f"auto_verified_at: '{now}'")
        head, rest = match.group(1), text[match.end():]
        new_head = _NEEDS_RE.sub(stamp, head, count=1)
        cleared += 1
        print(f"  clear {rel}: {basis}")
        if not args.dry_run:
            p.write_text(f"---\n{new_head.rstrip()}\n---\n\n{rest.lstrip()}", encoding="utf-8")

    print(f"review-autoverify: {cleared} cleared, {held} held for human review "
          f"(bar: {pol['a_sources']}xA or {pol['b_sources']}xB{' [dry-run]' if args.dry_run else ''})")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
