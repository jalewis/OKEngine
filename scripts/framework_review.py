#!/usr/bin/env python3
"""framework review — human-in-the-loop sign-off (okengine#69).

  framework review <pack>                                   # show the current review queue
  framework review <pack> --approve <wiki-path> --by NAME   # sign off a page at its current version

Approval sets `reviewed_by` / `reviewed_on` THROUGH the enforced MCP write path (write_server._update
— validates, bumps version, appends to wiki/log.md), never a bypass. The page then drops off the
review queue until it's edited again (which returns it for re-review). The queue itself is built by
the review-queue cron (review_queue.py) into wiki/dashboards/review-queue.md.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="framework review")
    ap.add_argument("pack", type=Path, help="pack / vault directory")
    ap.add_argument("--approve", metavar="WIKI_PATH",
                    help="sign off a page (wiki-relative, e.g. entities/a/acme)")
    ap.add_argument("--by", metavar="NAME", help="reviewer name (required with --approve)")
    args = ap.parse_args(argv)
    os.environ["WIKI_PATH"] = str(args.pack.expanduser())

    if args.approve:
        if not args.by:
            print("ERROR: --approve requires --by NAME", file=sys.stderr)
            return 2
        sys.path.insert(0, str(_HERE.parent / "okengine-mcp"))
        try:
            import write_server as ws
        except Exception as e:
            print(f"ERROR: cannot load the write path: {e}", file=sys.stderr)
            return 1
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = ws._update(args.approve, {"reviewed_by": args.by, "reviewed_on": date.today().isoformat(),
                                        "reviewed_at": stamp, "needs_review": False})
        print(f"  {res}")
        return 0 if res.startswith(("updated", "converged")) else 1

    dash = args.pack.expanduser() / "wiki" / "dashboards" / "review-queue.md"
    if dash.is_file():
        print(dash.read_text(encoding="utf-8"))
    else:
        print("no review-queue.md yet — run the review-queue cron (review_queue.py) to build it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
