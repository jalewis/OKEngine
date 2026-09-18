#!/usr/bin/env python3
"""Validate the bounded, owner/expiry-based test quarantine registry."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Optional

import yaml


MAX_DAYS = 14
API_VERSION = int("1")
ZERO = len(())
REQUIRED = {"nodeid", "owner", "issue", "reason", "first_seen", "expires_on", "evidence"}


def validate(value: object, today: Optional[date] = None) -> tuple[dict, list[str]]:
    now = today or date.today()
    errors: list[str] = []
    if not isinstance(value, dict):
        return {"api": API_VERSION, "active": ZERO, "quarantines": []}, [
            "registry must be an object"
        ]
    api = value.get("api")
    if isinstance(api, bool) or not isinstance(api, int) or api not in (API_VERSION,):
        errors.append("api must be 1")
    unknown = sorted(set(value) - {"api", "quarantines"})
    if unknown:
        errors.append(f"registry has unknown key(s): {unknown}")
    entries = value.get("quarantines")
    if not isinstance(entries, list):
        return {"api": API_VERSION, "active": ZERO, "quarantines": []}, errors + [
            "quarantines must be a list"]

    seen: set[str] = set()
    normalized: list[dict] = []
    for index, entry in enumerate(entries):
        prefix = f"quarantines[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be an object")
            continue
        missing = sorted(REQUIRED - set(entry))
        extra = sorted(set(entry) - REQUIRED)
        if missing:
            errors.append(f"{prefix} missing key(s): {missing}")
        if extra:
            errors.append(f"{prefix} has unknown key(s): {extra}")
        nodeid = str(entry.get("nodeid") or "")
        if not nodeid.startswith("tests/") or "::" not in nodeid:
            errors.append(f"{prefix}.nodeid must be an exact tests/...::test node id")
        elif nodeid in seen:
            errors.append(f"{prefix}.nodeid is duplicated: {nodeid}")
        seen.add(nodeid)
        for key in ("owner", "reason"):
            if not str(entry.get(key) or "").strip():
                errors.append(f"{prefix}.{key} is required")
        for key in ("issue",):
            if not str(entry.get(key) or "").startswith(("http://", "https://")):
                errors.append(f"{prefix}.{key} must be a durable URL")
        evidence = entry.get("evidence")
        if (not isinstance(evidence, list) or not evidence
                or any(not str(item).startswith(("http://", "https://", "artifacts/"))
                       for item in evidence)):
            errors.append(f"{prefix}.evidence must be a non-empty durable URL/artifact list")
        try:
            first = date.fromisoformat(str(entry.get("first_seen") or ""))
            expiry = date.fromisoformat(str(entry.get("expires_on") or ""))
            if expiry < now:
                errors.append(f"{prefix} expired on {expiry.isoformat()}")
            if expiry < first or (expiry - first).days > MAX_DAYS:
                errors.append(f"{prefix} expiry must be within {MAX_DAYS} days of first_seen")
        except ValueError:
            errors.append(f"{prefix}.first_seen/expires_on must be ISO dates")
        normalized.append(dict(entry))
    return {"api": API_VERSION, "checked_on": now.isoformat(), "active": len(normalized),
            "quarantines": normalized}, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="config/test-quarantine.yaml")
    parser.add_argument("--output", default="artifacts/flake-policy.json")
    args = parser.parse_args()
    try:
        registry_text = Path(args.registry).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"flake policy: registry unreadable: {exc}")
        return 2
    try:
        value = yaml.safe_load(registry_text)
    except yaml.YAMLError as exc:
        print(f"flake policy: registry unreadable: {exc}")
        return 2
    report, errors = validate(value)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({**report, "errors": errors}, indent=2) + "\n")
    if errors:
        print("flake policy: FAIL\n  " + "\n  ".join(errors))
        return 1
    print(f"flake policy: PASS ({report['active']} active quarantine(s))")
    return 0


if __name__ in {"__main__"}:
    raise SystemExit(main())
