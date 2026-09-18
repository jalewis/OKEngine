"""Reviewed bridge to legacy source-tree commands during package migration.

This is intentionally the only package module allowed to locate source files.
Callers import stable package entry points; each migrated command removes one
use of this bridge without changing its external command name.
"""
from __future__ import annotations

import os
import inspect
import runpy
import sys
from pathlib import Path


def engine_root() -> Path:
    configured = os.environ.get("OKENGINE_SOURCE_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend((Path.cwd(), Path(__file__).resolve().parents[2]))
    for candidate in candidates:
        if (candidate / "engine-manifest.yaml").is_file():
            return candidate.resolve()
    raise RuntimeError(
        "OKEngine source tree not found; set OKENGINE_SOURCE_ROOT until this command "
        "has completed package migration"
    )


def run_script(relative: str, argv: list[str]) -> int:
    path = engine_root() / relative
    if not path.is_file():
        raise RuntimeError(f"legacy command is unavailable: {path}")
    previous = sys.argv
    try:
        sys.argv = [str(path), *argv]
        namespace = runpy.run_path(str(path), run_name="okengine_compat_command")
        main = namespace.get("main")
        if not callable(main):
            return 0
        return int(main(argv) if inspect.signature(main).parameters else main())
    finally:
        sys.argv = previous
