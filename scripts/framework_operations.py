#!/usr/bin/env python3
"""Discover and run pack/extension operations through the framework CLI."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml


ENGINE_ROOT = Path(__file__).resolve().parents[1]
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,79}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class OperationsError(ValueError):
    pass


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _deployment(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir() or not (path / "wiki").is_dir():
        raise OperationsError(f"not an OKEngine deployment: {path}")
    return path


def _safe_relative(value: Any, field: str) -> Path:
    path = Path(str(value or ""))
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise OperationsError(f"operation {field} must be a safe deployment-relative path")
    return path


def _validate(raw: Any, source: Path, deployment: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise OperationsError(f"operation manifest must be a mapping: {source}")
    if raw.get("operation_api") != 1:
        raise OperationsError(f"unsupported operation_api in {source}")
    name = str(raw.get("name") or "")
    if not _NAME.fullmatch(name):
        raise OperationsError(f"invalid operation name in {source}: {name!r}")
    owner = str(raw.get("owner") or "").strip()
    if not owner:
        raise OperationsError(f"operation owner is required: {source}")
    entrypoint = _safe_relative(raw.get("entrypoint"), "entrypoint")
    resolved = (deployment / entrypoint).resolve()
    try:
        resolved.relative_to(deployment)
    except ValueError as exc:
        raise OperationsError(f"operation entrypoint escapes deployment: {source}") from exc
    if not resolved.is_file():
        raise OperationsError(f"operation entrypoint is missing: {entrypoint}")
    supports = raw.get("supports") or {}
    if not isinstance(supports, dict):
        raise OperationsError(f"operation supports must be a mapping: {source}")
    return {**raw, "name": name, "owner": owner, "entrypoint": entrypoint.as_posix(),
            "manifest_path": source.relative_to(deployment).as_posix()}


def discover(deployment: Path) -> dict[str, dict[str, Any]]:
    candidates = []
    for base in (deployment / "operations", deployment / ".okengine/operations"):
        if base.is_dir():
            candidates.extend(base.glob("*/operation.yaml"))  # glob-ok: operation/name/manifest contract
    operations: dict[str, dict[str, Any]] = {}
    for source in sorted(set(candidates)):
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise OperationsError(f"cannot read operation manifest {source}: {exc}") from exc
        manifest = _validate(raw, source, deployment)
        previous = operations.get(manifest["name"])
        if previous:
            comparable = {key: value for key, value in manifest.items() if key != "manifest_path"}
            prior = {key: value for key, value in previous.items() if key != "manifest_path"}
            if comparable != prior:
                raise OperationsError(
                    f"operation name collision: {manifest['name']} ({previous['manifest_path']} vs "
                    f"{manifest['manifest_path']})")
            # The deployed effective registry is preferred to the source copy.
            if manifest["manifest_path"].startswith(".okengine/"):
                operations[manifest["name"]] = manifest
        else:
            operations[manifest["name"]] = manifest
    return operations


def _operation(deployment: Path, name: str) -> dict[str, Any]:
    manifest = discover(deployment).get(name)
    if manifest is None:
        raise OperationsError(f"operation not found: {name}")
    return manifest


def operation_command(deployment: Path, manifest: dict[str, Any], arguments: list[str],
                      *, plan: bool = False, source: str = "cli") -> tuple[list[str], dict[str, str]]:
    """Build the one governed command used by CLI, API, Cockpit, and schedulers."""
    supports = manifest.get("supports") or {}
    if plan and not supports.get("plan"):
        raise OperationsError(f"operation does not support planning: {manifest['name']}")
    forwarded = list(arguments)
    if plan and "--dry-run" not in forwarded:
        forwarded.append("--dry-run")
    command = [sys.executable, str(deployment / manifest["entrypoint"]),
               "--target-vault", str(deployment), *forwarded]
    env = os.environ.copy()
    env.update({"WIKI_PATH": str(deployment), "OKENGINE_ROOT": str(ENGINE_ROOT),
                "OKENGINE_OPERATION_NAME": manifest["name"],
                "OKENGINE_OPERATION_OWNER": manifest["owner"],
                "OKENGINE_OPERATION_SOURCE": source})
    return command, env


def result_from_output(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _run_command(deployment: Path, manifest: dict[str, Any], arguments: list[str],
                 *, plan: bool = False) -> tuple[int, dict[str, Any] | None]:
    command, env = operation_command(deployment, manifest, arguments, plan=plan)
    completed = subprocess.run(command, cwd=deployment, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n")
    result = result_from_output(completed.stdout)
    if result is None and completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    return completed.returncode, result


def _find_receipt(deployment: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    if not _RUN_ID.fullmatch(run_id):
        raise OperationsError("invalid run id")
    base = deployment / ".okengine/operations/runs"
    # glob-ok: receipt namespace is operation/run.json by contract
    matches = list(base.glob(f"*/{run_id}.json")) if base.is_dir() else []
    if len(matches) != 1:
        raise OperationsError("operation receipt not found" if not matches else "ambiguous run id")
    try:
        receipt = json.loads(matches[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationsError(f"invalid operation receipt: {matches[0]}") from exc
    return matches[0], receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="framework operations", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "history"):
        p = sub.add_parser(command)
        p.add_argument("deployment")
        p.add_argument("--json", action="store_true")
        if command == "history":
            p.add_argument("--operation")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("deployment"); inspect.add_argument("operation")
    inspect.add_argument("--json", action="store_true")
    for command in ("plan", "run"):
        p = sub.add_parser(command)
        p.add_argument("deployment"); p.add_argument("operation")
        p.add_argument("arguments", nargs=argparse.REMAINDER)
    status = sub.add_parser("status")
    status.add_argument("deployment"); status.add_argument("run_id")
    status.add_argument("--watch", action="store_true"); status.add_argument("--json", action="store_true")
    logs = sub.add_parser("logs")
    logs.add_argument("deployment"); logs.add_argument("run_id")
    logs.add_argument("--follow", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("deployment"); resume.add_argument("run_id")
    resume.add_argument("arguments", nargs=argparse.REMAINDER)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("deployment"); cancel.add_argument("run_id")
    cancel.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        deployment = _deployment(args.deployment)
        if args.command == "list":
            rows = [{key: manifest.get(key) for key in
                     ("name", "owner", "title", "description", "execution", "mutates", "supports")}
                    for manifest in discover(deployment).values()]
            if args.json: _json({"operations": rows})
            elif rows:
                for row in rows: print(f"{row['name']:<24} {row['owner']:<36} {row.get('title') or ''}")
            else: print("no operations discovered")
            return 0
        if args.command == "inspect":
            manifest = _operation(deployment, args.operation)
            _json(manifest) if args.json else print(yaml.safe_dump(manifest, sort_keys=False).rstrip())
            return 0
        if args.command in {"plan", "run"}:
            manifest = _operation(deployment, args.operation)
            code, result = _run_command(deployment, manifest, args.arguments,
                                        plan=args.command == "plan")
            if result is not None: _json(result)
            return code
        if args.command == "history":
            base = deployment / ".okengine/operations/runs"
            rows = []
            # glob-ok: receipt namespace is operation/run.json by contract
            for path in sorted(base.glob("*/*.json"), reverse=True) if base.is_dir() else []:
                try: row = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError): continue
                if args.operation and row.get("operation") != args.operation: continue
                rows.append({key: row.get(key) for key in
                             ("run_id", "operation", "status", "started_at", "finished_at")})
            if args.json: _json({"runs": rows})
            else:
                for row in rows:
                    print(f"{row['run_id']:<24} {row['operation']:<24} {row['status']:<12} "
                          f"{row.get('finished_at') or row.get('started_at') or '—'}")
            return 0
        if args.command in {"status", "logs"}:
            while True:
                path, receipt = _find_receipt(deployment, args.run_id)
                _json(receipt)
                terminal = receipt.get("status") in {"succeeded", "failed", "canceled", "planned"}
                if args.command == "logs":
                    events = path.with_suffix(".jsonl")
                    if events.is_file(): print(events.read_text(encoding="utf-8"), end="")
                    return 0
                if not args.watch or terminal: return 0 if receipt.get("status") != "failed" else 1
                time.sleep(2)
        if args.command == "resume":
            _path, receipt = _find_receipt(deployment, args.run_id)
            manifest = _operation(deployment, str(receipt.get("operation") or ""))
            if not (manifest.get("supports") or {}).get("resume"):
                raise OperationsError(f"operation does not support resume: {manifest['name']}")
            forwarded = list(args.arguments)
            if manifest["name"] == "actor-review" and not any(
                    arg == "--all" or arg == "--actor" for arg in forwarded):
                forwarded.insert(0, "--all")
            forwarded += ["--resume", args.run_id]
            code, result = _run_command(deployment, manifest, forwarded)
            if result is not None: _json(result)
            return code
        if args.command == "cancel":
            _path, receipt = _find_receipt(deployment, args.run_id)
            manifest = _operation(deployment, str(receipt.get("operation") or ""))
            if not (manifest.get("supports") or {}).get("cancel"):
                raise OperationsError(f"operation does not support cancel: {manifest['name']}")
            request = {"run_id": args.run_id, "operation": manifest["name"], "reason": args.reason,
                       "requested_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}
            target = deployment / ".okengine/operations/cancel" / f"{args.run_id}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                prior = json.loads(target.read_text(encoding="utf-8"))
                if prior != request:
                    raise OperationsError("a different cancel request already exists")
            else:
                target.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _json(request)
            return 0
    except (OperationsError, OSError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
