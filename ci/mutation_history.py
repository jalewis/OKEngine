#!/usr/bin/env python3
"""Surface a mutation target that has silently STOPPED being measured (okengine#612).

The gate already reports, per run, that a target's campaign did not run. What it cannot see is
DURATION -- and duration is the whole signal. `okengine-mcp/write_server.py`, the enforced write
path and the most critical file in the manifest, stopped producing a score on 2026-08-05 and
nobody noticed until 2026-08-22. Seventeen nightly runs each said so, correctly, once.

A single run cannot tell "this target is unmeasured today" from "this target has been unmeasured
since the summer". This reads a window of runs and reports the streak, so the second one is a
named condition instead of something you find by reading a nine-hour log.

Deliberately NOT a job inside the pipeline it watches: if the runner is the broken thing, an
in-pipeline watcher does not run either (okengine#611). It takes its input from published
summaries, so it can be run from a workstation, a cron, or CI, and answers the same way.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 3


def measured_paths(run: dict) -> set[str]:
    """Targets that produced a real score in this run.

    A `score` of None means the campaign crashed, timed out, or found nothing mutable -- in none
    of those cases is there a measurement, so none of them counts.
    """
    return {
        target["path"]
        for target in run.get("targets") or []
        if isinstance(target, dict) and target.get("path") and target.get("score") is not None
    }


def dropout_streaks(runs: list[dict], expected: list[str]) -> dict[str, int]:
    """path -> number of consecutive most-recent runs in which it produced no score.

    `runs` is NEWEST FIRST. A target absent from a run entirely counts as unmeasured: dropping out
    of the manifest is the same observable outcome as failing in it, and is arguably worse because
    nothing in that run mentions it at all.

    Only targets currently expected (i.e. in the manifest) are reported -- a target deliberately
    retired should not haunt the report forever.
    """
    streaks: dict[str, int] = {}
    for path in expected:
        streak = 0
        for run in runs:
            if path in measured_paths(run):
                break
            streak += 1
        if streak:
            streaks[path] = streak
    return streaks


def load_summaries(directory: Path) -> list[dict]:
    """Published `summary.json` files, newest first by filename.

    Names are expected to sort chronologically (the CI helper writes `summary-<pipeline id>.json`,
    and pipeline ids increase). Sorting by name rather than mtime keeps a re-downloaded artifact
    from reordering history.
    """
    files = sorted(directory.glob("*.json"), reverse=True)
    runs = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"mutation-history: {path} is unreadable ({exc}); refusing to report "
                             f"a streak over a window with a hole in it")
        if isinstance(payload, dict):
            runs.append(payload)
    return runs


def expected_targets(manifest_path: Path, critical_only: bool) -> list[str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"mutation-history: cannot read {manifest_path} ({exc}) — the expected "
                         f"set is UNDETECTABLE, which is not the same as empty")
    targets = manifest.get("targets") or []
    return [t["path"] for t in targets
            if isinstance(t, dict) and t.get("path") and (t.get("critical") or not critical_only)]


def report(streaks: dict[str, int], runs: int, threshold: int, expected: int) -> tuple[list[str], int]:
    lines = [f"mutation-history: {expected} expected target(s) over the last {runs} run(s), "
             f"threshold {threshold}"]
    if not streaks:
        lines.append("  every expected target produced a score in the most recent run.")
        return lines, 0
    breached = {path: n for path, n in streaks.items() if n >= threshold}
    for path, n in sorted(streaks.items(), key=lambda kv: (-kv[1], kv[0])):
        mark = "✗" if n >= threshold else "⚠"
        plural = "" if n == 1 else "s"
        lines.append(f"  {mark} {path}: no score in the last {n} run{plural}")
    if breached:
        lines.append(f"mutation-history: {len(breached)} target(s) have produced no score for "
                     f"{threshold}+ consecutive runs — they are not being measured, and a floor "
                     f"they never reach cannot fail. See okengine#612.")
        return lines, 1
    return lines, 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summaries", type=Path, required=True,
                        help="directory of published summary.json files, one per run")
    parser.add_argument("--manifest", type=Path, default=Path("mutation/targets.json"))
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--critical-only", action="store_true")
    args = parser.parse_args(argv)

    if args.threshold < 1:
        raise SystemExit("mutation-history: --threshold must be at least 1")
    if not args.summaries.is_dir():
        raise SystemExit(f"mutation-history: {args.summaries} is not a directory — with no run "
                         f"history the streak is UNDETECTABLE, and reporting 'no dropouts' here "
                         f"would be a vacuous pass")

    runs = load_summaries(args.summaries)
    if not runs:
        raise SystemExit(f"mutation-history: no run summaries in {args.summaries} — the streak is "
                         f"UNDETECTABLE. Silence is not success.")

    expected = expected_targets(args.manifest, args.critical_only)
    if not expected:
        raise SystemExit("mutation-history: the manifest declares no matching targets, so this "
                         "measured nothing; refusing to report a clean sweep over an empty set")

    lines, code = report(dropout_streaks(runs, expected), len(runs), args.threshold, len(expected))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
