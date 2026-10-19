#!/usr/bin/env python3
"""Every import from the gateway's BAKED engine that the scripts about to be staged make.

`deploy-cron-scripts.sh` stages scripts into /opt/data/scripts. They import two things that are
not staged with them but baked into the gateway image: the `okengine` wheel and /opt/hermes/tools.
A stage onto an image older than the scripts therefore raises ImportError in every cron that
imports a missing name (okengine#997, #1017; v0.15.11 shipped `okengine.actor_types` without
extending the hand-listed probe). This scan derives the probe from the scripts themselves, so no
new baked module can be forgotten (#1016).

An import inside a `try` whose handler catches ImportError (or a base of it) is optional: the
script already copes without it, so it is left out of the probe.

Usage: baked_imports.py PATH...  (files or directories; directories are scanned recursively for
*.py). Prints one import statement per line, sorted and de-duplicated.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

#: Top-level packages that live in the gateway image, never in the staged scripts.
BAKED_PACKAGES = ("okengine", "tools")
_TOLERANT = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _tolerated(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    if caught is None:
        return True
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(isinstance(name, ast.Name) and name.id in _TOLERANT for name in names)


def _optional_nodes(tree: ast.AST) -> set[int]:
    """ids of every node inside the body of a `try` that tolerates an import failure."""
    optional: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_tolerated(h) for h in node.handlers):
            for statement in node.body:
                optional.update(id(child) for child in ast.walk(statement))
    return optional


def _baked(module: str | None) -> bool:
    return bool(module) and module.split(".")[0] in BAKED_PACKAGES


def statements(source: str) -> set[str]:
    """The required baked imports one source file makes, as import statements."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    optional = _optional_nodes(tree)
    out: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in optional:
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 0 and _baked(node.module):
            out.update(f"from {node.module} import {alias.name}" for alias in node.names
                       if alias.name != "*")
        elif isinstance(node, ast.Import):
            out.update(f"import {alias.name}" for alias in node.names if _baked(alias.name))
    return out


def scan(paths) -> list[str]:
    found: set[str] = set()
    for raw in paths:
        path = Path(raw)
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]  # glob-ok: source tree
        for file in files:
            if file.is_file() and "__pycache__" not in file.parts:
                found |= statements(file.read_text(encoding="utf-8", errors="replace"))
    return sorted(found)


if __name__ == "__main__":
    print("\n".join(scan(sys.argv[1:])))
