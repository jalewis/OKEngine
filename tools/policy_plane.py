#!/usr/bin/env python3
"""Canonical OKEngine policy catalog, composition, findings, and audit adapters.

The catalog says *what* must hold and where it must be enforced.  Evaluators stay
small Python functions at the authoritative boundary (write server, importer,
corpus audit, deploy validation, Cockpit).  This avoids both prompt-only policy
and a monolithic validator while giving every decision one stable rule identity.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

import yaml

SCHEMA_VERSION = 1
OUTCOMES = {"pass", "warn", "reject", "waived", "not-applicable"}
SEVERITIES = {"info", "warning", "review", "reject"}
ENFORCEMENT_POINTS = {"write", "importer", "audit", "ci", "deploy", "cockpit"}
EVALUATORS = {
    "field-capability", "strict-type-namespace", "source-metadata-completeness",
    "importer-envelope", "page-quality-finding", "policy-digest",
}
BODY_MODES = {"allow", "append-only", "deny"}
OPERATIONS = {"create", "update", "patch", "append", "tombstone", "converge",
              "flag", "review", "import"}
_SEVERITY_RANK = {"info": 0, "warning": 1, "review": 2, "reject": 3}
_RULE_REQUIRED = {
    "id", "owner", "description", "severity", "applies_to", "enforcement",
    "evaluator", "remediation", "override", "verified_by",
}
_CAPABILITY_KEYS = {
    "rule_id", "operations", "paths", "types", "update_fields",
    "required_fields", "protected_fields", "body",
}


class PolicyError(ValueError):
    """The policy source cannot be composed safely."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def engine_catalog_path() -> Path:
    override = os.environ.get("OKENGINE_POLICY_CATALOG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "config" / "policy" / "catalog.yaml"


def load_document(path: Path) -> dict:
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"cannot load policy {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyError(f"policy {path} must be a mapping")
    return value


def _list_of_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def validate_capability(actor: str, capability: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(capability, dict):
        return [f"capability {actor}: must be a mapping"]
    unknown = sorted(set(capability) - _CAPABILITY_KEYS)
    if unknown:
        errors.append(f"capability {actor}: unknown keys {unknown}")
    for key in ("operations", "paths", "types", "update_fields", "required_fields",
                "protected_fields"):
        if key in capability and not _list_of_strings(capability[key]):
            errors.append(f"capability {actor}.{key}: must be a list of non-empty strings")
    unknown_ops = sorted(set(capability.get("operations") or []) - OPERATIONS)
    if unknown_ops:
        errors.append(f"capability {actor}.operations: unknown operations {unknown_ops}")
    if capability.get("body", "deny") not in BODY_MODES:
        errors.append(f"capability {actor}.body: must be one of {sorted(BODY_MODES)}")
    if not isinstance(capability.get("rule_id"), str) or not capability.get("rule_id"):
        errors.append(f"capability {actor}.rule_id: required stable rule ID")
    overlap = set(capability.get("update_fields") or []) & set(capability.get("protected_fields") or [])
    if overlap:
        errors.append(f"capability {actor}: fields both allowed and protected: {sorted(overlap)}")
    required_outside = (set(capability.get("required_fields") or []) -
                        set(capability.get("update_fields") or []))
    if required_outside:
        errors.append(f"capability {actor}: required fields are not allowed: {sorted(required_outside)}")
    return errors


def validate_document(document: dict, *, source: str = "policy") -> list[str]:
    errors: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{source}: schema_version must be {SCHEMA_VERSION}")
    rules = document.get("rules")
    if not isinstance(rules, list):
        errors.append(f"{source}: rules must be a list")
        rules = []
    seen: set[str] = set()
    for index, rule in enumerate(rules):
        ctx = f"{source}: rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{ctx} must be a mapping")
            continue
        missing = sorted(_RULE_REQUIRED - set(rule))
        if missing:
            errors.append(f"{ctx} missing {missing}")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            errors.append(f"{ctx}.id must be a non-empty string")
        elif rule_id in seen:
            errors.append(f"{source}: duplicate rule ID {rule_id}")
        else:
            seen.add(rule_id)
        if rule.get("severity") not in SEVERITIES:
            errors.append(f"{ctx}.severity must be one of {sorted(SEVERITIES)}")
        if rule.get("evaluator") not in EVALUATORS:
            errors.append(f"{ctx}.evaluator unknown: {rule.get('evaluator')!r}")
        enforcement = rule.get("enforcement")
        if not _list_of_strings(enforcement):
            errors.append(f"{ctx}.enforcement must be a non-empty string list")
        else:
            unknown = sorted(set(enforcement) - ENFORCEMENT_POINTS)
            if unknown:
                errors.append(f"{ctx}.enforcement has unknown targets {unknown}")
        verified = rule.get("verified_by")
        if not _list_of_strings(verified):
            errors.append(f"{ctx}.verified_by must be a non-empty string list")
        elif isinstance(enforcement, list):
            missing_coverage = sorted(set(enforcement) - set(verified))
            if missing_coverage:
                errors.append(f"{ctx}.verified_by lacks enforcement coverage {missing_coverage}")
        if rule.get("override") not in {"forbidden", "tighten-only", "waivable"}:
            errors.append(f"{ctx}.override must be forbidden, tighten-only, or waivable")
        if not isinstance(rule.get("applies_to"), dict):
            errors.append(f"{ctx}.applies_to must be a mapping")
        for key in ("owner", "description", "remediation"):
            if not isinstance(rule.get(key), str) or not rule.get(key, "").strip():
                errors.append(f"{ctx}.{key} must be a non-empty string")
    capabilities = document.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        errors.append(f"{source}: capabilities must be a mapping")
    else:
        for actor, capability in capabilities.items():
            errors.extend(validate_capability(str(actor), capability))
            if isinstance(capability, dict) and capability.get("rule_id") not in seen:
                errors.append(f"capability {actor}: unknown rule_id {capability.get('rule_id')!r}")
    waivers = document.get("waivers") or []
    if not isinstance(waivers, list):
        errors.append(f"{source}: waivers must be a list")
    else:
        for index, waiver in enumerate(waivers):
            ctx = f"{source}: waivers[{index}]"
            required = {"rule_id", "owner", "reason", "scope", "created_at", "expires_at"}
            if not isinstance(waiver, dict):
                errors.append(f"{ctx} must be a mapping")
            else:
                missing = sorted(required - set(waiver))
                if missing:
                    errors.append(f"{ctx} missing {missing}")
    return errors


def discover_documents(vault: Path, extra: Iterable[Path] = ()) -> list[Path]:
    paths = [engine_catalog_path()]
    for candidate in (vault / "policy.yaml", vault / ".okengine" / "policy.yaml"):
        if candidate.is_file():
            paths.append(candidate)
    for root in (vault / "extensions", vault / ".okengine" / "extensions"):
        if root.is_dir():
            # glob-ok: extension IDs are one flat directory tier; policy files are not content pages.
            paths.extend(sorted(root.glob("*/policy.yaml")))
    paths.extend(Path(p) for p in extra)
    return paths


def compose_documents(paths: Iterable[Path]) -> dict:
    effective = {"schema_version": SCHEMA_VERSION, "rules": [], "capabilities": {},
                 "waivers": [], "sources": []}
    by_id: dict[str, dict] = {}
    for order, path in enumerate(paths):
        document = load_document(Path(path))
        errors = validate_document(document, source=str(path))
        if errors:
            raise PolicyError("\n".join(errors))
        effective["sources"].append(str(path))
        for rule in document.get("rules") or []:
            rule = dict(rule)
            rule["source"] = str(path)
            rule["composition_order"] = order
            existing = by_id.get(rule["id"])
            if existing is None:
                by_id[rule["id"]] = rule
                continue
            if existing.get("override") == "forbidden":
                raise PolicyError(f"rule {rule['id']} is non-overridable ({existing['source']})")
            if rule.get("evaluator") != existing.get("evaluator"):
                raise PolicyError(f"rule {rule['id']} changes evaluator while composing")
            if _SEVERITY_RANK[rule["severity"]] < _SEVERITY_RANK[existing["severity"]]:
                raise PolicyError(f"rule {rule['id']} weakens severity while composing")
            by_id[rule["id"]] = rule
        for actor, capability in (document.get("capabilities") or {}).items():
            if actor in effective["capabilities"]:
                raise PolicyError(f"ambiguous duplicate capability for actor {actor}")
            effective["capabilities"][actor] = dict(capability)
        effective["waivers"].extend(document.get("waivers") or [])
    effective["rules"] = [by_id[key] for key in sorted(by_id)]
    rules = {r["id"]: r for r in effective["rules"]}
    for waiver in effective["waivers"]:
        rule = rules.get(waiver.get("rule_id"))
        if rule is None:
            raise PolicyError(f"waiver references unknown rule {waiver.get('rule_id')!r}")
        if rule.get("override") != "waivable":
            raise PolicyError(f"rule {rule['id']} does not permit waivers")
    effective["digest"] = policy_digest(effective)
    return effective


def policy_digest(policy: dict) -> str:
    # Provenance paths and load ordinals are useful in the effective artifact,
    # but they are deployment-local metadata: the same catalog lives under a
    # host checkout path during deploy and /opt/hermes in the gateway. Hash only
    # authorization semantics so identical composed policy has one stable digest.
    semantic_rules = []
    for rule in policy.get("rules") or []:
        semantic_rules.append({key: value for key, value in rule.items()
                               if key not in {"source", "composition_order"}})
    material = {
        "schema_version": policy.get("schema_version"),
        "rules": semantic_rules,
        "capabilities": policy.get("capabilities"),
        "waivers": policy.get("waivers"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def effective_policy(vault: Path | None = None) -> dict:
    vault = Path(vault or os.environ.get("WIKI_PATH") or "/opt/vault")
    return compose_documents(discover_documents(vault))


def finding(rule_id: str, outcome: str, severity: str, subject: str, operation: str,
            actor: str, message: str, remediation: str, evidence: dict | None = None,
            *, enforcement_point: str = "write", evaluated_at: str | None = None) -> dict:
    if outcome not in OUTCOMES:
        raise PolicyError(f"invalid finding outcome {outcome}")
    return {
        "rule_id": rule_id, "outcome": outcome, "severity": severity,
        "subject": subject, "operation": operation, "actor": actor,
        "message": message, "remediation": remediation,
        "evidence": evidence or {}, "enforcement_point": enforcement_point,
        "evaluated_at": evaluated_at or utc_now(),
    }


def finding_message(result: dict) -> str:
    fields = result.get("evidence", {}).get("offending_fields") or []
    missing = result.get("evidence", {}).get("missing_fields") or []
    suffix = f"; offending fields: {', '.join(fields)}" if fields else ""
    suffix += f"; missing fields: {', '.join(missing)}" if missing else ""
    return (f"policy[{result['rule_id']}] {result['message']}{suffix}. "
            f"Remediation: {result['remediation']}")


def _path_in_scopes(rel_path: str, scopes: list[str]) -> bool:
    # Kept dependency-free so importer/audit tools do not import the MCP server.
    from fnmatch import fnmatch
    rel = rel_path.lstrip("/")
    if rel.endswith(".md"):
        rel = rel[:-3]
    for raw in scopes:
        pat = str(raw).split(":", 1)[-1].lstrip("/")
        if pat.startswith("wiki/"):
            pat = pat[5:]
        if pat in {"", "*", "**"}:
            return True
        prefix = pat.rstrip("*").rstrip("/")
        if (prefix and (rel == prefix or rel.startswith(prefix + "/"))) or fnmatch(rel, pat):
            return True
    return False


def evaluate_capability(policy: dict, actor: str, operation: str, subject: str,
                        page_type: str = "", changed_fields: Iterable[str] = (),
                        body_change: str = "none") -> dict | None:
    """Return a reject finding, or None. Admin callers bypass by identity, not by YAML."""
    if actor == "admin":
        return None
    capability = (policy.get("capabilities") or {}).get(actor)
    if not isinstance(capability, dict):
        return finding("engine-authenticated-writer", "reject", "reject", subject, operation,
                       actor, "caller has no declared write capability",
                       "Use an authenticated administrative caller or declare a least-privilege capability")
    capability_errors = validate_capability(actor, capability)
    rule_id = capability.get("rule_id")
    known_rule_ids = {r.get("id") for r in policy.get("rules", [])}
    if rule_id not in known_rule_ids:
        capability_errors.append(f"capability {actor}: unknown rule_id {rule_id!r}")
    if capability_errors:
        return finding(
            "engine-authenticated-writer", "reject", "reject", subject, operation, actor,
            "caller capability is invalid or references an unknown rule",
            "Fix and validate the server-side capability declaration before retrying",
            {"capability_errors": capability_errors},
        )
    rule = next((r for r in policy.get("rules", []) if r.get("id") == rule_id), {})
    remediation = rule.get("remediation") or "Narrow the write to the declared capability"
    reasons: list[str] = []
    if operation not in set(capability.get("operations") or []):
        reasons.append(f"operation {operation!r} is not allowed")
    if not _path_in_scopes(subject, capability.get("paths") or []):
        reasons.append("path is outside allowed scopes")
    allowed_types = set(capability.get("types") or [])
    if allowed_types and page_type not in allowed_types:
        reasons.append(f"page type {page_type!r} is not allowed")
    changed = set(str(field) for field in changed_fields)
    protected = changed & set(capability.get("protected_fields") or [])
    allowlist = set(capability.get("update_fields") or [])
    field_gated_operations = {"create", "update", "patch", "converge", "tombstone"}
    outside = changed - allowlist if operation in field_gated_operations else set()
    missing_required = (set(capability.get("required_fields") or []) - changed
                        if operation in field_gated_operations else set())
    if protected:
        reasons.append("protected fields would change")
    if outside:
        reasons.append("fields exceed the update allowlist")
    if missing_required:
        reasons.append("required candidate fields are missing")
    body_mode = capability.get("body", "deny")
    if body_change != "none" and body_mode == "deny":
        reasons.append("body mutation is denied")
    if body_change == "replace" and body_mode == "append-only":
        reasons.append("body replacement exceeds append-only authority")
    if not reasons:
        return None
    return finding(rule_id, "reject", rule.get("severity", "reject"), subject, operation,
                   actor, "; ".join(reasons), remediation,
                   {"offending_fields": sorted(protected | outside),
                    "missing_fields": sorted(missing_required),
                    "allowed_operations": capability.get("operations") or [],
                    "allowed_paths": capability.get("paths") or [],
                    "allowed_types": capability.get("types") or [],
                    "allowed_fields": capability.get("update_fields") or [],
                    "required_fields": capability.get("required_fields") or [],
                    "body": body_mode})


def validate_importer_envelope(envelope: dict, *, actor: str = "source-connector") -> dict | None:
    required = {"connector_id", "source_native_id", "source_revision", "observed_at",
                "source_authority", "source_permission", "data_sensitivity", "payload"}
    missing = sorted(key for key in required if envelope.get(key) in (None, ""))
    if not missing:
        return None
    return finding("engine-importer-envelope", "reject", "reject",
                   str(envelope.get("source_native_id") or "<unknown>"), "import", actor,
                   "normalized importer envelope is incomplete",
                   "Populate stable identity, revision, provenance, observation time, and payload",
                   {"missing_fields": missing}, enforcement_point="importer")


def append_event(vault: Path, result: dict) -> None:
    path = Path(vault) / ".okengine" / "policy-events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")


def coverage(policy: dict) -> dict:
    rows = []
    for rule in policy.get("rules", []):
        declared = sorted(rule.get("enforcement") or [])
        verified = sorted(set(rule.get("verified_by") or []))
        rows.append({"rule_id": rule["id"], "owner": rule["owner"],
                     "declared": declared, "verified_by": verified,
                     "covered": bool(verified) and all(point in verified for point in declared),
                     "source": rule.get("source")})
    return {"schema_version": 1, "policy_digest": policy["digest"],
            "generated_at": utc_now(), "rules": rows}


def _read_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---\n"):
            return {}
        raw = text.split("\n---", 1)[0][4:]
        value = yaml.safe_load(raw) or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def audit(vault: Path, policy: dict) -> list[dict]:
    wiki = vault / "wiki"
    results: list[dict] = []
    for path in sorted(wiki.rglob("*.md")) if wiki.is_dir() else []:
        rel = path.relative_to(wiki).as_posix()
        fm = _read_frontmatter(path)
        if rel.startswith("sources/") and fm.get("type") == "source":
            missing = [key for key in ("publisher", "published") if fm.get(key) in (None, "", "undefined")]
            if missing:
                results.append(finding(
                    "engine-source-metadata-complete", "warn", "review", rel, "audit",
                    "scheduled-policy-audit", "source metadata is incomplete",
                    "Recover publisher and publication time from the locally captured source",
                    {"missing_fields": missing}, enforcement_point="audit"))
        if not fm.get("type") and not rel.startswith(("operational/", "dashboards/")):
            results.append(finding(
                "engine-page-quality-review", "warn", "review", rel, "audit",
                "scheduled-policy-audit", "knowledge page has no type",
                "Classify the page under the composed schema or quarantine it",
                {"missing_fields": ["type"]}, enforcement_point="audit"))
    event_path = vault / ".okengine" / "policy-events.jsonl"
    if event_path.is_file():
        for line in event_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("outcome") in {"reject", "warn"}:
                results.append(event)
    return results


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def materialize(vault: Path, *, run_audit: bool = False) -> dict:
    policy = effective_policy(vault)
    cov = coverage(policy)
    state = vault / ".okengine"
    _atomic_json(state / "effective-policy.json", policy)
    _atomic_json(state / "policy-coverage.json", cov)
    findings = audit(vault, policy) if run_audit else []
    if run_audit:
        _atomic_json(state / "policy-findings.json", {
            "schema_version": 1, "policy_digest": policy["digest"],
            "generated_at": utc_now(), "findings": findings,
        })
        _write_dashboard(vault, policy, cov, findings)
    return {"digest": policy["digest"], "rules": len(policy["rules"]),
            "findings": len(findings)}


def _write_dashboard(vault: Path, policy: dict, cov: dict, findings: list[dict]) -> None:
    active_waivers = []
    now = utc_now()
    for waiver in policy.get("waivers") or []:
        active_waivers.append((waiver, str(waiver.get("expires_at")) >= now))
    counts: dict[str, int] = {}
    for item in findings:
        counts[item.get("outcome", "unknown")] = counts.get(item.get("outcome", "unknown"), 0) + 1
    lines = [
        "---", "type: dashboard", "id: dashboard:policy-health", "title: Policy health",
        f"updated: {utc_now()}", "---", "", "# Policy health", "",
        f"Policy digest: `{policy['digest']}`", "",
        f"Rules: **{len(policy['rules'])}** · fully covered: **{sum(1 for r in cov['rules'] if r['covered'])}** "
        f"· findings: **{len(findings)}** · waivers: **{len(active_waivers)}**", "",
        "## Enforcement coverage", "",
        "| Rule | Declared | Verified | Status |", "|---|---|---|---|",
    ]
    for row in cov["rules"]:
        lines.append(f"| `{row['rule_id']}` | {', '.join(row['declared'])} | "
                     f"{', '.join(row['verified_by']) or 'none'} | "
                     f"{'covered' if row['covered'] else 'gap'} |")
    lines += ["", "## Recent findings", ""]
    if findings:
        lines += ["| Outcome | Rule | Subject | Message |", "|---|---|---|---|"]
        for item in findings[-100:]:
            message = str(item.get("message") or "").replace("|", "\\|")
            lines.append(f"| {item.get('outcome')} | `{item.get('rule_id')}` | "
                         f"`{item.get('subject')}` | {message} |")
    else:
        lines.append("No active findings in this audit snapshot.")
    lines += ["", "## Waivers", ""]
    if active_waivers:
        for waiver, active in active_waivers:
            lines.append(f"- `{waiver['rule_id']}` — {'active' if active else 'EXPIRED'}; "
                         f"{waiver['reason']} (expires {waiver['expires_at']})")
    else:
        lines.append("No waivers declared.")
    target = vault / "wiki" / "operational" / "policy-health.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def check_prompt(policy: dict, actor: str, prompt: str) -> list[str]:
    capability = (policy.get("capabilities") or {}).get(actor)
    if not capability:
        return [f"no capability for {actor}"]
    errors = []
    for field in capability.get("update_fields") or []:
        if field not in prompt:
            errors.append(f"prompt does not name allowed field {field}")
    if capability.get("body") == "deny" and "NO body" not in prompt:
        errors.append("prompt does not state NO body for body-denied capability")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OKEngine policy-plane validator and audit")
    parser.add_argument("command", choices=("validate", "materialize", "audit", "digest"))
    parser.add_argument("--vault", default=os.environ.get("WIKI_PATH") or ".")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            policy = effective_policy(Path(args.vault))
            print(json.dumps({"ok": True, "digest": policy["digest"],
                              "rules": len(policy["rules"])}))
        elif args.command == "digest":
            print(effective_policy(Path(args.vault))["digest"])
        else:
            result = materialize(Path(args.vault), run_audit=args.command == "audit")
            print(json.dumps(result, sort_keys=True))
        return 0
    except PolicyError as exc:
        print(f"policy invalid: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
