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
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

# Cosmic Ray's ExceptionReplacer cannot mutate an Attribute node inside a
# tuple-valued except clause. Bind the same class to a Name so malformed YAML
# remains a first-class, mutation-testable error path rather than an
# INCOMPETENT runner result.
YamlError = yaml.YAMLError

SCHEMA_VERSION = 1
OUTCOMES = {"pass", "warn", "reject", "waived", "not-applicable"}
SEVERITIES = {"info", "warning", "review", "reject"}
ENFORCEMENT_POINTS = {"write", "importer", "audit", "ci", "deploy", "cockpit"}
EVALUATORS = {
    "field-capability", "strict-type-namespace", "source-metadata-completeness",
    "importer-envelope", "page-quality-finding", "policy-digest",
}
AUDIT_EVALUATORS = {
    "strict-type-namespace", "source-metadata-completeness", "page-quality-finding",
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
    "required_fields", "protected_fields", "body", "forbidden_values",
}
# okengine#1032 [6]: the identity-edit grant a networked extension's token carries (#1028). Valid on
# a CALLER capability only (`validate_capability(..., caller=True)`): the write path reads the grant
# from the token store, which `extensions enable`/deploy fill from the manifest under the tier and
# acceptance gate (#1032 [12]). A composed policy document is never read for one, so declaring it
# there stays an unknown key rather than a silent no-op.
_CALLER_CAPABILITY_KEYS = _CAPABILITY_KEYS | {"identity_edit"}
_IDENTITY_EDIT_KEYS = {"fields", "retype_from"}
# Body authority from least to most. An ordering, so position is the only thing that matters.
_BODY_ORDER = ("deny", "append-only", "allow")

# okengine#983: a model-facing chat writer (`chat:<platform>`) is the one actor class a pack may
# REPLACE, and only an engine-declared one, and only inside this ceiling. Every other duplicate
# capability stays an ambiguity error (okengine#985 tracks the general cross-lane grant problem).
CHAT_ACTOR_PREFIX = "chat:"
CHAT_CEILING_OPERATIONS = frozenset({"create", "update", "append"})
# The most body authority a chat capability may hold per operation: write a body when creating a
# page, append to an existing one, never replace an existing page's body.
CHAT_CEILING_BODY = {"create": "allow", "update": "deny", "append": "append-only"}
# Namespaces a chat writer may never reach: lane-owned evidence and judgments, generated views,
# and engine runtime state. Top-level files, glob namespaces and `_`/`.` paths are refused too.
CHAT_FORBIDDEN_NAMESPACES = frozenset({
    "sources", "raw", "assessments", "dashboards", "operational", "system", "reviews",
})
# okengine#993: the `status` values the engine treats as RETIRED. `tombstoned` is the engine-wide
# spelling (scripts/cron/okf_migrate.py `is_tombstoned`, pinned by tier_lib and the write path's
# `_tombstone_refuse`); `superseded` and `retired` are retracted judgments (corpus_audit_domain's
# `_judgment_status`, and the cockpit's current-evidence filter in cockpit_services/reviews.py).
# Every consumer that hides a retired record hides a page carrying one, so a chat writer may set an
# ordinary status but never one of these. `deprecated` is deliberately absent: packs use it as an
# ordinary status (an ATT&CK-deprecated group stays a live page) and no engine consumer retires it.
RETIREMENT_STATUSES = frozenset({"tombstoned", "superseded", "retired"})
CHAT_CEILING_FORBIDDEN_VALUES = {"status": RETIREMENT_STATUSES}


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
    except (OSError, YamlError) as exc:
        raise PolicyError(f"cannot load policy {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PolicyError(f"policy {path} must be a mapping")
    return value


def _list_of_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _identity_edit_errors(actor: str, grant: Any) -> list[str]:
    """The grant's shape, as `identity_guard.permission` reads it. Which fields it may name, and that
    a creating caller holds none, are the manifest validator's and the write path's to decide."""
    where = f"capability {actor}.identity_edit"
    if not isinstance(grant, dict):
        return [f"{where}: must be a mapping"]
    errors = []
    unknown = sorted(set(grant) - _IDENTITY_EDIT_KEYS)
    if unknown:
        errors.append(f"{where}: unknown keys {unknown}")
    if not _list_of_strings(grant.get("fields")) or not grant.get("fields"):
        errors.append(f"{where}.fields: must be a non-empty list of non-empty strings")
    if "retype_from" in grant and not _list_of_strings(grant["retype_from"]):
        errors.append(f"{where}.retype_from: must be a list of non-empty strings")
    return errors


def validate_capability(actor: str, capability: dict, *, caller: bool = False) -> list[str]:
    """`caller` validates the capability an authenticated caller presents (a networked extension's
    token record), which may carry an `identity_edit` grant; a policy document's may not."""
    errors: list[str] = []
    if not isinstance(capability, dict):
        return [f"capability {actor}: must be a mapping"]
    unknown = sorted(set(capability) - (_CALLER_CAPABILITY_KEYS if caller else _CAPABILITY_KEYS))
    if unknown:
        errors.append(f"capability {actor}: unknown keys {unknown}")
    if caller and "identity_edit" in capability:
        errors.extend(_identity_edit_errors(actor, capability["identity_edit"]))
    for key in ("operations", "paths", "types", "update_fields", "required_fields",
                "protected_fields"):
        if key in capability and not _list_of_strings(capability[key]):
            errors.append(f"capability {actor}.{key}: must be a list of non-empty strings")
    unknown_ops = sorted(set(capability.get("operations") or []) - OPERATIONS)
    if unknown_ops:
        errors.append(f"capability {actor}.operations: unknown operations {unknown_ops}")
    body = capability.get("body", "deny")
    if isinstance(body, dict):
        # Per-operation body authority (okengine#983). An operation the mapping omits is denied.
        unknown_body_ops = sorted(str(op) for op in set(body) - OPERATIONS)
        if unknown_body_ops:
            errors.append(f"capability {actor}.body: unknown operations {unknown_body_ops}")
        if any(mode not in BODY_MODES for mode in body.values()):
            errors.append(f"capability {actor}.body: modes must be one of {sorted(BODY_MODES)}")
    elif body not in BODY_MODES:
        errors.append(f"capability {actor}.body: must be one of {sorted(BODY_MODES)} "
                      "or a mapping of operation to mode")
    if "forbidden_values" in capability and not _forbidden_values_valid(
            capability["forbidden_values"]):
        errors.append(f"capability {actor}.forbidden_values: must map each field name to a "
                      "non-empty list of non-empty strings")
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


def _forbidden_values_valid(value: Any) -> bool:
    return (isinstance(value, dict) and all(
        isinstance(field, str) and field and isinstance(values, list) and _list_of_strings(values)
        and values for field, values in value.items()))


def value_key(value: Any) -> str:
    """How a forbidden value compares: case- and whitespace-insensitively, exactly as
    `okf_migrate.is_tombstoned` reads `status`."""
    return str(value).strip().casefold()


def forbidden_value_sets(capability: dict) -> dict[str, frozenset]:
    """The capability's `forbidden_values`, each list folded by `value_key`."""
    return {field: frozenset(value_key(v) for v in values)
            for field, values in (capability.get("forbidden_values") or {}).items()}


def _value_keys(value: Any) -> frozenset:
    return frozenset(value_key(v) for v in (value if isinstance(value, list) else [value])
                     if v is not None)


def _forbidden_value_hits(capability: dict, changed: set, values: Any,
                          stored: Any) -> list[tuple[str, str]]:
    """Why a write sets a forbidden value, judged only on fields the write moves: an echo of a
    page's stored value is not a move to it, and removing the field sets nothing. A field moves
    when the write changes it, or when what the write stores (`values`, after its aliasing)
    differs from the stored value as consumers read it (`stored`, raw): a legacy `status: gone`
    that a value alias maps to `retired` is moved by an update that never names `status`. A
    changed field whose resulting value the caller did not supply fails closed. Returns
    (field, reason) pairs."""
    hits = []
    for field, forbidden in sorted(forbidden_value_sets(capability).items()):
        moved = (stored is not None and values is not None and field in values
                 and _value_keys(values[field]) != _value_keys(stored.get(field)))
        if field not in changed and not moved:
            continue
        if values is None:
            hits.append((field, f"field {field!r} cannot be checked against its forbidden values"))
            continue
        if field not in values:
            continue
        sent = values[field]
        for item in sent if isinstance(sent, list) else [sent]:
            if item is not None and value_key(item) in forbidden:
                hits.append((field, f"field {field!r} may not be set to {item!r}"))
    return hits


def body_mode(capability: dict, operation: str) -> str:
    """The body authority a capability grants one operation. A single string applies to every
    operation (the original form); a mapping is per operation and denies what it omits."""
    body = capability.get("body", "deny")
    if isinstance(body, dict):
        return str(body.get(operation, "deny"))
    return str(body)


def _chat_path_error(pattern: str) -> Optional[str]:
    if ":" in str(pattern):
        return f"path {pattern!r} must be a plain wiki-relative path"
    rel = str(pattern).lstrip("/")
    if rel.startswith("wiki/"):
        rel = rel[5:]
    namespace, slash, _rest = rel.partition("/")
    if not slash or not namespace:
        return f"path {pattern!r} must name a namespace (`<namespace>/**`)"
    if any(ch in namespace for ch in "*?[]"):
        return f"path {pattern!r} has a glob namespace"
    if _forbidden_chat_segment(namespace):
        return f"path {pattern!r} reaches the forbidden namespace {namespace!r}"
    # Below the first segment the pattern may name a walk-up sub-domain's namespace
    # (`<sub-domain>/sources/`), or a shard directory (`entities/t/_/`). Only a reserved namespace
    # NAME is refused there; `_` is the shard bucket for a non-alphanumeric letter, and a
    # sub-domain's `_`/`.` paths are refused at write time against the resolved namespace.
    for segment in rel.split("/")[1:]:
        if segment in CHAT_FORBIDDEN_NAMESPACES:
            return f"path {pattern!r} reaches the forbidden namespace {segment!r}"
    return None


def _forbidden_chat_segment(segment: str) -> bool:
    return segment.startswith(("_", ".")) or segment in CHAT_FORBIDDEN_NAMESPACES


def chat_namespace_error(namespace: str) -> Optional[str]:
    """Why a chat write into this namespace is outside the ceiling, or None.

    `namespace` is the page's namespace with any walk-up sub-domain containers in front
    (`okcti/sources`, or `entities` in a flat vault), as the write path resolves it through the
    governing schema. Every segment of it is checked: composition cannot tell a sub-domain scope
    (`okcti/**`) from a namespace scope (`entities/**`). Shard directories below the namespace
    (`entities/t/_/`) are never part of it (okengine#991)."""
    for segment in namespace.split("/"):
        if _forbidden_chat_segment(segment):
            return f"path reaches the forbidden namespace {segment!r}"
    return None


def chat_ceiling_errors(actor: str, capability: dict, floor: dict) -> list[str]:
    """Why a chat capability exceeds the engine ceiling, relative to the engine floor it replaces.

    Pure: the composer calls it for a pack replacement, and for the engine's own declaration
    (with itself as the floor) so the floor can never sit above its own ceiling."""
    errors = []
    extra_ops = sorted(set(capability.get("operations") or []) - CHAT_CEILING_OPERATIONS)
    if extra_ops:
        errors.append(f"operations {extra_ops} exceed the chat ceiling "
                      f"{sorted(CHAT_CEILING_OPERATIONS)}")
    for operation in sorted(OPERATIONS):
        granted = body_mode(capability, operation)
        ceiling = CHAT_CEILING_BODY.get(operation, "deny")
        if _BODY_ORDER.index(granted) > _BODY_ORDER.index(ceiling):
            errors.append(f"body {granted!r} on {operation} exceeds the chat ceiling {ceiling!r}")
    paths = capability.get("paths") or []
    if not paths:
        errors.append("paths must be declared: an empty scope is unbounded")
    for pattern in paths:
        error = _chat_path_error(pattern)
        if error:
            errors.append(error)
    missing = sorted(set(floor.get("protected_fields") or [])
                     - set(capability.get("protected_fields") or []))
    if missing:
        errors.append(f"must protect at least the engine floor's fields; missing {missing}")
    # okengine#993: the retirement statuses are the ceiling for every chat capability, the engine
    # floor's own value rules are the floor for a pack replacement; neither may be dropped.
    granted = forbidden_value_sets(capability)
    required = dict(CHAT_CEILING_FORBIDDEN_VALUES)
    for field, values in forbidden_value_sets(floor).items():
        required[field] = required.get(field, frozenset()) | values
    for field, values in sorted(required.items()):
        lacking = sorted(values - granted.get(field, frozenset()))
        if lacking:
            errors.append(f"must forbid at least the engine floor's values for field {field!r}; "
                          f"missing {lacking}")
    return [f"capability {actor}: {error}" for error in errors]


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
            # A pack's chat replacement names the ENGINE rule it is governed by, which lives in
            # another document; compose_documents resolves it against the composed rule set.
            chat = str(actor).startswith(CHAT_ACTOR_PREFIX)
            if isinstance(capability, dict) and not chat and capability.get("rule_id") not in seen:
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


def _tier1_root(module_root: Path, runtime_root: Path) -> Optional[Path]:
    """Where first-party extensions live for THIS process (okengine#875), or None to keep
    extension_discovery's own default. Intervene only when that default cannot work: its root has
    no `extensions/` but the runtime root the deploy stages them to does. In a gateway the baked
    /opt/hermes/scripts is chosen for discovery while /opt/hermes has no extensions/ -- they are
    staged to /opt/data/extensions -- so policy-audit failed "enabled extension ... not discovered
    in any tier" on every deployment that did not set OKENGINE_ENGINE_ROOT by hand, and the write
    path only worked because an earlier import happened to cache a runtime-rooted module. An
    explicit OKENGINE_ENGINE_ROOT is always honored."""
    if os.environ.get("OKENGINE_ENGINE_ROOT") or (module_root / "extensions").is_dir():
        return None
    return runtime_root if (runtime_root / "extensions").is_dir() else None


def discover_documents(vault: Path, extra: Iterable[Path] = ()) -> list[Path]:
    # Policy must consume the SAME effective enablement state as schema and
    # cron composition. A disabled extension keeps its files on disk; a bare
    # policy.yaml glob would silently retain revoked write capabilities.
    source_scripts = Path(__file__).resolve().parents[1] / "scripts"
    runtime_root = Path(os.environ.get("OKENGINE_DATA") or
                        os.environ.get("HERMES_HOME") or "/opt/data")
    runtime_scripts = runtime_root / "scripts"
    scripts_dir = (source_scripts if (source_scripts / "extension_discovery.py").is_file()
                   else runtime_scripts)
    if not (scripts_dir / "extension_discovery.py").is_file():
        raise PolicyError(f"extension discovery unavailable at {scripts_dir}")
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import extension_discovery

    if vault.resolve() == scripts_dir.parent.resolve():
        # CI validates the engine catalog with the source checkout as "vault".
        # Do not rediscover engine extensions a second time as pack-tier copies.
        records, discovery_errors = extension_discovery.discover(
            None, engine_root=scripts_dir.parent)
        effective_ids, state_errors = extension_discovery.effective_enabled(vault, records)
        resolved, resolution_errors = extension_discovery.resolve_enabled(
            sorted(effective_ids), records)
        errors = discovery_errors + state_errors + resolution_errors
    else:
        resolved, errors = extension_discovery.resolve_for_pack(
            vault, engine_root=_tier1_root(
                Path(getattr(extension_discovery, "ENGINE_ROOT", scripts_dir.parent)), runtime_root))
    if errors:
        raise PolicyError("extension discovery/enablement failed: " + "; ".join(errors))
    paths = [engine_catalog_path()]
    for candidate in (vault / "policy.yaml", vault / ".okengine" / "policy.yaml"):
        if candidate.is_file():
            paths.append(candidate)
    for ext_id in sorted(resolved):
        candidate = Path(resolved[ext_id]["dir"]) / "policy.yaml"
        if candidate.is_file():
            paths.append(candidate)
    paths.extend(Path(p) for p in extra)
    return paths


def compose_documents(paths: Iterable[Path]) -> dict:
    effective = {"schema_version": SCHEMA_VERSION, "rules": [], "capabilities": {},
                 "waivers": [], "sources": []}
    by_id: dict[str, dict] = {}
    engine_chat: dict[str, dict] = {}
    replaced: dict[str, str] = {}
    paths = list(paths)
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
        # The engine catalog is always composed first (discover_documents); identifying it by
        # position, not by path, keeps a host checkout and the baked image copy equivalent.
        is_engine = path is paths[0]
        for actor, capability in (document.get("capabilities") or {}).items():
            chat = str(actor).startswith(CHAT_ACTOR_PREFIX)
            if chat and is_engine:
                errors = chat_ceiling_errors(actor, capability, capability)
                if errors:
                    raise PolicyError("\n".join(errors))
                engine_chat[actor] = dict(capability)
            elif chat:
                # okengine#983: a pack may replace an ENGINE-declared chat capability, once,
                # inside the ceiling. A chat actor the engine never declared binds no writer.
                floor = engine_chat.get(actor)
                if floor is None:
                    raise PolicyError(f"capability {actor}: only an engine-declared chat "
                                      "capability may be replaced by pack policy")
                if actor in replaced:
                    raise PolicyError(f"ambiguous duplicate capability for actor {actor}: "
                                      f"already replaced by {replaced[actor]}")
                errors = chat_ceiling_errors(actor, capability, floor)
                if errors:
                    raise PolicyError("\n".join(errors))
                replaced[actor] = str(path)
                effective["capabilities"][actor] = dict(capability)
                continue
            if actor in effective["capabilities"]:
                raise PolicyError(f"ambiguous duplicate capability for actor {actor}")
            effective["capabilities"][actor] = dict(capability)
        effective["waivers"].extend(document.get("waivers") or [])
    effective["rules"] = [by_id[key] for key in sorted(by_id)]
    rules = {r["id"]: r for r in effective["rules"]}
    for actor in sorted(set(engine_chat) | set(replaced)):
        rule_id = effective["capabilities"][actor].get("rule_id")
        if rule_id not in rules:
            raise PolicyError(f"capability {actor}: unknown rule_id {rule_id!r}")
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
                        body_change: str = "none", namespace: Optional[str] = None,
                        values: Optional[dict] = None,
                        stored: Optional[dict] = None) -> dict | None:
    """Return a reject finding, or None. Admin callers bypass by identity, not by YAML.

    `namespace` is the subject's namespace as the write path resolves it, sub-domain prefix
    included (`okcti/entities`); the chat ceiling is applied to it. `values` is the frontmatter
    the write would store, after its own key and value aliasing, and `stored` the page as stored
    (None for a create); `forbidden_values` is judged on them (okengine#993)."""
    if actor == "admin":
        return None
    capability = (policy.get("capabilities") or {}).get(actor)
    if not isinstance(capability, dict):
        return finding("engine-authenticated-writer", "reject", "reject", subject, operation,
                       actor, "caller has no declared write capability",
                       "Use an authenticated administrative caller or declare a least-privilege capability")
    capability_errors = validate_capability(actor, capability, caller=True)
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
    if str(actor).startswith(CHAT_ACTOR_PREFIX):
        # Without a resolved namespace (a pure caller with no vault), the first segment is it.
        ceiling_error = chat_namespace_error(
            subject.split("/", 1)[0] if namespace is None else namespace)
        if ceiling_error:
            reasons.append(ceiling_error)
    allowed_types = set(capability.get("types") or [])
    # "*" admits any type, exactly as update_fields' "*" and a "**" path scope do (okengine#876).
    # Contract-derived capabilities pass `allowed_types: ["*"]` through literally, so without this
    # every "any type" engine lane had every write to a typed page refused.
    if allowed_types and "*" not in allowed_types and page_type not in allowed_types:
        reasons.append(f"page type {page_type!r} is not allowed")
    changed = set(str(field) for field in changed_fields)
    protected = changed & set(capability.get("protected_fields") or [])
    allowlist = set(capability.get("update_fields") or [])
    field_gated_operations = {"create", "update", "patch", "converge", "tombstone"}
    outside = (changed - allowlist if "*" not in allowlist and operation in field_gated_operations
               else set())
    missing_required = (set(capability.get("required_fields") or []) - changed
                        if operation in field_gated_operations else set())
    if protected:
        reasons.append("protected fields would change")
    if outside:
        reasons.append("fields exceed the update allowlist")
    if missing_required:
        reasons.append("required candidate fields are missing")
    value_hits = _forbidden_value_hits(capability, changed, values, stored)
    reasons.extend(reason for _field, reason in value_hits)
    valued = {field for field, _reason in value_hits}
    granted_body = body_mode(capability, operation)
    # Membership, not string comparison: the three modes and three changes are closed sets.
    if body_change != "none" and granted_body not in ("append-only", "allow"):
        reasons.append("body mutation is denied")
    if body_change not in ("none", "append") and granted_body == "append-only":
        reasons.append("body replacement exceeds append-only authority")
    if not reasons:
        return None
    evidence = {"offending_fields": sorted(protected | outside | valued),
                "missing_fields": sorted(missing_required),
                "allowed_operations": capability.get("operations") or [],
                "allowed_paths": capability.get("paths") or [],
                "allowed_types": capability.get("types") or [],
                "allowed_fields": capability.get("update_fields") or [],
                "required_fields": capability.get("required_fields") or [],
                "body": granted_body}
    if "forbidden_values" in capability:
        evidence["forbidden_values"] = capability["forbidden_values"]
    return finding(rule_id, "reject", rule.get("severity", "reject"), subject, operation,
                   actor, "; ".join(reasons), remediation, evidence)


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
        verified = set(rule.get("verified_by") or [])
        # Audit is executable evidence, not a self-attested YAML label. A rule may only claim it
        # when audit() has a registered evaluator for that rule's evaluator kind.
        if "audit" in verified and rule.get("evaluator") not in AUDIT_EVALUATORS:
            verified.remove("audit")
        verified = sorted(verified)
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


def _audit_schema(vault: Path) -> dict:
    """Load the same composed schema used by the write path, in source and baked layouts."""
    import importlib
    import sys
    candidates = (
        Path(__file__).resolve().parents[1] / "scripts" / "cron",
        Path("/opt/hermes/scripts/cron"),
        Path("/opt/data/scripts"),
    )
    for candidate in candidates:
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    try:
        return importlib.import_module("schema_lib").merged_schema(vault)
    except Exception:
        return {}


def _audit_rule_page(rule: dict, rel: str, fm: dict, schema: dict) -> dict | None:
    evaluator = rule.get("evaluator")
    remediation = rule.get("remediation") or "Repair the page and rerun the policy audit."
    namespace = rel.split("/", 1)[0]
    applies = rule.get("applies_to") or {}
    if applies.get("namespace") and applies["namespace"] != namespace:
        return None
    if applies.get("type") and applies["type"] != fm.get("type"):
        return None
    if evaluator == "source-metadata-completeness":
        missing = [key for key in ("publisher", "published")
                   if fm.get(key) in (None, "", "undefined")]
        if missing:
            return finding(rule["id"], "warn", rule.get("severity", "review"), rel, "audit",
                           "scheduled-policy-audit", "source metadata is incomplete", remediation,
                           {"missing_fields": missing}, enforcement_point="audit")
    elif evaluator == "page-quality-finding":
        if not fm.get("type") and not rel.startswith(("operational/", "dashboards/")):
            return finding(rule["id"], "warn", rule.get("severity", "review"), rel, "audit",
                           "scheduled-policy-audit", "knowledge page has no type", remediation,
                           {"missing_fields": ["type"]}, enforcement_point="audit")
    elif evaluator == "strict-type-namespace" and fm.get("type"):
        typ = str(fm["type"])
        types = schema.get("types") or {}
        try:
            import schema_lib
            home = schema_lib.type_home_namespace(schema, typ)
        except Exception:
            home = None
        reasons = []
        if typ not in types:
            reasons.append(f"type {typ!r} is not declared by the composed schema")
        if home and home != namespace:
            reasons.append(f"type {typ!r} belongs in {home!r}, not {namespace!r}")
        if reasons:
            return finding(rule["id"], "warn", rule.get("severity", "reject"), rel, "audit",
                           "scheduled-policy-audit", "; ".join(reasons), remediation,
                           {"actual_namespace": namespace, "expected_namespace": home,
                            "page_type": typ}, enforcement_point="audit")
    return None


def audit(vault: Path, policy: dict) -> list[dict]:
    wiki = vault / "wiki"
    results: list[dict] = []
    rules = [rule for rule in policy.get("rules", [])
             if "audit" in (rule.get("enforcement") or [])]
    schema = _audit_schema(vault) if any(
        rule.get("evaluator") == "strict-type-namespace" for rule in rules) else {}
    for path in sorted(wiki.rglob("*.md")) if wiki.is_dir() else []:
        rel = path.relative_to(wiki).as_posix()
        fm = _read_frontmatter(path)
        for rule in rules:
            result = _audit_rule_page(rule, rel, fm, schema)
            if result:
                results.append(result)
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
    # Finding totals are rendered directly from ``findings`` below; do not keep
    # an unconsumed parallel accumulator that can drift from the artifact.

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
    if body_mode(capability, "update") not in ("append-only", "allow") and "NO body" not in prompt:
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
