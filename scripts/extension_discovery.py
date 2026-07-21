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
    return enabled, []


def set_enabled(pack_dir, ext_id: str, enabled: bool,
                config: dict | None = None) -> list[str]:
    """Write the enable/disable bit for ``ext_id`` into
    ``<pack>/.okengine/extensions.yaml`` (creating it if absent), preserving every
    other entry. Returns an errors list (empty = ok). Reading + this writer are the
    only state mutators — the enable/disable *flow* (validation, regen) is the CLI."""
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
        state[ext_id] = entry
        disabled.discard(ext_id)              # re-enabling clears any explicit disable
    else:
        state.pop(ext_id, None)
        disabled.add(ext_id)                  # explicit OFF — also overrides core default-on
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
    return []


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
    return set(d) if isinstance(d, list) else set()


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


def resolve_for_pack(pack_dir) -> tuple[dict[str, dict], list[str]]:
    """Discover + resolve the EFFECTIVELY-enabled extensions for a pack (explicit ∪
    core-not-disabled). The single chokepoint the compose / stage / sidecar / schema
    paths use, so default-on core extensions are honored everywhere uniformly."""
    extensions, disc_err = discover(pack_dir)
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
