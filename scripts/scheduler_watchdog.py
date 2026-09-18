#!/usr/bin/env python3
"""Scheduler-independent fleet watchdog with durable JSON evidence."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_RUN_RECORD_READ_ERRORS = (OSError, json.JSONDecodeError)


def _age_seconds(value: object, now: float) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, now - parsed.timestamp())


def inspect(
    deployment: Path, now: float, max_tick_age: int, *, max_running_age: int = 3600,
    max_corpus_lock_age: int = 300,
) -> dict:
    tick = deployment / ".hermes-data/cron-plus/.tick.lock"
    stalled = deployment / ".hermes-data/cron-plus/.scheduler-stalled"
    reasons = []
    try:
        age = max(0.0, now - tick.stat().st_mtime)
    except OSError:
        age = None
        reasons.append("scheduler tick evidence is absent")
    if age is not None and age > max_tick_age:
        reasons.append(f"scheduler tick is stale ({int(age)}s > {max_tick_age}s)")
    try:
        stall_text = stalled.read_text(encoding="utf-8").strip()
    except OSError:
        stall_text = ""
    if stall_text:
        reasons.append("scheduler reports a stalled job store")
    interrupted = []
    runs = deployment / ".hermes-data/cron-plus/runs"
    for path in runs.glob("*/*.json"):  # glob-ok: flat per-lane run registry
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except _RUN_RECORD_READ_ERRORS:
            continue
        if isinstance(record, dict) and record.get("status") == "running":
            run_age = _age_seconds(record.get("started_at"), now)
            stale = run_age is None or run_age > max_running_age
            interrupted.append({
                "lane": record.get("lane") or record.get("job_id"),
                "kind": ("agent" if record.get("completion") == "per-selected-item"
                         else "deterministic"),
                "started_at": record.get("started_at"),
                "age_seconds": run_age,
                "stale": stale,
                "run_record": str(path.relative_to(deployment)),
            })
            if stale:
                lane = record.get("lane") or record.get("job_id") or "unknown lane"
                age_text = "unknown" if run_age is None else f"{int(run_age)}s"
                reasons.append(
                    f"{lane} has a stale running record ({age_text} > {max_running_age}s)"
                )

    owner_path = deployment / ".okengine/corpus/lock-owner.json"
    corpus_lock_owner = None
    if owner_path.exists():
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
        except _RUN_RECORD_READ_ERRORS:
            reasons.append("corpus lock owner evidence is unreadable")
        else:
            if not isinstance(owner, dict):
                reasons.append("corpus lock owner evidence is not an object")
            else:
                owner_age = _age_seconds(owner.get("acquired_at"), now)
                corpus_lock_owner = {key: owner.get(key) for key in (
                    "pid", "hostname", "command", "writer", "operation", "mode", "acquired_at",
                )}
                corpus_lock_owner["age_seconds"] = owner_age
                corpus_lock_owner["stale"] = owner_age is None or owner_age > max_corpus_lock_age
                if corpus_lock_owner["stale"]:
                    age_text = "unknown" if owner_age is None else f"{int(owner_age)}s"
                    reasons.append(
                        f"corpus lock owner is stale ({age_text} > {max_corpus_lock_age}s)"
                    )
    return {
        "deployment": str(deployment.resolve()), "healthy": not reasons,
        "tick_age_seconds": age, "stalled": bool(stall_text), "reasons": reasons,
        "running_records": interrupted,
        "corpus_lock_owner": corpus_lock_owner,
    }


def restart(deployment: Path) -> dict:
    started = time.monotonic()
    result = subprocess.run(
        ["docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "gateway"],
        cwd=deployment, capture_output=True, text=True, timeout=180)
    return {"attempted": True, "returncode": result.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "stderr": result.stderr[-1000:]}


def reconcile_stale_runs(deployment: Path, check: dict, now: float) -> dict:
    """Terminalize stale receipts only after replacement proves their old runner is gone."""
    reconciled = 0
    errors = []
    for item in check["running_records"]:
        if not item["stale"]:
            continue
        path = deployment / item["run_record"]
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or record.get("status") != "running":
                continue
            record.update({
                "status": "indeterminate",
                "ended_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "error": "watchdog replaced the unhealthy gateway after this run became stale",
            })
            _write(path, record)
            reconciled += 1
        except _RUN_RECORD_READ_ERRORS as exc:
            errors.append(f"{item['run_record']}: {type(exc).__name__}")
    return {"reconciled": reconciled, "errors": errors}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deployment", nargs="+")
    parser.add_argument("--max-tick-age", type=int, default=180)
    parser.add_argument("--max-running-age", type=int, default=3600)
    parser.add_argument("--max-corpus-lock-age", type=int, default=300)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--restart", action="store_true",
                        help="explicitly recreate an unhealthy single gateway")
    args = parser.parse_args(argv)
    now = time.time()
    checks = [inspect(
        Path(value), now, args.max_tick_age, max_running_age=args.max_running_age,
        max_corpus_lock_age=args.max_corpus_lock_age,
    ) for value in args.deployment]
    for check in checks:
        if not check["healthy"] and args.restart:
            check["recovery"] = restart(Path(check["deployment"]))
            if check["recovery"]["returncode"] == 0:
                check["stale_run_reconciliation"] = reconcile_stale_runs(
                    Path(check["deployment"]), check, now,
                )
    payload = {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "watchdog": "scheduler-independent-host-process",
        "max_tick_age_seconds": args.max_tick_age,
        "max_running_age_seconds": args.max_running_age,
        "max_corpus_lock_age_seconds": args.max_corpus_lock_age,
        "healthy": all(item["healthy"] for item in checks), "deployments": checks,
    }
    _write(Path(args.evidence), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
