#!/usr/bin/env python3
"""Static lint for source patterns cosmic-ray cannot mutate (okengine#1046).

cosmic-ray's ExceptionReplacer replaces each element of an ``except (...)`` tuple by assigning the
element's ``.value``. That works for a bare name and raises AttributeError for anything else -- a
dotted ``yaml.YAMLError``, a call, a subscript, a starred name -- so the worker dies and the gate
reports an incompetent-worker error. ``mutation-changed`` is a campaign, so the defect was first
seen on main (pipeline 13045, after !1329). This check is static and takes well under a second, so
it runs on every merge request through the full suite (tests/ci/test_mutation_lint.py).

A single dotted handler (``except yaml.YAMLError:``) is not flagged: the operator mutates it
without raising. tests/ci/test_mutation_lint.py pins every verdict against the real operator.

Scope is the mutation manifest: a module nobody mutates cannot produce the error. Registering a
module as a target brings it under the lint in the same change.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    """``line`` is 1-based and ``column`` 0-based: the same position cosmic-ray prints in the
    mutant key (``assessments.py:275:12:core/ExceptionReplacer:6`` in pipeline 13045)."""

    line: int
    column: int
    element: str
    hint: str


def _hint(element: ast.expr) -> str:
    if isinstance(element, ast.Attribute):
        return f"from {ast.unparse(element.value)} import {element.attr}"
    return "bind the exception class to a bare name before the try"


def incompetent_except_elements(source: str, filename: str = "<source>") -> list[Finding]:
    """Every ``except (...)`` tuple element that is not a bare name, in source order."""
    findings = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Tuple):
            findings.extend(
                Finding(element.lineno, element.col_offset, ast.unparse(element),
                        _hint(element))
                for element in node.type.elts if not isinstance(element, ast.Name))
    return sorted(findings, key=lambda finding: (finding.line, finding.column))


PUBLISH_SCRIPT = "scripts/publish-snapshot.sh"
_EXCLUDE = re.compile(r"^EXCLUDE=\($(.*?)^\)", re.S | re.M)
_ENTRY = re.compile(r'^\s*"([^"]+)"', re.M)


def publish_excluded(repo: Path) -> list[str] | None:
    """The public snapshot's EXCLUDE entries, or None where the snapshot script is absent.

    The script excludes itself, so None means this IS a public snapshot tree.
    """
    script = repo / PUBLISH_SCRIPT
    if not script.is_file():
        return None
    match = _EXCLUDE.search(script.read_text(encoding="utf-8"))
    return _ENTRY.findall(match.group(1)) if match else []


def scan_report(repo: Path, manifest: Path) -> tuple[list[str], list[str]]:
    """Findings across every manifest target, and the targets this tree does not carry.

    The public snapshot drops its EXCLUDE entries, some of which are mutation targets
    (ci/registry_canary.py; main pipeline 13068). There, an absent target is reported and skipped.
    In the source tree, which carries the snapshot script, every target must exist: an absent one
    raises, publish-excluded or not. tests/ci/test_mutation_lint.py proves on the source tree that
    the public tree lacks exactly the manifest's publish-excluded targets.
    """
    targets = sorted({entry["path"] for entry in json.loads(
        manifest.read_text(encoding="utf-8"))["targets"]})
    public_tree = publish_excluded(repo) is None
    messages, absent = [], []
    for path in targets:
        if public_tree and not (repo / path).is_file():
            absent.append(path)
            continue
        source = (repo / path).read_text(encoding="utf-8")
        messages.extend(
            f"{path}:{finding.line}:{finding.column}: `{finding.element}` in an except tuple: "
            f"cosmic-ray's ExceptionReplacer cannot mutate it (incompetent worker). "
            f"Use a bare name: {finding.hint}"
            for finding in incompetent_except_elements(source, path))
    return messages, absent


def scan(repo: Path, manifest: Path) -> list[str]:
    """One message per finding across every manifest target, ``path:line:col: ...``."""
    return scan_report(repo, manifest)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=Path("mutation/targets.json"))
    args = parser.parse_args(argv)
    manifest = args.manifest if args.manifest.is_absolute() else args.repo / args.manifest
    messages, absent = scan_report(args.repo, manifest)
    for message in messages:
        print(message)
    print(f"mutation lint: {len(messages)} incompetent except-tuple element(s)")
    if absent:
        print(f"mutation lint: {len(absent)} target(s) absent from this public snapshot tree: "
              + ", ".join(absent))
    return 1 if messages else 0


if __name__ == "__main__":
    sys.exit(main())
