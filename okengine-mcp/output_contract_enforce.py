"""Synchronous enforcement for model-write output contracts."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

_WIKILINK = re.compile(r"\[\[([^\]|#\n]+)")
_PLACEHOLDER = re.compile(r"\[[^\]\n]+\]\(\s*#\s*\)")
_cache = {"key": None, "jobs": {}}


def link_index(wiki: Path) -> tuple[set[str], dict[tuple[str, str], int]]:
    """Index confined pages once for the offline audit's many link checks."""
    root = wiki.resolve()
    exact: set[str] = set()
    scoped: dict[tuple[str, str], int] = {}
    for page in root.rglob("*.md"):
        try:
            if not page.is_file() or not page.resolve().is_relative_to(root):
                continue
            parts = page.relative_to(root).parts
        except (OSError, ValueError):
            # A page removed/replaced during the read-only audit is not evidence.
            continue
        if len(parts) < 2:
            continue
        logical = "/".join(parts)[:-3]
        exact.add(logical)
        stem = page.stem
        for depth in range(1, len(parts)):
            scope = "/".join(parts[:depth])
            key = (scope, stem)
            scoped[key] = scoped.get(key, 0) + 1
    return exact, scoped


def link_resolves(wiki: Path, raw: str,
                  index: tuple[set[str], dict[tuple[str, str], int]] | None = None) -> bool:
    """Resolve an exact or unique shard-omitting link within its logical parent.

    A basename in a DIFFERENT namespace is not evidence, and an ambiguous
    same-scope basename must fail. Paths may never escape wiki through `..` or
    a symlink. The live enforcer scans only the needed subtree; the audit can
    pass a prebuilt confined index for the whole vault.
    """
    target = raw.strip().removesuffix(".md")
    if target.startswith("/") or target.endswith("/") or "\\" in target:
        return False
    parts = target.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        return False
    scope, stem = "/".join(parts[:-1]), parts[-1]
    namespace = parts[0]
    if index is not None:
        exact, scoped = index
        return target in exact or (scoped.get((scope, stem), 0) == 1
                                   and scoped.get((namespace, stem), 0) == 1)
    root = wiki.resolve()
    direct = root / f"{target}.md"
    if direct.is_file() and direct.resolve().is_relative_to(root):
        return True
    parent = root / scope
    if not parent.is_dir() or not parent.resolve().is_relative_to(root):
        return False
    namespace_root = root / namespace
    if not namespace_root.is_dir() or not namespace_root.resolve().is_relative_to(root):
        return False
    resolved_parent = parent.resolve()
    matches = 0
    parent_hits = 0
    for page in namespace_root.rglob(f"{stem}.md"):
        if page.is_file() and page.resolve().is_relative_to(root):
            matches += 1
            if page.resolve().is_relative_to(resolved_parent):
                parent_hits += 1
            if matches > 1:
                return False
    return matches == 1 and parent_hits == 1


def relationship_findings(
    frontmatter: dict,
    contract: dict,
    wiki: Path,
    index: tuple[set[str], dict[tuple[str, str], int]] | None = None,
) -> list[tuple[str, str]]:
    """Return relationship findings shared by live enforcement and audit."""
    out: list[tuple[str, str]] = []
    required = set(contract.get("required_relationships", []))
    relationships = dict.fromkeys([
        *contract.get("required_relationships", []),
        *contract.get("optional_relationships", []),
    ])
    for field in relationships:
        values = frontmatter.get(field)
        values = values if isinstance(values, list) else ([values] if values else [])
        if not values:
            if field in required:
                out.append((
                    "required_relationship_missing",
                    f"required relationship {field!r} is absent",
                ))
            continue
        for value in values:
            raw = str(value).strip()
            match = re.fullmatch(r"\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]", raw)
            target = (match.group(1) if match else raw).strip().removesuffix(".md")
            if not link_resolves(wiki, target, index):
                out.append((
                    "relationship_unresolved",
                    f"{field!r} target {raw!r} does not resolve",
                ))
    return out


def _digest(contract: dict) -> str:
    raw = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _jobs() -> dict:
    path = Path(os.environ.get("OKENGINE_CRON_JOBS") or
                "/opt/data/cron-plus/jobs.json")
    key = (str(path), path.stat().st_mtime_ns if path.is_file() else None)
    if key != _cache["key"]:
        rows = []
        try:
            doc = json.loads(path.read_text())
            rows = doc.get("jobs", []) if isinstance(doc, dict) else doc
        except (OSError, ValueError):
            pass
        _cache.update(key=key, jobs={str(j.get("name")): j for j in rows if isinstance(j, dict)})
    return _cache["jobs"]


def resolve(caller: dict) -> tuple[dict | None, str | None]:
    """Resolve only server-authenticated job identity; clients never select a contract."""
    if caller.get("kind") != "job":
        return None, None
    actor = str(caller.get("actor") or "")
    lane = actor.removeprefix("cron:")
    job = _jobs().get(lane)
    if not job:
        return {"_missing": True}, lane
    if not isinstance(job.get("output_contract"), dict):
        return (None if job.get("output_contract_exempt") else {"_missing": True}), lane
    contract = job["output_contract"]
    stamped = job.get("output_contract_digest")
    if stamped and stamped != _digest(contract):
        return {"_invalid_digest": True}, lane
    return contract, lane


def capability(caller: dict) -> dict | None:
    """Derive policy-plane authority from a server-resolved cron contract.

    The jobs file and its digest are supplied by the dedicated writer process;
    neither the model nor an MCP argument can widen this capability.  Structural
    field and body constraints remain the output-contract evaluator's job.
    """
    contract, _lane = resolve(caller)
    if not isinstance(contract, dict) or contract.get("_missing") \
            or contract.get("_invalid_digest"):
        return None
    namespaces = contract.get("allowed_namespaces") or []
    paths = ["**"] if "*" in namespaces else [f"{name}/**" for name in namespaces]
    return {
        "rule_id": "engine-authenticated-writer",
        "operations": list(contract.get("operations") or []),
        "paths": paths,
        "types": list(contract.get("allowed_types") or []),
        "update_fields": ["*"],
        "body": "allow",
    }


def evaluate(caller: dict, *, operation: str, namespace: str, page_type: str,
             frontmatter: dict, body: str, unknown_fields: list[str], wiki: Path) -> list[dict]:
    contract, lane = resolve(caller)
    if contract is None:
        return []
    def finding(code, message):
        return {"code": code, "message": message, "lane": lane}
    if contract.get("_invalid_digest"):
        return [finding("contract_digest_mismatch", "generated contract digest does not match")]
    if contract.get("_missing"):
        return [finding("contract_not_resolved", "authenticated lane has no declared contract or exemption")]
    out = []
    namespaces = contract.get("allowed_namespaces", [])
    types = contract.get("allowed_types", [])
    if "*" not in namespaces and namespace not in namespaces:
        out.append(finding("namespace_not_allowed", f"namespace {namespace!r} is outside the lane contract"))
    if "*" not in types and page_type not in types:
        out.append(finding("type_not_allowed", f"type {page_type!r} is outside the lane contract"))
    if operation not in contract.get("operations", []):
        out.append(finding("operation_not_allowed", f"operation {operation!r} is outside the lane contract"))
    missing = [key for key in contract.get("required_fields", [])
               if frontmatter.get(key) in (None, "", [], {})]
    if missing:
        out.append(finding("required_field_missing", "missing required field(s): " + ", ".join(missing)))
    bspec = contract.get("body") or {}
    meaningful = len("".join((body or "").split()))
    if bspec.get("required") and not meaningful:
        out.append(finding("body_required", "a non-empty body is required"))
    if meaningful < int(bspec.get("min_non_whitespace") or 0):
        out.append(finding("body_too_short", f"body has {meaningful} meaningful characters"))
    if unknown_fields and contract.get("unknown_fields") == "reject":
        out.append(finding("unknown_fields", "unknown model-authored field(s): " + ", ".join(unknown_fields)))
    body_links_changed = caller.get("body_links_changed", True)
    if body_links_changed and _PLACEHOLDER.search(body or "") \
            and contract.get("placeholder_links") == "reject":
        out.append(finding("placeholder_link", "Markdown placeholder links are forbidden"))
    unresolved = []
    for match in _WIKILINK.finditer(body or "") if body_links_changed else ():
        target = match.group(1).strip().removesuffix(".md")
        if not link_resolves(wiki, target):
            unresolved.append(target)
    if unresolved and contract.get("unresolved_links") == "reject":
        out.append(finding("unresolved_link", "unresolved wikilink(s): " + ", ".join(dict.fromkeys(unresolved))))
    for code, message in relationship_findings(frontmatter, contract, wiki):
        out.append(finding(code, message))
    return out
