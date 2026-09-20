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
    "required_fields", "required_relationships", "optional_relationships", "body", "unknown_fields",
    "unresolved_links", "placeholder_links", "completion",
    "required_write_path",
}
BODY_KEYS = {"required", "min_non_whitespace", "max_non_whitespace"}


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
    for key in ("required_fields", "required_relationships", "optional_relationships"):
        try:
            _strings(contract.get(key, []), f"{where}.{key}")
        except ValueError as exc:
            errors.append(str(exc))
    required = contract.get("required_relationships", [])
    optional = contract.get("optional_relationships", [])
    if isinstance(required, list) and isinstance(optional, list) and all(
        isinstance(value, str) for value in [*required, *optional]
    ):
        overlap = sorted(set(required) & set(optional))
        if overlap:
            errors.append(f"{where}.optional_relationships overlaps required_relationships: {overlap}")
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
        maximum = body.get("max_non_whitespace")
        if maximum is not None and (
            not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0
        ):
            errors.append(f"{where}.body.max_non_whitespace must be a positive integer")
        if isinstance(minimum, int) and isinstance(maximum, int) and maximum < minimum:
            errors.append(f"{where}.body.max_non_whitespace must be at least the minimum")
        if minimum and body.get("required") is False:
            errors.append(f"{where}.body cannot set a minimum while required is false")
    for key in ("unknown_fields", "unresolved_links", "placeholder_links"):
        if contract.get(key) not in POLICIES:
            errors.append(f"{where}.{key} must be one of {sorted(POLICIES)}")
    if contract.get("completion") not in COMPLETION:
        errors.append(f"{where}.completion must be one of {sorted(COMPLETION)}")
    required_path = contract.get("required_write_path")
    if required_path is not None:
        if not isinstance(required_path, str) or not required_path.strip():
            errors.append(f"{where}.required_write_path must be a non-empty string")
        elif required_path.startswith("/") or ".." in required_path.split("/"):
            errors.append(f"{where}.required_write_path must be wiki-relative")
        elif contract.get("completion") != "run":
            errors.append(f"{where}.required_write_path requires completion=run")
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
    for key in ("required_fields", "required_relationships", "optional_relationships"):
        values = list(dict.fromkeys([*floor.get(key, []), *policy.get(key, [])]))
        if key != "optional_relationships" or values:
            out[key] = values
        else:
            out.pop(key, None)
    # A pack may tighten an optional engine-floor relationship into a required
    # one. The composed contract keeps the stronger requirement, not both modes.
    optional = [field for field in out.get("optional_relationships", [])
                if field not in out["required_relationships"]]
    if optional:
        out["optional_relationships"] = optional
    else:
        out.pop("optional_relationships", None)
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
    floor_max = fb.get("max_non_whitespace")
    policy_max = pb.get("max_non_whitespace")
    if floor_max is not None and policy_max is not None and policy_max > floor_max:
        raise ValueError(f"{where}.body.max_non_whitespace policy may not weaken the engine floor")
    effective_max = policy_max if policy_max is not None else floor_max
    if effective_max is not None:
        out["body"]["max_non_whitespace"] = effective_max
    if floor["completion"] == "per-selected-item" and policy["completion"] != "per-selected-item":
        raise ValueError(f"{where}.completion policy may not weaken the engine floor")
    out["completion"] = policy["completion"]
    floor_path = floor.get("required_write_path")
    policy_path = policy.get("required_write_path")
    if floor_path and policy_path and floor_path != policy_path:
        raise ValueError(f"{where}.required_write_path policy may not replace the engine floor")
    out["required_write_path"] = policy_path or floor_path
    if out["required_write_path"] is None:
        out.pop("required_write_path")
    out["api"] = API_VERSION
    return out
