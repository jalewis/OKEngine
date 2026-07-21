#!/usr/bin/env python3
"""edge_index.py — okengine.reevaluation: proposition → cited-evidence dependency edges.

CHE core step 1 (okengine#234). Builds the inverted map the dependency-aware selector
(#235) walks: for every OPEN proposition page, every evidence page it cites — from the
schema-relevant frontmatter ref fields (`subject`, `sources`, `basis` by default — the
write path normalizes these to plain paths, so `.backlinks.json`'s body-wikilink parse
misses them), from `evidence[].source` records, and from body wikilinks.

SCHEDULED SWEEP, NEVER EVENT-DRIVEN (the §3 guardrail): a full scan rebuilding a derived
sidecar wholesale, exactly the backlinks_refresh pattern. The artifact is machine state
(`wiki/.reevaluation-edges.json`), not canonical content:

    { "generated": "...", "proposition_count": n, "edge_count": n,
      "edges": { "<cited page>": [ {"page": "...", "status": "...",
                                    "resolves_by": "...", "via": ["sources", ...]} ] } }

Type-agnostic: which types are propositions, which statuses are open, and which fields
carry refs are env config — okcti's diagnostic class (#236) registers with zero change
here. Pure ``no_agent``; atomic tmp→replace write; ``wakeAgent=false`` always; tolerates
pages vanishing mid-scan.

Env: WIKI_PATH (default /opt/vault) · OKENGINE_REEVAL_TYPES ("prediction")
     · OKENGINE_REEVAL_OPEN_STATUSES ("open,active") · OKENGINE_REEVAL_REF_FIELDS
     ("subject,sources,basis")
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
ARTIFACT = WIKI / ".reevaluation-edges.json"

TYPES = {t.strip() for t in os.environ.get(
    "OKENGINE_REEVAL_TYPES",
    os.environ.get("OKENGINE_REEVALUATION_PROPOSITION_TYPES", "prediction"),
).split(",") if t.strip()}
OPEN = {s.strip() for s in os.environ.get(
    "OKENGINE_REEVAL_OPEN_STATUSES",
    os.environ.get("OKENGINE_REEVALUATION_OPEN_STATUSES", "open,active"),
).split(",") if s.strip()}
REF_FIELDS = [f.strip() for f in os.environ.get(
    "OKENGINE_REEVAL_REF_FIELDS",
    os.environ.get("OKENGINE_REEVALUATION_REF_FIELDS", "subject,sources,basis"),
).split(",") if f.strip()]

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]", re.DOTALL)
# operational output, not corpus — same skip set as corpus_audit
SKIP_PARTS = {"dashboards", "operational", "_archived", ".okengine", ".backlinks"}


def _norm(ref) -> str | None:
    """Normalize a ref (wikilink or plain path) to a vault-relative page key:
    strip [[ ]], |alias, #anchor, .md, leading wiki/ or /."""
    if not isinstance(ref, str):
        return None
    r = ref.strip()
    if r.startswith("[[") and r.endswith("]]"):
        r = r[2:-2]
    r = r.split("|", 1)[0].split("#", 1)[0].strip()
    if r.endswith(".md"):
        r = r[:-3]
    r = r.lstrip("/")
    if r.startswith("wiki/"):
        r = r[5:]
    return r or None


def _refs(fm: dict, body: str) -> dict:
    """{normalized ref -> set(via)} for one proposition page."""
    out: dict = defaultdict(set)
    for field in REF_FIELDS:
        v = fm.get(field)
        for item in (v if isinstance(v, list) else [v]):
            n = _norm(item)
            if n:
                out[n].add(field)
    ev = fm.get("evidence")
    if isinstance(ev, list):
        for item in ev:
            if isinstance(item, dict):
                n = _norm(item.get("source"))
                if n:
                    out[n].add("evidence.source")
    for m in _WIKILINK_RE.finditer(body or ""):
        n = _norm(m.group(0))
        if n:
            out[n].add("body")
    return out


def build(vault: Path) -> dict:
    """Scan once; return the artifact dict (pure, testable)."""
    wiki = vault / "wiki"
    edges: dict = defaultdict(list)
    props = 0
    for p in sorted(wiki.rglob("*.md")):
        rel = p.relative_to(wiki)
        if SKIP_PARTS.intersection(rel.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue                          # vanished mid-scan (mover-lane race)
        m = _FM_RE.match(text)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue
        if str(fm.get("type") or "") not in TYPES:
            continue
        if str(fm.get("status") or "") not in OPEN:
            continue
        props += 1
        page = rel.as_posix()[:-3]
        row = {
            "page": page,
            "status": str(fm.get("status")),
            "resolves_by": str(fm.get("resolves_by") or "") or None,
        }
        for cited, via in _refs(fm, text[m.end():]).items():
            if cited == page:
                continue                       # self-reference is not a dependency
            edges[cited].append({**row, "via": sorted(via)})
    return {
        "generated": date.today().isoformat(),
        "proposition_types": sorted(TYPES),
        "proposition_count": props,
        "edge_count": sum(len(v) for v in edges.values()),
        "edges": {k: edges[k] for k in sorted(edges)},
    }


def main() -> int:
    if not WIKI.is_dir():
        print(f"reevaluation-edges | no wiki/ under {VAULT} — nothing to index")
        print(json.dumps({"wakeAgent": False}))
        return 0
    art = build(VAULT)
    tmp = ARTIFACT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(art, indent=1, sort_keys=False), encoding="utf-8")
    os.replace(tmp, ARTIFACT)
    print(
        f"reevaluation-edges | {art['proposition_count']} open proposition(s) "
        f"({', '.join(art['proposition_types'])}) -> {art['edge_count']} edge(s) over "
        f"{len(art['edges'])} cited page(s) -> {ARTIFACT.relative_to(VAULT)}"
    )
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
