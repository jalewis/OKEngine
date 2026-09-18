#!/usr/bin/env python3
"""Materialize secure, default-on PostgreSQL projection config for an OKEngine deployment."""
from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import quote

PLACEHOLDERS = {
    "", "REPLACE_BEFORE_ENABLING", "REPLACE_WITH_A_DIFFERENT_VALUE",
    "okengine-projection-local", "okengine-reader-local",
}


def _read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    values = {}
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return lines, values


def _set(lines: list[str], key: str, value: str) -> None:
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")


def ensure(pack: Path, engine: Path) -> list[str]:
    compose = pack / "docker-compose.yml"
    if not compose.is_file():
        raise ValueError(f"{pack} has no docker-compose.yml")
    env_path = pack / ".env"
    lines, values = _read_env(env_path)
    changes = []

    writer = values.get("OKENGINE_PROJECTION_WRITER_PASSWORD", "")
    if writer in PLACEHOLDERS:
        writer = secrets.token_urlsafe(36)
        _set(lines, "OKENGINE_PROJECTION_WRITER_PASSWORD", writer)
        changes.append("generated projection writer credential")
    reader = values.get("OKENGINE_PROJECTION_READER_PASSWORD", "")
    if reader in PLACEHOLDERS or reader == writer:
        reader = secrets.token_urlsafe(36)
        _set(lines, "OKENGINE_PROJECTION_READER_PASSWORD", reader)
        changes.append("generated distinct projection reader credential")
    dsn = f"postgresql://okengine_reader:{quote(reader, safe='')}@postgres:5432/okengine"
    if values.get("OKENGINE_PROJECTION_READER_DSN") != dsn:
        _set(lines, "OKENGINE_PROJECTION_READER_DSN", dsn)
        changes.append("configured projection reader DSN")

    if "okengine-projection:" not in compose.read_text(encoding="utf-8"):
        generated = pack / ".hermes-data/projection-compose.yml"
        generated.parent.mkdir(parents=True, exist_ok=True)
        source = engine / "templates/pack/projection-compose-overlay.yml"
        body = source.read_text(encoding="utf-8")
        if not generated.is_file() or generated.read_text(encoding="utf-8") != body:
            generated.write_text(body, encoding="utf-8")
            changes.append("materialized projection compose overlay")
        compose_files = [item for item in
                         values.get("COMPOSE_FILE", "docker-compose.yml").split(":")
                         if item != ".okengine/projection-compose.yml"]
        overlay = ".hermes-data/projection-compose.yml"
        if overlay not in compose_files:
            compose_files.append(overlay)
            changes.append("enabled projection compose overlay")
        desired_compose_files = ":".join(compose_files)
        if values.get("COMPOSE_FILE", "docker-compose.yml") != desired_compose_files:
            _set(lines, "COMPOSE_FILE", desired_compose_files)
        legacy = pack / ".okengine/projection-compose.yml"
        if legacy.is_file() and legacy.read_text(encoding="utf-8").startswith(
                "# Engine-managed compatibility overlay"):
            legacy.unlink()
            try:
                legacy.parent.rmdir()
            except OSError:
                pass
            changes.append("removed legacy generated projection overlay")

    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    return changes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path)
    parser.add_argument("--engine", type=Path, required=True)
    args = parser.parse_args(argv)
    for change in ensure(args.pack.resolve(), args.engine.resolve()):
        print(f"    {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
