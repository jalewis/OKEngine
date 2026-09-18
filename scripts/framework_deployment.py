#!/usr/bin/env python3
"""Render, diff, and migrate engine-owned deployment Compose."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent
BASE = ENGINE / "templates/pack/skeleton/docker-compose.yml"
ALLOWED_SERVICE_KEYS = {"environment", "ports", "profiles", "deploy", "image"}
# These fields are engine-owned and deliberately cannot survive as pack overrides.
# Migration discards their legacy values so an old generated Compose file can adopt
# the current build contract (for example, repository-root wheel build contexts).
MIGRATED_ENGINE_KEYS = {"build"}
REQUIRED_SERVICES = {"gateway", "okengine-reader", "okengine-mcp", "okengine-cockpit",
                     "okengine-projection", "postgres"}
SERVICE_SHAPES = {
    "environment": (dict, list), "ports": (list,), "profiles": (list,),
    "deploy": (dict,), "image": (str,), "build": (str, dict),
    "volumes": (list,), "depends_on": (dict, list), "healthcheck": (dict,),
}


class DeploymentError(ValueError):
    pass


def _init_module():
    spec = importlib.util.spec_from_file_location("framework_init_for_deployment",
                                                  ENGINE / "scripts/framework_init.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _pack(pack: Path) -> dict:
    try:
        value = yaml.safe_load((pack / "pack.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DeploymentError(f"cannot load pack.yaml: {exc}") from exc
    if not isinstance(value, dict) or not value.get("name"):
        raise DeploymentError("pack.yaml must declare name")
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_override(value: object, base: dict) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("api") != 1:
        raise DeploymentError("deployment.compose.yaml must be an object with api: 1")
    unknown = set(value) - {"api", "services"}
    if unknown:
        raise DeploymentError(f"unknown override keys: {', '.join(sorted(unknown))}")
    services = value.get("services")
    if services is None:
        services = {}
    if not isinstance(services, dict):
        raise DeploymentError("override services must be an object")
    for name, service in services.items():
        if name not in base["services"]:
            raise DeploymentError(f"override names unknown service: {name}")
        if not isinstance(service, dict) or set(service) - ALLOWED_SERVICE_KEYS:
            raise DeploymentError(f"{name} override may contain only "
                                  f"{', '.join(sorted(ALLOWED_SERVICE_KEYS))}")
        rendered = json.dumps(service)
        if re.search(r'(?i)(password|token|secret)"\s*:\s*"(?!\$\{)', rendered):
            raise DeploymentError("literal secrets are forbidden; preserve them in .env variables")
    return services


def validate_effective(compose: object) -> dict:
    """Validate the engine-owned subset of the effective Compose model."""
    if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict):
        raise DeploymentError("effective Compose must contain a services object")
    services = compose["services"]
    missing = REQUIRED_SERVICES - set(services)
    if missing:
        raise DeploymentError(f"effective Compose lacks required services: {sorted(missing)}")
    for name, service in services.items():
        if not isinstance(service, dict):
            raise DeploymentError(f"effective service {name} must be an object")
        if not service.get("image") and not service.get("build"):
            raise DeploymentError(f"effective service {name} must declare image or build")
        for key, expected in SERVICE_SHAPES.items():
            if key in service and not isinstance(service[key], expected):
                kinds = "/".join(item.__name__ for item in expected)
                raise DeploymentError(f"effective service {name}.{key} must be {kinds}")
    return compose


def render(pack: Path) -> str:
    metadata = _pack(pack)
    offset = int(metadata.get("port_offset") or 0)
    tokens = _init_module()._tokens(pack, str(metadata.get("description") or ""), offset)
    text = BASE.read_text(encoding="utf-8")
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    if re.search(r"\{\{[A-Z][A-Z0-9_]*\}\}", text):
        raise DeploymentError("engine Compose base contains unresolved template tokens")
    base = yaml.safe_load(text)
    override_path = pack / "deployment.compose.yaml"
    override = yaml.safe_load(override_path.read_text(encoding="utf-8")) \
        if override_path.is_file() else None
    services = validate_override(override, base)
    base["services"] = _deep_merge(base["services"], services)
    validate_effective(base)
    return yaml.safe_dump(base, sort_keys=False, width=1000)


def discover_packs(roots: list[Path]) -> list[Path]:
    packs = set()
    for root in roots:
        if (root / "pack.yaml").is_file():
            packs.add(root.resolve())
        elif root.is_dir():
            packs.update(path.parent.resolve() for path in root.rglob("pack.yaml"))
    return sorted(packs)


def parity(roots: list[Path]) -> list[str]:
    """Return drift findings for every pack that has adopted contract v1."""
    errors = []
    packs = discover_packs(roots)
    if not packs:
        return ["no packs discovered"]
    for pack in packs:
        if not (pack / "deployment.compose.yaml").is_file():
            errors.append(f"{pack}: deployment Compose contract v1 not adopted")
            continue
        try:
            effective = render(pack)
            current = (pack / "docker-compose.yml").read_text(encoding="utf-8")
            if current != effective:
                errors.append(f"{pack}: docker-compose.yml differs from engine render")
        except (DeploymentError, OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{pack}: {exc}")
    return errors


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _migration_override(current: dict, base: dict) -> dict:
    result = {"api": 1, "services": {}}
    for name, service in current.get("services", {}).items():
        if name not in base.get("services", {}):
            raise DeploymentError(f"legacy Compose adds unsupported service {name}")
        changed = {key: value for key, value in service.items()
                   if base["services"][name].get(key) != value
                   and key not in MIGRATED_ENGINE_KEYS}
        unsupported = set(changed) - ALLOWED_SERVICE_KEYS
        if unsupported:
            raise DeploymentError(f"legacy {name} has unsupported differences: "
                                  f"{', '.join(sorted(unsupported))}")
        if changed:
            result["services"][name] = changed
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("render", "diff", "migrate"):
        command = sub.add_parser(action)
        command.add_argument("pack")
    parity_parser = sub.add_parser("parity")
    parity_parser.add_argument("roots", nargs="+")
    args = parser.parse_args(argv)
    if args.action == "parity":
        errors = parity([Path(root).resolve() for root in args.roots])
        for error in errors:
            print(f"FAIL: {error}")
        if not errors:
            print("deployment Compose parity: clean")
        return 1 if errors else 0
    pack = Path(args.pack).resolve()
    try:
        effective = render(pack)
        target = pack / "docker-compose.yml"
        if args.action == "render":
            _write(target, effective)
            print(f"rendered {target} from Compose contract v1")
            return 0
        if args.action == "diff":
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            diff = "".join(difflib.unified_diff(
                current.splitlines(True), effective.splitlines(True),
                fromfile=str(target), tofile="engine-rendered"))
            print(diff, end="")
            return 1 if diff else 0
        current = yaml.safe_load(target.read_text(encoding="utf-8"))
        base = yaml.safe_load(effective)
        override = _migration_override(current, base)
        override["migrated_from_sha256"] = hashlib.sha256(
            target.read_bytes()).hexdigest()
        # Provenance is informational, not part of the input contract.
        provenance = override.pop("migrated_from_sha256")
        _write(pack / "deployment.compose.yaml",
               f"# migrated_from_sha256: {provenance}\n" +
               yaml.safe_dump(override, sort_keys=False))
        print(f"migrated allowed differences to {pack / 'deployment.compose.yaml'}; "
              "docker-compose.yml and .env were not changed")
        return 0
    except (DeploymentError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
