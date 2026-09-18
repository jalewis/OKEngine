#!/usr/bin/env python3
"""framework doctor DEPLOYMENT [--checks LIST] [--json] — bounded LIVE diagnostics (okengine#405).

Runs the shared deployment checks (scripts/cron/deployment_checks.py — the same checks the daily
in-gateway validator runs) against a host deployment directory and reports every finding. This is
the live counterpart to `framework validate`, which is the OFFLINE conformance check on pack sources;
doctor inspects the deployed/staged state a running (or host-mounted) deployment actually carries:
pins, composed schema, sub-domains, cron fleet, timezone, partition dups, rules, extensions, storage
ownership, auth posture, the enforced write-path libs, provenance, and operation runs.

  --checks LIST   comma-separated subset (e.g. --checks pins,crons,write-path); default: all checks
  --json          machine-readable findings + counts

Exit: 0 = no FAIL, 1 = at least one FAIL, 2 = usage error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import framework_diagnostics as D  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="framework doctor", add_help=True,
                                 description="Bounded live diagnostics over a deployment.")
    ap.add_argument("deployment", help="path to the deployment (pack/vault) directory")
    ap.add_argument("--checks", default="", help="comma-separated check subset (default: all)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--list-checks", action="store_true", help="list available check names and exit")
    args = ap.parse_args(argv)

    if args.list_checks:
        print("\n".join(D.C.CHECKS))
        return D.EXIT_OK

    names = None
    if args.checks.strip():
        names = [n.strip() for n in args.checks.split(",") if n.strip()]
        unknown = [n for n in names if n not in D.C.CHECKS]
        if unknown:
            print(f"ERROR: unknown check(s) {unknown}; known: {', '.join(D.C.CHECKS)}", file=sys.stderr)
            return D.EXIT_USAGE
    else:
        names = list(D.C.CHECKS)   # doctor default = the FULL registry (incl. operations)

    dep, err = D.resolve(args.deployment)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return D.EXIT_USAGE

    findings = D.run(names)
    fails, warns, infos = D.counts(findings)
    code = D.exit_code(findings)

    if args.json:
        D.emit_json({
            "deployment": str(dep), "checks": names,
            "summary": {"fail": fails, "warn": warns, "info": infos,
                        "status": "FAIL" if fails else "OK"},
            "findings": [{"level": l, "area": a, "message": m} for l, a, m in findings],
        })
        return code

    print(f"framework doctor — {dep}")
    print(f"  checks: {', '.join(names)}")
    if not findings:
        print("  (no findings)")
    for l, a, m in findings:
        print(f"  {l:<4} [{a}] {m}")
    print(f"\n{'FAIL' if fails else 'OK'} — {fails} fail · {warns} warn · {infos} info")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
