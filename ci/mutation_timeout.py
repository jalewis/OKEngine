#!/usr/bin/env python3
"""Hard per-mutant watchdog that terminates the complete test process group."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


def checkout_environment(cwd: Path | None = None) -> dict[str, str]:
    """Prefer the checkout in which Cosmic Ray launched this watchdog."""
    root = (cwd or Path.cwd()).resolve()
    checkout_roots = [root / "src", root / "scripts" / "cron", root / "okengine-mcp", root]
    inherited = os.environ.get("PYTHONPATH")
    pythonpath = [*(str(path) for path in checkout_roots)]
    if inherited:
        pythonpath.append(inherited)
    return {**os.environ, "PYTHONPATH": os.pathsep.join(pythonpath)}


def run_guarded(command: list[str], seconds: float, env: dict[str, str] | None = None,
                ) -> tuple[int, str, str, bool]:
    """Run ``command`` in its own process group; kill the whole group after ``seconds``.

    Returns ``(returncode, stdout, stderr, timed_out)``; a timed-out run reports returncode 124.
    """
    process = subprocess.Popen(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, env=env if env is not None else checkout_environment(),
    )
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return 124, stdout or "", stderr or "", True
    return process.returncode, stdout or "", stderr or "", False


def timeout_message(seconds: float, command: list[str]) -> str:
    return f"OKENGINE_MUTATION_TIMEOUT after {seconds:g}s: {command[0]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, required=True)
    # Cosmic Ray keeps only a failed command's STDOUT. A baseline killed by this watchdog therefore
    # surfaced as a truncated line of progress dots, which read like a test failure (okengine#895).
    # Baseline configurations pass this flag so the timeout is named where the operator reads it.
    # Per-mutant runs keep the sentinel on stderr: changing how a mutant timeout is SCORED is a
    # separate decision from making a baseline timeout legible.
    parser.add_argument("--timeout-on-stdout", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.seconds <= 0 or not command:
        parser.error("a positive --seconds and command after -- are required")

    returncode, stdout, stderr, timed_out = run_guarded(command, args.seconds)
    if timed_out:
        print(timeout_message(args.seconds, command), file=sys.stderr, flush=True)
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    if timed_out and args.timeout_on_stdout:
        print(timeout_message(args.seconds, command), flush=True)
    return returncode

if __name__ == "__main__":
    sys.exit(main())
