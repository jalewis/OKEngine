#!/usr/bin/env python3
"""CLI wrapper around IWE — markdown knowledge-graph over wiki/.

The okengine-mcp server invokes this SERVER-SIDE (inside the MCP container, where the
IWE binary is installed) to back its graph tools — find_references / retrieve_context /
graph_stats. The agent reaches the graph ONLY through those MCP tools; it does not run
this directly (the gateway/agent image ships no IWE binary). IWE indexes the vault's
links — both [[wikilinks]] and markdown — into a graph; generated catalog pages (HOT,
the Wiki Index, per-directory INDEX) are filtered from the reference lists
(_filter_iwe_refs) so they don't pollute the agent's recall.

This wrapper pins the IWE binary path + the project root (wiki/, where .iwe/
config lives and where [[entities/x]]-style links resolve) so callers don't
have to cd. READ-ONLY: it exposes only non-mutating subcommands — it will
NOT run normalize/squash/extract/rename/delete/update (which rewrite files).

Usage:
    kb_graph.py stats
    kb_graph.py find "example-topic"
    kb_graph.py retrieve concepts/example-topic
    kb_graph.py tree concepts/example-topic

Exit codes: 0 ok · 1 disallowed subcommand · 2 iwe error.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

# Prefer the image-installed binary (on PATH); fall back to the /opt/data copy.
IWE_BIN = os.environ.get("IWE_BIN") or shutil.which("iwe") or "/opt/data/iwe/bin/iwe"
WIKI_ROOT = os.environ.get("WIKI_PATH", "/opt/vault") + "/wiki"
# Only read-only graph operations are exposed via this wrapper.
ALLOWED = {"stats", "find", "retrieve", "tree", "count", "export", "schema"}
# Subcommands whose output carries reference/backlink lists we should de-noise.
_FILTERED = {"find", "retrieve", "stats"}

_EXCLUDED_NS: frozenset[str] | None = None


def _excluded_namespaces() -> frozenset[str]:
    """Top-level namespaces the pack's schema.yaml marks `exclude:` (e.g. operational/)
    — generated/operator content that shouldn't appear in the knowledge graph the agent
    traverses. Read once; empty if schema/yaml unavailable (reserved-name filter still
    applies)."""
    global _EXCLUDED_NS
    if _EXCLUDED_NS is not None:
        return _EXCLUDED_NS
    out: set[str] = set()
    try:
        import yaml
        sp = os.path.join(os.environ.get("WIKI_PATH", "/opt/vault"), "schema.yaml")
        with open(sp, encoding="utf-8") as f:
            for e in (yaml.safe_load(f) or {}).get("exclude") or []:
                seg = str(e).strip().strip("/")
                seg = (seg[5:] if seg.startswith("wiki/") else seg).strip("/").split("/")[0]
                if seg:
                    out.add(seg)
    except Exception:
        pass
    _EXCLUDED_NS = frozenset(out)
    return _EXCLUDED_NS


def _is_reserved_key(key: str) -> bool:
    """True for generated/operational pages that must NOT appear as graph edges:
    HOT/log/index/INDEX(/-pNN) root artifacts, underscore/dot/backup reserved files,
    and any `exclude:`-ed namespace. Keeps real entities/sources/concepts/briefings.
    IWE keys arrive without the .md extension (e.g. 'index', 'entities/a/INDEX')."""
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(".md"):
        name += ".md"
    if (name in ("HOT.md", "log.md", "INDEX.md", "index.md")
            or name.startswith(("INDEX-", "index-", "_", "."))
            or ".bak." in name):
        return True
    ns = key.split("/", 1)[0] if "/" in key else ""
    return bool(ns) and ns in _excluded_namespaces()


def _filter_iwe_refs(text: str) -> str:
    """Drop reserved/generated entries from IWE's `references:`/`referencedBy:` lists so
    the agent's knowledge-graph recall isn't polluted by the catalog pages (HOT, the
    Wiki Index, per-dir INDEX pages) that link to everything. Line-scoped: only `- key:`
    items inside a reference section are removed, along with their indented continuation;
    page bodies and other output pass through unchanged."""
    out: list[str] = []
    in_ref = dropping = False
    for line in text.splitlines(keepends=True):
        s = line.rstrip("\n")
        if in_ref and s.startswith("- key:"):
            dropping = _is_reserved_key(s[len("- key:"):].strip())
            if not dropping:
                out.append(line)
            continue
        if s[:1].isspace():                       # indented continuation of a list item
            if not dropping:
                out.append(line)
            continue
        dropping = False                          # any column-0 line ends the current item
        if s and not s.startswith("-"):
            in_ref = s.split(":", 1)[0].strip() in ("references", "referencedBy")
        out.append(line)
    return "".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Navigate the wiki knowledge graph via IWE.")
    ap.add_argument("subcommand", help=f"one of: {', '.join(sorted(ALLOWED))}")
    ap.add_argument("args", nargs=argparse.REMAINDER, help="args passed to iwe")
    ns = ap.parse_args(argv)

    if ns.subcommand not in ALLOWED:
        print(f"ERROR: '{ns.subcommand}' is not a permitted (read-only) IWE op. "
              f"Allowed: {', '.join(sorted(ALLOWED))}", file=sys.stderr)
        return 1

    cmd = [IWE_BIN, ns.subcommand, *ns.args]
    try:
        proc = subprocess.run(cmd, cwd=WIKI_ROOT, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print(f"ERROR: iwe binary not found at {IWE_BIN}.", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("ERROR: iwe timed out building the graph.", file=sys.stderr)
        return 2

    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return 2
    out = _filter_iwe_refs(proc.stdout) if ns.subcommand in _FILTERED else proc.stdout
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
