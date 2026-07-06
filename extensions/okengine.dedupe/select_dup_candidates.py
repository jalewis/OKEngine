#!/usr/bin/env python3
"""okengine.dedupe wake-gate. Scans entity pages for likely DUPLICATES — distinct pages whose
normalized name collides, or whose name matches another page's `aliases:`. Prints a digest of the
candidate groups, then a final `{"wakeAgent": bool}` line (the cron-plus wake-gate protocol). No
writes here — the agent reviews each group and merges true duplicates via the write MCP.
LOCAL-ONLY; deterministic name/alias matching (the okengine.embeddings sidecar adds semantic
candidates later)."""
import json
import os
import re
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except Exception:                              # pragma: no cover
    yaml = None

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
ENTITIES = VAULT / "wiki" / "entities"
MAX_GROUPS = int(os.environ.get("OKENGINE_CONFIG_MAX_GROUPS", "25"))
_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _frontmatter(text: str) -> dict:
    m = _FM.match(text)
    if not m or yaml is None:
        return {}
    try:
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def scan(entities: Path, vault: Path) -> dict:
    """slug -> {name, norm, aliases:set[norm]}. Skips tombstoned (already-merged) pages."""
    pages = {}
    if not entities.is_dir():
        return pages
    for p in entities.rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = _frontmatter(text)
        if str(fm.get("status") or "") == "tombstoned":
            continue
        slug = p.relative_to(vault / "wiki").as_posix()[:-3]
        name = str(fm.get("title") or p.stem)
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        pages[slug] = {"name": name, "norm": _norm(name),
                       "aliases": {_norm(a) for a in aliases if _norm(a)}}
    return pages


def find_groups(pages: dict) -> list:
    """Candidate groups: ≥2 distinct pages sharing a normalized name- or alias-key."""
    by_key = defaultdict(set)
    for slug, info in pages.items():
        if info["norm"]:
            by_key[info["norm"]].add(slug)
        for a in info["aliases"]:
            by_key[a].add(slug)
    groups, seen = [], set()
    for key, slugs in sorted(by_key.items()):
        members = tuple(sorted(slugs))
        if len(members) >= 2 and members not in seen:
            seen.add(members)
            groups.append((key, list(members)))
    return groups


def main() -> int:
    pages = scan(ENTITIES, VAULT)
    groups = find_groups(pages)
    if not groups:
        print("dedupe: no duplicate-entity candidates (no name/alias collisions).")
        print(json.dumps({"wakeAgent": False}))
        return 0
    print(f"{len(groups)} duplicate-entity candidate group(s) — name/alias collision "
          f"(showing up to {MAX_GROUPS}):")
    for key, members in groups[:MAX_GROUPS]:
        labels = " | ".join(f"[[{m}]] ({pages[m]['name']})" for m in members)
        print(f"  - «{key}»  {labels}")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
