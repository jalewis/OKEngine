"""Fail deterministic cron runs that create a separator-equivalent live page."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import yaml


_RESERVED = {"index.md", "log.md", "agents.md", "hot.md", "bundle.md", "health.md"}
_ID_LIB: ModuleType | None = None


class SlugIdentityCollision(RuntimeError):
    """A deterministic writer introduced a weak-identity collision."""


class Snapshot(NamedTuple):
    paths: frozenset[str]


def _id_lib() -> ModuleType:
    global _ID_LIB
    if _ID_LIB is not None:
        return _ID_LIB
    scripts = Path(os.environ.get("OKENGINE_CRON_SCRIPTS", "/opt/data/scripts"))
    target = scripts / "id_lib.py"
    spec = importlib.util.spec_from_file_location("okengine_cron_id_lib", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load governed identity library at {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _ID_LIB = module
    return module


def _eligible_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.endswith(".md")
        and lowered not in _RESERVED
        and not name.startswith(("_", "."))
        and ".bak." not in name
    )


def _iter_paths(wiki: Path):
    if not wiki.is_dir():
        return
    for root, directories, files in os.walk(wiki):
        directories[:] = [
            name for name in directories if not name.startswith(("_", "."))
        ]
        root_path = Path(root)
        relative_root = root_path.relative_to(wiki)
        for name in files:
            if not _eligible_name(name):
                continue
            path = root_path / name
            rel = (relative_root / name).as_posix()
            yield rel, path


def snapshot(wiki: Path) -> Snapshot:
    """Capture names only; deterministic lanes must not parse the corpus twice."""
    return Snapshot(frozenset(rel for rel, _path in _iter_paths(wiki)))


def _frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    try:
        value = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _live(path: Path) -> bool:
    return str(_frontmatter(path).get("status") or "").strip().lower() != "tombstoned"


def enforce(
    wiki: Path,
    before: Snapshot,
    quarantine_root: Path,
    *,
    job_id: str,
) -> list[dict]:
    """Quarantine only collisions introduced by this invocation, then fail."""
    identity = _id_lib()
    created_paths = {
        rel: path for rel, path in _iter_paths(wiki) if rel not in before.paths
    }
    created = sorted(created_paths)
    if not created:
        return []

    new_keys = {
        (
            identity.qualified_namespace(wiki, created_paths[rel]),
            identity.slug_identity(created_paths[rel].stem),
        )
        for rel in created
    }
    new_keys.discard(("", ""))
    groups: dict[tuple[str, str], list[str]] = {key: [] for key in new_keys if all(key)}
    for rel, path in _iter_paths(wiki):
        key = (
            identity.qualified_namespace(wiki, path),
            identity.slug_identity(path.stem),
        )
        if key in groups and _live(path):
            groups[key].append(rel)

    introduced: list[tuple[str, list[str]]] = []
    for rel in created:
        path = created_paths[rel]
        key = (identity.qualified_namespace(wiki, path), identity.slug_identity(path.stem))
        matches = sorted(groups.get(key, []))
        if _live(path) and len(matches) > 1:
            introduced.append((rel, matches))
    if not introduced:
        return []

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_root = quarantine_root / stamp
    records: list[dict] = []
    for rel, matches in introduced:
        source = created_paths[rel]
        target = run_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        records.append({
            "job_id": job_id,
            "path": rel,
            "matches": matches,
            "quarantined_to": target.as_posix(),
        })
    (run_root / "receipt.json").write_text(
        json.dumps({"api": 1, "collisions": records}, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = "; ".join(f"{item['path']} -> {item['matches']}" for item in records)
    raise SlugIdentityCollision(
        "deterministic writer introduced separator-equivalent page(s); "
        f"quarantined outside wiki: {summary}"
    )
