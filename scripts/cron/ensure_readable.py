#!/usr/bin/env python3
"""ensure-readable — keep every vault page reader-readable (no_agent).

Pages written through the enforced `okengine-write` path land world-readable
(0644). But an agent that writes a page via Hermes' built-in `file` tool — e.g.
the daily brief — creates it 0600 (owner-only). The read-only reader runs as a
different uid and serves pages via the other-read bit, so a 0600 page returns
"page not found" even though it exists on disk.

This sweep restores the group- and other-read bit on any `wiki/**.md` missing it.
It is:
  * writer-agnostic — catches the brief and anything else, regardless of tool;
  * additive only — never clears a bit, so it can't widen write access or break
    a deliberately-restricted file beyond making it READable;
  * idempotent + cheap — stats every page but only chmods the (usually zero)
    offenders, so it is safe to run frequently.

Pure script / no_agent. Env: WIKI_PATH (default /opt/vault).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
_WANT = stat.S_IRGRP | stat.S_IROTH   # g+r, o+r — the bits the reader needs


def run(wiki: Path) -> dict:
    """Add g+r/o+r to any page missing it. Returns {fixed, pages}."""
    fixed: list[str] = []
    if not wiki.is_dir():
        return {"fixed": 0, "pages": fixed}
    for p in wiki.rglob("*.md"):
        try:
            mode = p.stat().st_mode
        except OSError:
            continue
        if (mode & _WANT) != _WANT:
            try:
                os.chmod(p, mode | _WANT)
                fixed.append(p.relative_to(wiki).as_posix())
            except OSError:
                continue
    return {"fixed": len(fixed), "pages": fixed}


def main() -> int:
    r = run(WIKI)
    if r["fixed"]:
        print(f"ensure-readable: restored g+r/o+r on {r['fixed']} page(s): "
              f"{', '.join(r['pages'][:10])}", file=sys.stderr)
    else:
        print("ensure-readable: all pages already reader-readable", file=sys.stderr)
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
