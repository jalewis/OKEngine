"""Ownership manifests and drift checks for install-domain composed packs (#270)."""
from __future__ import annotations

import hashlib
import ast
import json
import re
from pathlib import Path

import yaml


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_hash(value) -> str:
    return _hash_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _job_contract(value: dict) -> dict:
    # `enabled` is an instance-local operator switch, not an upstream ownership field. A refresh
    # must not turn on a deliberately disabled feed lane, and validation must not call that drift.
    return {key: item for key, item in value.items() if key != "enabled"}


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "pack"


def manifest_path(host: Path, name: str) -> Path:
    return host / ".okengine" / "installed-domains" / f"{_safe_name(name)}.json"


def load(host: Path, name: str) -> dict:
    path = manifest_path(host, name)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def source_manifest(pack: Path, shape: str) -> dict:
    meta = yaml.safe_load((pack / "pack.yaml").read_text(encoding="utf-8")) or {}
    name = str(meta.get("name") or pack.name)
    all_scripts = {}
    scripts_dir = pack / "crons" / "scripts"
    if scripts_dir.is_dir():
        all_scripts = {path.name: _hash_bytes(path.read_bytes())
                       for path in sorted(scripts_dir.glob("*.py"))}  # glob-ok: flat runtime script directory
    jobs = {}
    cron_path = pack / "crons" / "domain-crons.json"
    if cron_path.is_file():
        raw = json.loads(cron_path.read_text(encoding="utf-8"))
        rows = raw.get("jobs", []) if isinstance(raw, dict) else raw
        eligible = [row for row in rows if isinstance(row, dict) and
                    str(row.get("name") or "").startswith(f"{name}-")]
        jobs = {str(row.get("name")): _json_hash(_job_contract(row)) for row in eligible}
        entrypoints = {Path(str(row.get("script"))).name for row in eligible if row.get("script")}
    else:
        entrypoints = set()
    # Local modules imported by an entrypoint are support code in the deployment's flat script
    # namespace. They may be shared by several packs, so record them but never claim exclusive
    # refresh ownership.
    support, pending = set(), list(entrypoints)
    while pending:
        current = pending.pop()
        path = scripts_dir / current
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        modules = {node.module.split(".")[0] for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.module}
        modules |= {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        for module in modules:
            candidate = f"{module}.py"
            if candidate in all_scripts and candidate not in entrypoints and candidate not in support:
                support.add(candidate); pending.append(candidate)
    scripts = {name: digest for name, digest in all_scripts.items() if name in entrypoints}
    return {"manifest_version": 1, "pack": name, "pack_version": str(meta.get("version") or ""),
            "shape": shape, "lane_scripts": scripts, "cron_jobs": jobs,
            "shared_support_scripts": {name: all_scripts[name] for name in sorted(support)},
            "scope": "deployable-runtime-assets"}


def write(host: Path, manifest: dict) -> bool:
    path = manifest_path(host, str(manifest["pack"]))
    content = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return True


def installed_drift(host: Path, manifest: dict) -> list[str]:
    """Compare installed, deployable host assets with their last accepted pack snapshot."""
    drift = []
    for name, expected in (manifest.get("lane_scripts") or {}).items():
        path = host / "crons" / "scripts" / name
        if not path.is_file():
            drift.append(f"{manifest.get('pack')}: missing crons/scripts/{name}")
        elif _hash_bytes(path.read_bytes()) != expected:
            drift.append(f"{manifest.get('pack')}: modified crons/scripts/{name}")
    cron_path = host / "crons" / "domain-crons.json"
    try:
        raw = json.loads(cron_path.read_text(encoding="utf-8"))
        rows = raw.get("jobs", []) if isinstance(raw, dict) else raw
        jobs = {str(row.get("name")): row for row in rows
                if isinstance(row, dict) and row.get("name")}
    except (OSError, json.JSONDecodeError):
        jobs = {}
    for name, expected in (manifest.get("cron_jobs") or {}).items():
        if name not in jobs:
            drift.append(f"{manifest.get('pack')}: missing cron job {name}")
        elif _json_hash(_job_contract(jobs[name])) != expected:
            drift.append(f"{manifest.get('pack')}: modified cron job {name}")
    return drift


def all_installed_drift(host: Path) -> list[str]:
    base = host / ".okengine" / "installed-domains"
    drift = []
    for path in sorted(base.glob("*.json")) if base.is_dir() else []:  # glob-ok: flat manifest directory
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            drift.append(f"invalid installed-domain manifest: {path.name}")
            continue
        drift.extend(installed_drift(host, manifest))
    return drift
