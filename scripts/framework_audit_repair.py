#!/usr/bin/env python3
"""Composed audit and immutable-plan guarded repair commands (okengine#407)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENGINE = Path(__file__).resolve().parents[1]
AUDITS = {
    "conformance": "conformance_audit.py",
    "corpus": "corpus_audit.py",
    "grounding": "grounding_audit.py",
    "page-quality": "page_quality_audit.py",
    "policy": "policy_audit.py",
    "schema-drift": "wiki_schema_audit.py",
    "year-derivation": "year_derivation_audit.py",
    "implausible-dates": "check_implausible_dates.py",
}
REPAIRS = {
    "body-integrity": ("repair_body_integrity.py", "--vault"),
    "malformed-slugs": ("repair_malformed_slugs.py", "--vault"),
}


class AuditRepairError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _deployment(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir() or not (path / "wiki").is_dir():
        raise AuditRepairError(f"not an OKEngine deployment: {path}")
    return path


def _digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((root / "wiki").rglob("*.md")):
        if any(part in {"dashboards", "operational", ".okengine"} for part in path.parts):
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _atomic_new(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise AuditRepairError(f"immutable artifact already exists: {path.name}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _run(script: str, deployment: Path, extra: list[str] | None = None) -> dict[str, Any]:
    env = os.environ.copy()
    env["WIKI_PATH"] = str(deployment)
    result = subprocess.run([sys.executable, str(ENGINE / "scripts/cron" / script), *(extra or [])],
                            cwd=deployment, env=env, text=True, capture_output=True, check=False)
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def audit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework audit")
    parser.add_argument("deployment")
    select = parser.add_mutually_exclusive_group(required=True)
    select.add_argument("--all", action="store_true")
    select.add_argument("--checks")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    deployment = _deployment(args.deployment)
    checks = sorted(AUDITS) if args.all else [item.strip() for item in args.checks.split(",") if item.strip()]
    unknown = sorted(set(checks) - AUDITS.keys())
    if unknown:
        parser.error(f"unknown checks: {', '.join(unknown)}")
    run_id = f"audit-{_now().replace(':', '').replace('+00:00', 'Z')}-{uuid.uuid4().hex[:8]}"
    started = _now()
    children = []
    for name in checks:
        result = _run(AUDITS[name], deployment, ["--check"] if name == "schema-drift" else None)
        children.append({"check": name, **result})
    status = "succeeded" if all(child["exit_code"] == 0 for child in children) else "failed"
    receipt = {"api": 1, "kind": "audit", "run_id": run_id, "status": status,
               "requested": started, "finished": _now(), "deployment": str(deployment),
               "input_digest": _digest(deployment), "checks": checks, "children": children}
    path = deployment / ".okengine/operations/runs" / f"{run_id}.json"
    _atomic_new(path, receipt)
    summary = {"run_id": run_id, "status": status, "checks": checks,
               "failed": [child["check"] for child in children if child["exit_code"]]}
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else
          f"Audit {run_id}: {status} ({len(checks)} checks; receipt {path})")
    return 0 if status == "succeeded" else 1


def _load_artifact(deployment: Path, kind: str, artifact_id: str) -> tuple[Path, dict[str, Any]]:
    base = deployment / ".okengine/operations" / ("plans" if kind == "plan" else "runs")
    path = base / f"{artifact_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditRepairError(f"cannot read {kind} {artifact_id}: {exc}") from exc
    if payload.get("kind") != kind:
        raise AuditRepairError(f"artifact is not a {kind}: {artifact_id}")
    return path, payload


def _plan(args: argparse.Namespace) -> int:
    deployment = _deployment(args.deployment)
    _, audit_receipt = _load_artifact(deployment, "audit", args.from_audit)
    repairs = sorted(set(args.repairs.split(",")))
    unknown = sorted(set(repairs) - REPAIRS.keys())
    if unknown:
        raise AuditRepairError(f"unknown repairs: {', '.join(unknown)}")
    plan_id = f"repair-{uuid.uuid4().hex}"
    plan = {"api": 1, "kind": "plan", "plan_id": plan_id, "created": _now(),
            "from_audit": audit_receipt["run_id"], "deployment": str(deployment),
            "input_digest": _digest(deployment), "repairs": repairs, "applied_by": None}
    path = deployment / ".okengine/operations/plans" / f"{plan_id}.json"
    _atomic_new(path, plan)
    print(json.dumps(plan, indent=2, sort_keys=True) if args.json else
          f"Repair plan {plan_id}: {len(repairs)} guarded repair(s); immutable {path}")
    return 0


def _execute(args: argparse.Namespace, *, verify: bool) -> int:
    deployment = _deployment(args.deployment)
    plan_path, plan = _load_artifact(deployment, "plan", args.plan)
    if Path(plan.get("deployment", "")).resolve() != deployment:
        raise AuditRepairError("repair plan targets a different deployment")
    prior = []
    # glob-ok: operation run receipts are a deliberately flat run-id registry.
    for receipt_path in (deployment / ".okengine/operations/runs").glob("repair-*.json"):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if receipt.get("plan_id") == plan["plan_id"] and receipt.get("status") == "succeeded":
            prior.append(receipt)
    applied = any(receipt.get("kind") == "apply" for receipt in prior)
    if verify and not applied:
        raise AuditRepairError("repair plan has no successful apply receipt")
    if not verify and applied:
        raise AuditRepairError("repair plan was already applied; verify it instead")
    current = _digest(deployment)
    if not verify and current != plan.get("input_digest"):
        raise AuditRepairError("deployment changed since plan; create a fresh repair plan")
    children = []
    for name in plan["repairs"]:
        script, vault_flag = REPAIRS[name]
        extra = [vault_flag, str(deployment)] + ([] if verify else ["--apply"])
        children.append({"repair": name, **_run(script, deployment, extra)})
    status = "succeeded" if all(child["exit_code"] == 0 for child in children) else "failed"
    action = "verify" if verify else "apply"
    run_id = f"repair-{action}-{uuid.uuid4().hex}"
    receipt = {"api": 1, "kind": action, "run_id": run_id, "plan_id": plan["plan_id"],
               "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
               "status": status, "finished": _now(), "children": children,
               "result_digest": _digest(deployment)}
    path = deployment / ".okengine/operations/runs" / f"{run_id}.json"
    _atomic_new(path, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True) if args.json else
          f"Repair {action} {run_id}: {status}; receipt {path}")
    return 0 if status == "succeeded" else 1


def repair(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework repair")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("deployment")
    plan.add_argument("--from-audit", required=True)
    plan.add_argument("--repairs", required=True,
                      help=f"comma-separated: {','.join(sorted(REPAIRS))}")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(handler=_plan)
    for action in ("apply", "verify"):
        command = sub.add_parser(action)
        command.add_argument("deployment")
        command.add_argument("--plan", required=True)
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=lambda a, verify=action == "verify": _execute(a, verify=verify))
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except AuditRepairError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
