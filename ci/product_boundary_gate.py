#!/usr/bin/env python3
"""Validate the accepted OKEngine product-boundary decision."""
from __future__ import annotations

import json
from pathlib import Path

EXPECTED_KERNEL = {"schema", "policy", "transactions", "provenance", "maintenance", "projections"}
EXPECTED_CONSUMERS = {"reader", "cockpit", "applications"}


def validate(root: Path) -> list[str]:
    errors = []
    try:
        policy = json.loads((root / "config/product-boundary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"product boundary unreadable: {exc}"]
    if policy.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if policy.get("decision") != ("OKEngine is the governed compilation and maintenance plane "
                                   "for evidence-backed knowledge products."):
        errors.append("primary product decision is missing or changed")
    if set(policy.get("kernel") or []) != EXPECTED_KERNEL:
        errors.append("stable kernel classification drifted")
    if (policy.get("runtime") or {}).get("hermes") != "execution-adapter":
        errors.append("Hermes must be classified as the execution adapter")
    if set(policy.get("consumers") or []) != EXPECTED_CONSUMERS:
        errors.append("reader, cockpit, and applications must be classified as consumers")
    if not isinstance(policy.get("adapter_contract_version"), int) \
            or policy["adapter_contract_version"] < 1:
        errors.append("adapter_contract_version must be a positive integer")
    if len(set(policy.get("non_goals") or [])) < 5:
        errors.append("at least five explicit non-goals are required")
    try:
        text = (root / "docs/design/product-boundary.md").read_text(encoding="utf-8")
    except OSError as exc:
        return errors + [f"product boundary document unreadable: {exc}"]
    for heading in ("Primary product", "Runtime adapter", "Consumers", "Decision filter",
                    "Explicit non-goals", "Compatibility and change control"):
        if f"## {heading}" not in text:
            errors.append(f"product boundary document missing section: {heading}")
    return errors


def main() -> int:
    errors = validate(Path(__file__).resolve().parents[1])
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors))
        return 1
    print("product-boundary: accepted decision and adapter contract are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
