#!/usr/bin/env python3
"""Find staged extension script dirs that the current stage plan no longer stages (okengine#994 [27]).

deploy-cron-scripts.sh stages each enabled in-gateway extension's *.py into
/opt/data/scripts/<id>/ by tar extraction, which only ever ADDS. An extension that is later
disabled, removed from the pack, or no longer discovered keeps its staged dir forever: its code
stays importable, and deployment_checks' "staged scripts but has NO lane" WARN fires on every
validate run until someone deletes the dir by hand, which trains operators to ignore it.

Split like staged_drift.py: the decision is pure and tested here; the caller lists the container's
subdirs with one `docker exec` and pipes them in with `--staged-listing -`.

Only a name that is a valid extension id (extension_manifest.ID_RE) is a candidate, so
`__pycache__` and anything that cannot be an extension dir are never reported. Removal is the
caller's job and is opt-in (`--prune-stale-extensions`): the default is to report.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extension_manifest import ID_RE  # noqa: E402


def stale_dirs(staged: list[str], planned: list[str]) -> list[str]:
    """Staged extension dir names that the stage plan does not stage, sorted."""
    keep = set(planned)
    return sorted({name for name in staged if ID_RE.match(name) and name not in keep})


def _plan_ids(plan_text: str) -> list[str]:
    """The ids from `framework extensions stage-plan` output (`<id>\\t<src-dir>` per line)."""
    return [line.split("\t")[0] for line in plan_text.splitlines() if line.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged-listing", required=True,
                        help="file of staged subdir names, one per line ('-' for stdin)")
    parser.add_argument("--plan", required=True,
                        help="file holding the stage-plan output (<id>\\t<dir> per line)")
    args = parser.parse_args(argv)
    listing = (sys.stdin.read() if args.staged_listing == "-"
               else Path(args.staged_listing).read_text(encoding="utf-8"))
    plan = Path(args.plan).read_text(encoding="utf-8")
    staged = [line.strip() for line in listing.splitlines() if line.strip()]
    for name in stale_dirs(staged, _plan_ids(plan)):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
