#!/usr/bin/env python3
"""Scheduled adapter for the canonical policy catalog (no agent, deterministic)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

for candidate in (Path(__file__).resolve().parents[2] / "tools", Path("/opt/hermes/tools")):
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))

import policy_plane  # noqa: E402


def main() -> int:
    vault = Path(os.environ.get("WIKI_PATH") or "/opt/vault")
    try:
        result = policy_plane.materialize(vault, run_audit=True)
    except policy_plane.PolicyError as exc:
        print(json.dumps({"wakeAgent": False, "error": str(exc), "status": "failed"}))
        return 1
    print(json.dumps({"wakeAgent": False, "status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
