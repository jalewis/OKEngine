#!/usr/bin/env python3
"""Wake-gate + digest for the prediction-candidate-watch cron (okengine#36).

Lists entities a RECENT SOURCE cites (i.e. a feed actually reported on them lately)
and that have NO open prediction yet, so the agent can file a falsifiable, dated
prediction where one is genuinely warranted (the prompt is conservative — defer is
the default). Gating on a citing source — NOT on `last_updated` — is deliberate: the
token-free importers bump `last_updated` on every page, so that signal flags thousands
of static catalog stubs (the prediction namespace stayed empty because the agent only
ever saw stubs and rightly deferred). A recent citing source means something actually
happened. Wakes only when at least `PREDICTION_CANDIDATE_MIN` such entities exist.

Pure script / no LLM. Generic: entity TYPES that warrant a forward claim come from
the env (pack-tunable), defaulting to common forward-lookable kinds.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P   # noqa: E402

N = int(os.environ.get("PREDICTION_CANDIDATE_BATCH_SIZE", "10"))
RECENT_DAYS = int(os.environ.get("PREDICTION_CANDIDATE_RECENT_DAYS", "30"))
MIN_TO_FIRE = int(os.environ.get("PREDICTION_CANDIDATE_MIN", "3"))
# Entity types worth a forward claim. Empty default = NO type filter (consider any
# entity), keeping the engine domain-agnostic; a pack narrows to its forward-lookable
# kinds by setting PREDICTION_CANDIDATE_TYPES (comma-separated) in its cron env.
TYPES = {t.strip().lower() for t in os.environ.get("PREDICTION_CANDIDATE_TYPES", "").split(",")
         if t.strip()}


def main() -> int:
    v = P.vault()
    covered: set[str] = set()
    for _, fm in P.predictions(v):
        if P.is_open(fm):
            covered |= P.subject_slugs(fm)
    cutoff = P.days_ago_iso(RECENT_DAYS)
    recent_sources = P.recent_source_slugs(v, cutoff)

    cands = []
    for p in P.iter_pages(v, "entities"):
        fm = P.read_fm(p)
        if TYPES and str(fm.get("type", "")).strip().lower() not in TYPES:
            continue
        slug = p.stem.lower()
        if slug in covered:
            continue
        hits = P.entity_source_slugs(fm) & recent_sources
        if hits:
            lu = P.fm_date(fm, "last_updated", "updated")
            cands.append((len(hits), lu, slug, str(fm.get("name") or slug),
                          str(fm.get("type")), p))
    cands.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)   # most-cited, then most-recent

    print("=== prediction-candidate-watch wake-gate ===")
    print(f"  vault: {v}")
    print(f"  sources published since {cutoff}: {len(recent_sources)}")
    print(f"  open predictions cover {len(covered)} subject(s)")
    print(f"  entities cited by a recent source, no open prediction: {len(cands)}")

    if len(cands) < MIN_TO_FIRE:
        print(f"  → SKIP: only {len(cands)} candidate(s) (threshold {MIN_TO_FIRE})")
        print(json.dumps({"wakeAgent": False}))
        return 0

    chosen = cands[:N]
    print(f"  batch: {len(chosen)} of {len(cands)}\n")
    print("=== batch ===")
    print(f"Each entity below is cited by a source from the last {RECENT_DAYS} days. File a "
          "falsifiable, dated prediction in `predictions/` ONLY if a specific, observable, "
          "dated claim is genuinely warranted (include a `## What would refute this` "
          "section). DEFER otherwise — coverage is not a goal.\n")
    for i, (nh, lu, slug, name, typ, p) in enumerate(chosen, 1):
        rel = p.relative_to(v).as_posix()
        print(f"## {i}. {name}  ({typ}, {nh} recent source citation(s), updated {lu})")
        print(f"  page: `{rel}`  ·  subject ref: `[[entities/{slug}]]`\n")

    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
