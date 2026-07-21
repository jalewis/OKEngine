#!/usr/bin/env python3
"""Application-profile discovery and pack-binding conformance.

Applications are declared compositions, not executable plugins. A profile in the engine catalog
defines the supported operating contract; a pack opts in through
``.okengine/application.yaml`` and binds domain-owned types, fields, operations, surfaces, and
measures to that contract.

The module is intentionally pure apart from loading YAML so framework validation and focused
application tests use the same rules.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


ENGINE_ROOT = Path(__file__).resolve().parents[1]
DECLARATION = Path(".okengine/application.yaml")
ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ApplicationProfileError(ValueError):
    pass


def _load(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ApplicationProfileError(f"{path}: cannot load YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise ApplicationProfileError(f"{path}: top level must be a mapping")
    return value


def load_declaration(pack: Path) -> dict | None:
    path = Path(pack) / DECLARATION
    return _load(path) if path.is_file() else None


def load_profile(profile_id: str, engine_root: Path = ENGINE_ROOT) -> dict:
    """Load and resolve a catalog profile, including its single parent when declared."""
    return _load_profile(profile_id, Path(engine_root), ())


def _load_profile(profile_id: str, engine_root: Path, ancestry: tuple[str, ...]) -> dict:
    if not isinstance(profile_id, str) or not ID_RE.fullmatch(profile_id):
        raise ApplicationProfileError(f"invalid application profile id: {profile_id!r}")
    if profile_id in ancestry:
        cycle = " -> ".join((*ancestry, profile_id))
        raise ApplicationProfileError(f"application profile inheritance cycle: {cycle}")
    path = Path(engine_root) / "applications" / profile_id / "application.yaml"
    if not path.is_file():
        raise ApplicationProfileError(f"unknown application profile {profile_id!r}")
    profile = _load(path)
    if profile.get("id") != profile_id:
        raise ApplicationProfileError(
            f"{path}: id {profile.get('id')!r} does not match directory {profile_id!r}")
    extends = profile.get("extends")
    profile_errors = validate_profile_manifest(profile, allow_inherited=extends is not None)
    if profile_errors:
        raise ApplicationProfileError(f"{path}: " + "; ".join(profile_errors))
    if extends is None:
        return profile

    parent_id = extends["profile"]
    try:
        parent = _load_profile(parent_id, engine_root, (*ancestry, profile_id))
    except ApplicationProfileError as exc:
        raise ApplicationProfileError(
            f"profile {profile_id!r} extends.profile {parent_id!r}: {exc}") from exc
    floor = _floor(extends.get("version"))
    parent_version = _version(parent.get("version"))
    if floor is None:
        raise ApplicationProfileError(
            f"profile {profile_id!r} extends.version must be a semantic-version floor (>=X.Y.Z)")
    if parent_version is None or parent_version < floor:
        raise ApplicationProfileError(
            f"profile {profile_id!r} extends {parent_id!r} {extends.get('version')}; "
            f"catalog parent version is {parent.get('version')!r}")
    effective = _merge_profiles(parent, profile)
    effective_errors = validate_profile_manifest(effective)
    if effective_errors:
        raise ApplicationProfileError(
            f"profile {profile_id!r} effective inherited contract: " + "; ".join(effective_errors))
    return effective


def _version(value) -> tuple[int, int, int] | None:
    match = VERSION_RE.fullmatch(str(value or ""))
    return tuple(map(int, match.groups())) if match else None


def _floor(requirement) -> tuple[int, int, int] | None:
    value = str(requirement or "")
    return _version(value[2:]) if value.startswith(">=") else None


def _ordered_union(parent: list, child: list) -> list:
    return [*parent, *(item for item in child if item not in parent)]


def _merge_floor(parent_value, child_value, where: str):
    parent_floor = _floor(parent_value)
    child_floor = _floor(child_value)
    if parent_floor is None or child_floor is None:
        raise ApplicationProfileError(f"{where} must use semantic-version floors (>=X.Y.Z)")
    return child_value if child_floor >= parent_floor else parent_value


def _merge_requires(parent: dict, child: dict, child_id: str) -> dict:
    merged = dict(parent)
    for key, value in child.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            rows = dict(merged[key])
            for dependency, requirement in value.items():
                if dependency in rows:
                    rows[dependency] = _merge_floor(
                        rows[dependency], requirement,
                        f"profile {child_id!r} requires.{key}.{dependency}")
                else:
                    rows[dependency] = requirement
            merged[key] = rows
        elif key in merged and key == "engine":
            merged[key] = _merge_floor(
                merged[key], value, f"profile {child_id!r} requires.engine")
        else:
            merged[key] = value
    return merged


def _merge_role_contract(parent: dict, child: dict, child_id: str, role: str) -> dict:
    merged = dict(parent)
    if "minimum" in child:
        merged["minimum"] = max(parent.get("minimum", 1), child["minimum"])
    for key in ("required_fields", "required_operations"):
        if key in child:
            merged[key] = _ordered_union(parent.get(key, []), child[key])
    if "allow_multiple" in child:
        if parent.get("allow_multiple") is False and child["allow_multiple"] is True:
            raise ApplicationProfileError(
                f"profile {child_id!r} binding_contract.required_roles.{role}.allow_multiple "
                "cannot weaken inherited false to true")
        merged["allow_multiple"] = child["allow_multiple"]
    return merged


def _merge_binding_contract(parent: dict, child: dict, child_id: str) -> dict:
    merged = dict(parent)
    if "minimum_proposition_classes" in child:
        merged["minimum_proposition_classes"] = max(
            parent.get("minimum_proposition_classes", 1), child["minimum_proposition_classes"])
    for key in ("required_fields", "required_operations"):
        if key in child:
            merged[key] = _ordered_union(parent.get(key, []), child[key])
    roles = {key: dict(value) for key, value in (parent.get("required_roles") or {}).items()}
    for role, contract in (child.get("required_roles") or {}).items():
        roles[role] = _merge_role_contract(roles.get(role, {}), contract, child_id, role)
    if roles:
        merged["required_roles"] = roles
    return merged


def _merge_profiles(parent: dict, child: dict) -> dict:
    """Compose parent before child without permitting silent contract weakening."""
    child_id = child["id"]
    merged = dict(parent)
    merged.update({key: value for key, value in child.items() if key not in {
        "requires", "binding_contract", "operating_loop", "required_surfaces",
        "required_queues", "required_success_measures", "policy",
    }})
    merged["requires"] = _merge_requires(
        parent.get("requires") or {}, child.get("requires") or {}, child_id)
    merged["binding_contract"] = _merge_binding_contract(
        parent.get("binding_contract") or {}, child.get("binding_contract") or {}, child_id)

    stages = [dict(row) for row in parent.get("operating_loop") or []]
    inherited = {row["id"]: row for row in stages}
    for row in child.get("operating_loop") or []:
        prior = inherited.get(row["id"])
        if prior is not None:
            if prior != row:
                raise ApplicationProfileError(
                    f"profile {child_id!r} operating_loop stage {row['id']!r} conflicts "
                    "with inherited stage")
            continue
        stages.append(dict(row))
        inherited[row["id"]] = row
    merged["operating_loop"] = stages

    for key in ("required_surfaces", "required_queues", "required_success_measures"):
        merged[key] = _ordered_union(parent.get(key) or [], child.get(key) or [])

    policy = dict(parent.get("policy") or {})
    for key, value in (child.get("policy") or {}).items():
        if policy.get(key) is True and value is not True:
            raise ApplicationProfileError(
                f"profile {child_id!r} policy.{key} cannot weaken inherited true invariant")
        policy[key] = value
    merged["policy"] = policy
    return merged


def validate_profile_manifest(profile: dict, *, allow_inherited: bool = False) -> list[str]:
    """Validate the engine-owned catalog entry before trusting its pack contract."""
    errors: list[str] = []
    allowed = {
        "schema_version", "id", "version", "name", "description", "extends", "requires",
        "binding_contract", "operating_loop", "required_surfaces", "required_queues",
        "required_success_measures", "policy",
    }
    unknown = sorted(set(profile) - allowed)
    if unknown:
        errors.append(f"unknown profile key(s): {unknown}")
    if profile.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not ID_RE.fullmatch(str(profile.get("id") or "")):
        errors.append("id must be lowercase kebab-case")
    if _version(profile.get("version")) is None:
        errors.append("version must be semantic version X.Y.Z")
    extends = profile.get("extends")
    if extends is not None:
        if not isinstance(extends, dict):
            errors.append("extends must be a mapping")
        else:
            unknown_extends = sorted(set(extends) - {"profile", "version"})
            if unknown_extends:
                errors.append(f"unknown extends key(s): {unknown_extends}")
            if not ID_RE.fullmatch(str(extends.get("profile") or "")):
                errors.append("extends.profile must be lowercase kebab-case")
            if extends.get("profile") == profile.get("id"):
                errors.append("extends.profile cannot reference itself")
            if _floor(extends.get("version")) is None:
                errors.append("extends.version must be a semantic-version floor (>=X.Y.Z)")
    requires = profile.get("requires")
    if requires is None and allow_inherited:
        pass
    elif not isinstance(requires, dict) or not isinstance(requires.get("extensions"), dict):
        errors.append("requires.extensions must be a mapping")
    contract = profile.get("binding_contract")
    if contract is None and allow_inherited:
        pass
    elif not isinstance(contract, dict):
        errors.append("binding_contract must be a mapping")
    else:
        minimum = contract.get("minimum_proposition_classes")
        if minimum is None and allow_inherited:
            pass
        elif not isinstance(minimum, int) or minimum < 1:
            errors.append("binding_contract.minimum_proposition_classes must be a positive integer")
        for key in ("required_fields", "required_operations"):
            value = contract.get(key)
            if value is None and allow_inherited:
                continue
            if not isinstance(value, list) or not value:
                errors.append(f"binding_contract.{key} must be a non-empty list")
        roles = contract.get("required_roles", {})
        if not isinstance(roles, dict):
            errors.append("binding_contract.required_roles must be a mapping")
        else:
            for role, role_contract in roles.items():
                where = f"binding_contract.required_roles.{role}"
                if not ROLE_RE.fullmatch(str(role)):
                    errors.append(f"{where}: role id must be lowercase snake_case")
                if not isinstance(role_contract, dict):
                    errors.append(f"{where} must be a mapping")
                    continue
                unknown_role = sorted(set(role_contract) - {
                    "minimum", "required_fields", "required_operations", "allow_multiple"})
                if unknown_role:
                    errors.append(f"{where} unknown key(s): {unknown_role}")
                minimum = role_contract.get("minimum", 1)
                if not isinstance(minimum, int) or minimum < 1:
                    errors.append(f"{where}.minimum must be a positive integer")
                fields = role_contract.get("required_fields")
                if not isinstance(fields, list) or not fields or \
                        not all(isinstance(item, str) and item for item in fields) or \
                        len(fields) != len(set(fields)):
                    errors.append(f"{where}.required_fields must be a non-empty unique list")
                operations = role_contract.get("required_operations", [])
                if not isinstance(operations, list) or \
                        not all(isinstance(item, str) and item for item in operations) or \
                        len(operations) != len(set(operations)):
                    errors.append(f"{where}.required_operations must be a unique list")
                if not isinstance(role_contract.get("allow_multiple", False), bool):
                    errors.append(f"{where}.allow_multiple must be boolean")
    loop = profile.get("operating_loop")
    if loop is None and allow_inherited:
        pass
    elif not isinstance(loop, list) or not loop:
        errors.append("operating_loop must be a non-empty list")
    else:
        ids = [row.get("id") for row in loop if isinstance(row, dict)]
        if len(ids) != len(loop) or any(not isinstance(item, str) or not item for item in ids):
            errors.append("every operating_loop stage requires an id")
        elif len(set(ids)) != len(ids):
            errors.append("operating_loop stage ids must be unique")
        else:
            known: set[str] = set()
            for row in loop:
                after = row.get("after")
                if not isinstance(after, list):
                    errors.append(f"operating_loop {row['id']}.after must be a list")
                    continue
                missing = sorted(set(after) - known)
                if missing and not allow_inherited:
                    errors.append(
                        f"operating_loop {row['id']} depends on later/unknown stage(s): {missing}")
                known.add(row["id"])
    for key in ("required_surfaces", "required_queues", "required_success_measures"):
        value = profile.get(key)
        if value is None and allow_inherited:
            continue
        if not isinstance(value, list) or not value or len(value) != len(set(value)):
            errors.append(f"{key} must be a non-empty unique list")
    if not (allow_inherited and profile.get("policy") is None) and not isinstance(profile.get("policy"), dict):
        errors.append("policy must be a mapping")
    return errors


def _schema_parts(pack: Path, engine_root: Path) -> tuple[dict, set[str], set[str]]:
    """Return the effective base-plus-pack type, namespace, and field vocabulary.

    Packs are forbidden to redeclare engine-owned core types, so application validation must use
    the same base-under-pack precedence as schema composition. Extension item fields are added by
    the enabled-extension pass below; pack/common fields are sufficient to declare application
    lifecycle bindings without tightening a shared core type for every other application.
    """
    base = _load(Path(engine_root) / "config" / "base-schema.yaml")
    schema = _load(Path(pack) / "schema.yaml")
    types = dict(base.get("types") if isinstance(base.get("types"), dict) else {})
    types.update(schema.get("types") if isinstance(schema.get("types"), dict) else {})
    namespaces: set[str] = set()
    known_fields: set[str] = set()
    for layer in (base, schema):
        partitioning = layer.get("partitioning")
        if isinstance(partitioning, dict) and isinstance(partitioning.get("namespaces"), dict):
            namespaces.update(map(str, partitioning["namespaces"]))
        okf = layer.get("okf")
        if isinstance(okf, dict) and isinstance(okf.get("namespaces"), list):
            namespaces.update(map(str, okf["namespaces"]))
        if isinstance(layer.get("common_optional"), list):
            known_fields.update(map(str, layer["common_optional"]))
        for section in ("field_shapes", "field_items", "field_enums"):
            if isinstance(layer.get(section), dict):
                known_fields.update(map(str, layer[section]))
    for contract in types.values():
        if isinstance(contract, dict):
            known_fields.update(map(str, contract.get("required") or []))
            known_fields.update(map(str, contract.get("optional") or []))
    return types, namespaces, known_fields


def _enabled_extensions(pack: Path, engine_root: Path) -> tuple[dict, list[str]]:
    # Load by path to keep this helper usable from installed engine layouts without package setup.
    import importlib.util

    path = Path(engine_root) / "scripts" / "extension_discovery.py"
    spec = importlib.util.spec_from_file_location("application_extension_discovery", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    discovered, errors = module.discover(pack, engine_root=engine_root)
    enabled, state_errors = module.load_enabled_state(pack)
    effective_ids, effective_errors = module.effective_enabled(pack, discovered)
    resolved, resolution_errors = module.resolve_enabled(sorted(effective_ids), discovered)
    records = list(resolved.values())
    versions = {row["id"]: row["manifest"].get("version") for row in records}
    return {"versions": versions, "state": enabled, "records": records}, [
        *errors, *state_errors, *effective_errors, *resolution_errors
    ]


def _operations(pack: Path, enabled_records: list[dict]) -> set[str]:
    out: set[str] = set()
    for row in enabled_records:
        operations = row["manifest"].get("operations")
        if isinstance(operations, dict):
            out.update(f"{row['id']}:{name}" for name in operations)
    cron_file = Path(pack) / "crons" / "domain-crons.json"
    if cron_file.is_file():
        import json

        try:
            value = json.loads(cron_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = []
        rows = value.get("jobs", []) if isinstance(value, dict) else value
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict):
                name = row.get("name") or row.get("id")
                if isinstance(name, str) and name:
                    out.add(name)
    return out


def _required_packs(pack: Path) -> set[str]:
    """Return explicitly required pack IDs, excluding extension requirements."""
    manifest = Path(pack) / "pack.yaml"
    if not manifest.is_file():
        return set()
    try:
        value = _load(manifest)
    except ApplicationProfileError:
        return set()
    requires = value.get("requires") or []
    if not isinstance(requires, list):
        return set()
    out: set[str] = set()
    for requirement in requires:
        if not isinstance(requirement, str) or requirement.startswith("ext:"):
            continue
        out.add(requirement.split("@", 1)[0])
    return out


def validate(pack: Path, engine_root: Path = ENGINE_ROOT) -> list[str]:
    """Return conformance errors. An absent declaration is valid and returns no errors."""
    pack = Path(pack)
    declaration = load_declaration(pack)
    if declaration is None:
        return []
    errors: list[str] = []
    allowed = {
        "profile", "profile_version", "bindings", "surfaces", "queues", "success_measures"
    }
    unknown = sorted(set(declaration) - allowed)
    if unknown:
        errors.append(f"unknown application declaration key(s): {unknown}")
    profile_id = declaration.get("profile")
    try:
        profile = load_profile(profile_id, engine_root)
    except ApplicationProfileError as exc:
        return [str(exc)]

    inherited: dict[str, set[str]] = {}
    parent_id = ((profile.get("extends") or {}).get("profile")
                 if isinstance(profile.get("extends"), dict) else None)
    if parent_id:
        parent = load_profile(parent_id, engine_root)
        inherited = {
            "extensions": set(((parent.get("requires") or {}).get("extensions") or {})),
            "roles": set(((parent.get("binding_contract") or {}).get("required_roles") or {})),
            "surfaces": set(parent.get("required_surfaces") or []),
            "queues": set(parent.get("required_queues") or []),
            "success_measures": set(parent.get("required_success_measures") or []),
        }

    def origin(section: str, item: str) -> str:
        return (f" (inherited from {parent_id})"
                if item in inherited.get(section, set()) else f" (required by {profile_id})")

    requested = _version(declaration.get("profile_version"))
    supplied = _version(profile.get("version"))
    if requested is None:
        errors.append("profile_version must be semantic version X.Y.Z")
    elif supplied is None or requested[0] != supplied[0] or requested > supplied:
        errors.append(
            f"profile_version {declaration.get('profile_version')!r} is not compatible with "
            f"catalog version {profile.get('version')!r}")

    try:
        enabled, extension_errors = _enabled_extensions(pack, engine_root)
    except Exception as exc:
        enabled, extension_errors = {"versions": {}, "state": {}, "records": []}, [str(exc)]
    errors.extend(f"extension resolution: {message}" for message in extension_errors)
    for ext_id, requirement in ((profile.get("requires") or {}).get("extensions") or {}).items():
        actual = enabled["versions"].get(ext_id)
        if actual is None:
            errors.append(
                f"required extension {ext_id} is not enabled{origin('extensions', ext_id)}")
            continue
        floor = _floor(requirement)
        actual_version = _version(actual)
        if floor is None or actual_version is None or actual_version < floor:
            errors.append(
                f"required extension {ext_id} {requirement}; enabled version is {actual!r}"
                f"{origin('extensions', ext_id)}")

    try:
        types, namespaces, known_fields = _schema_parts(pack, engine_root)
    except ApplicationProfileError as exc:
        return [*errors, str(exc)]
    contract = profile.get("binding_contract") or {}
    bindings = declaration.get("bindings")
    if isinstance(bindings, dict):
        unknown_binding_sections = sorted(set(bindings) - {"propositions", "roles"})
        if unknown_binding_sections:
            errors.append(f"bindings unknown key(s): {unknown_binding_sections}")
    propositions = bindings.get("propositions") if isinstance(bindings, dict) else None
    if not isinstance(propositions, list):
        errors.append("bindings.propositions must be a list")
        propositions = []
    minimum = contract.get("minimum_proposition_classes", 1)
    if len(propositions) < minimum:
        errors.append(f"at least {minimum} proposition binding(s) required")
    operations = _operations(pack, enabled["records"])
    required_packs = _required_packs(pack)
    seen: set[str] = set()
    for index, binding in enumerate(propositions):
        where = f"bindings.propositions[{index}]"
        if not isinstance(binding, dict):
            errors.append(f"{where} must be a mapping")
            continue
        missing = [field for field in contract.get("required_fields", []) if field not in binding]
        if missing:
            errors.append(f"{where} missing required field(s): {missing}")
        type_name = binding.get("type")
        if type_name in seen:
            errors.append(f"{where}.type duplicates {type_name!r}")
        seen.add(type_name)
        type_contract = types.get(type_name)
        if not isinstance(type_contract, dict):
            errors.append(f"{where}.type {type_name!r} is not declared in schema.types")
            type_contract = {}
        namespace = binding.get("namespace")
        if namespace not in namespaces:
            errors.append(f"{where}.namespace {namespace!r} is not declared in schema partitioning")
        for key in ("status_field", "confidence_field", "evidence_field", "resolution_field", "review_field"):
            field = binding.get(key)
            if field and field not in known_fields:
                errors.append(
                    f"{where}.{key} names {field!r}, which is not declared by the effective schema")
        for key in ("open_values", "resolved_values"):
            if not isinstance(binding.get(key), list) or not binding.get(key):
                errors.append(f"{where}.{key} must be a non-empty list")
        if set(binding.get("open_values") or []) & set(binding.get("resolved_values") or []):
            errors.append(f"{where} open_values and resolved_values must not overlap")
        bound_ops = binding.get("operations")
        if not isinstance(bound_ops, dict):
            errors.append(f"{where}.operations must be a mapping")
            continue
        for operation_kind in contract.get("required_operations", []):
            operation = bound_ops.get(operation_kind)
            if not isinstance(operation, str) or not operation:
                errors.append(f"{where}.operations.{operation_kind} is required")
            elif operation not in operations:
                errors.append(f"{where}.operations.{operation_kind} references unknown operation {operation!r}")

    role_contracts = contract.get("required_roles") or {}
    role_bindings = bindings.get("roles", {}) if isinstance(bindings, dict) else {}
    if not isinstance(role_bindings, dict):
        errors.append("bindings.roles must be a mapping")
        role_bindings = {}
    unknown_roles = sorted(set(role_bindings) - set(role_contracts))
    if unknown_roles:
        errors.append(f"bindings.roles declares unknown role(s): {unknown_roles}")
    for role, role_contract in role_contracts.items():
        rows = role_bindings.get(role)
        where = f"bindings.roles.{role}"
        if not isinstance(rows, list):
            errors.append(f"{where} must be a list")
            rows = []
        minimum = role_contract.get("minimum", 1)
        if len(rows) < minimum:
            errors.append(
                f"{where} requires at least {minimum} binding(s); found {len(rows)}"
                f"{origin('roles', role)}")
        if len(rows) > 1 and not role_contract.get("allow_multiple", False):
            errors.append(f"{where} permits only one binding")
        role_seen: set[tuple[object, object]] = set()
        required_fields = role_contract.get("required_fields") or []
        required_operations = role_contract.get("required_operations") or []
        allowed_keys = {*required_fields, "operations", "provided_by"}
        for index, binding in enumerate(rows):
            item_where = f"{where}[{index}]"
            if not isinstance(binding, dict):
                errors.append(f"{item_where} must be a mapping")
                continue
            unknown_keys = sorted(set(binding) - allowed_keys)
            if unknown_keys:
                errors.append(f"{item_where} unknown key(s): {unknown_keys}")
            missing = [field for field in required_fields if field not in binding]
            if missing:
                errors.append(f"{item_where} missing required field(s): {missing}")
            type_name = binding.get("type")
            namespace = binding.get("namespace")
            provider = binding.get("provided_by")
            external = provider is not None
            if external and (not isinstance(provider, str) or provider not in required_packs):
                errors.append(
                    f"{item_where}.provided_by {provider!r} must name a pack declared in "
                    "pack.yaml requires")
            validate_contract = not external or type_name in types or namespace in namespaces
            identity = (type_name, namespace)
            if identity in role_seen:
                errors.append(f"{item_where} duplicates binding {identity!r}")
            role_seen.add(identity)
            if validate_contract and not isinstance(types.get(type_name), dict):
                errors.append(f"{item_where}.type {type_name!r} is not declared in schema.types")
            if validate_contract and namespace not in namespaces:
                errors.append(
                    f"{item_where}.namespace {namespace!r} is not declared in schema partitioning")
            for field_role in required_fields:
                if field_role in {"type", "namespace"}:
                    continue
                field = binding.get(field_role)
                if validate_contract and field is not None and field not in known_fields:
                    errors.append(
                        f"{item_where}.{field_role} names {field!r}, which is not declared "
                        "by the effective schema")
            bound_ops = binding.get("operations", {})
            if not isinstance(bound_ops, dict):
                errors.append(f"{item_where}.operations must be a mapping")
                bound_ops = {}
            unknown_operation_kinds = sorted(set(bound_ops) - set(required_operations))
            if unknown_operation_kinds:
                errors.append(
                    f"{item_where}.operations unknown key(s): {unknown_operation_kinds}")
            for operation_kind in required_operations:
                operation = bound_ops.get(operation_kind)
                if not isinstance(operation, str) or not operation:
                    errors.append(f"{item_where}.operations.{operation_kind} is required")
                elif operation not in operations:
                    errors.append(
                        f"{item_where}.operations.{operation_kind} references unknown "
                        f"operation {operation!r}")

    for section, required_ids in (
        ("surfaces", profile.get("required_surfaces") or []),
        ("queues", profile.get("required_queues") or []),
        ("success_measures", profile.get("required_success_measures") or []),
    ):
        values = declaration.get(section)
        if not isinstance(values, dict):
            errors.append(f"{section} must be a mapping")
            continue
        for item_id in required_ids:
            value = values.get(item_id)
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"{section}.{item_id} is required{origin(section, item_id)}")

    # The indexer must see every declared proposition class; otherwise the profile would validate
    # while caused reassessment silently omitted one class.
    reevaluation = enabled["state"].get("okengine.reevaluation") or {}
    reevaluation_config = reevaluation.get("config") if isinstance(reevaluation, dict) else {}
    configured = str((reevaluation_config or {}).get("proposition_types") or "prediction")
    indexed_types = {item.strip() for item in configured.split(",") if item.strip()}
    omitted = sorted(seen - indexed_types)
    if omitted:
        errors.append(
            f"okengine.reevaluation config proposition_types omits bound type(s): {omitted}")
    return errors


def validate_lifecycle_record(record: dict, proposition_types: set[str]) -> list[str]:
    """Validate the portable evidence-to-learning proof used by application conformance.

    This is not a canonical content type. It is an integration-test receipt showing that a
    supported profile can preserve the causal and governance facts CHE promises across component
    boundaries.
    """
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["lifecycle record must be a mapping"]
    for key in ("proposition", "proposition_type", "changed_evidence", "assessment_change",
                "review", "resolution", "learning"):
        if key not in record:
            errors.append(f"lifecycle record missing {key}")
    if record.get("proposition_type") not in proposition_types:
        errors.append(f"unbound proposition_type {record.get('proposition_type')!r}")
    changed = record.get("changed_evidence")
    if not isinstance(changed, list) or not changed or not all(isinstance(x, str) and x for x in changed):
        errors.append("changed_evidence must be a non-empty list of references")
        changed = []
    change = record.get("assessment_change")
    if not isinstance(change, dict):
        errors.append("assessment_change must be a mapping")
        change = {}
    for key in ("prior", "new", "cause", "evidence", "evaluator", "method"):
        if key not in change:
            errors.append(f"assessment_change.{key} is required")
    if change.get("prior") == change.get("new"):
        errors.append("assessment_change must preserve distinct prior and new states")
    if change.get("cause") not in changed:
        errors.append("assessment_change.cause must name changed evidence")
    evidence = change.get("evidence")
    if not isinstance(evidence, list) or change.get("cause") not in evidence:
        errors.append("assessment_change.evidence must include the causal evidence")
    review = record.get("review")
    if not isinstance(review, dict):
        errors.append("review must be a mapping")
        review = {}
    if review.get("required") is not True:
        errors.append("conformance lifecycle must exercise a required review boundary")
    if review.get("status") != "approved" or not review.get("reviewer"):
        errors.append("required review must be explicitly approved by a reviewer")
    resolution = record.get("resolution")
    if not isinstance(resolution, dict) or not resolution.get("status") or not resolution.get("outcome"):
        errors.append("resolution must preserve status and outcome")
    learning = record.get("learning")
    if not isinstance(learning, dict) or not learning.get("measure") or "value" not in learning:
        errors.append("learning must preserve a measure and value")
    return errors
