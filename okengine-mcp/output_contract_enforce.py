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


def _digest(contract: dict) -> str:
    raw = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _jobs() -> dict:
    path = Path(os.environ.get("OKENGINE_CRON_JOBS") or
                "/opt/hermes/config/cron-plus-jobs.json")
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
    if _PLACEHOLDER.search(body or "") and contract.get("placeholder_links") == "reject":
        out.append(finding("placeholder_link", "Markdown placeholder links are forbidden"))
    unresolved = []
    for match in _WIKILINK.finditer(body or ""):
        target = match.group(1).strip().strip("/").removesuffix(".md")
        if "/" not in target or not (wiki / f"{target}.md").is_file():
            unresolved.append(target)
    if unresolved and contract.get("unresolved_links") == "reject":
        out.append(finding("unresolved_link", "unresolved wikilink(s): " + ", ".join(dict.fromkeys(unresolved))))
    for field in contract.get("required_relationships", []):
        values = frontmatter.get(field)
        values = values if isinstance(values, list) else ([values] if values else [])
        if not values:
            out.append(finding("required_relationship_missing", f"required relationship {field!r} is absent"))
            continue
        for value in values:
            raw = str(value).strip()
            match = re.fullmatch(r"\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]", raw)
            target = (match.group(1) if match else raw).strip().strip("/").removesuffix(".md")
            if "/" not in target or not (wiki / f"{target}.md").is_file():
                out.append(finding("relationship_unresolved", f"{field!r} target {raw!r} does not resolve"))
    return out
