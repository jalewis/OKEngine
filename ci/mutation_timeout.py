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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.seconds <= 0 or not command:
        parser.error("a positive --seconds and command after -- are required")

    process = subprocess.Popen(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, env=checkout_environment(),
    )
    try:
        stdout, stderr = process.communicate(timeout=args.seconds)
    except subprocess.TimeoutExpired:
        print(
            f"OKENGINE_MUTATION_TIMEOUT after {args.seconds:g}s: {command[0]}",
            file=sys.stderr,
            flush=True,
        )
        try:
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        sys.stdout.write(stdout or "")
        sys.stderr.write(stderr or "")
        return 124
    sys.stdout.write(stdout or "")
    sys.stderr.write(stderr or "")
    return process.returncode


if __name__ == "__main__":
    sys.exit(main())
