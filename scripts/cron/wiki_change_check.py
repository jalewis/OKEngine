#!/usr/bin/env python3
"""Pre-check for the `wiki-health-audit` cron job.

If nothing in $WIKI_PATH/wiki/ has been edited since the last lint pass,
emits {"wakeAgent": false} as the final stdout line — Hermes' scheduler
skips the agent invocation entirely. Otherwise emits a brief delta
summary so the agent has a hint about what's new before it lints.

State: $HERMES_HOME/scripts/lint-state.json — last-seen baseline mtime.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
STATE_PATH = HERMES_HOME / "scripts" / "lint-state.json"


def _skip(name: str) -> bool:
    """Generated / reserved files the lint never targets (they're regenerated, not authored):
    the per-directory INDEX pages (build_index_tree), backups, and underscore/dot reserved.
    Including them floods the changeset on every index rebuild and overruns the lint agent."""
    return (name.startswith(("_", ".")) or ".bak." in name
            or name in ("INDEX.md", "index.md")
            or name.startswith(("INDEX-", "index-")))


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_baseline_mtime": 0.0}
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last_baseline_mtime": 0.0}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def commit_baseline(candidate: float) -> int:
    """Advance only after the lint agent completed its report/log/index writes."""
    state = load_state()
    pending = float(state.get("pending_baseline_mtime", 0.0))
    if not pending or abs(pending - candidate) > 0.000001:
        print("wiki-change-check: baseline commit refused — candidate is not the current pending scan",
              file=sys.stderr)
        return 1
    state["last_baseline_mtime"] = pending
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    state.pop("pending_baseline_mtime", None)
    state.pop("pending_started_at", None)
    save_state(state)
    print(f"wiki-change-check: committed successful lint baseline {pending}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--commit-baseline", type=float)
    args, _ = ap.parse_known_args(argv)
    if args.commit_baseline is not None:
        return commit_baseline(args.commit_baseline)

    wiki_dir = VAULT / "wiki"
    if not wiki_dir.exists():
        print(f"# wiki-change-check: `{wiki_dir}` does not exist")
        print('{"wakeAgent": false}')
        return 0

    state = load_state()
    baseline = float(state.get("last_baseline_mtime", 0.0))

    files = [p for p in wiki_dir.rglob("*.md") if p.is_file() and not _skip(p.name)]
    if not files:
        print("# wiki-change-check: wiki has no markdown pages yet")
        print('{"wakeAgent": false}')
        return 0

    max_mtime = max(p.stat().st_mtime for p in files)
    changed = [p for p in files if p.stat().st_mtime > baseline]

    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"# Wiki change check — {now_iso}\n")
    print(f"**Wiki path:** `{wiki_dir}`")
    print(f"**Total markdown pages:** {len(files)}")
    print(f"**Pages modified since last lint:** {len(changed)}")
    print(f"**Baseline mtime:** {datetime.fromtimestamp(baseline, tz=timezone.utc).isoformat() if baseline else '(never linted)'}")
    print(f"**Latest mtime:** {datetime.fromtimestamp(max_mtime, tz=timezone.utc).isoformat()}\n")

    if not changed:
        print("No wiki pages have changed since the last lint pass. Lint can be skipped.")
        print('{"wakeAgent": false}')
        return 0

    SAMPLE = 25
    print(f"## Sample of pages modified since last lint (up to {SAMPLE})\n")
    changed.sort(key=lambda p: -p.stat().st_mtime)
    for p in changed[:SAMPLE]:
        rel = p.relative_to(VAULT)
        ts = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        print(f"- `{rel}` (mtime: {ts})")
    if len(changed) > SAMPLE:
        print(f"- ... and {len(changed) - SAMPLE} more")
    print()

    # A pre-run script cannot know whether the following agent succeeds. Record a CANDIDATE only;
    # the agent commits it after the lint report/log/index writes complete. If the agent fails, the
    # durable baseline stays put and the same change set is offered again next week.
    state["pending_baseline_mtime"] = max_mtime
    state["pending_started_at"] = now_iso
    save_state(state)
    print("## Success acknowledgement required\n")
    print("After the lint report, log append, and index update ALL succeed, run this exact command:")
    print(f"`python3 /opt/data/scripts/wiki_change_check.py --commit-baseline {max_mtime}`")
    print("Do not run it after a partial or failed lint; leaving it pending makes the changes retry.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
