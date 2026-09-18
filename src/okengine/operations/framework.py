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

from okengine.compat import engine_root


_NAME = re.compile(r"^[a-z][a-z0-9-]{1,79}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_LOCK = re.compile(r"^[a-z0-9][a-z0-9/_.-]{0,120}$")
_ARG_TYPES = {"boolean", "string", "int", "float", "page-ref", "enum"}
_EXECUTION = {"deterministic", "model", "mixed"}


def _safe_glob(value: Any, field: str, source: Path) -> list[str]:
    """A list of safe deployment-relative glob patterns (inputs/outputs). Globs (`*`, `**`) are
    allowed; absolute paths and `..` traversal are not."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise OperationsError(f"operation {field} must be a list of paths: {source}")
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        parts = Path(text).parts
        if not text or Path(text).is_absolute() or ".." in parts:
            raise OperationsError(
                f"operation {field} entry must be a safe deployment-relative path: {item!r} ({source})")
        out.append(text)
    return out


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

    # supports: plan/resume/cancel are booleans (the runner reads these to gate --dry-run/resume/cancel)
    supports = raw.get("supports") or {}
    if not isinstance(supports, dict):
        raise OperationsError(f"operation supports must be a mapping: {source}")
    for key in ("plan", "resume", "cancel"):
        if key in supports and not isinstance(supports[key], bool):
            raise OperationsError(f"operation supports.{key} must be true/false: {source}")

    # consequence classification — the runner uses these to decide plan-before-mutate + confirmation
    execution = raw.get("execution")
    if execution is not None and execution not in _EXECUTION:
        raise OperationsError(
            f"operation execution must be one of {sorted(_EXECUTION)}: {source} (got {execution!r})")
    if "mutates" in raw and not isinstance(raw["mutates"], bool):
        raise OperationsError(f"operation mutates must be true/false: {source}")

    # arguments: {name: {type, repeatable, ...}} — the CLI/API validate args against this schema
    arguments = raw.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise OperationsError(f"operation arguments must be a mapping: {source}")
    for arg, spec in arguments.items():
        if not isinstance(spec, dict):
            raise OperationsError(f"operation argument {arg!r} spec must be a mapping: {source}")
        if spec.get("type") is not None and spec["type"] not in _ARG_TYPES:
            raise OperationsError(
                f"operation argument {arg!r} has unknown type {spec['type']!r} "
                f"(allowed: {sorted(_ARG_TYPES)}): {source}")
        if "repeatable" in spec and not isinstance(spec["repeatable"], bool):
            raise OperationsError(f"operation argument {arg!r} repeatable must be true/false: {source}")

    # locks: durable resource identifiers the runner must acquire before mutating (#402)
    locks = raw.get("locks") or []
    if not isinstance(locks, list):
        raise OperationsError(f"operation locks must be a list: {source}")
    for lock in locks:
        if not isinstance(lock, str) or not _LOCK.fullmatch(lock):
            raise OperationsError(f"operation lock resource invalid: {lock!r} ({source})")

    # inputs/outputs: safe deployment-relative globs — inputs feed the engine snapshot digest (#402),
    # outputs are validated to exist before a run may report `succeeded`.
    inputs = _safe_glob(raw.get("inputs"), "inputs", source)
    outputs = _safe_glob(raw.get("outputs"), "outputs", source)

    # permissions.capability: the authorization the runner checks before starting
    permissions = raw.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise OperationsError(f"operation permissions must be a mapping: {source}")
    capability = permissions.get("capability")
    if capability is not None and (not isinstance(capability, str) or not capability.strip()):
        raise OperationsError(f"operation permissions.capability must be a non-empty string: {source}")

    # receipt_schema: optional safe-relative path (shape-checked; existence is a runtime concern)
    if raw.get("receipt_schema") is not None:
        _safe_relative(raw["receipt_schema"], "receipt_schema")

    # timeout: optional positive seconds
    timeout = raw.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or timeout <= 0):
        raise OperationsError(f"operation timeout must be a positive number of seconds: {source}")

    return {**raw, "name": name, "owner": owner, "entrypoint": entrypoint.as_posix(),
            "inputs": inputs, "outputs": outputs, "locks": list(locks),
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
    env.update({"WIKI_PATH": str(deployment), "OKENGINE_ROOT": str(engine_root()),
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
            from okengine.operations import run as operation_run
            manifest = _operation(deployment, args.operation)
            code, result = operation_run.run(deployment, manifest, args.arguments,
                                             source="cli", dry_run=args.command == "plan")
            _json(result)
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
            from okengine.operations import run as operation_run
            _path, receipt = _find_receipt(deployment, args.run_id)
            manifest = _operation(deployment, str(receipt.get("operation") or ""))
            if not (manifest.get("supports") or {}).get("resume"):
                raise OperationsError(f"operation does not support resume: {manifest['name']}")
            forwarded = list(args.arguments)
            if manifest["name"] == "actor-review" and not any(
                    arg == "--all" or arg == "--actor" for arg in forwarded):
                forwarded.insert(0, "--all")
            forwarded += ["--resume", args.run_id]
            # Re-use the SAME engine-owned run id + receipt so status/history stay coherent across resume.
            code, result = operation_run.run(deployment, manifest, forwarded,
                                             source="cli", run_id=args.run_id)
            _json(result)
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
