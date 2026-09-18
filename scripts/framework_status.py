#!/usr/bin/env python3
"""framework status DEPLOYMENT [--json] — a compact LIVE summary of a deployment (okengine#405).

A one-glance health line for a host deployment directory: engine/Hermes version (the runtime stamp),
scheduler state (jobs.json + enabled lane count), and a per-area roll-up from a curated, fast subset
of the shared deployment checks (scripts/cron/deployment_checks.py). For the full finding list use
`framework doctor`; for the OFFLINE pack-source conformance check use `framework validate`.

Exit: 0 = no FAIL, 1 = at least one FAIL, 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import framework_diagnostics as D  # noqa: E402

# A bounded, high-signal subset (skips the heavier partition-dups rglob / baked-vs-staged diff, which
# `doctor` covers) so `status` stays a fast glance.
_STATUS_CHECKS = ["pins", "schema", "crons", "runtime-ownership", "extensions", "operations"]


def _stamp(data: Path) -> dict:
    rt = data / "engine-runtime.yaml"
    out = {"engine_release": None, "hermes_pin": None}
    if rt.is_file():
        for line in rt.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                if k.strip() in out:
                    out[k.strip()] = v.strip() or None
    return out


def _scheduler(data: Path) -> dict:
    jf = data / "cron-plus" / "jobs.json"
    if not jf.is_file():
        return {"jobs_store": False, "enabled_lanes": None}
    try:
        jobs = json.loads(jf.read_text(encoding="utf-8")).get("jobs", [])
        enabled = sum(1 for j in jobs if j.get("enabled", True))
    except (OSError, ValueError):
        return {"jobs_store": True, "enabled_lanes": None, "unparseable": True}
    stalled = (data / "cron-plus" / ".scheduler-stalled").is_file()
    return {"jobs_store": True, "enabled_lanes": enabled, "total_lanes": len(jobs), "stalled": stalled}


def _rollup(findings) -> dict[str, str]:
    """Worst level seen per area (FAIL > WARN > INFO)."""
    rank = {"INFO": 0, "WARN": 1, "FAIL": 2}
    worst: dict[str, str] = {}
    for level, area, _ in findings:
        if rank.get(level, 0) >= rank.get(worst.get(area, "INFO"), 0):
            worst[area] = level
    return worst


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="framework status",
                                 description="Compact live summary of a deployment.")
    ap.add_argument("deployment", help="path to the deployment (pack/vault) directory")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    dep, err = D.resolve(args.deployment)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return D.EXIT_USAGE

    data = dep / ".hermes-data"
    stamp = _stamp(data)
    sched = _scheduler(data)
    findings = D.run(_STATUS_CHECKS)
    fails, warns, infos = D.counts(findings)
    rollup = _rollup(findings)
    code = D.exit_code(findings)

    if args.json:
        D.emit_json({
            "deployment": str(dep), "version": stamp, "scheduler": sched,
            "areas": rollup, "summary": {"fail": fails, "warn": warns, "info": infos,
                                         "status": "FAIL" if fails else "OK"},
        })
        return code

    print(f"framework status — {dep}")
    print(f"  engine: {stamp['engine_release'] or '?'}   hermes: {stamp['hermes_pin'] or '?'}")
    if not sched["jobs_store"]:
        print("  scheduler: NO jobs.json (dead scheduler?)")
    elif sched.get("stalled"):
        print(f"  scheduler: STALLED sentinel present ({sched.get('enabled_lanes','?')} enabled lanes)")
    else:
        print(f"  scheduler: {sched.get('enabled_lanes','?')}/{sched.get('total_lanes','?')} lanes enabled")
    if rollup:
        print("  areas:")
        for area in sorted(rollup, key=lambda a: ({"FAIL": 0, "WARN": 1, "INFO": 2}[rollup[a]], a)):
            print(f"    {rollup[area]:<4} {area}")
    print(f"\n{'FAIL' if fails else 'OK'} — {fails} fail · {warns} warn · {infos} info "
          "(run `framework doctor` for detail)")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
