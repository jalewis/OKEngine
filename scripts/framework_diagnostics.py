#!/usr/bin/env python3
"""framework_diagnostics.py — shared plumbing for `framework status` / `framework doctor` (okengine#405).

Both live-diagnostic commands point the shared checks library (scripts/cron/deployment_checks.py — the
same 13 checks the daily in-gateway validator runs) at a HOST deployment directory and report the
findings. A host deployment dir is the pack/vault root: the vault content (schema.yaml, wiki/, config/,
.okengine/) lives at the top and the runtime data dir is `<deployment>/.hermes-data` (= /opt/data in the
gateway). There is no baked engine image on the host, so HERMES is unset and the baked-vs-staged
write-path check honestly reports "undetectable" rather than a vacuous pass.

Exit codes are stable across both commands: 0 = no FAIL (clean, or warnings only), 1 = at least one
FAIL, 2 = usage error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
import deployment_checks as C  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
_ORDER = {"FAIL": 0, "WARN": 1, "INFO": 2}


def resolve(deployment: str) -> tuple[Path | None, str]:
    """Validate + configure the checks for a host deployment dir. Returns (path, error): on success
    error is '' and the checks library is pointed at VAULT=<dep>, DATA=<dep>/.hermes-data, HERMES
    unset (no baked image on the host)."""
    dep = Path(deployment).expanduser()
    if not dep.is_dir():
        return None, f"deployment directory not found: {dep}"
    if not (dep / "schema.yaml").is_file() and not (dep / "wiki").is_dir():
        return None, (f"{dep} does not look like a deployment (no schema.yaml or wiki/); "
                      "pass the pack/vault root")
    data = dep / ".hermes-data"
    # HERMES unset on the host: keep it a non-existent path so write-path libs report 'undetectable'.
    C.configure(dep, data=data, hermes=dep / ".no-baked-image-on-host")
    return dep, ""


def run(names=None) -> list[tuple[str, str, str]]:
    findings = C.run(names)
    findings.sort(key=lambda x: (_ORDER.get(x[0], 9), x[1]))
    return findings


def counts(findings) -> tuple[int, int, int]:
    fails = sum(1 for l, _, _ in findings if l == "FAIL")
    warns = sum(1 for l, _, _ in findings if l == "WARN")
    infos = sum(1 for l, _, _ in findings if l == "INFO")
    return fails, warns, infos


def exit_code(findings) -> int:
    return EXIT_FAIL if any(l == "FAIL" for l, _, _ in findings) else EXIT_OK


def emit_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))
