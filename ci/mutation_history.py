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
import datetime as dt
import json
import re
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 3
# The gate's re-baseline window (okengine#943): a score carried forward from an older measurement
# is fresh for at most this long. The watcher applies its OWN bound rather than trusting the one a
# summary declares, so a misconfigured gate cannot widen what counts as measured.
DEFAULT_REBASELINE_DAYS = 7


def _moment(value) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else None


def fresh_carry(run: dict, target: dict, days: int) -> bool:
    """Whether a carried-forward score was measured within ``days`` of this run.

    Missing or unreadable dates are not fresh: an undatable carry is no evidence of measurement.
    """
    reused = target.get("reused")
    run_at, measured_at = _moment(run.get("generated_at")), _moment(
        reused.get("measured_at") if isinstance(reused, dict) else None)
    if run_at is None or measured_at is None:
        return False
    return dt.timedelta(0) <= run_at - measured_at <= dt.timedelta(days=days)


def measured_paths(run: dict, days: int = DEFAULT_REBASELINE_DAYS) -> set[str]:
    """Targets that produced a real score in this run.

    A `score` of None means the campaign crashed, timed out, or found nothing mutable -- in none
    of those cases is there a measurement, so none of them counts. A score carried forward from an
    earlier run (okengine#943) counts only while that measurement is inside the re-baseline window.
    """
    return {
        target["path"]
        for target in run.get("targets") or []
        if isinstance(target, dict) and target.get("path") and target.get("score") is not None
        and (not target.get("reused") or fresh_carry(run, target, days))
    }


def dropout_streaks(runs: list[dict], expected: list[str],
                    days: int = DEFAULT_REBASELINE_DAYS) -> dict[str, int]:
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
            if path in measured_paths(run, days):
                break
            streak += 1
        if streak:
            streaks[path] = streak
    return streaks


def chronological_key(path: Path) -> tuple:
    """Sort key that orders `summary-<pipeline id>.json` by the id's VALUE, not its spelling.

    A plain string sort puts `summary-9947` after `summary-13159`, so every four-digit pipeline
    read as newer than every five-digit one; the watcher then reported six critical targets as
    unmeasured for 16 runs that predated them, while the real newest run had scored all six
    (okengine#1047). Splitting on digit runs keeps strings and integers at fixed positions, so
    names without an id still compare deterministically.
    """
    return tuple(int(part) if index % 2 else part
                 for index, part in enumerate(re.split(r"(\d+)", path.name)))


def load_summaries(directory: Path) -> list[dict]:
    """Published `summary.json` files, newest first by pipeline id.

    The CI helper writes `summary-<pipeline id>.json`, and pipeline ids increase, so the id's
    numeric value is chronological. Sorting by name rather than mtime keeps a re-downloaded
    artifact from reordering history.
    """
    files = sorted(directory.glob("*.json"), key=chronological_key, reverse=True)
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
    parser.add_argument("--rebaseline-days", type=int, default=DEFAULT_REBASELINE_DAYS,
                        help="a carried-forward score counts as measured only this long after "
                             "its measurement (okengine#943)")
    args = parser.parse_args(argv)

    if args.threshold < 1:
        raise SystemExit("mutation-history: --threshold must be at least 1")
    if args.rebaseline_days < 1:
        raise SystemExit("mutation-history: --rebaseline-days must be at least 1")
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

    lines, code = report(dropout_streaks(runs, expected, args.rebaseline_days), len(runs),
                         args.threshold, len(expected))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
