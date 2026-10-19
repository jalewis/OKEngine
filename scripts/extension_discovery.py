#!/usr/bin/env python3
"""extension_discovery — the three-tier extension discovery scanner (#134).

Implements `docs/design/discovery-spec.md`: scan the three roots, key each
extension by its manifest `id`, and enforce the load-bearing collision rules
**fail-loud, before enable and before any generated file is written** (§9):

  - an id may appear in AT MOST ONE tier — a duplicate across tiers is a hard FAIL
    (no shadowing, no highest-tier-wins);
  - `okengine.*` ids are reserved to tier-1 (engine) — a tier-2/3 claim is a FAIL.

Discovery is presence-based (an `extension.yaml` makes a dir an extension) and
**discovered ≠ enabled**: this module only finds and validates; enablement state
lives in `<pack>/.okengine/extensions.yaml` and the enable/disable + cron-regen
lifecycle is #113, which consumes the scanner here.

Mirrors the `(out, errors)` tuple convention of cron_pack_split.discover_packs;
a non-empty error list means DO NOT proceed (deploy/enable gate).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# repo root (framework.py:30 convention). OKENGINE_ENGINE_ROOT overrides it — used to point
# the tier-1 engine-extensions scan elsewhere (test isolation; alternate engine checkouts).
ENGINE_ROOT = (Path(os.environ["OKENGINE_ENGINE_ROOT"]).resolve()
               if os.environ.get("OKENGINE_ENGINE_ROOT") else _HERE.parent)

ENGINE_DIRNAME = "extensions"                     # tier-1: <engine>/extensions/
PACK_DIRNAME = "extensions"                       # tier-2: <pack>/extensions/
OPERATOR_REL = (".okengine", "extensions")        # tier-3: <pack>/.okengine/extensions/

# Discovery tiers, most-trusted first (informational ordering only — tiers do NOT
# shadow one another; see the duplicate-id rule).
TIERS = ("engine", "pack", "operator")

ENABLED_STATE_REL = (".okengine", "extensions.yaml")   # <pack>/.okengine/extensions.yaml


def _manifest_mod():
    """Load the sibling extension_manifest module by path (no package assumptions)."""
    p = _HERE / "extension_manifest.py"
    spec = importlib.util.spec_from_file_location("extension_manifest", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _scan_root(root: Path, tier: str, em) -> tuple[list[dict], list[str]]:
    """Enumerate subdirs of ``root`` carrying an extension.yaml. Each ->
    ``{id, tier, dir, manifest}``. Manifest parse/validate faults become errors
    (the extension is still listed when it has a usable id, so `list` can show it)."""
    out: list[dict] = []
    errors: list[str] = []
    root = Path(root)
    if not root.is_dir():
        return out, errors
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            manifest = em.load_manifest(d)
        except em.ManifestError as e:
            errors.append(str(e))
            continue
        if manifest is None:
            continue                              # not an extension dir
        m_errors, _ = em.validate_manifest(manifest)
        m_errors = list(m_errors) + em.validate_dropins(manifest, d)   # okengine#1004
        ext_id = manifest.get("id")
        for msg in m_errors:
            errors.append(f"{d}: {msg}")
        if not isinstance(ext_id, str) or not em.ID_RE.match(ext_id):
            continue                              # no usable id -> can't key it; error already logged
        out.append({"id": ext_id, "tier": tier, "dir": str(d), "manifest": manifest})
    return out, errors


def discover(pack_dir: Path | None = None, *, engine_root: Path | None = None
             ) -> tuple[list[dict], list[str]]:
    """Scan all three roots and apply the collision rules.

    Returns ``(extensions, errors)`` where each extension is
    ``{id, tier, dir, manifest}``. A non-empty ``errors`` list is a hard gate."""
    em = _manifest_mod()
    eroot = Path(engine_root) if engine_root is not None else ENGINE_ROOT
    records: list[dict] = []
    errors: list[str] = []

    roots = [(eroot / ENGINE_DIRNAME, "engine")]
    if pack_dir is not None:
        pack_dir = Path(pack_dir)
        roots.append((pack_dir / PACK_DIRNAME, "pack"))
        roots.append((pack_dir.joinpath(*OPERATOR_REL), "operator"))

    for root, tier in roots:
        recs, errs = _scan_root(root, tier, em)
        records.extend(recs)
        errors.extend(errs)

    # Rule 1: okengine.* reserved to tier-1 (namespace-squatting guard).
    for r in records:
        if em.is_reserved_id(r["id"]) and r["tier"] != "engine":
            errors.append(
                f"FAIL: reserved id '{r['id']}' claimed by {r['tier']} tier "
                f"({r['dir']}) — 'okengine.*' is reserved to the engine tier")

    # Rule 2: an id may appear in at most one tier (no shadowing, reject duplicate).
    by_id: dict[str, list[dict]] = {}
    for r in records:
        by_id.setdefault(r["id"], []).append(r)
    for ext_id, recs in by_id.items():
        if len(recs) > 1:
            where = " and ".join(f"{x['tier']} ({x['dir']})" for x in recs)
            errors.append(f"FAIL: extension id '{ext_id}' found in multiple tiers: {where}")

    return records, errors


def load_enabled_state(pack_dir: Path) -> tuple[dict, list[str]]:
    """Read ``<pack>/.okengine/extensions.yaml`` -> ``(enabled_map, errors)``.

    The enabled map is ``{id: {config: {...}}}``; absent file -> empty map (no
    extensions enabled). Reading only — the enable/disable writers are #113."""
    em = _manifest_mod()
    if em.yaml is None:
        return {}, ["PyYAML not available to read enabled-state"]
    path = Path(pack_dir).joinpath(*ENABLED_STATE_REL)
    if not path.is_file():
        return {}, []
    try:
        data = em.yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {}, [f"{path}: unparseable enabled-state: {e}"]
    if not isinstance(data, dict):
        return {}, [f"{path}: enabled-state must be a YAML mapping"]
    enabled = data.get("enabled", {})
    if not isinstance(enabled, dict):
        return {}, [f"{path}: 'enabled' must be a mapping of id -> settings"]
    disabled = data.get("disabled", [])
    if not isinstance(disabled, list) or any(not isinstance(value, str) for value in disabled):
        return {}, [f"{path}: 'disabled' must be a list of extension id strings"]
    return enabled, []


# ── operator-tier trust acceptance (okengine#124, okengine#994 [14]) ─────────────────────────────
# An OPERATOR-tier (<pack>/.okengine/extensions/) `trust: in-gateway` extension is third-party code
# running in the gateway with full access. `extensions enable --allow-untrusted` is the operator's
# explicit acceptance, and it is RECORDED in the enabled-state entry so the load paths (cron regen,
# script staging) and `framework validate` can re-check it. Enforcing it only at enable let an
# extension reach the gateway without it: a hand-edited extensions.yaml, or a manifest that flipped
# from sidecar to in-gateway after it was enabled.
TRUST_ACCEPTED_KEY = "trust_accepted"
# okengine#1032 [12]: the recorded acceptance of a pack/operator-tier identity-edit grant. Read by
# extension_tokens.is_identity_edit_accepted (the same key; a round-trip test pins the agreement).
IDENTITY_ACCEPTED_KEY = "identity_edit_accepted"


# Agent toolsets that cannot execute code or write files outside the enforced write path. ALLOWLIST,
# not a blocklist of dangerous names (the deployment_checks api_server precedent): a composite alias
# such as `hermes-cron` expands to terminal/file/code_execution, so anything not listed here, or a
# toolsets value that is not a list, counts as code-capable. `okengine-write-*` are the per-lane
# write servers (okengine#969); `file_read`/`file_read_exact` are the read-only file toolsets
# (patches/10-read-only-file-toolset.patch).
_NO_CODE_TOOLSETS = frozenset({"okengine", "okengine-write", "web", "todo", "no_mcp",
                               "file_read", "file_read_exact"})


def _code_capable_toolsets(op: dict) -> bool:
    toolsets = op.get("toolsets")
    if toolsets is None:
        return False                      # the composer's default: [okengine, okengine-write]
    if not isinstance(toolsets, list):
        return True
    return any(not (isinstance(t, str) and (t in _NO_CODE_TOOLSETS or t.startswith("okengine-write-")))
               for t in toolsets)


def runs_gateway_code(record: dict) -> bool:
    """okengine#1004: would this extension put code into the gateway, whatever its `trust` label says?

    Decided from what the load paths actually do, not from the label: `extension_compose` gives any
    non-sidecar operation an in-gateway job, `staging_targets` then copies the extension dir's
    top-level *.py into /opt/data/scripts/<id>/, and a script entrypoint becomes the job's `script`.
    So it runs code when a non-image operation (manifest block or `crons/*.cron.json` drop-in) names
    a script or lists a code-capable toolset (an agent with `terminal`/`file`/a composite can run
    or write anything), or when it has any non-image operation and ships *.py to stage. A sidecar (isolated,
    scoped MCP) and an extension with no operation run nothing in the gateway."""
    manifest = record.get("manifest") or {}
    if manifest.get("trust") == "sidecar" or manifest.get("kind") != "operation":
        return False                      # only kind: operation synthesizes a job (extension_compose)
    em = _manifest_mod()
    ops: list = []
    if isinstance(manifest.get("operation"), dict):
        ops.append(manifest["operation"])
    if isinstance(manifest.get("operations"), dict):
        ops.extend(op for op in manifest["operations"].values() if isinstance(op, dict))
    dropins, dropin_error = em.load_dropin_ops(record.get("dir"))
    if dropin_error:
        return True                       # an unreadable drop-in can mask a script one: assume code
    ops.extend(op for _, op in dropins)
    gateway_ops = [op for op in ops
                   if not (isinstance(op.get("entrypoint"), dict) and "image" in op["entrypoint"])]
    if any(em.script_entrypoint(op) or _code_capable_toolsets(op) for op in gateway_ops):
        return True
    ext_dir = record.get("dir")
    return bool(gateway_ops) and bool(ext_dir) and any(Path(ext_dir).glob("*.py"))  # glob-ok: the extension root, what staging tars


def requires_trust_acceptance(record: dict) -> bool:
    """An operator-tier extension that runs code in the gateway: a `trust: in-gateway` manifest, or
    one whose operations would stage or schedule code under any other label (okengine#1004)."""
    if record.get("tier") != "operator":
        return False
    return (record.get("manifest") or {}).get("trust") == "in-gateway" or runs_gateway_code(record)


def is_trust_accepted(settings, record: dict) -> bool:
    """The enabled-state entry records acceptance of the trust level the manifest declares NOW."""
    accepted = settings.get(TRUST_ACCEPTED_KEY) if isinstance(settings, dict) else None
    trust = (record.get("manifest") or {}).get("trust")
    # a manifest with no trust declares nothing to accept: `trust_accepted: {}` must not match it
    return isinstance(accepted, dict) and trust is not None and accepted.get("trust") == trust


def trust_accept_command(pack_dir, ext_id: str) -> str:
    return f"framework extensions enable {pack_dir} {ext_id} --allow-untrusted"


def unaccepted_trust(pack_dir, resolved: dict, enabled: dict | None = None) -> dict[str, str]:
    """{id: FAIL line} for the resolved extensions that need trust acceptance and lack it.

    Load paths EXCLUDE these ids (nothing staged, no job generated) and report the lines, so a
    deploy fails loudly with the accept command instead of shipping unaccepted code."""
    if enabled is None:
        enabled, _ = load_enabled_state(pack_dir)
    blocked: dict[str, str] = {}
    for ext_id in sorted(resolved):
        record = resolved[ext_id]
        if requires_trust_acceptance(record) and not is_trust_accepted(enabled.get(ext_id), record):
            blocked[ext_id] = (
                f"FAIL: operator-tier extension '{ext_id}' runs code in the gateway "
                f"(`trust: {(record.get('manifest') or {}).get('trust')}`) with no "
                "recorded trust acceptance (okengine#124), so it is not staged or scheduled. If you "
                f"trust this code, accept it once: `{trust_accept_command(pack_dir, ext_id)}`. "
                "Otherwise disable it or repackage it as `trust: sidecar`.")
    return blocked


def set_enabled(pack_dir, ext_id: str, enabled: bool,
                config: dict | None = None, trust_accepted: str | None = None,
                identity_edit_accepted: dict | None = None) -> list[str]:
    """Write the enable/disable bit for ``ext_id`` into
    ``<pack>/.okengine/extensions.yaml`` (creating it if absent), preserving every
    other entry. Returns an errors list (empty = ok). Reading + this writer are the
    only state mutators — the enable/disable *flow* (validation, regen) is the CLI.

    ``trust_accepted`` records the operator's acceptance of that trust level (enable only), and
    ``identity_edit_accepted`` the acceptance of an identity-edit grant, bound to its digest
    (okengine#1032, `extension_tokens.identity_edit_acceptance`). Disabling drops the whole entry,
    so a later re-enable must accept again."""
    em = _manifest_mod()
    if em.yaml is None:
        return ["PyYAML not available to write enabled-state"]
    current, errors = load_enabled_state(pack_dir)
    if errors:
        return errors
    state = dict(current)
    disabled = _load_disabled(pack_dir)
    if enabled:
        entry = dict(state.get(ext_id) or {})
        if config is not None:
            entry["config"] = config
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if trust_accepted is not None:
            entry[TRUST_ACCEPTED_KEY] = {"trust": trust_accepted, "accepted_at": now}
        if identity_edit_accepted is not None:
            entry[IDENTITY_ACCEPTED_KEY] = {**identity_edit_accepted, "accepted_at": now}
        state[ext_id] = entry
        disabled.discard(ext_id)              # re-enabling clears any explicit disable
    else:
        state.pop(ext_id, None)
        disabled.add(ext_id)                  # explicit OFF — also overrides core default-on
    _write_state(pack_dir, state, disabled)
    return []


def _write_state(pack_dir, state: dict, disabled: set) -> None:
    em = _manifest_mod()
    path = Path(pack_dir).joinpath(*ENABLED_STATE_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ("# Generated/maintained by `framework extensions enable|disable`.\n"
            "# Enabled-state is vault-level (not in the extension package), so one\n"
            "# package runs in many deployments. present-on-disk != enabled.\n"
            "# `disabled:` turns OFF a core (default-on) extension.\n")
    doc = {"enabled": state}
    if disabled:
        doc["disabled"] = sorted(disabled)
    path.write_text(body + em.yaml.safe_dump(doc, sort_keys=True), encoding="utf-8")


def forget_command(pack_dir, ext_id: str) -> str:
    return f"framework extensions forget {pack_dir} {ext_id}"


def stale_disabled_errors(pack_dir, discovered: list[dict]) -> list[str]:
    """A ``disabled:`` id no tier discovers (okengine#1042 [32]). The opt-out no longer applies to
    anything: a renamed core extension is back ON under its new id. Both validators FAIL it, and
    the line names the command that clears it."""
    ids = {record.get("id") for record in discovered}
    return [f"FAIL: disabled extension '{ext_id}' is not discovered in any tier (renamed or "
            f"removed): if it was renamed, disable its new id; then clear the stale opt-out with "
            f"`{forget_command(pack_dir, ext_id)}`"
            for ext_id in sorted(_load_disabled(pack_dir) - ids)]


def forget_disabled(pack_dir, ext_id: str) -> tuple[bool, list[str]]:
    """Drop ``ext_id`` from ``disabled:``. Returns ``(changed, errors)``; the caller decides
    whether the id may be forgotten (a discovered core id would turn back ON)."""
    current, errors = load_enabled_state(pack_dir)
    if errors:
        return False, errors
    disabled = _load_disabled(pack_dir)
    if ext_id not in disabled:
        return False, []
    disabled.discard(ext_id)
    _write_state(pack_dir, current, disabled)
    return True, []


def _load_disabled(pack_dir) -> set[str]:
    """Ids explicitly turned OFF in the enabled-state (``disabled: [...]``). A core
    extension stays on unless its id appears here. Parse errors are reported by
    ``load_enabled_state``; this returns an empty set on any read problem."""
    em = _manifest_mod()
    if em.yaml is None:
        return set()
    path = Path(pack_dir).joinpath(*ENABLED_STATE_REL)
    if not path.is_file():
        return set()
    try:
        data = em.yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    d = data.get("disabled", [])
    return set(d) if isinstance(d, list) and all(isinstance(value, str) for value in d) else set()


def is_core(record: dict) -> bool:
    """An engine-tier extension that declares ``core: true`` — default-ON (still
    'present != enabled' for non-core). Only the engine tier may be core (a pack/operator
    extension can't force itself on)."""
    return record.get("tier") == "engine" and record.get("manifest", {}).get("core") is True


def effective_enabled(pack_dir, discovered: list[dict]) -> tuple[set[str], list[str]]:
    """The set of ids that are actually active = explicitly enabled  ∪  (core extensions
    not explicitly disabled). The one place 'core: true' default-on is resolved."""
    enabled, errors = load_enabled_state(pack_dir)
    disabled = _load_disabled(pack_dir)
    eff = set(enabled.keys())
    for rec in discovered:
        if is_core(rec) and rec["id"] not in disabled:
            eff.add(rec["id"])
    return eff, errors


def resolve_for_pack(pack_dir, *, engine_root: Path | None = None
                     ) -> tuple[dict[str, dict], list[str]]:
    """Discover + resolve the EFFECTIVELY-enabled extensions for a pack (explicit ∪
    core-not-disabled). The single chokepoint the compose / stage / sidecar / schema
    paths use, so default-on core extensions are honored everywhere uniformly.
    `engine_root` overrides where tier-1 (first-party) extensions are found; None keeps the
    module default (OKENGINE_ENGINE_ROOT, else this file's repo root)."""
    extensions, disc_err = discover(pack_dir, engine_root=engine_root)
    enabled, _ = load_enabled_state(pack_dir)
    eff, en_err = effective_enabled(pack_dir, extensions)
    resolved, res_err = resolve_enabled(sorted(eff), extensions)
    config_err: list[str] = []
    for ext_id, rec in list(resolved.items()):
        settings = enabled.get(ext_id) or {}
        overrides = settings.get("config") if isinstance(settings, dict) else None
        if overrides is None:
            continue
        if not isinstance(overrides, dict):
            config_err.append(f"FAIL: enabled extension '{ext_id}' config override must be a mapping")
            continue
        manifest = dict(rec.get("manifest") or {})
        declarations = manifest.get("config") or {}
        if not isinstance(declarations, dict):
            config_err.append(f"FAIL: extension '{ext_id}' manifest config must be a mapping")
            continue
        unknown = sorted(set(overrides) - set(declarations))
        if unknown:
            config_err.append(
                f"FAIL: enabled extension '{ext_id}' has unknown config override(s): "
                + ", ".join(unknown)
            )
            continue
        effective_config = {}
        for key, declaration in declarations.items():
            if isinstance(declaration, dict):
                effective = dict(declaration)
                if key in overrides:
                    effective["default"] = overrides[key]
            else:
                effective = overrides.get(key, declaration)
            effective_config[key] = effective
        manifest["config"] = effective_config
        resolved[ext_id] = {**rec, "manifest": manifest}
    return resolved, list(en_err) + list(disc_err) + list(res_err) + config_err


def resolve_enabled(enabled_ids, discovered: list[dict]) -> tuple[dict[str, dict], list[str]]:
    """Resolve each enabled id to its single discovered record by bare id.

    The no-shadow rule guarantees a bare id resolves to at most one extension, so
    enabled-state never needs tier qualification. An enabled id that resolves to
    ZERO discovered extensions is a FAIL (referenced-but-absent)."""
    index: dict[str, dict] = {r["id"]: r for r in discovered}
    resolved: dict[str, dict] = {}
    errors: list[str] = []
    for ext_id in enabled_ids:
        rec = index.get(ext_id)
        if rec is None:
            errors.append(f"FAIL: enabled extension '{ext_id}' is not discovered in any tier")
            continue
        resolved[ext_id] = rec
    return resolved, errors
