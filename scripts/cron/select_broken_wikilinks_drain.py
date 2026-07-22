#!/usr/bin/env python3
"""Wake-gate + digest for the broken-wikilinks-drain cron.

Scans `wiki/**/*.md` for `[[wikilinks]]` whose targets don't resolve to
existing files. Groups by target with inbound-reference count, surfaces
top-N highest-impact targets per batch. Wakes the agent only when at
least one target has ≥MIN_INBOUND inbound references — single-orphan
broken links are noise; 14 sources all linking to a missing entity is
signal.

The agent then classifies each batch target:

  1. Missing entity stub — clearly a real entity (organization /
     product / sub-topic / variant) that hasn't been stubbed yet (e.g. `acme`
     × 14). Action: create `wiki/entities/SLUG.md` with minimal valid
     frontmatter (`type:`, `tags:`, `created:`, `sources:` populated
     from the inbound citations) + a one-line "stub — refresh via
     entity-backfill" body.

  2. Bare publisher name (e.g. `[[Acme Labs]]` instead of
     `[[entities/acme-labs]]`). Action: rewrite each citing source page
     to use the canonical entity path; do NOT create a stub.

  3. Typo / variant (e.g. `[[entities/acme-labs]]` when the
     entity is `[[entities/acme]]`). Action: rewrite each citing
     source page to use the correct path.

  4. Archived / deleted target (target was a source rotated to
     `_archived/` or removed). Action: rewrite link to new path or
     replace with plain text.

  5. Genuinely speculative (target doesn't yet exist and isn't a clear
     stub candidate). Action: defer; surface to human review.

Designed batch size of 10 keeps cost bounded (~30 reads/run between
target inbound-source inspection and write-back).
"""
from __future__ import annotations

import json
import os
import difflib
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from selection_manifest import write_selection_manifest  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
BATCH_SIZE = int(os.environ.get("BWD_BATCH_SIZE", "10"))
MIN_INBOUND = int(os.environ.get("BWD_MIN_INBOUND", "3"))

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
# Match [[wikilink]] or [[wikilink|alias]] or [[wikilink#anchor]]
_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def all_pages():
    """Every .md under wiki/, excluding backup directories and hidden dirs."""
    out = []
    for p in (VAULT / "wiki").rglob("*.md"):
        if not p.is_file():
            continue
        if any(".bak." in part or part.startswith(".") for part in p.parts):
            continue
        out.append(p)
    return out


def is_operational(rel: str) -> bool:
    """Operational/report pages: lint reports, logs, triage docs, the operational/ subtree.
    These shouldn't count as inbound 'sources' for broken targets — they're disposable
    queue artifacts that mention the targets only to flag them as broken."""
    if rel.startswith("operational/"):
        return True
    if "/" not in rel:
        stem = rel[:-3] if rel.endswith(".md") else rel
        if stem.startswith(("lint-", "log-", "log", "triage-", "queue-snapshots", "latest-pdb")):
            return True
    return False


def build_valid_target_set(pages):
    """A target is valid if any of these resolve to a file in `pages`:
      - <subdir>/<slug>.md exactly (e.g. 'entities/foo')
      - just <slug>.md (Obsidian fuzzy resolution by basename)
    Returns set of acceptable target strings."""
    valid = set()
    by_basename = defaultdict(list)
    for p in pages:
        rel = p.relative_to(VAULT / "wiki")
        # Full path form
        valid.add(str(rel.with_suffix("")))
        # Basename-only form (Obsidian's loose resolution)
        valid.add(p.stem)
        by_basename[p.stem].append(rel)
    return valid, by_basename


def classify_hint(target: str, by_basename: dict) -> str:
    """Best-effort classification HINT (the agent does the actual decision).
    Returns a short label that helps prioritization in the batch."""
    # Bare basename — check if a same-named file exists in any subdir
    if "/" not in target:
        if target.lower() in {bn.lower() for bn in by_basename}:
            return "case-mismatch (entity exists with different case)"
        return "candidate: missing entity stub or bare-publisher-link"
    # Has a subdir prefix
    subdir, slug = target.split("/", 1)
    if subdir == "entities" and slug in by_basename:
        return "case-mismatch in entities/"
    if subdir == "entities":
        return "candidate: missing entity stub"
    if subdir == "sources":
        return "archived/deleted source — check sources/_archived/ or rewrite to plain text"
    if subdir == "concepts":
        return "candidate: missing concept page"
    if subdir == "predictions":
        return "candidate: missing prediction page"
    return "(unknown subdir)"


def _tok(slug: str) -> set:
    # split on ANY non-alphanumeric so escape artifacts (a trailing "\\" from
    # `[[entities/acme-corp\\]]`), slashes, dashes, dots all separate cleanly.
    return set(t for t in re.split(r"[^a-z0-9]+", slug.lower()) if t)


def build_token_index(by_basename):
    """One-pass token index over existing page stems, for fast candidate
    prefiltering in suggest_repair."""
    stem_tokens, tokidx = {}, defaultdict(set)
    for stem in by_basename:
        ts = _tok(stem)
        stem_tokens[stem] = ts
        for t in ts:
            tokidx[t].add(stem)
    return stem_tokens, tokidx


def suggest_repair(target, by_basename, stem_tokens, tokidx, min_score=88.0):
    """Lexical nearest existing page for a broken target. Token-set + containment
    matcher (high precision on the high-confidence set, no embeddings needed —
    broken targets are overwhelmingly slug variants/truncations/typos).
    Suggestion ONLY; the agent verifies before rewriting.
    Returns (path-without-suffix, score) or None below the confidence floor."""
    slug = target.split("/")[-1]
    qt = _tok(slug)
    if not qt:
        return None
    cands = set()
    for t in qt:
        cands |= tokidx.get(t, set())
    best_stem, best_sc = None, 0.0
    for stem in cands:
        if stem == slug:
            continue
        ct = stem_tokens[stem]
        contain = len(qt & ct) / len(qt)
        sc = 100 * max(0.55 * contain + 0.45 * difflib.SequenceMatcher(None, slug, stem).ratio(),
                       len(qt & ct) / len(qt | ct))
        if sc > best_sc:
            best_stem, best_sc = stem, sc
    if best_stem and best_sc >= min_score:
        return str(by_basename[best_stem][0].with_suffix("")), best_sc
    return None


def main() -> int:
    pages = all_pages()
    if not pages:
        print("ERROR: no pages found in wiki/", file=sys.stderr)
        return 1

    valid, by_basename = build_valid_target_set(pages)
    stem_tokens, tokidx = build_token_index(by_basename)

    # broken_targets[target] = list of (source_page_relpath, count_in_that_source)
    broken_inbound = defaultdict(list)
    for p in pages:
        rel = str(p.relative_to(VAULT / "wiki"))
        # Skip operational/report pages — they cite broken targets only to flag them,
        # which would inflate inbound counts and mask real body-reference signal.
        if is_operational(rel):
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        # Strip frontmatter so [[wikilinks]] inside FM (e.g. sources: list) DON'T count as broken
        # — they're lookup keys, not body references
        m = _FM_RE.match(txt)
        body = txt[m.end():] if m else txt
        seen_in_this_page = defaultdict(int)
        for wm in _WIKILINK_RE.finditer(body):
            tgt = wm.group(1).strip()
            # Resolve: full path, full path with .md, basename
            if tgt in valid or tgt.split("/")[-1] in valid:
                continue
            seen_in_this_page[tgt] += 1
        for tgt, cnt in seen_in_this_page.items():
            broken_inbound[tgt].append((rel, cnt))

    # Sort by inbound source count, descending
    ranked = sorted(
        broken_inbound.items(),
        key=lambda kv: (-len(kv[1]), -sum(c for _, c in kv[1]), kv[0]),
    )

    # High-impact = inbound threshold OR cited from a BRIEFING. >=MIN_INBOUND is right for the
    # sources tree (single-orphan links are noise at 10k+ pages), but a briefing is the curated,
    # user-facing surface — one dead link there is one a human hits TODAY. Without this, a
    # brief's single-ref invented slug sat below the gate as "orphan noise" forever (live
    # incident: 4 broken links on okcti's 2026-07-06 daily brief). The write path now rejects
    # unresolvable briefing links at create/update; this is the backstop for anything already
    # in the vault or written outside the enforced path.
    high_impact = [t for t in ranked
                   if len(t[1]) >= MIN_INBOUND
                   or any(rel.split("/")[0] == "briefings" for rel, _ in t[1])]
    total_broken = len(ranked)
    total_high_impact = len(high_impact)

    print("=== broken-wikilinks-drain wake-gate ===")
    print(f"  vault: {VAULT}")
    print(f"  total wiki pages scanned: {len(pages)}")
    print(f"  unique broken targets: {total_broken}")
    print(f"  high-impact (>={MIN_INBOUND} inbound sources, or any briefing-cited): {total_high_impact}")
    print(f"  batch size: {BATCH_SIZE}")
    print()

    batch = high_impact[:BATCH_SIZE]
    manifest = None
    if batch:
        manifest = write_selection_manifest(
            [target for target, _ in batch],
            Path(os.environ.get("HERMES_HOME", "/opt/data")) / "cron-plus" / "selections" / "broken-wikilinks-drain.json",
        )
    print(f"=== batch ({len(batch)} of {total_high_impact}, max {BATCH_SIZE} per run) ===")
    print("Process IN ORDER. For each target: inspect 2-3 inbound source pages")
    print("to understand context, then classify (create-stub / rewrite-link / defer).")
    print()
    for i, (target, sources) in enumerate(batch, 1):
        n_sources = len(sources)
        total_refs = sum(c for _, c in sources)
        hint = classify_hint(target, by_basename)
        print(f"{i}. `{target}`  inbound: {n_sources} sources, {total_refs} refs total")
        print(f"   hint: {hint}")
        sug = suggest_repair(target, by_basename, stem_tokens, tokidx)
        if sug:
            print(f"   repair candidate: `{sug[0]}` ({sug[1]:.0f}% lexical match) "
                  f"— if the inbound context matches, rewrite the link to this; else create a stub")
        # Show top 3 inbound sources for context
        top = sorted(sources, key=lambda x: -x[1])[:3]
        for src, cnt in top:
            print(f"     - {src} (×{cnt})")
        if n_sources > 3:
            print(f"     - ... and {n_sources - 3} more inbound sources")
        print()

    # Long-tail summary
    if total_broken > total_high_impact:
        print(f"=== long-tail: {total_broken - total_high_impact} broken targets with <{MIN_INBOUND} inbound sources ===")
        print("(deferred for future passes once high-impact queue drains)")
        print()

    if manifest:
        print(f"selection input_digest: {manifest['input_digest']}")

    wake = bool(batch)
    print(json.dumps({"wakeAgent": wake}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
