#!/usr/bin/env python3
"""pack_meta — pack identity + composition validation (composable okpacks P3).

A composed deployment is one engine + N packs building one vault. Each pack
declares its identity and what it OWNS in a `pack.yaml`; the engine enumerates the
installed packs (presence-based) and validates the composition is sound BEFORE
merging crons/schema. v1 is **additive / disjoint / fail-loud**: two packs may not
own the same type or namespace; `requires:` must be present (and version-satisfied
if a spec is given); all composed packs must share one trust level.

`pack.yaml`::

    name: okpack-attack
    version: 0.1.0
    trust: public            # public | private  (compose only within one level)
    owns:
      types: [attack-pattern]        # types this pack defines + owns
      namespaces: [attack-pattern]   # vault namespaces it writes/owns
    requires:
      - okpack-base                  # presence
      - okpack-foo@>=0.2.0           # presence + version floor (>= or ^ or bare)

Pure functions over metadata dicts/files — no engine coupling, fully test-vectored.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


# The top-level keys an author writes in pack.yaml (the closed pack grammar). Source of
# truth for the authoring-a-pack.md doc-parity guard (tests/test_pack_doc_parity.py).
# `kind` selects the pack shape ("pack" default | "bundle"); a bundle owns nothing and
# carries a `bundle:` recipe (host + compose[]) instead of a schema/content (okengine#181).
PACK_YAML_KEYS = frozenset(
    {"name", "version", "kind", "trust", "owns", "requires", "port_offset", "bundle"})


def load_pack_meta(pack_dir) -> dict | None:
    """Load + normalize a pack's `pack.yaml`. None if absent/unparseable."""
    p = Path(pack_dir) / "pack.yaml"
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    owns = data.get("owns") if isinstance(data.get("owns"), dict) else {}
    try:
        offset = max(0, int(data.get("port_offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    bundle = data.get("bundle") if isinstance(data.get("bundle"), dict) else {}
    return {
        "name": str(data.get("name") or Path(pack_dir).name),
        "version": str(data.get("version") or "0.0.0"),
        "kind": str(data.get("kind") or "pack"),
        "trust": str(data.get("trust") or "private"),
        "owns_types": {str(x) for x in (owns.get("types") or [])},
        "owns_namespaces": {str(x) for x in (owns.get("namespaces") or [])},
        "requires": [str(r) for r in (data.get("requires") or [])],
        "port_offset": offset,            # default host-port offset (reader 9200 / mcp 8730)
        # bundle recipe (only meaningful when kind == "bundle"): host pack + composed guests.
        "bundle_host": str(bundle.get("host") or "") if bundle else "",
        "bundle_compose": [str(x) for x in (bundle.get("compose") or [])],
        "dir": str(pack_dir),
    }


def _parse_req(req: str) -> tuple[str, str]:
    name, sep, spec = str(req).partition("@")
    return name.strip(), (spec.strip() if sep else "")


def extension_requires(meta: dict) -> list[tuple[str, str]]:
    """The ``ext:<id>[@spec]`` entries from a pack's ``requires`` (okengine#142) — the
    pack->extension dependency edges. Pack->pack requires are handled by
    validate_composition; these need the deployment's enabled-state to check, so the
    caller (framework validate) resolves them. Returns ``[(ext_id, spec), ...]``."""
    out: list[tuple[str, str]] = []
    for req in meta.get("requires", []):
        name, spec = _parse_req(req)
        if name.startswith("ext:"):
            out.append((name[len("ext:"):], spec))
    return out


def satisfies(present: str, spec: str) -> bool:
    """Public version-spec check (``>=`` / ``^`` / bare floor) for extension reqs."""
    return _satisfies(present, spec)


def _ver(v: str) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", str(v))[:3]]
    return tuple((nums + [0, 0, 0])[:3])  # type: ignore[return-value]


def _satisfies(present: str, spec: str) -> bool:
    """A minimal version check: `>=x.y.z`, `^x.y.z` (same major, >=), or a bare
    version (treated as a floor). Empty spec → presence-only (always satisfied)."""
    if not spec:
        return True
    caret = spec.startswith("^")
    floor = _ver(spec[2:] if spec.startswith(">=") else spec[1:] if caret else spec)
    have = _ver(present)
    return have >= floor and (have[0] == floor[0] if caret else True)


def validate_bundle_recipe(meta: dict) -> list[str]:
    """Structural errors for a single `kind: bundle` pack's recipe (empty = sound; empty for
    non-bundles). A bundle owns nothing and composes other packs — a `host` base vault plus a
    `compose` list install-domain'd onto it. Enforces: owns-nothing, host present, non-empty
    compose, host not also in compose, no duplicate/self entries, and every recipe member
    declared in `requires` (so the dep graph stays explicit). The no-nested-bundle guard needs
    the full composed set and lives in validate_composition (okengine#181)."""
    errors: list[str] = []
    if meta.get("kind") != "bundle":
        return errors
    name = meta.get("name", "?")
    if meta.get("owns_types") or meta.get("owns_namespaces"):
        errors.append(f"bundle '{name}' must own nothing (owns.types/namespaces must be empty)")
    host = meta.get("bundle_host") or ""
    compose = meta.get("bundle_compose") or []
    if not host:
        errors.append(f"bundle '{name}' is missing bundle.host")
    if not compose:
        errors.append(f"bundle '{name}' is missing a non-empty bundle.compose list")
    if host and host in compose:
        errors.append(f"bundle '{name}' lists its host '{host}' in bundle.compose")
    if len(set(compose)) != len(compose):
        errors.append(f"bundle '{name}' has duplicate entries in bundle.compose")
    if host == name or name in compose:
        errors.append(f"bundle '{name}' cannot compose itself")
    req_names = {_parse_req(r)[0] for r in meta.get("requires", [])}
    for member in dict.fromkeys(([host] if host else []) + list(compose)):
        if member and member not in req_names:
            errors.append(
                f"bundle '{name}' composes '{member}' but does not declare it in requires")
    return errors


def validate_composition(metas: list[dict]) -> list[str]:
    """Return composition errors (empty = sound). Enforces v1's disjoint ownership,
    `requires` satisfaction, single-trust-level, and (okengine#181) bundle-recipe rules."""
    errors: list[str] = []
    by_name = {m["name"]: m for m in metas}

    owners_t: dict[str, str] = {}
    owners_ns: dict[str, str] = {}
    for m in metas:
        for t in sorted(m["owns_types"]):
            if t in owners_t and owners_t[t] != m["name"]:
                errors.append(f"type '{t}' is owned by both {owners_t[t]} and {m['name']}")
            owners_t[t] = m["name"]
        for ns in sorted(m["owns_namespaces"]):
            if ns in owners_ns and owners_ns[ns] != m["name"]:
                errors.append(f"namespace '{ns}' is owned by both {owners_ns[ns]} and {m['name']}")
            owners_ns[ns] = m["name"]

    for m in metas:
        for req in m["requires"]:
            name, spec = _parse_req(req)
            if name.startswith("ext:"):
                continue                    # pack->extension edge — checked at validate-time
            dep = by_name.get(name)         # (needs the deployment's enabled-state)
            if dep is None:
                errors.append(f"{m['name']} requires '{name}' which is not installed")
            elif spec and not _satisfies(dep["version"], spec):
                errors.append(
                    f"{m['name']} requires {name}@{spec} but {dep['version']} is installed")

    trusts = {m["trust"] for m in metas}
    if len(trusts) > 1:
        errors.append(f"mixed trust levels {sorted(trusts)} — compose only within one "
                      "trust boundary (public + private must be separate instances)")

    for m in metas:
        if m.get("kind") != "bundle":
            continue
        errors.extend(validate_bundle_recipe(m))
        members = ([m.get("bundle_host") or ""] + list(m.get("bundle_compose") or []))
        for member in members:
            dep = by_name.get(member)
            if dep is not None and dep.get("kind") == "bundle":
                errors.append(
                    f"bundle '{m['name']}' composes '{member}' which is itself a bundle "
                    "(bundles cannot nest)")
    return errors
