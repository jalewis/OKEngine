#!/usr/bin/env python3
"""framework budget — inspect / control the engine spend-cap guard (okengine#35, #97).

  framework budget --status     Show the budget-guard pause state (paused? which crons?).
  framework budget --resume     Manually resume after a budget trip: re-enable the
                                cost-bearing crons the guard paused and clear the pause
                                state. This is the supported recovery path when
                                OKENGINE_BUDGET_RESUME=manual (auto mode resumes itself
                                once window usage ages back under budget).

Honours the same env as budget_guard (OKENGINE_CRON_PLUS_CLI / _JOBS, HERMES_HOME) so it
targets the same cron-plus instance and state file the guard wrote.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_GUARD_PATH = Path(__file__).resolve().parent / "cron" / "budget_guard.py"


def _guard():
    spec = importlib.util.spec_from_file_location("budget_guard", _GUARD_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="framework budget",
        description="Inspect/control the engine spend-cap guard (okengine#35).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--resume", action="store_true",
                   help="re-enable the crons a budget trip paused and clear the pause state")
    g.add_argument("--status", action="store_true",
                   help="show whether the guard is currently paused and which crons it paused")
    args = ap.parse_args(argv)
    guard = _guard()

    if args.resume:
        guard.resume("manual")
        return 0

    # --status
    state = guard._load_state()
    if not state.get("paused"):
        print("budget-guard: not paused — no active budget trip.")
        return 0
    names = state.get("paused_names") or state.get("paused_ids") or []
    print(f"budget-guard: ⛔ PAUSED — {len(names)} cost-bearing cron(s) held.")
    if state.get("reason"):
        print(f"  reason:    {state['reason']}")
    if state.get("tripped_at"):
        print(f"  tripped_at: {state['tripped_at']}")
    if names:
        print(f"  paused:    {', '.join(map(str, names))}")
    print("  resume with: framework budget --resume")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
