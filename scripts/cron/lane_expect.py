"""Lane outcome expectations — what "performing as desired" means for a lane (okengine#889/#890).

An `output_contract` says what a lane MAY write. `expect:` says what it SHOULD produce, so a
lane that runs on schedule and produces nothing can be told apart from one that works. The
dashboard (#892) evaluates it against artifacts; this module is the one definition of the
shape, shared by `framework validate`, extension composition, the engine's CI and the
dashboard, so none of them can disagree about what a valid expectation is.

    expect:
      produces:  {namespace: trends, at_least: 1, per: 4w}   # or {type: trend, ...}
      drains:    {}                                          # its backlog shrinks or is empty
      maintains: {artifact: wiki/dashboards/fleet-health.md, max_age: 3x}   # vault-relative
      may_idle:  {max_idle_runs: 8}                          # a gate may skip — not forever

Shapes combine (e.g. produces + may_idle). Durations: `<n>h|d|w`, or `<n>x` = n × cadence.
Docs: docs/lane-expectations.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DOC = "docs/lane-expectations.md"
SHAPES: dict[str, dict[str, tuple[str, bool]]] = {
    # shape -> {key: (kind, required)}
    "produces": {"namespace": ("name", False), "type": ("name", False),
                 "at_least": ("count", True), "per": ("window", True)},
    "drains": {"max_stalled_runs": ("count", False)},
    "maintains": {"artifact": ("path", True), "max_age": ("window", True)},
    "may_idle": {"max_idle_runs": ("count", True)},
}
_WINDOW = re.compile(r"^[1-9][0-9]*[hdwx]\Z")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*\Z")


def _key_error(where: str, key: str, kind: str, value: Any) -> str | None:
    if kind == "count":
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 1
        return None if ok else f"{where}.{key} must be an integer >= 1 (got {value!r})"
    if kind == "window":
        ok = isinstance(value, str) and bool(_WINDOW.match(value))
        return None if ok else (f"{where}.{key} must be a window like 7d, 4w, 12h or 3x "
                                f"(3x = three schedule cadences) (got {value!r})")
    if kind == "name":
        ok = isinstance(value, str) and bool(_NAME.match(value))
        return None if ok else f"{where}.{key} must be a namespace/type name (got {value!r})"
    ok = isinstance(value, str) and bool(value.strip()) and not value.startswith("/")
    return None if ok else (f"{where}.{key} must be a vault-relative path — a file, or a "
                            f"directory judged by its newest file (got {value!r})")


def validate(expect: Any, where: str, contract: dict | None = None) -> list[str]:
    """Structural errors in one lane's `expect:` (empty list = valid).

    `contract` is the lane's output_contract, when it has one: a `produces` namespace must be
    one the lane may write, or the expectation can never be met (the #1153 selector/contract
    disagreement, one level up)."""
    if not isinstance(expect, dict) or not expect:
        return [f"{where} must be a non-empty mapping of shape -> settings ({DOC})"]
    errors: list[str] = []
    for shape in sorted(set(expect) - set(SHAPES)):
        errors.append(f"{where}: unknown shape {shape!r} — use one of {sorted(SHAPES)} ({DOC})")
    for shape, keys in SHAPES.items():
        if shape not in expect:
            continue
        spec = expect[shape]
        at = f"{where}.{shape}"
        if not isinstance(spec, dict):
            errors.append(f"{at} must be a mapping (use {{}} for no settings)")
            continue
        errors.extend(f"{at}: unknown key {k!r} — allowed {sorted(keys)}"
                      for k in sorted(set(spec) - set(keys)))
        for key, (kind, required) in keys.items():
            if key not in spec:
                if required:
                    errors.append(f"{at}.{key} is required")
                continue
            err = _key_error(at, key, kind, spec[key])
            if err:
                errors.append(err)
        if shape == "produces":
            errors.extend(_produces_errors(spec, at, contract))
    return errors


def _produces_errors(spec: dict, at: str, contract: dict | None) -> list[str]:
    if "namespace" not in spec and "type" not in spec:
        return [f"{at} needs a namespace or a type to count"]
    allowed = (contract or {}).get("allowed_namespaces") if isinstance(contract, dict) else None
    ns = spec.get("namespace")
    if isinstance(allowed, list) and "*" not in allowed and isinstance(ns, str) \
            and ns not in allowed:
        return [f"{at}.namespace {ns!r} is outside the lane's output_contract {allowed} — "
                "the expectation could never be met"]
    return []


def example(lane: dict) -> str:
    """A copyable `expect:` for this lane's kind — the fix, printed where the failure is."""
    contract = lane.get("output_contract") if isinstance(lane, dict) else None
    namespaces = [n for n in (contract or {}).get("allowed_namespaces") or [] if n != "*"]
    if not lane.get("no_agent") and namespaces:
        body = {"produces": {"namespace": namespaces[0], "at_least": 1, "per": "4x"},
                "may_idle": {"max_idle_runs": 8}}
    else:
        body = {"maintains": {"artifact": "wiki/dashboards/<the-page-this-lane-writes>.md",
                              "max_age": "3x"}}
    return '"expect": ' + json.dumps(body)


def missing_message(name: str, source: str, lane: dict) -> str:
    return (f"lane {name!r} ({source}) declares no `expect:` — nothing can tell whether it is "
            f"producing, so it can stop working unnoticed. Add one (see {DOC}), e.g. "
            f"{example(lane)}")


def load_allowlist(path: Path) -> tuple[dict[str, str], int | None, list[str]]:
    """({lane: reason}, declared max_entries, errors) from config/expect-allowlist.yaml."""
    import yaml
    from yaml import YAMLError
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, YAMLError) as exc:
        return {}, None, [f"{path.name}: unreadable ({exc})"]
    lanes = doc.get("lanes") if isinstance(doc, dict) else None
    if not isinstance(lanes, dict):
        return {}, None, [f"{path.name}: `lanes` must be a mapping of lane -> reason"]
    errors = [f"{path.name}: {name!r} needs a non-empty reason"
              for name, reason in lanes.items() if not isinstance(reason, str) or not reason.strip()]
    cap = doc.get("max_entries")
    if not isinstance(cap, int) or isinstance(cap, bool):
        errors.append(f"{path.name}: `max_entries` must be an integer")
        cap = None
    return {str(k): str(v) for k, v in lanes.items()}, cap, errors


def lane_findings(name: str, source: str, lane: dict,
                  allowlist: dict[str, str]) -> tuple[list[str], bool]:
    """(errors, undeclared_but_allowlisted) for one enabled lane."""
    if not isinstance(lane, dict) or lane.get("enabled") is False:
        return [], False
    expect = lane.get("expect")
    if expect is None:
        return ([], True) if name in allowlist else ([missing_message(name, source, lane)], False)
    return validate(expect, f"lane {name!r} ({source}) expect", lane.get("output_contract")), False
