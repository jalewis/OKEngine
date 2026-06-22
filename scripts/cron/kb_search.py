#!/usr/bin/env python3
"""CLI wrapper around qmd — local hybrid search over the wiki corpus.

Agents invoke this via the `terminal` tool to search the knowledge base
semantically (concept/narrative queries) instead of grepping. qmd indexes
wiki/ (BM25 + vector + LLM rerank), all on-device. This wrapper sets the
persistent data dir + CPU mode so callers don't have to.

Modes:
    query   — hybrid (BM25 + vector + rerank), best quality (needs embeddings)
    search  — BM25 keyword only, instant, no models (always available)
    vsearch — vector similarity only (needs embeddings)

Usage:
    kb_search.py "example topic in a sub-domain"              # hybrid
    kb_search.py --mode search "specific keyword phrase"      # BM25
    kb_search.py --limit 5 "entity consolidation trend"
    kb_search.py --tier hot,warm "recent entity moves"        # G4 tier filter
    kb_search.py --raw "..."        # pass qmd output through unfiltered

--tier filters hits by DERIVED hot/warm/cold tier (tier_lib, G4): a hit's tier
comes from its path/date at query time (nothing stored). Untiered namespaces are
always kept. `--raw` skips the filter.

Exit codes: 0 ok · 2 qmd error.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import tier_lib
except Exception:  # pragma: no cover - tier filtering simply unavailable
    tier_lib = None

# qmd emits one hit per block beginning with `qmd://<index>/<wiki-rel-path>:<line>`.
_HIT_RE = re.compile(r"^qmd://[^/]+/(.+?\.md)(?::|\s|$)")
_WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"

QMD_ENV = {
    "XDG_CACHE_HOME": "/opt/data/qmd/cache",
    "XDG_CONFIG_HOME": "/opt/data/qmd/config",
    "QMD_FORCE_CPU": "1",          # no GPU in the gateway container
    "HOME": os.environ.get("HOME", "/opt/data"),
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
}


def _filter_by_tier(out: str, tiers: set[str]) -> tuple[str, int]:
    """Keep only qmd hit-blocks whose derived tier is in `tiers` (untiered hits
    are always kept). Returns (filtered_text, dropped_count)."""
    if tier_lib is None:
        return out, 0
    cfg = tier_lib.load_cfg(_WIKI.parent)
    today = datetime.now(timezone.utc).date()
    lines = out.split("\n")
    # split into blocks at each `qmd://` header line
    blocks, cur = [], []
    for ln in lines:
        if _HIT_RE.match(ln) and cur:
            blocks.append(cur); cur = [ln]
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    kept, dropped = [], 0
    for b in blocks:
        head = next((l for l in b if _HIT_RE.match(l)), None)
        if head is None:
            kept.append(b); continue          # preamble / non-hit block
        rel = _HIT_RE.match(head).group(1)
        t = tier_lib.tier_of(rel, tier_lib.fm_of(_WIKI / rel), cfg, today)
        if t is None or t in tiers:
            kept.append(b)
        else:
            dropped += 1
    return "\n".join("\n".join(b) for b in kept), dropped


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Search the wiki KB via qmd.")
    ap.add_argument("query")
    ap.add_argument("--mode", choices=["query", "search", "vsearch"], default="query")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--tier", default="", help="comma list of hot,warm,cold to keep (G4)")
    ap.add_argument("--raw", action="store_true", help="pass qmd output through unmodified")
    args = ap.parse_args(argv)
    tiers = {t.strip().lower() for t in args.tier.split(",") if t.strip()}

    cmd = ["qmd", args.mode, args.query, "--limit", str(args.limit)]
    env = {**os.environ, **QMD_ENV}
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        print("ERROR: qmd not installed in this container.", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("ERROR: qmd timed out (cold model load can take ~20s; retry).", file=sys.stderr)
        return 2

    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return 2

    out = proc.stdout
    note = ""
    if tiers and not args.raw:
        out, dropped = _filter_by_tier(out, tiers)
        if dropped:
            note = f" — tier∈{{{','.join(sorted(tiers))}}}, {dropped} hit(s) filtered out"
    if not args.raw:
        # qmd emits a leading header + ANSI; keep it simple and readable for prompts.
        print(f"## KB search ({args.mode}): {args.query}{note}\n")
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
