#!/usr/bin/env python3
"""Ratcheted entrypoint size and dependency-direction gate."""
from __future__ import annotations

import json
from pathlib import Path


def validate(root: Path) -> list[str]:
    policy = json.loads((root / "config/architecture-boundaries.json").read_text())
    errors = []
    budgets = {**policy["entrypoint_budgets"], **policy.get("module_budgets", {})}
    for relative, budget in budgets.items():
        path = root / relative
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > int(budget["maximum_lines"]):
            errors.append(f"{relative}: {lines} lines exceeds ratchet {budget['maximum_lines']}")
        if int(budget["target_lines"]) > int(budget["maximum_lines"]):
            errors.append(f"{relative}: target must not exceed the current ratchet")
    graph = {key: set(value) for key, value in policy["allowed_dependencies"].items()}
    layers = set(policy["layers"])
    if set(graph) != layers:
        errors.append("dependency graph must define every declared layer exactly once")

    def reaches(start: str, target: str, seen: set[str]) -> bool:
        if start == target:
            return True
        if start in seen:
            return False
        return any(reaches(child, target, seen | {start}) for child in graph.get(start, set()))

    for layer in layers:
        if any(reaches(child, layer, {layer}) for child in graph.get(layer, set())):
            errors.append(f"dependency cycle reaches {layer}")
    return errors


def main() -> int:
    errors = validate(Path(__file__).resolve().parents[1])
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors))
        return 1
    print("architecture-boundaries: size ratchets and dependency DAG valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
