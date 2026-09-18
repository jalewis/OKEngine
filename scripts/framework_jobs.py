#!/usr/bin/env python3
"""Operate cron-plus jobs by stable composed name, never by runtime hash."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ENGINE_ROOT = Path(__file__).resolve().parents[1]
LIVE_JOBS = Path(".hermes-data/cron-plus/jobs.json")


class JobsError(ValueError):
    pass


def _deployment(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir() or not (path / "wiki").is_dir():
        raise JobsError(f"not an OKEngine deployment: {path}")
    if not (path / "docker-compose.yml").is_file():
        raise JobsError(f"deployment has no docker-compose.yml: {path}")
    return path


def _read_live_jobs(deployment: Path) -> Any:
    path = deployment / LIVE_JOBS
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except PermissionError:
        pass
    except json.JSONDecodeError as exc:
        raise JobsError(f"invalid cron-plus jobs state: {path}: {exc}") from exc

    command = ["docker", "compose", "-f", str(deployment / "docker-compose.yml"),
               "exec", "-T", "gateway", "cat", "/opt/data/cron-plus/jobs.json"]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or "gateway unavailable"
        raise JobsError(f"cannot read live cron-plus jobs for {deployment}: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise JobsError(f"gateway returned invalid cron-plus jobs JSON: {exc}") from exc


def _jobs(deployment: Path) -> dict[str, dict[str, Any]]:
    raw = _read_live_jobs(deployment)
    values = raw.get("jobs") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        raise JobsError("cron-plus jobs state must contain a jobs list")
    registry: dict[str, dict[str, Any]] = {}
    for job in values:
        if not isinstance(job, dict):
            raise JobsError("cron-plus jobs state contains a non-object job")
        name = job.get("name")
        runtime_id = job.get("id")
        if not isinstance(name, str) or not name.strip() or not isinstance(runtime_id, str) \
                or not runtime_id.strip():
            raise JobsError("every cron-plus job requires a stable name and runtime id")
        if name in registry:
            raise JobsError(f"duplicate composed job name: {name}")
        registry[name] = job
    return registry


def _job(deployment: Path, name: str) -> dict[str, Any]:
    job = _jobs(deployment).get(name)
    if job is None:
        raise JobsError(f"job not found: {name}")
    return job


def _public(job: dict[str, Any]) -> dict[str, Any]:
    """Return operator state without leaking cron-plus' unstable hash identifier."""
    return {key: value for key, value in job.items() if key != "id"}


def _invoke(deployment: Path, action: str, runtime_id: str) -> None:
    env = os.environ.copy()
    env["CRON_PACK_DIR"] = str(deployment)
    command = ["bash", str(ENGINE_ROOT / "scripts/cron-plus.sh"), action, runtime_id]
    result = subprocess.run(command, env=env, text=True, capture_output=True, check=False)
    if result.returncode:
        # cron-plus includes the runtime id in errors; keep that implementation detail private.
        raise JobsError(f"cron-plus {action} failed for the selected job")


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _list(args: argparse.Namespace) -> int:
    deployment = _deployment(args.deployment)
    jobs = [_public(job) for _, job in sorted(_jobs(deployment).items())]
    if args.json:
        _emit({"jobs": jobs}, as_json=True)
        return 0
    if not jobs:
        print("No jobs configured.")
        return 0
    print(f"{'NAME':<42} {'ENABLED':<8} {'SCHEDULE':<24} {'LAST'}")
    for job in jobs:
        schedule = job.get("schedule") or {}
        cadence = schedule.get("expr") or schedule.get("interval_s") or schedule.get("run_at") or "?"
        last = job.get("last_run_at") or "never"
        print(f"{job['name']:<42} {'yes' if job.get('enabled', True) else 'no':<8} "
              f"{str(cadence):<24} {last}")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    public = _public(_job(_deployment(args.deployment), args.job))
    if args.json:
        _emit(public, as_json=True)
    else:
        for key, value in public.items():
            print(f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}")
    return 0


def _change(args: argparse.Namespace) -> int:
    deployment = _deployment(args.deployment)
    before = _job(deployment, args.job)
    prior_run = before.get("last_run_at")
    _invoke(deployment, args.action, before["id"])
    payload: dict[str, Any] = {"job": args.job, "action": args.action, "accepted": True}
    if args.action == "run" and args.wait:
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            current = _job(deployment, args.job)
            if current.get("last_run_at") != prior_run:
                payload.update({"completed": True,
                                "success": current.get("last_run_success"),
                                "last_run_at": current.get("last_run_at")})
                break
            time.sleep(args.poll_interval)
        else:
            raise JobsError(f"timed out waiting for job completion: {args.job}")
    if args.json:
        _emit(payload, as_json=True)
    else:
        suffix = " and completed" if payload.get("completed") else ""
        print(f"{args.action.capitalize()} accepted{suffix}: {args.job}")
    return 0


def _logs(args: argparse.Namespace) -> int:
    deployment = _deployment(args.deployment)
    _job(deployment, args.job)  # Validate the stable name before handing it to the log helper.
    env = os.environ.copy()
    env["CRON_PACK_DIR"] = str(deployment)
    command = ["bash", str(ENGINE_ROOT / "scripts/cron-plus-logs.sh"), "runs", args.job]
    if args.follow:
        command.append("--follow")
    return subprocess.call(command, env=env)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="framework jobs", description="Manage cron-plus jobs by stable composed name.")
    sub = parser.add_subparsers(dest="command", required=True)

    listed = sub.add_parser("list")
    listed.add_argument("deployment")
    listed.add_argument("--json", action="store_true")
    listed.set_defaults(handler=_list)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("deployment")
    inspect.add_argument("job")
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(handler=_inspect)

    run = sub.add_parser("run")
    run.add_argument("deployment")
    run.add_argument("job")
    run.add_argument("--wait", action="store_true")
    run.add_argument("--timeout", type=float, default=300.0)
    run.add_argument("--poll-interval", type=float, default=2.0, help=argparse.SUPPRESS)
    run.add_argument("--json", action="store_true")
    run.set_defaults(handler=_change, action="run")

    logs = sub.add_parser("logs")
    logs.add_argument("deployment")
    logs.add_argument("job")
    logs.add_argument("--follow", action="store_true")
    logs.set_defaults(handler=_logs)

    for action in ("pause", "resume"):
        change = sub.add_parser(action)
        change.add_argument("deployment")
        change.add_argument("job")
        change.add_argument("--json", action="store_true")
        change.set_defaults(handler=_change, action=action, wait=False)

    args = parser.parse_args(argv)
    if getattr(args, "timeout", 1) <= 0:
        parser.error("--timeout must be positive")
    try:
        return args.handler(args)
    except JobsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
