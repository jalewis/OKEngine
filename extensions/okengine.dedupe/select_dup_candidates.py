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
import hashlib
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except Exception:                              # pragma: no cover - yaml is a runtime dep
    yaml = None

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
ENTITIES = VAULT / "wiki" / "entities"
MAX_GROUPS = int(os.environ.get(
    "OKENGINE_CONFIG_MAX_GROUPS", os.environ.get("OKENGINE_DEDUPE_MAX_GROUPS", "25")
))
_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)
STATE = VAULT / ".okengine" / "dedupe-selector-state.json"
MANIFEST = VAULT / ".okengine" / "dedupe-selection.json"


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
        if p.name == "INDEX.md" or p.name.startswith("INDEX-"):
            continue
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
        # The write path only coerces a scalar STRING -> list; a non-string scalar (`aliases: 8220`
        # -> YAML int, a bool, a date) lands as a bare scalar and `{_norm(a) for a in 8220}` raised
        # TypeError, killing the whole dedupe wake-gate (invariant-audit #28). Wrap any non-list scalar.
        if not isinstance(aliases, list):
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


def _group_id(members: list[str]) -> str:
    raw = json.dumps(sorted(members), separators=(",", ":"))
    return "dedupe-group:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _select_rotating(groups: list, limit: int, state_path: Path) -> list:
    """Rotate through the complete candidate set so ambiguous early groups cannot starve later work."""
    if not groups or limit <= 0:
        return []
    try:
        state = json.loads(state_path.read_text())
        cursor = int(state.get("cursor", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        cursor = 0
    cursor %= len(groups)
    count = min(limit, len(groups))
    selected = [groups[(cursor + offset) % len(groups)] for offset in range(count)]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps({"api": 1, "cursor": (cursor + count) % len(groups),
                                "candidate_count": len(groups)}, indent=2) + "\n")
    temp.replace(state_path)
    return selected


def _write_manifest(keys: list[str], path: Path) -> dict:
    manifest = {"api": 1, "selected": keys,
                "input_digest": "sha256:" + hashlib.sha256(
                    json.dumps(keys, separators=(",", ":")).encode()).hexdigest(),
                "lane_id": os.environ.get("OKENGINE_LANE_ID", ""),
                "contract_digest": os.environ.get("OKENGINE_CONTRACT_DIGEST", "")}
    target = Path(os.environ.get("OKENGINE_SELECTION_MANIFEST", str(path)))
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, indent=2) + "\n")
    temp.replace(target)
    return manifest


def main() -> int:
    pages = scan(ENTITIES, VAULT)
    groups = find_groups(pages)
    if not groups:
        print("dedupe: no duplicate-entity candidates (no name/alias collisions).")
        print(json.dumps({"wakeAgent": False}))
        return 0
    selected_groups = _select_rotating(groups, MAX_GROUPS, STATE)
    selected_keys = [_group_id(members) for _key, members in selected_groups]
    manifest = _write_manifest(selected_keys, MANIFEST)
    print(f"{len(groups)} duplicate-entity candidate group(s) — name/alias collision "
          f"(rotating batch of {len(selected_groups)}):")
    for item_key, (key, members) in zip(selected_keys, selected_groups):
        labels = " | ".join(f"[[{m}]] ({pages[m]['name']})" for m in members)
        print(f"  - `{item_key}` «{key}»  {labels}")
    receipt = {"api": 1, "lane_id": manifest["lane_id"],
               "contract_digest": manifest["contract_digest"],
               "input_digest": manifest["input_digest"],
               "items": [{"key": key,
                           "disposition": "<accepted|duplicate|skipped|rejected|failed|deferred>",
                           "writes": [{"path": "wiki/entities/<path>.md",
                                       "sha256": "sha256:<current-file-hash>"}],
                           "reason": "<required unless accepted>"} for key in selected_keys]}
    print("FINAL RESPONSE CONTRACT (MANDATORY): return ONLY this fenced receipt; use skipped "
          "with a reason for distinct entities and remove placeholder writes from non-accepted items.")
    print("```okengine-receipt")
    print(json.dumps(receipt, indent=2))
    print("```")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
