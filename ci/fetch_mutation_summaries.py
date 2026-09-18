#!/usr/bin/env python3
"""Download recent mutation-campaign summaries so ci/mutation_history.py has a window to read.

Kept SEPARATE from the detector on purpose. The detector is pure and testable over synthetic
runs; fetching needs a network, a token and a specific forge. Splitting them means the thing that
decides whether the write path is being measured has no I/O in it, and this can be replaced (a
different forge, a local archive, a cron that rsyncs artifacts) without touching that logic.

Uses `glab`, which already holds the credentials on the machines this runs from. Names each file
`summary-<pipeline id>.json` so the detector's newest-first ordering is chronological.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

JOB_NAMES = ("mutation-critical-aggregate", "mutation-full-aggregate",
             "mutation-critical", "mutation-full")


def glab_json(path: str):
    proc = subprocess.run(["glab", "api", path], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    # glab prints diagnostics before the payload; take from the first JSON delimiter.
    text = proc.stdout
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="24")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--limit", type=int, default=20, help="pipelines to scan")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    pipelines = glab_json(f"projects/{args.project}/pipelines?ref={args.ref}&per_page={args.limit}")
    if not pipelines:
        sys.exit("fetch-summaries: could not list pipelines — the window is UNDETECTABLE, which is "
                 "not the same as empty. Check `glab auth status`.")

    args.out.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for pipeline in pipelines:
        jobs = glab_json(f"projects/{args.project}/pipelines/{pipeline['id']}/jobs?per_page=100")
        if not jobs:
            continue
        eligible = [j for j in jobs if j["name"] in JOB_NAMES
                    and j["status"] in ("success", "failed")]
        job = min(eligible, key=lambda item: JOB_NAMES.index(item["name"])) \
            if eligible else None
        if job is None:
            continue
        target = args.out / f"summary-{pipeline['id']}.json"
        if target.exists():
            skipped += 1
            continue
        payload = glab_json(
            f"projects/{args.project}/jobs/{job['id']}/artifacts/artifacts/mutation/summary.json")
        if payload is None:
            # An expired or missing artifact is a HOLE, not a clean run. Say so rather than
            # writing a placeholder the detector would read as "measured nothing".
            print(f"  no summary artifact for pipeline {pipeline['id']} "
                  f"(job {job['id']}, {job['name']}) — window will be short by one", file=sys.stderr)
            continue
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written += 1
    print(f"fetch-summaries: {written} new, {skipped} already present, in {args.out}")
    if not written and not skipped:
        sys.exit("fetch-summaries: nothing fetched — refusing to leave an empty window that would "
                 "read as 'no dropouts'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
