#!/usr/bin/env python3
"""framework — the OKF engine CLI.

A single branded entrypoint over the engine's operator commands, so the docs say
`framework init` / `framework validate` instead of `python scripts/framework_init.py`.

Commands:
  framework init <dest> [--domain ...] [--interactive] [--port-offset N]
      Scaffold a NEW domain pack from the skeleton (delegates to framework_init).
  framework pull <source> [dest] [--into DIR] [--ref REF] [--force] [--update] [--port-offset N]
      Fetch an EXISTING pack definition from a repo or the catalog (framework_pull).
      --update refreshes a deployed pack in place, keeping .env/runtime/content and
      surfacing changed definition files as <file>.upstream for manual merge.
  framework list [--catalog URL|PATH] [--json]
      Browse the pack catalog (delegates to framework_list).
  framework validate <pack> [--probe-feeds] [--quiet]
      Pre-deploy sanity check on a pack (delegates to framework_validate).

Each subcommand's own --help lists its flags. Exit code is the subcommand's.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, _HERE / filename)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_COMMANDS = {
    "init": ("framework_init", "framework_init.py"),
    "pull": ("framework_pull", "framework_pull.py"),
    "list": ("framework_list", "framework_list.py"),
    "validate": ("framework_validate", "framework_validate.py"),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        print("\nAvailable commands:", ", ".join(_COMMANDS))
        return 0 if (argv and argv[0] in ("-h", "--help", "help")) else 2
    cmd, rest = argv[0], argv[1:]
    if cmd not in _COMMANDS:
        print(f"ERROR: unknown command '{cmd}'. Try: {', '.join(_COMMANDS)}", file=sys.stderr)
        return 2
    modname, filename = _COMMANDS[cmd]
    return _load(modname, filename).main(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
