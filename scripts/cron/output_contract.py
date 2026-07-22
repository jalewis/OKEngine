"""Versioned output contracts for model-authored cron lanes.

The contract is deliberately domain-neutral: packs name their own namespaces,
types, fields and relationships; the engine validates and composes the shape.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json


API_VERSION = 1
POLICIES = {"allow", "review", "reject"}
OPERATIONS = {"create", "update", "patch", "append", "converge", "tombstone", "flag"}
COMPLETION = {"run", "per-selected-item"}
KEYS = {
    "api", "allowed_namespaces", "allowed_types", "operations",
    "required_fields", "required_relationships", "body", "unknown_fields",
    "unresolved_links", "placeholder_links", "completion",
}
BODY_KEYS = {"required", "min_non_whitespace"}


def digest(contract: dict) -> str:
    """Stable content identity used by enforcement and run receipts."""
    canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _strings(value, where: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{where} must be a list of non-empty strings")
    if nonempty and not value:
        raise ValueError(f"{where} must not be empty")
    if len(value) != len(set(value)):
        raise ValueError(f"{where} contains duplicates")
    return value


def validate(contract: object, where: str = "output_contract") -> list[str]:
    """Return actionable validation errors; never raise for author input."""
    errors: list[str] = []
    if not isinstance(contract, dict):
        return [f"{where} must be an object"]
    unknown = sorted(set(contract) - KEYS)
    if unknown:
        errors.append(f"{where} has unknown key(s): {unknown}")
    if contract.get("api") != API_VERSION:
        errors.append(f"{where}.api must be {API_VERSION}")
    for key in ("allowed_namespaces", "allowed_types"):
        try:
            _strings(contract.get(key), f"{where}.{key}", nonempty=True)
        except ValueError as exc:
            errors.append(str(exc))
    for key in ("required_fields", "required_relationships"):
        try:
            _strings(contract.get(key, []), f"{where}.{key}")
        except ValueError as exc:
            errors.append(str(exc))
    try:
        operations = set(_strings(contract.get("operations"), f"{where}.operations", nonempty=True))
        bad = sorted(operations - OPERATIONS)
        if bad:
            errors.append(f"{where}.operations has unsupported value(s): {bad}")
    except ValueError as exc:
        errors.append(str(exc))
    body = contract.get("body", {})
    if not isinstance(body, dict):
        errors.append(f"{where}.body must be an object")
    else:
        extra = sorted(set(body) - BODY_KEYS)
        if extra:
            errors.append(f"{where}.body has unknown key(s): {extra}")
        if "required" in body and not isinstance(body["required"], bool):
            errors.append(f"{where}.body.required must be boolean")
        minimum = body.get("min_non_whitespace", 0)
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
            errors.append(f"{where}.body.min_non_whitespace must be a non-negative integer")
        if minimum and body.get("required") is False:
            errors.append(f"{where}.body cannot set a minimum while required is false")
    for key in ("unknown_fields", "unresolved_links", "placeholder_links"):
        if contract.get(key) not in POLICIES:
            errors.append(f"{where}.{key} must be one of {sorted(POLICIES)}")
    if contract.get("completion") not in COMPLETION:
        errors.append(f"{where}.completion must be one of {sorted(COMPLETION)}")
    return errors


def compose(floor: dict | None, policy: dict | None, where: str = "output_contract") -> dict | None:
    """Compose a pack policy over an engine floor without permitting weakening.

    Set-valued permissions intersect; requirements union; enforcement policies
    may only move allow -> review -> reject; body minima may only increase.
    """
    if floor is None:
        if policy is None:
            return None
        errors = validate(policy, where)
        if errors:
            raise ValueError("; ".join(errors))
        return deepcopy(policy)
    if policy is None:
        errors = validate(floor, where)
        if errors:
            raise ValueError("; ".join(errors))
        return deepcopy(floor)
    errors = validate(floor, f"{where} floor") + validate(policy, f"{where} policy")
    if errors:
        raise ValueError("; ".join(errors))
    out = deepcopy(floor)
    for key in ("allowed_namespaces", "allowed_types", "operations"):
        if key != "operations" and "*" in floor[key]:
            narrowed = list(policy[key])
        elif key != "operations" and "*" in policy[key]:
            narrowed = list(floor[key])
        else:
            narrowed = [x for x in floor[key] if x in set(policy[key])]
        if not narrowed:
            raise ValueError(f"{where}.{key} composition has no allowed values")
        if (key == "operations" or "*" not in floor[key]) and set(policy[key]) - set(floor[key]):
            raise ValueError(f"{where}.{key} policy may not widen the engine floor")
        out[key] = narrowed
    for key in ("required_fields", "required_relationships"):
        out[key] = list(dict.fromkeys([*floor.get(key, []), *policy.get(key, [])]))
    rank = {"allow": 0, "review": 1, "reject": 2}
    for key in ("unknown_fields", "unresolved_links", "placeholder_links"):
        if rank[policy[key]] < rank[floor[key]]:
            raise ValueError(f"{where}.{key} policy may not weaken the engine floor")
        out[key] = policy[key]
    fb, pb = floor.get("body", {}), policy.get("body", {})
    if fb.get("required") and not pb.get("required"):
        raise ValueError(f"{where}.body.required policy may not weaken the engine floor")
    if pb.get("min_non_whitespace", 0) < fb.get("min_non_whitespace", 0):
        raise ValueError(f"{where}.body.min_non_whitespace policy may not weaken the engine floor")
    out["body"] = {
        "required": bool(fb.get("required") or pb.get("required")),
        "min_non_whitespace": max(fb.get("min_non_whitespace", 0),
                                    pb.get("min_non_whitespace", 0)),
    }
    if floor["completion"] == "per-selected-item" and policy["completion"] != "per-selected-item":
        raise ValueError(f"{where}.completion policy may not weaken the engine floor")
    out["completion"] = policy["completion"]
    out["api"] = API_VERSION
    return out
