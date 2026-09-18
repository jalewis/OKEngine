"""Installed OKEngine command entry points."""
from __future__ import annotations

import argparse
import sys

from okengine.compat import run_script


def framework() -> int:
    return run_script("scripts/framework.py", sys.argv[1:])


def cron() -> int:
    parser = argparse.ArgumentParser(prog="okengine-cron")
    parser.add_argument("command", help="cron command filename without .py")
    args, remainder = parser.parse_known_args()
    if not args.command.replace("-", "_").isidentifier():
        parser.error("command must be a Python identifier")
    command = args.command.replace("-", "_")
    return run_script(f"scripts/cron/{command}.py", remainder)
