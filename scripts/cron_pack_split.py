#!/usr/bin/env python3
"""cron-pack-split — the engine/domain-pack cron generator (M3 keystone, prototyped in M2).

Per docs/engine-domain-boundary.md §3, the deployed cron-plus-jobs.json must be a
GENERATED merge, not a hand-bundled file:

    engine cron defs            (tier: engine          — full defs)
  + engine-template SCRIPT defs (tier: engine-template — schedule/script, NO prompt)
  + domain-pack cron defs       (tier: domain          — full defs)
  + engine-template PROMPTS     (supplied by the domain pack, keyed by job name)
  ───────────────────────────────────────────────────────────────────────────────
  = cron-plus-jobs.json

This tool implements both directions, keyed by config/cron-tiers.yaml:

  split  cron-plus-jobs.json  ->  engine/ + domain-pack/   (3 artifacts)
  merge  engine/ + domain-pack/  ->  cron-plus-jobs.json
  check  round-trip: split then merge must reproduce the input exactly

Decision-independent: works the same whether engine + pack end up in one repo or
two. `check` is the success criterion — if the boundary is real, the round-trip
is lossless.

Usage:
  cron_pack_split.py check                       # round-trip self-test (default)
  cron_pack_split.py split [--out DIR]
  cron_pack_split.py merge --in DIR [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
JOBS = REPO / "config" / "cron-plus-jobs.json"
TIERS = REPO / "config" / "cron-tiers.yaml"

# Engine artifact: the engine's cron half (engine full + engine-template scripts).
ENGINE_CRONS = "engine-crons.json"
# Domain-pack artifacts.
DOMAIN_CRONS = "domain-crons.json"
DOMAIN_PROMPTS = "engine-template-prompts.json"

# Slice-2 source-of-truth locations (the deployed cron-plus-jobs.json is GENERATED):
#   engine half  -> config/engine-crons.json  (this repo)
#   domain half  -> <pack>/crons/{domain-crons,engine-template-prompts}.json
ENGINE_CRONS_FILE = REPO / "config" / ENGINE_CRONS
PACK_DIR = Path(os.environ.get("CRON_PACK_DIR", "/path/to/pack"))

# Runtime fields stripped when capturing from the live scheduler state.
RUNTIME_FIELDS = {"next_run_at", "last_run_at", "last_run_success",
                  "last_error", "last_delivery_error"}


def sanitize(jobs: list[dict]) -> list[dict]:
    out = []
    for j in jobs:
        sj = {k: v for k, v in j.items() if k not in RUNTIME_FIELDS}
        if isinstance(sj.get("repeat"), dict) and "completed" in sj["repeat"]:
            sj["repeat"] = {k: v for k, v in sj["repeat"].items() if k != "completed"}
        out.append(sj)
    return out


def _by_name(jobs: list[dict]) -> list[dict]:
    return sorted(jobs, key=lambda j: j.get("name", ""))


def _dump_jobs(jobs: list[dict]) -> str:
    """Canonical cron-plus-jobs.json text — name-sorted. DISABLED jobs
    (`enabled: false` placeholders) are NOT written to the deployed artifact:
    cron-plus would otherwise validate their never-fires sentinel expr (`0 0 30 2 *`)
    every tick and log 'invalid cron expr' noise (#27). They stay in the SOURCE
    (the pack's domain-crons.json); flip enabled:true + set a real expr to deploy
    one. The in-memory merge keeps them, so split/compose stay lossless."""
    live = [j for j in jobs if j.get("enabled", True)]
    return json.dumps({"jobs": _by_name(live)}, indent=2, ensure_ascii=False) + "\n"


def _dump_list(jobs: list[dict]) -> str:
    return json.dumps(_by_name(jobs), indent=2, ensure_ascii=False) + "\n"


def _dump_prompts(prompts: dict) -> str:
    return json.dumps(prompts, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _load_jobs(path: Path) -> list[dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    return d["jobs"] if isinstance(d, dict) else d


def _tier_map(path: Path) -> dict[str, str]:
    t = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = {}
    for tier in ("engine", "engine-template", "domain"):
        for name in t.get(tier) or []:
            out[name] = tier
    return out


def split(jobs: list[dict], tier_of: dict[str, str]) -> dict[str, object]:
    engine, domain, prompts = [], [], {}
    for j in jobs:
        name = j["name"]
        tier = tier_of.get(name)
        if tier == "engine":
            engine.append(j)
        elif tier == "engine-template":
            stub = {k: v for k, v in j.items() if k != "prompt"}
            engine.append(stub)               # engine ships the script/schedule
            if "prompt" in j:
                prompts[name] = j["prompt"]   # pack supplies the prompt
        elif tier == "domain":
            domain.append(j)
        else:
            raise SystemExit(f"unclassified job (not in cron-tiers.yaml): {name!r}")
    return {ENGINE_CRONS: engine, DOMAIN_CRONS: domain, DOMAIN_PROMPTS: prompts}


def merge(engine: list[dict], domain: list[dict], prompts: dict[str, str],
          tier_of: dict[str, str] | None = None) -> list[dict]:
    """Re-attach pack prompts onto the engine half.

    A pack OPTS IN to a shared `engine-template` job by supplying its prompt: when
    `tier_of` is given, an engine-template stub with NO pack prompt is SKIPPED
    (otherwise it would ship enabled + promptless = broken — the multi-pack bug).
    Pure-`engine` jobs always ship. `tier_of=None` preserves the legacy behavior
    (ship every engine cron) so the round-trip check stays a pure symmetry test."""
    out = []
    for j in engine:
        name = j["name"]
        if tier_of is not None and tier_of.get(name) == "engine-template" \
                and name not in prompts:
            continue                          # pack didn't opt into this template job
        j = dict(j)
        if name in prompts:                   # re-attach engine-template prompt
            j["prompt"] = prompts[name]
        out.append(j)
    out.extend(domain)
    return out


def merge_packs(engine: list[dict], packs: list[dict],
                tier_of: dict[str, str]) -> tuple[list[dict], list[str]]:
    """N-way compose: engine half + N packs -> one job list (composable okpacks P3).

    Each pack is ``{"name": str, "domain": [jobs], "prompts": {job: prompt}}``.
    Rules (additive / disjoint / fail-loud):
      - pure-`engine` jobs always ship; `engine-template` stubs ship only as
        **per-pack instances** ``<job>@<pack>`` (so two packs can both drive
        e.g. entity-backfill without colliding);
      - domain jobs are **pack-prefixed** ``<pack>:<job>`` (no silent collision);
      - a prompt for a non-engine-template job, or a duplicate job id, is an ERROR.
    Returns ``(jobs, errors)`` — a non-empty error list means do not deploy.
    """
    errors: list[str] = []
    out: list[dict] = [dict(j) for j in engine if tier_of.get(j["name"]) != "engine-template"]
    engine_by_name = {j["name"]: j for j in engine}
    seen: dict[str, str] = {}

    for pk in packs:
        pname = pk["name"]
        for jobname, prompt in (pk.get("prompts") or {}).items():
            base = engine_by_name.get(jobname)
            if base is None or tier_of.get(jobname) != "engine-template":
                errors.append(f"{pname}: prompt for '{jobname}' which is not an "
                              "engine-template job")
                continue
            inst = dict(base)
            inst["name"] = f"{jobname}@{pname}"
            inst["prompt"] = prompt
            if inst["name"] in seen:
                errors.append(f"job-id collision: {inst['name']}")
            seen[inst["name"]] = pname
            out.append(inst)
        for j in (pk.get("domain") or []):
            j2 = dict(j)
            j2["name"] = f"{pname}:{j.get('name')}"
            if j2["name"] in seen:
                errors.append(f"job-id collision: {j2['name']}")
            seen[j2["name"]] = pname
            out.append(j2)
    return out, errors


def _pack_meta():
    """Load the sibling pack_meta module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "pack_meta.py"
    spec = importlib.util.spec_from_file_location("pack_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def discover_packs(packs_dir: Path) -> list[dict]:
    """Enumerate installed packs — subdirs of `packs_dir` that carry a `pack.yaml`
    (presence-based). Each -> ``{name, meta, domain, prompts}``."""
    pm = _pack_meta()
    out: list[dict] = []
    packs_dir = Path(packs_dir)
    if not packs_dir.is_dir():
        return out
    for d in sorted(packs_dir.iterdir()):
        if not d.is_dir():
            continue
        meta = pm.load_pack_meta(d)
        if meta is None:
            continue
        dc, dp = d / "crons" / DOMAIN_CRONS, d / "crons" / DOMAIN_PROMPTS
        out.append({
            "name": meta["name"], "meta": meta,
            "domain": _load_jobs(dc) if dc.is_file() else [],
            "prompts": json.loads(dp.read_text(encoding="utf-8")) if dp.is_file() else {},
        })
    return out


def compose(packs_dir: Path) -> tuple[list[dict], list[str]]:
    """Compose the engine half + ALL installed packs -> ``(jobs, errors)``. Errors
    come from composition validation (disjoint ownership / requires / single trust)
    AND the N-way job merge. A non-empty error list means DO NOT deploy."""
    pm = _pack_meta()
    engine = json.loads(ENGINE_CRONS_FILE.read_text())
    packs = discover_packs(packs_dir)
    errors = pm.validate_composition([p["meta"] for p in packs])
    jobs, merge_errors = merge_packs(engine, packs, _tier_map(TIERS))
    return jobs, errors + merge_errors


def regen_composed(packs_dir: Path) -> list[dict]:
    """compose + write config/cron-plus-jobs.json. Raises if the composition is
    unsound (writes nothing) — fail-loud before a bad deploy."""
    jobs, errors = compose(packs_dir)
    if errors:
        raise SystemExit("composition errors (not deploying):\n  " + "\n  ".join(errors))
    JOBS.write_text(_dump_jobs(jobs), encoding="utf-8")
    return jobs


def _write_split(parts: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for fname, data in parts.items():
        (out / fname).write_text(json.dumps(data, indent=2, sort_keys=False) + "\n",
                                 encoding="utf-8")


def _canon(jobs: list[dict]) -> list:
    # order-insensitive, field-identical comparison key
    return sorted((json.dumps(j, sort_keys=True) for j in jobs))


def regen() -> list[dict]:
    """Generate config/cron-plus-jobs.json from the engine half + the domain pack."""
    engine = json.loads(ENGINE_CRONS_FILE.read_text())
    domain = json.loads((PACK_DIR / "crons" / DOMAIN_CRONS).read_text())
    prompts = json.loads((PACK_DIR / "crons" / DOMAIN_PROMPTS).read_text())
    merged = merge(engine, domain, prompts, tier_of=_tier_map(TIERS))
    JOBS.write_text(_dump_jobs(merged), encoding="utf-8")
    print(f"regen: {len(engine)} engine-half + {len(domain)} domain + {len(prompts)} "
          f"prompts -> {JOBS.name} ({len(merged)} jobs)")
    return merged


def dump_from_live(livefile: str) -> None:
    """Capture live scheduler state -> engine half + pack, then regenerate."""
    live = json.loads(Path(livefile).read_text())
    jobs = sanitize(live["jobs"] if isinstance(live, dict) else live)
    parts = split(jobs, _tier_map(TIERS))
    ENGINE_CRONS_FILE.write_text(_dump_list(parts[ENGINE_CRONS]), encoding="utf-8")
    (PACK_DIR / "crons").mkdir(parents=True, exist_ok=True)
    (PACK_DIR / "crons" / DOMAIN_CRONS).write_text(_dump_list(parts[DOMAIN_CRONS]), encoding="utf-8")
    (PACK_DIR / "crons" / DOMAIN_PROMPTS).write_text(_dump_prompts(parts[DOMAIN_PROMPTS]), encoding="utf-8")
    print(f"dump: live -> engine-crons.json ({len(parts[ENGINE_CRONS])}) + pack "
          f"domain ({len(parts[DOMAIN_CRONS])}) + prompts ({len(parts[DOMAIN_PROMPTS])})")
    regen()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="check",
                    choices=["check", "split", "merge", "regen", "compose", "dump"])
    ap.add_argument("--in", dest="indir")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--live", dest="live", help="live jobs.json for the dump command")
    ap.add_argument("--packs", dest="packs",
                    help="packs directory for the compose command (N-way)")
    args = ap.parse_args(argv)

    if args.cmd == "regen":
        regen()
        return 0
    if args.cmd == "compose":
        pdir = args.packs or os.environ.get("CRON_PACKS_DIR")
        if not pdir:
            ap.error("compose requires --packs DIR (or CRON_PACKS_DIR)")
        jobs, errors = compose(Path(pdir))
        if errors:
            print("composition errors (not writing):")
            for e in errors:
                print(f"  - {e}")
            return 1
        JOBS.write_text(_dump_jobs(jobs), encoding="utf-8")
        print(f"composed {len(jobs)} jobs from {pdir} -> {JOBS.name}")
        return 0
    if args.cmd == "dump":
        if not args.live:
            ap.error("dump requires --live <jobs.json>")
        dump_from_live(args.live)
        return 0

    tier_of = _tier_map(TIERS)

    if args.cmd == "split":
        jobs = _load_jobs(JOBS)
        parts = split(jobs, tier_of)
        out = Path(args.out or (REPO / "build" / "cron-pack"))
        _write_split(parts, out)
        print(f"split {len(jobs)} jobs -> {out}/")
        print(f"  {ENGINE_CRONS}: {len(parts[ENGINE_CRONS])} (engine + engine-template scripts)")
        print(f"  {DOMAIN_CRONS}: {len(parts[DOMAIN_CRONS])} (domain)")
        print(f"  {DOMAIN_PROMPTS}: {len(parts[DOMAIN_PROMPTS])} engine-template prompts")
        return 0

    if args.cmd == "merge":
        d = Path(args.indir or (REPO / "build" / "cron-pack"))
        engine = json.loads((d / ENGINE_CRONS).read_text())
        domain = json.loads((d / DOMAIN_CRONS).read_text())
        prompts = json.loads((d / DOMAIN_PROMPTS).read_text())
        merged = merge(engine, domain, prompts)
        text = json.dumps({"jobs": merged}, indent=2) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"merged {len(merged)} jobs -> {args.out}")
        else:
            sys.stdout.write(text)
        return 0

    # check: round-trip
    jobs = _load_jobs(JOBS)
    with tempfile.TemporaryDirectory() as td:
        parts = split(jobs, tier_of)
        _write_split(parts, Path(td))
        engine = json.loads((Path(td) / ENGINE_CRONS).read_text())
        domain = json.loads((Path(td) / DOMAIN_CRONS).read_text())
        prompts = json.loads((Path(td) / DOMAIN_PROMPTS).read_text())
        merged = merge(engine, domain, prompts)
    ok = _canon(jobs) == _canon(merged)
    print(f"round-trip: input={len(jobs)} jobs, "
          f"split=({len(parts[ENGINE_CRONS])} engine-half / "
          f"{len(parts[DOMAIN_CRONS])} domain / {len(parts[DOMAIN_PROMPTS])} prompts), "
          f"merged={len(merged)} jobs")
    if ok:
        print("✓ lossless — boundary is real (merge(split(x)) == x)")
        return 0
    a, b = set(_canon(jobs)), set(_canon(merged))
    print("✗ MISMATCH", file=sys.stderr)
    for j in list(a - b)[:3]:
        print("  only in input:", j[:200], file=sys.stderr)
    for j in list(b - a)[:3]:
        print("  only in merged:", j[:200], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
