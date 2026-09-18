#!/usr/bin/env python3
"""Explain page provenance and create bounded evidence exports (okengine#409)."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

LINK = re.compile(r"\[\[([^\]|#]+)")
SENSITIVE_KEY = re.compile(r"(?:token|secret|password|credential|api[_-]?key)", re.I)


class ExplainError(ValueError):
    """Invalid deployment, reference, scope, or requested evidence."""


def _dep(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not (path / "wiki").is_dir():
        raise ExplainError(f"not an OKEngine deployment: {path}")
    return path


def _ref(dep: Path, value: str) -> Path:
    raw = value.removesuffix(".md").lstrip("/")
    if raw.startswith("wiki/"):
        raw = raw[5:]
    path = (dep / "wiki" / f"{raw}.md").resolve()
    try:
        path.relative_to((dep / "wiki").resolve())
    except ValueError as exc:
        raise ExplainError("page reference escapes wiki") from exc
    if not path.is_file():
        raise ExplainError(f"page not found: {value}")
    return path


def _page(path: Path, dep: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = {}
    if text.startswith("---\n"):
        try:
            front, body = text[4:].split("\n---", 1); fm = yaml.safe_load(front) or {}
        except (ValueError, yaml.YAMLError): body = text
    else: body = text
    refs = sorted(set(LINK.findall(text)))
    return {"path": path.relative_to(dep / "wiki").as_posix(), "frontmatter": fm,
            "body": body.lstrip("\n"), "references": refs,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _schema(dep: Path) -> dict:
    for path in (dep / "composed-schema.yaml", dep / "schema.yaml"):
        if path.is_file():
            try: return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError: return {}
    return {}


def _production(dep: Path, rel: str) -> list[dict]:
    found = []
    root = dep / ".okengine/operations/runs"
    # glob-ok: operation run receipts are a deliberately flat run-id registry.
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try: receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): continue
        outputs = receipt.get("outputs") or receipt.get("declared_outputs") or []
        if any(rel == str(item) or rel in str(item) for item in outputs):
            found.append({"run_id": receipt.get("run_id"), "operation": receipt.get("operation"),
                          "status": receipt.get("status"), "finished": receipt.get("finished")})
    return found


def _explanation(dep: Path, path: Path, field: str | None = None) -> dict:
    page = _page(path, dep); fm = page["frontmatter"]; typ = fm.get("type")
    schema = _schema(dep); type_rule = (schema.get("types") or {}).get(typ, {}) if typ else {}
    result = {"page": page["path"], "type": typ, "owner": type_rule.get("owner"),
              "producer": fm.get("producer_lane") or fm.get("generated_by"),
              "source_lineage": fm.get("sources") or [r for r in page["references"] if r.startswith("sources/")],
              "quality_flags": fm.get("quality_flags") or fm.get("flags") or [],
              "review_reason": fm.get("review_reason") or fm.get("reason"),
              "policy_outcome": fm.get("policy_outcome") or fm.get("decision"),
              "last_producing_operations": _production(dep, page["path"]), "sha256": page["sha256"]}
    if field is not None:
        fields = type_rule.get("fields") or {}
        result.update({"field": field, "value": fm.get(field), "schema_rule": fields.get(field),
                       "declared": field in fields or field in (type_rule.get("required") or [])})
    return result


def explain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework explain")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("page", "assessment"):
        p = sub.add_parser(command); p.add_argument("deployment"); p.add_argument("reference"); p.add_argument("--json", action="store_true")
    field = sub.add_parser("field"); field.add_argument("deployment"); field.add_argument("reference"); field.add_argument("field"); field.add_argument("--json", action="store_true")
    config = sub.add_parser("config"); config.add_argument("deployment"); config.add_argument("key"); config.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        dep = _dep(args.deployment)
        if args.command == "config":
            candidates = [dep / "config.yaml", dep / ".hermes-data/config.yaml"]
            path = next((p for p in candidates if p.is_file()), None)
            if path is None:
                raise ExplainError("deployment config not found")
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            value = data
            for part in args.key.split("."):
                if not isinstance(value, dict) or part not in value:
                    raise ExplainError(f"config key not found: {args.key}")
                value = value[part]
            redacted = any(SENSITIVE_KEY.search(part) for part in args.key.split("."))
            payload = {"key": args.key, "value": "<redacted>" if redacted else value,
                       "redacted": redacted, "owner": "deployment",
                       "source": path.relative_to(dep).as_posix()}
        else:
            payload = _explanation(dep, _ref(dep, args.reference), getattr(args, "field", None))
            if args.command == "assessment" and payload["type"] != "assessment":
                raise ExplainError(f"page is not an assessment: {args.reference}")
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except ExplainError as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1


def _evidence(dep: Path, actor: str | None, assessment: str | None) -> list[Path]:
    if actor:
        seeds = [_ref(dep, actor)]
    else:
        seeds = []
        for path in (dep / "wiki").rglob("*.md"):
            page = _page(path, dep)
            if page["frontmatter"].get("run_id") == assessment: seeds.append(path)
        if not seeds:
            raise ExplainError(f"assessment run not found: {assessment}")
    selected = {path.resolve() for path in seeds}
    for seed in list(seeds):
        for ref in _page(seed, dep)["references"]:
            if ref.startswith(("sources/", "assessments/")):
                try: selected.add(_ref(dep, ref).resolve())
                except ExplainError: pass
    return sorted(selected)


def _manifest(dep: Path, paths: list[Path], scope: str) -> dict:
    return {"api": 1, "kind": "analytical-export", "scope": scope,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": [{"path": p.relative_to(dep / "wiki").as_posix(),
                       "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths],
            "excludes": ["runtime-state", "secrets", "scheduler-state", "backup-metadata"]}


def snapshot(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework snapshot evidence")
    parser.add_argument("kind", choices=["evidence"]); parser.add_argument("deployment")
    group = parser.add_mutually_exclusive_group(required=True); group.add_argument("--actor"); group.add_argument("--assessment")
    parser.add_argument("--json", action="store_true"); args = parser.parse_args(argv)
    try:
        dep = _dep(args.deployment); paths = _evidence(dep, args.actor, args.assessment)
        scope = f"actor:{args.actor}" if args.actor else f"assessment:{args.assessment}"
        manifest = _manifest(dep, paths, scope)
        digest = hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest()
        out = dep / ".okengine/snapshots" / digest; out.mkdir(parents=True, exist_ok=True)
        manifest_path = out / "manifest.json"
        if not manifest_path.exists():
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            for path in paths:
                target = out / "wiki" / path.relative_to(dep / "wiki")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        payload = {"snapshot_id": digest, "manifest": str(manifest_path), "files": len(paths)}
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else f"Evidence snapshot {digest}: {len(paths)} files")
        return 0
    except ExplainError as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1


def _scope(dep: Path, scope: str) -> list[Path]:
    if scope.startswith("page:"):
        return [_ref(dep, scope[5:])]
    if scope.startswith("namespace:"):
        name = scope[10:]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ExplainError("invalid namespace scope")
        root = dep / "wiki" / name
        if not root.is_dir():
            raise ExplainError(f"namespace not found: {name}")
        return sorted(root.rglob("*.md"))
    raise ExplainError("scope must be page:REF or namespace:NAME")


def export(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework export")
    parser.add_argument("deployment"); parser.add_argument("--scope", required=True)
    parser.add_argument("--format", choices=["json", "md", "bundle"], required=True); parser.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        dep = _dep(args.deployment); paths = _scope(dep, args.scope); manifest = _manifest(dep, paths, args.scope)
        suffix = {"json": ".json", "md": ".md", "bundle": ".tar.gz"}[args.format]
        output = Path(args.output).expanduser().resolve() if args.output else dep / ".okengine/exports" / (hashlib.sha256(args.scope.encode()).hexdigest()[:16] + suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        if args.format == "json":
            output.write_text(json.dumps({"manifest": manifest, "pages": [_page(p, dep) for p in paths]}, indent=2, sort_keys=True) + "\n")
        elif args.format == "md":
            output.write_text("\n\n---\n\n".join(p.read_text(encoding="utf-8") for p in paths))
        else:
            with tarfile.open(output, "w:gz") as archive:
                for path in paths: archive.add(path, arcname=f"wiki/{path.relative_to(dep / 'wiki').as_posix()}")
                raw = json.dumps(manifest, indent=2, sort_keys=True).encode(); info = tarfile.TarInfo("manifest.json"); info.size = len(raw); archive.addfile(info, io.BytesIO(raw))
        print(json.dumps({"output": str(output), "format": args.format, "files": len(paths)}, sort_keys=True))
        return 0
    except ExplainError as exc: print(f"ERROR: {exc}", file=sys.stderr); return 1
