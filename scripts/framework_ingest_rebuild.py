#!/usr/bin/env python3
"""Source ingestion and derived-only rebuild operations (okengine#408)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

_MANIFEST_READ_ERRORS = (OSError, yaml.YAMLError)
_HEALTH_READ_ERRORS = (OSError, json.JSONDecodeError)

ENGINE = Path(__file__).resolve().parents[1]
REBUILDS = {
    "indexes": ["rebuild_index.py", "build_index_tree.py"],
    "dashboards": ["refresh_kb_dashboards.py"],
    "backlinks": ["backlinks_refresh.py"],
    "projection": ["@postgres-projection"],
}
DEFAULT_REBUILDS = ("indexes", "dashboards", "backlinks", "projection")
DERIVED_NAMES = {
    "INDEX.md",
    "index.md",
    "BUNDLE.md",
    "HEALTH.md",
    "AGENTS.md",
    ".backlinks.json",
}


class IngestError(ValueError):
    pass


def _dep(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not (path / "wiki").is_dir():
        raise IngestError(f"not an OKEngine deployment: {path}")
    return path


def _run(script: str, dep: Path, args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    if script == "@postgres-projection":
        return subprocess.run(
            ["docker", "compose", "run", "--rm", "okengine-projection",
             "python", "/app/service.py", "--reconcile"],
            cwd=dep, env=os.environ.copy(), text=True, capture_output=True, check=False)
    env = os.environ.copy()
    env["WIKI_PATH"] = str(dep)
    return subprocess.run([sys.executable, str(ENGINE / "scripts/cron" / script), *(args or [])],
                          cwd=dep, env=env, text=True, capture_output=True, check=False)


def _manifests(dep: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for base in (dep / "connectors", dep / "sources/connectors"):
        if not base.is_dir():
            continue
        for path in sorted([*base.rglob("*.yaml"), *base.rglob("*.yml")]):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except _MANIFEST_READ_ERRORS as exc:
                raise IngestError(f"invalid connector manifest {path}: {exc}") from exc
            name = raw.get("id") if isinstance(raw, dict) else None
            if not isinstance(name, str) or not name:
                raise IngestError(f"connector manifest has no id: {path}")
            if name in found:
                raise IngestError(f"duplicate connector id: {name}")
            found[name] = path
    return found


def _health(dep: Path) -> list[dict]:
    values = []
    root = dep / ".okengine/connectors/health"
    for path in sorted(root.rglob("*.json")) if root.is_dir() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except _HEALTH_READ_ERRORS:
            continue
        if isinstance(item, dict):
            values.append(item)
    return values


def ingest(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework ingest")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status"); status.add_argument("deployment"); status.add_argument("--json", action="store_true")
    run = sub.add_parser("run"); run.add_argument("deployment"); run.add_argument("--connector"); run.add_argument("--json", action="store_true")
    retry = sub.add_parser("retry"); retry.add_argument("deployment"); retry.add_argument("--failed", action="store_true", required=True); retry.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        dep = _dep(args.deployment)
        manifests = _manifests(dep)
        if args.command == "status":
            health = _health(dep)
            payload = {"connectors": sorted(manifests), "health": health,
                       "failed": sorted(str(h.get("connector_id")) for h in health if not h.get("ok"))}
        else:
            names = [args.connector] if args.command == "run" and args.connector else sorted(manifests)
            if args.command == "retry":
                names = sorted({str(h.get("connector_id")) for h in _health(dep) if not h.get("ok")})
            missing = [name for name in names if name not in manifests]
            if missing:
                raise IngestError(f"connector not found: {', '.join(missing)}")
            results = []
            for name in names:
                result = _run("source_connector.py", dep, ["--manifest", str(manifests[name]),
                              "--state-root", str(dep / ".okengine/connectors/state"),
                              "--archive-root", str(dep / "raw/connectors"),
                              "--health-root", str(dep / ".okengine/connectors/health"), "--summary-only"])
                results.append({"connector": name, "exit_code": result.returncode,
                                "stdout": result.stdout, "stderr": result.stderr})
            payload = {"connectors": results, "status": "succeeded" if all(not x["exit_code"] for x in results) else "failed"}
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else json.dumps(payload, sort_keys=True))
        return 0 if payload.get("status", "succeeded") == "succeeded" else 1
    except IngestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


def sources(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework sources")
    sub = parser.add_subparsers(dest="command", required=True)
    hydrate = sub.add_parser("hydrate"); hydrate.add_argument("deployment"); hydrate.add_argument("--missing-body", action="store_true", required=True); hydrate.add_argument("--json", action="store_true")
    reconcile = sub.add_parser("reconcile"); reconcile.add_argument("deployment"); reconcile.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        dep = _dep(args.deployment)
        if args.command == "hydrate":
            opml = dep / "feeds/feeds.opml"
            if not opml.is_file():
                raise IngestError(f"feed configuration not found: {opml}")
            command = ["--opml", str(opml), "--out-dir", str(dep / "raw/feeds"),
                       "--state", str(dep / ".okengine/feed-state.json"), "--capture-full-text",
                       "--capture-dir", str(dep / "raw/captures")]
            result = _run("feed_fetch.py", dep, command)
        else:
            result = _run("classify_sources.py", dep, ["--vault", str(dep)])
        payload = {"operation": args.command, "status": "succeeded" if result.returncode == 0 else "failed",
                   "stdout": result.stdout, "stderr": result.stderr}
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else
              f"Sources {args.command}: {payload['status']}")
        return result.returncode
    except IngestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


def _canonical_snapshot(dep: Path) -> dict[str, tuple[int, int]]:
    """Cheap mutation tripwire: derived rebuilds must not touch canonical pages."""
    out = {}
    for path in (dep / "wiki").rglob("*.md"):
        if path.name in DERIVED_NAMES or path.name.startswith("INDEX-p") or "dashboards" in path.parts:
            continue
        stat = path.stat()
        out[path.relative_to(dep).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return out


def rebuild(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework rebuild")
    parser.add_argument("deployment")
    group = parser.add_mutually_exclusive_group(required=True)
    for option in ("indexes", "dashboards", "backlinks", "projection", "all-derived"):
        group.add_argument(f"--{option}", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        dep = _dep(args.deployment)
        selected = (list(DEFAULT_REBUILDS) if args.all_derived
                    else [name for name in REBUILDS if getattr(args, name)])
        before = _canonical_snapshot(dep)
        children = []
        for family in selected:
            for script in REBUILDS[family]:
                result = _run(script, dep)
                children.append({"family": family, "script": script, "exit_code": result.returncode,
                                 "stdout": result.stdout, "stderr": result.stderr})
        if _canonical_snapshot(dep) != before:
            raise IngestError("derived rebuild modified canonical wiki content")
        status = "succeeded" if all(not child["exit_code"] for child in children) else "failed"
        payload = {"status": status, "families": selected, "children": children}
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else
              f"Rebuild {','.join(selected)}: {status}")
        return 0 if status == "succeeded" else 1
    except IngestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1
