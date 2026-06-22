#!/usr/bin/env python3
"""Wake-gate for the broken-YAML repair cron (propose/dispose phase 1).

Scans wiki/ for pages whose frontmatter won't parse but DO have a locatable
frontmatter/body boundary (a closing `---`), and emits a batch the agent
uses to propose corrected frontmatter. Files with no closing delimiter are
out of scope here (the deterministic repair_broken_frontmatter cron handles
the body-bleed class; truly structureless files are reported, not batched).

Emits each batch item with: path, the raw (broken) frontmatter, and the
yaml error — enough for the agent to fix syntax without guessing.

Batch size BATCH_SIZE (default 8). Skips backup/_archived/debris.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
BATCH_SIZE = int(os.environ.get("YAML_REPAIR_BATCH_SIZE", "8"))

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?(.*)\Z", re.S)
_SKIP_SUBSTRINGS = (".bak", "_archived/", ".was-broken", ".restored",
                    ".corrupt", ".recovered", ".backup", ".applied")
# wiki-root debris log fragments (gitignored) — never batch these
_DEBRIS_NAME_RE = re.compile(r"(log[-_]|ingest-log|lint-|index_)")


def _is_skippable(path: Path) -> bool:
    if any(frag in str(path) for frag in _SKIP_SUBSTRINGS):
        return True
    # wiki-root debris (depth-1 only)
    if path.parent == WIKI and _DEBRIS_NAME_RE.search(path.name):
        return True
    return False


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: {WIKI} does not exist", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    batchable = []      # (path, fm_text, error, kind) — kind: "closed" | "structureless"
    for path in sorted(WIKI.rglob("*.md")):
        if _is_skippable(path):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        m = _FM_RE.match(text)
        if not m:
            # opening --- but no clean close. Structureless body-bleed — the
            # agent must also supply body_starts_with so the applier can split.
            head = text[4:]
            try:
                yaml.safe_load(head.split("\n---")[0])
                continue  # head parses; not actually broken
            except Exception as e:
                err = str(e).split("\n")[0][:80]
                # show the first ~30 lines so the agent sees the fm/body bleed
                preview = "\n".join(text.splitlines()[:30])
                batchable.append((str(path.relative_to(VAULT)), preview, err, "structureless"))
            continue
        fm_text = m.group(1)
        try:
            data = yaml.safe_load(fm_text)
            if isinstance(data, dict):
                continue  # parses fine
        except Exception as e:
            err = str(e).split("\n")[0][:80]
            batchable.append((str(path.relative_to(VAULT)), fm_text, err, "closed"))

    n_closed = sum(1 for b in batchable if b[3] == "closed")
    n_struct = sum(1 for b in batchable if b[3] == "structureless")
    print("=== select-broken-yaml ===")
    print(f"  broken batchable: {len(batchable)} ({n_closed} closed, {n_struct} structureless)")
    print(f"  batch size: {min(len(batchable), BATCH_SIZE)} / {BATCH_SIZE}")
    print()

    if not batchable:
        print("  → SKIP: no batchable broken-YAML files")
        print(json.dumps({"wakeAgent": False}))
        return 0

    batch = batchable[:BATCH_SIZE]
    print("=== batch ===")
    for i, (rel, fm, err, kind) in enumerate(batch, 1):
        print(f"\n#{i} `{rel}`  [{kind}]")
        print(f"  yaml error: {err}")
        if kind == "structureless":
            print("  NO closing `---` — body bled into the block. Provide BOTH a")
            print("  corrected `frontmatter` AND `body_starts_with` (the verbatim")
            print("  first body line) so the applier can split + preserve the body.")
            print("  current head (frontmatter + bled body, first 30 lines):")
        else:
            print("  current (broken) frontmatter:")
        for line in fm.rstrip("\n").splitlines():
            print(f"    {line}")
    print()
    if len(batchable) > BATCH_SIZE:
        print(f"_Deferred to subsequent passes: {len(batchable) - BATCH_SIZE}._")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
