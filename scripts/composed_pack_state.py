"""Ownership manifests and drift checks for install-domain composed packs (#270)."""
from __future__ import annotations

import hashlib
import ast
import json
from json import JSONDecodeError
import re
from pathlib import Path

import yaml

# Cosmic Ray's ExceptionReplacer cannot mutate an Attribute inside a tuple-valued except clause
# (an incompetent run fails the mutation gate). Bind the exception classes to Names instead.
YamlError = yaml.YAMLError
JsonDecodeError = json.JSONDecodeError


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
    except (OSError, JSONDecodeError):
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
    try:
        schema = yaml.safe_load((pack / "schema.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        schema = {}
    owned = [str(value) for value in ((meta.get("owns") or {}).get("namespaces") or [])]
    partitioning = ((schema.get("partitioning") or {}).get("namespaces") or {})
    permissions = ((schema.get("permissions") or {}).get("namespaces") or {})
    tiers = ((schema.get("tier") or {}).get("namespaces") or {})
    namespace_contracts = {
        ns: {"partitioning": partitioning.get(ns) or {"strategy": "flat"},
             "permissions": permissions.get(ns), "tier": tiers.get(ns)}
        for ns in owned
    }
    manifest = {"manifest_version": 2, "pack": name, "pack_version": str(meta.get("version") or ""),
                "shape": shape, "lane_scripts": scripts, "cron_jobs": jobs,
                "shared_support_scripts": {name: all_scripts[name] for name in sorted(support)},
                "owned_namespaces": namespace_contracts,
                "scope": "deployable-runtime-assets"}
    chat = chat_capabilities(pack)
    if chat:  # recorded only when declared, so no other pack's manifest changes (okengine#991)
        manifest["chat_capabilities"] = chat
    return manifest


def chat_capabilities(pack: Path) -> dict:
    """The `chat:*` capabilities each of the pack's policy files declares, by pack-relative path.

    These are the files `policy_plane.discover_documents` composes when the pack IS the deployment.
    Installed into a host by install-domain, none of them is composed. An unparseable file
    declares nothing here; the policy-plane check FAILs it where it is composed."""
    files = [pack / "policy.yaml", pack / ".okengine" / "policy.yaml",
             *sorted((pack / "extensions").glob("*/policy.yaml"))]  # glob-ok: flat pack directory
    found = {}
    for path in files:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, YamlError):
            continue
        capabilities = document.get("capabilities") if isinstance(document, dict) else None
        if not isinstance(capabilities, dict):
            continue
        actors = sorted(str(actor) for actor in capabilities if str(actor).startswith("chat:"))
        if actors:
            found[path.relative_to(pack).as_posix()] = actors
    return found


def with_chat_record(accepted: dict, incoming: dict) -> dict:
    """`accepted` with its chat declaration replaced by `incoming`'s, and nothing else changed.

    install-domain keeps a manifest's last accepted runtime hashes until --refresh accepts the
    pack update. The chat declaration is not a runtime artifact (a guest's policy files are never
    installed), so it follows the source on every install (okengine#993)."""
    merged = {key: value for key, value in accepted.items() if key != "chat_capabilities"}
    if "chat_capabilities" in incoming:
        merged["chat_capabilities"] = incoming["chat_capabilities"]
    return merged


def guest_chat_warning(manifest: dict) -> str | None:
    """Why an installed guest's declared chat capability does nothing in this host, or None."""
    declared = manifest.get("chat_capabilities")
    if not isinstance(declared, dict) or not declared:
        return None
    where = "; ".join(f"{path} declares {', '.join(map(str, actors))}"
                      for path, actors in declared.items())
    if manifest.get("shape") == "subtree":
        reach = ("its pages live under its wiki/<slug>/ subtree, which the host's chat scope "
                 "(entities/**, concepts/** by default) does not reach, so Chat cannot write them")
    else:
        reach = ("its types are merged into the host schema, so Chat writes them only where the "
                 "host's own chat capability allows (entities/**, concepts/** by default)")
    return (f"{manifest.get('pack')}: {where}, which is never composed in this host: the host "
            f"composes only its own policy files. {reach}. To change what Chat may write here, "
            "declare it in the host's policy.yaml (okengine#991).")


def guest_chat_warnings(host: Path) -> list[str]:
    """`guest_chat_warning` for every installed guest's manifest. An unreadable manifest is left
    to `all_installed_drift`, which reports it."""
    base = host / ".okengine" / "installed-domains"
    warnings = []
    for path in sorted(base.glob("*.json")) if base.is_dir() else []:  # glob-ok: flat manifest directory
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, JsonDecodeError):
            continue
        warning = guest_chat_warning(manifest) if isinstance(manifest, dict) else None
        if warning:
            warnings.append(warning)
    return warnings


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
    except (OSError, JSONDecodeError):
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
        except (OSError, JSONDecodeError):
            drift.append(f"invalid installed-domain manifest: {path.name}")
            continue
        drift.extend(installed_drift(host, manifest))
    return drift
