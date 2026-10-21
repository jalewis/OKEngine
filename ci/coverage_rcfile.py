#!/usr/bin/env python3
"""Write pyproject.toml's coverage settings as an rcfile whose paths are absolute (okengine#1067).

``[tool.coverage.run] source`` lists directories relative to the checkout. coverage.py resolves a
relative entry against the CURRENT working directory of each measured process, and pytest-cov
hands the same configuration to every child process. Two things then go wrong:

* a test that copies the engine tree into a temporary directory and runs it in a subprocess is
  measured against the COPY, which adds every copied file as an unexecuted statement;
* an entry that names no directory in the working directory is treated as an importable package,
  so an installed distribution with the same name (``tools`` in site-packages) is measured too.

The public test job measured 191,799 statements at 33.64% this way, where the checkout holds
62,676. This helper derives the absolute rcfile from the one configured table, rather than a
second hand-kept copy, so every process measures the same absolute trees and omits the same
absolute files whatever its working directory. The ``[report]`` table is carried over as well, so
the rcfile alone reproduces the configured exclusions and floor.
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


class RcfileError(ValueError):
    """The coverage configuration cannot be made absolute."""


def _absolute(pattern: str, root: Path) -> str:
    """A path pattern anchored at ``root``; a pattern that already matches anywhere is kept."""
    if pattern.startswith(("*", "?")) or Path(pattern).is_absolute():
        return pattern
    return str(root / pattern)


def _section(name: str, table: dict) -> list[str]:
    lines = [f"[{name}]"]
    for key, value in table.items():
        if isinstance(value, list):
            lines.append(f"{key} =" + "".join(f"\n    {item}" for item in value))
        elif isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        else:
            lines.append(f"{key} = {value}")
    return lines


def absolute_rcfile(root: Path, pyproject: Path | None = None) -> str:
    """The ``[tool.coverage.run]`` and ``[tool.coverage.report]`` tables as an INI rcfile."""
    root = root.resolve()
    path = pyproject or root / "pyproject.toml"
    try:
        coverage = tomllib.loads(path.read_text(encoding="utf-8"))["tool"]["coverage"]
        run = dict(coverage["run"])
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise RcfileError(f"{path}: no readable [tool.coverage.run] table ({exc})") from exc
    if not run.get("source"):
        raise RcfileError(f"{path}: [tool.coverage.run] declares no source")
    run["source"] = [_absolute(source, root) for source in run["source"]]
    run["omit"] = [_absolute(pattern, root) for pattern in run.get("omit", [])]
    lines = _section("run", run)
    if coverage.get("report"):
        lines += [""] + _section("report", coverage["report"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        text = absolute_rcfile(args.root)
    except RcfileError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"coverage rcfile with absolute paths -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
