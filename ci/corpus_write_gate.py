#!/usr/bin/env python3
"""Reject new obvious canonical writes outside reviewed transaction boundaries."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOTS = ("scripts", "extensions", "okengine-mcp", "tools")
MUTATORS = {"write_text", "write_bytes", "replace", "rename", "unlink"}
# Legacy utilities are explicit debt: they are operator-invoked or scheduled beneath a fenced
# runner. Adding a new path requires review here; ordinary new direct wiki writes fail closed.
APPROVED = {
    "scripts/framework_extensions.py", "scripts/dedup_entity_slugs.py", "scripts/import_lib.py",
    "scripts/cron/build_hot_set.py", "scripts/cron/build_index_tree.py",
    "extensions/okengine.dedupe/same_story_dedupe.py",
}


def findings(root: Path) -> list[str]:
    out: list[str] = []
    for top in ROOTS:
        for path in (root / top).rglob("*.py"):
            relative = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in MUTATORS):
                    continue
                receiver = ast.unparse(node.func.value).lower()
                if "wiki" in receiver and relative not in APPROVED:
                    out.append(f"{relative}:{node.lineno}: direct canonical {node.func.attr}()")
    return out


def main() -> int:
    errors = findings(Path(__file__).resolve().parents[1])
    if errors:
        print("ERROR: canonical writes must use corpus_transaction or a reviewed fenced runner")
        print("\n".join(errors))
        return 1
    print("corpus-write-gate: no ungoverned direct canonical writes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
