#!/usr/bin/env python3
"""Wake-gate + digest builder for the concept-backfill cron.

Walks `wiki/` for `[[concepts/X]]` wikilinks where `wiki/concepts/X.md`
does NOT exist. Ranks the missing concepts by inbound-link count.
Emits the top N (default 10) along with up to 5 citing sources per
concept so the agent has context to write the page.

Wakes the agent only when at least `MIN_INBOUND_TO_FIRE` (default 3)
inbound references exist for the top missing concept — prevents firing
on noise like one-off speculative wikilinks.
"""
from __future__ import annotations

import os
import re
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import okf_migrate  # noqa: E402  — same physical-path contract as the MCP writer

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
N = int(os.environ.get("CONCEPT_BACKFILL_BATCH_SIZE", "10"))
MIN_INBOUND_TO_FIRE = int(os.environ.get("CONCEPT_BACKFILL_MIN_INBOUND", "3"))
MAX_CITING_SOURCES = int(os.environ.get("CONCEPT_BACKFILL_MAX_CITING", "5"))

# [[concepts/something]] or [[concepts/something|alias]] — capture the slug after concepts/
WIKILINK_RE = re.compile(r"\[\[concepts/([^\]\|#]+)(?:\|[^\]]+)?(?:#[^\]]+)?\]\]")


def list_existing_concepts() -> set[str]:
    """Existing concept slugs in BOTH the hierarchical (``t/example-topic``) and
    bare-stem (``example-topic``) forms, so a wikilink in either form is matched.

    Concepts are sharded by-letter (``wiki/concepts/<letter>/<slug>.md``) after
    the OKF hierarchical migration, so this MUST recurse — a flat ``glob('*.md')``
    finds only ``INDEX.md`` and reports every existing concept as "missing"
    (which would flood the concept-backfill lane)."""
    cdir = VAULT / "wiki" / "concepts"
    if not cdir.is_dir():
        return set()
    out = set()
    for p in cdir.rglob("*.md"):
        if p.name.startswith("_") or p.name == "INDEX.md":
            continue
        out.add(p.relative_to(cdir).with_suffix("").as_posix())  # t/example-topic
        out.add(p.stem)                                          # example-topic
    return out


def scan_wikilinks() -> dict[str, list[Path]]:
    """Return {concept_slug: [paths_referencing_it, ...]}."""
    refs: dict[str, set[Path]] = defaultdict(set)
    wiki_dir = VAULT / "wiki"
    if not wiki_dir.is_dir():
        return {}
    # Skip the lint reports and dashboards — they enumerate broken links
    # and would inflate counts without representing real usage.
    for p in wiki_dir.rglob("*.md"):
        if "/lint-" in p.as_posix() or "/dashboards/" in p.as_posix():
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        for m in WIKILINK_RE.finditer(txt):
            slug = m.group(1).strip().rstrip("/")
            if slug:
                refs[slug].add(p)
    return {k: sorted(v) for k, v in refs.items()}


def canonical_concept_key(reference: str) -> str:
    """Physical key for a missing logical concept reference.

    References may be bare (``concepts/foo``) or already carry the first-letter
    shard (``concepts/f/foo``).  The page identity is the final slug in either
    form; ``write_key`` then applies the live schema and any active reshard.
    """
    slug = reference.strip("/").split("/")[-1]
    return okf_migrate.write_key(VAULT, "concepts", slug, {"type": "concept"})


def main() -> int:
    existing = list_existing_concepts()
    refs = scan_wikilinks()
    missing = [(slug, paths) for slug, paths in refs.items() if slug not in existing]
    missing.sort(key=lambda t: (-len(t[1]), t[0]))

    print("=== concept-backfill wake-gate ===")
    print(f"  vault: {VAULT}")
    print(f"  existing concept pages: {len(existing)}")
    print(f"  unique concept wikilinks total: {len(refs)}")
    print(f"  missing concept pages: {len(missing)}")

    if not missing:
        print("  → SKIP: every wikilinked concept has a target page")
        print(json.dumps({"wakeAgent": False}))
        return 0

    top_inbound = len(missing[0][1]) if missing else 0
    if top_inbound < MIN_INBOUND_TO_FIRE:
        print(f"  → SKIP: top missing concept has only {top_inbound} inbound refs (threshold: {MIN_INBOUND_TO_FIRE})")
        print(json.dumps({"wakeAgent": False}))
        return 0

    chosen = missing[:N]

    print(f"  top missing has {top_inbound} inbound refs")
    print(f"  batch: {len(chosen)} of {len(missing)}")
    print()
    print("=== batch ===")
    print("For each missing concept below, create it through "
          "`mcp_okengine_write_create_entity` using the displayed canonical key, "
          "with frontmatter and a body synthesized from the citing sources. Per "
          "the vault CLAUDE.md, a concept page describes a category, pattern, "
          "policy, or trend — NOT a specific organization or named actor (those "
          "are entities). Do not use direct file-write tools for wiki pages.\n")

    for i, (slug, paths) in enumerate(chosen, 1):
        print(f"## {i}. `concepts/{slug}` ({len(paths)} inbound refs)")
        print()
        print(f"  canonical write key: `{canonical_concept_key(slug)}`")
        print(f"  citing pages (showing up to {MAX_CITING_SOURCES}):")
        for citing in paths[:MAX_CITING_SOURCES]:
            rel = citing.relative_to(VAULT).as_posix()
            print(f"    - `{rel}`")
        if len(paths) > MAX_CITING_SOURCES:
            print(f"    - ... and {len(paths) - MAX_CITING_SOURCES} more")
        print()

    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    # DeepSeek off-peak deferral (CRON_DEFER_UTC_HOURS): during the configured peak UTC window
    # emit nothing — cron-plus wakes the agent only on non-empty stdout (scheduler.py), so this
    # bulk drain silently defers to the next off-peak fire (no model call at 2x price).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from offpeak import offpeak_defer
    if offpeak_defer():
        sys.exit(0)
    sys.exit(main())
