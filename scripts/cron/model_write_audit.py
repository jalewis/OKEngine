#!/usr/bin/env python3
"""Read-only audit and dry-run repair planning for model-write contracts."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

_FM = re.compile(r"\A---[ \t]*\n(.*?)\n---(.*)\Z", re.S)
_LINK = re.compile(r"\[\[([^\]|#\n]+)")
_PLACEHOLDER = re.compile(r"\[[^\]\n]+\]\(\s*#\s*\)")


def _contract_module():
    path = Path(__file__).with_name("output_contract.py")
    spec = importlib.util.spec_from_file_location("audit_output_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(path: Path) -> tuple[dict, str] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        match = _FM.match(text)
        fm = yaml.safe_load(match.group(1)) if match else None
        return (fm, match.group(2)) if isinstance(fm, dict) else None
    except (OSError, yaml.YAMLError):
        return None


def audit(vault: Path, jobs_path: Path, *, now: str | None = None) -> dict:
    oc = _contract_module()
    doc = json.loads(jobs_path.read_text())
    jobs = doc.get("jobs", []) if isinstance(doc, dict) else doc
    findings = []
    contracts = {}
    audit_markers = {}
    for job in jobs:
        writer = not job.get("no_agent") and "okengine-write" in (job.get("enabled_toolsets") or [])
        contract = job.get("output_contract")
        if writer and contract is None and not job.get("output_contract_exempt"):
            findings.append({"scope": "lane", "lane": job.get("name"), "type": None,
                             "reason": "contract_missing"})
        if contract is not None:
            for error in oc.validate(contract, f"job {job.get('name')}"):
                findings.append({"scope": "lane", "lane": job.get("name"), "type": None,
                                 "reason": "contract_invalid", "detail": error})
            contracts[job.get("name")] = contract
            audit_markers[job.get("name")] = job.get("audit_markers") or []
    wiki = vault / "wiki"
    rels: set[str] = set()
    by_base: dict[str, set[str]] = {}
    for indexed in wiki.rglob("*.md"):
        try:
            indexed_rel = indexed.relative_to(wiki).as_posix()[:-3]
        except (OSError, ValueError):
            continue
        rels.add(indexed_rel)
        by_base.setdefault(indexed.stem, set()).add(indexed_rel)
    for path in wiki.rglob("*.md"):
        parsed = _page(path)
        if not parsed:
            continue
        fm, body = parsed
        rel = path.relative_to(wiki).as_posix()
        namespace = rel.split("/", 1)[0]
        page_type = str(fm.get("type") or "")
        lane = str(fm.get("producer_lane") or fm.get("generated_by") or "") or None
        candidates = [(name, c) for name, c in contracts.items()
                      if ("*" in c["allowed_namespaces"] or namespace in c["allowed_namespaces"])
                      and ("*" in c["allowed_types"] or page_type in c["allowed_types"])
                      and audit_markers.get(name)
                      and all(fm.get(key) not in (None, "", [], {})
                              for key in audit_markers[name])]
        if lane in contracts:
            contract = contracts[lane]
        elif len(candidates) == 1:
            lane, contract = candidates[0]
        else:
            # Class-level defects do not need producer attribution. Keep them separate from lane
            # contract findings so repair planning never applies a guessed lane policy.
            namespace_supported = any(
                ("*" in c["allowed_namespaces"] or namespace in c["allowed_namespaces"])
                and ("*" in c["allowed_types"] or page_type in c["allowed_types"])
                for c in contracts.values())
            if namespace_supported and not len("".join(body.split())):
                try:
                    page_digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                findings.append({"scope": "page", "path": rel, "lane": "unattributed",
                                 "type": page_type, "reason": "class_empty_body",
                                 "detail": "empty body independent of lane attribution",
                                 "sha256": page_digest, "version": fm.get("version")})
            if namespace_supported and _PLACEHOLDER.search(body):
                try:
                    page_digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                findings.append({"scope": "page", "path": rel, "lane": "unattributed",
                                 "type": page_type, "reason": "class_placeholder_link",
                                 "detail": "Markdown destination is #", "sha256": page_digest,
                                 "version": fm.get("version")})
            continue
        reasons = []
        missing = [k for k in contract.get("required_fields", []) if fm.get(k) in (None, "", [], {})]
        if missing:
            reasons.append(("required_field_missing", ", ".join(missing)))
        meaningful = len("".join(body.split()))
        minimum = int((contract.get("body") or {}).get("min_non_whitespace") or 0)
        if (contract.get("body") or {}).get("required") and not meaningful:
            reasons.append(("body_required", "empty body"))
        elif meaningful < minimum:
            reasons.append(("body_too_short", f"{meaningful} < {minimum}"))
        if contract.get("placeholder_links") == "reject" and _PLACEHOLDER.search(body):
            reasons.append(("placeholder_link", "Markdown destination is #"))
        if contract.get("unresolved_links") == "reject":
            bad = []
            for target in _LINK.findall(body):
                target = target.strip().strip("/").removesuffix(".md")
                base = target.rsplit("/", 1)[-1]
                # Canonical refs omit physical shard directories (entities/q/qilin is linked as
                # entities/qilin). A unique basename is therefore resolvable even when exact rel
                # differs; ambiguous basenames remain findings.
                if target not in rels and len(by_base.get(base, set())) != 1:
                    bad.append(target)
            if bad:
                reasons.append(("unresolved_link", ", ".join(dict.fromkeys(bad))))
        for field in contract.get("required_relationships", []):
            values = fm.get(field)
            values = values if isinstance(values, list) else ([values] if values else [])
            if not values:
                reasons.append(("required_relationship_missing", field))
        for reason, detail in reasons:
            try:
                page_digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            findings.append({"scope": "page", "path": rel, "lane": lane or "unattributed",
                             "type": page_type, "reason": reason, "detail": detail,
                             "sha256": page_digest,
                             "version": fm.get("version")})
    stamp = now or datetime.now(timezone.utc).isoformat()
    grouped = {}
    for f in findings:
        key = f"{f.get('lane') or 'unattributed'}|{f.get('type') or '-'}|{f['reason']}"
        grouped[key] = grouped.get(key, 0) + 1
    lane_ready = {name: not any(f.get("lane") == name for f in findings) for name in contracts}
    return {"api": 1, "last_successful_audit": stamp, "findings": findings,
            "counts": {"total": len(findings), "by_lane_type_reason": grouped},
            "strict_readiness": lane_ready}


def repair_plan(report: dict) -> dict:
    actions = []
    for finding in report.get("findings", []):
        if finding.get("scope") != "page":
            continue
        action = "recompile-from-declared-raw" if finding["reason"] in {
            "body_required", "body_too_short", "required_field_missing"} else "quarantine-for-review"
        actions.append({"path": finding["path"], "expected_sha256": finding["sha256"],
                        "expected_version": finding.get("version"), "action": action,
                        "fabricate_evidence": False})
    return {"api": 1, "dry_run": True, "actions": actions}


def readiness_alerts(report: dict, receipt_counts: dict | None = None, *,
                     now: datetime | None = None, max_age_hours: int = 26,
                     min_acceptance_rate: float = 0.8) -> list[dict]:
    alerts = []
    now = now or datetime.now(timezone.utc)
    try:
        audited = datetime.fromisoformat(report["last_successful_audit"].replace("Z", "+00:00"))
        age = (now - audited).total_seconds() / 3600
        if age > max_age_hours:
            alerts.append({"reason": "audit_stale", "age_hours": round(age, 1)})
    except (KeyError, TypeError, ValueError):
        alerts.append({"reason": "audit_missing"})
    counts = receipt_counts or {}
    selected, accepted = int(counts.get("selected") or 0), int(counts.get("accepted") or 0)
    if selected and accepted / selected < min_acceptance_rate:
        alerts.append({"reason": "acceptance_regression", "selected": selected,
                       "accepted": accepted, "rate": accepted / selected})
    if int(counts.get("undisposed") or 0):
        alerts.append({"reason": "undisposed_inputs", "count": int(counts["undisposed"])})
    return alerts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vault", type=Path)
    parser.add_argument("jobs", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    report = audit(args.vault, args.jobs)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    else:
        print(text, end="")
    if args.plan:
        args.plan.write_text(json.dumps(repair_plan(report), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
