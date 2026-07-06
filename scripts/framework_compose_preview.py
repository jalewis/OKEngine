#!/usr/bin/env python3
"""framework compose-preview — okengine#90 Phase 1: the multi-pack composition SAFETY GATE.

READ-ONLY. Given >= 2 packs, merges their schemas + domain crons and reports every collision an
operator must resolve BEFORE deploy:

  - schema type/namespace OWNERSHIP conflicts (via the engine's compose_schema fail-loud engine),
  - cron name collisions + shared-schedule contention,
  - ID-authority scope overlap (duplicate-entity / bad-auto-merge risk),
  - trust-level incompatibility (never silently compose public + private),
  - the union of required secrets (the .env surface the composed instance needs).

Exits non-zero when the composition is UNSAFE (hard conflicts), so it can gate a deploy. It does
NOT write or deploy — the durable multi-pack config + the actual compose/deploy path are #90 P2+.

  framework compose-preview <pack-dir> <pack-dir> [...] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
from schema_lib import base_schema, compose_schema  # noqa: E402


def _load_yaml(p: Path) -> dict:
    try:
        return yaml.safe_load(p.read_text(errors="replace")) or {}
    except Exception:
        return {}


def _pack_meta(d: str | Path) -> dict:
    d = Path(d)
    pack = _load_yaml(d / "pack.yaml")
    schema = _load_yaml(d / "schema.yaml")
    crons: list = []
    cj = d / "crons" / "domain-crons.json"
    if cj.is_file():
        try:
            cd = json.loads(cj.read_text())
            crons = cd.get("jobs", cd) if isinstance(cd, dict) else cd
        except Exception:
            crons = []
    secrets: list[str] = []
    ee = d / ".env.example"
    if ee.is_file():
        for line in ee.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                secrets.append(line.split("=", 1)[0].strip())
    return {"dir": d, "name": pack.get("name") or d.name, "trust": pack.get("trust", "?"),
            "owns": pack.get("owns") or {}, "schema": schema, "crons": crons, "secrets": secrets}


def analyze(pack_dirs: list[str]) -> dict:
    metas = [_pack_meta(p) for p in pack_dirs]
    hard: list[str] = []
    warn: list[str] = []

    # 1. trust compatibility
    trusts = {m["trust"] for m in metas}
    if len(trusts) > 1:
        hard.append(f"TRUST mismatch: packs span {sorted(trusts)} — compose only within one trust level")

    # 2. schema composition — reuse the engine's fail-loud conflict detector
    root = metas[0]["dir"]
    fragments = []
    for m in metas[1:]:
        owns = m["owns"]
        types = {t: (m["schema"].get("types") or {}).get(t, {}) for t in (owns.get("types") or [])}
        fragments.append((f"pack:{m['name']}",
                          {"owns": {"namespaces": owns.get("namespaces") or [], "types": types}}))
    composed, schema_errors = compose_schema(root, fragments=fragments)
    hard += [f"SCHEMA: {e}" for e in schema_errors]

    # 2b. tightening detection — a pack that DECLARES a core type with EXTRA required fields would
    # reject another pack's pages under composition (a core type's required set is fixed; pack
    # additions must be optional). The fix is `extends` (optional) + workflow enforcement.
    core_types = base_schema().get("types") or {}
    for m in metas:
        for tname, tdef in (m["schema"].get("types") or {}).items():
            if tname in core_types and isinstance(tdef, dict):
                extra = sorted(set(tdef.get("required") or []) - set(core_types[tname].get("required") or []))
                if extra:
                    hard.append(f"TIGHTEN: {m['name']} makes core type '{tname}' require {extra} (core does "
                                f"not) — would reject other packs' {tname} pages; make them optional + enforce in ingest")

    # 3. cron-merge: duplicate names (hard) + shared schedules (warn)
    seen_name: dict[str, str] = {}
    seen_sched: dict[str, str] = {}
    njobs = 0
    for m in metas:
        for j in m["crons"]:
            njobs += 1
            nm = j.get("name")
            if nm in seen_name:
                hard.append(f"CRON name collision: '{nm}' in both {seen_name[nm]} and {m['name']}")
            elif nm:
                seen_name[nm] = m["name"]
            ex = (j.get("schedule") or {}).get("expr")
            if ex and not str(ex).startswith("@jitter"):
                if ex in seen_sched and seen_sched[ex] != m["name"]:
                    warn.append(f"CRON schedule '{ex}' shared by {seen_sched[ex]} + {m['name']} — stagger to avoid contention")
                seen_sched[ex] = m["name"]

    # 4. ID-authority scope overlap
    auth: dict[str, list] = {}
    for m in metas:
        for tname, tdef in (m["schema"].get("types") or {}).items():
            if isinstance(tdef, dict) and tdef.get("id_authority"):
                auth.setdefault(tdef["id_authority"], []).append(f"{m['name']}:{tname}")
    for scope, owners in auth.items():
        if len({o.split(':')[0] for o in owners}) > 1:
            warn.append(f"ID-authority scope '{scope}' claimed by multiple packs {owners} — confirm convergence, not duplicate-entity risk")

    # 5. secrets union
    sec: dict[str, list] = {}
    for m in metas:
        for s in m["secrets"]:
            sec.setdefault(s, []).append(m["name"])

    return {
        "packs": [m["name"] for m in metas],
        "trust": (sorted(trusts)[0] if len(trusts) == 1 else "MIXED"),
        "merged_types": sorted(composed.get("types", {})),
        "merged_namespaces": sorted((composed.get("partitioning") or {}).get("namespaces", {})),
        "jobs": njobs, "secrets": sec, "hard": hard, "warn": warn,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="framework compose-preview")
    ap.add_argument("packs", nargs="+", help="pack directories to compose (>= 2)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if len(args.packs) < 2:
        print("compose-preview needs >= 2 pack dirs", file=sys.stderr)
        return 2

    r = analyze(args.packs)
    if args.json:
        print(json.dumps(r, indent=2))
        return 1 if r["hard"] else 0

    print(f"=== compose-preview: {' + '.join(r['packs'])} ===")
    print(f"  trust: {r['trust']}{'  ✗' if r['trust'] == 'MIXED' else ''}")
    print(f"  merged types ({len(r['merged_types'])}): {', '.join(r['merged_types'])}")
    print(f"  merged namespaces: {', '.join(r['merged_namespaces'])}")
    print(f"  cron jobs: {r['jobs']}")
    print(f"  secrets required: {', '.join(sorted(r['secrets'])) or '(none)'}")
    if r["warn"]:
        print("\n  ⚠ human review:")
        for w in r["warn"]:
            print(f"    - {w}")
    if r["hard"]:
        print("\n  ✗ BLOCKING conflicts (resolve before deploy):")
        for h in r["hard"]:
            print(f"    - {h}")
        print("\n  → UNSAFE: do not deploy this composition as-is.")
        return 1
    print("\n  ✓ SAFE: no blocking conflicts. (Phase 1 preview — the compose/deploy path is #90 P2.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
