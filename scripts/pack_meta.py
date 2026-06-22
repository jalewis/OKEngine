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
    return {
        "name": str(data.get("name") or Path(pack_dir).name),
        "version": str(data.get("version") or "0.0.0"),
        "trust": str(data.get("trust") or "private"),
        "owns_types": {str(x) for x in (owns.get("types") or [])},
        "owns_namespaces": {str(x) for x in (owns.get("namespaces") or [])},
        "requires": [str(r) for r in (data.get("requires") or [])],
        "port_offset": offset,            # default host-port offset (reader 9200 / mcp 8730)
        "dir": str(pack_dir),
    }


def _parse_req(req: str) -> tuple[str, str]:
    name, sep, spec = str(req).partition("@")
    return name.strip(), (spec.strip() if sep else "")


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


def validate_composition(metas: list[dict]) -> list[str]:
    """Return composition errors (empty = sound). Enforces v1's disjoint ownership,
    `requires` satisfaction, and single-trust-level rules."""
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
            dep = by_name.get(name)
            if dep is None:
                errors.append(f"{m['name']} requires '{name}' which is not installed")
            elif spec and not _satisfies(dep["version"], spec):
                errors.append(
                    f"{m['name']} requires {name}@{spec} but {dep['version']} is installed")

    trusts = {m["trust"] for m in metas}
    if len(trusts) > 1:
        errors.append(f"mixed trust levels {sorted(trusts)} — compose only within one "
                      "trust boundary (public + private must be separate instances)")
    return errors
