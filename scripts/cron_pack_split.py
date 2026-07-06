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
# Extension-tier jobs (okengine#141): folded by the extension pass, NOT a cron-tiers.yaml
# source — carried through split/merge as their own partition so the boundary stays lossless.
EXTENSIONS = "extensions"

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


def _is_extension_job(j: dict, tier_of: dict[str, str]) -> bool:
    """An extension-tier job (okengine#141): carries the composer's `extension` marker, or
    (legacy artifacts) is unclassified by cron-tiers but namespaced like an extension id
    (`<id>` / `<id>:<op>` — chars engine/pack cron names never use)."""
    if j.get("extension"):
        return True
    name = j.get("name", "")
    return tier_of.get(name) is None and ("." in name or ":" in name)


def split(jobs: list[dict], tier_of: dict[str, str]) -> dict[str, object]:
    engine, domain, prompts, extensions = [], [], {}, []
    for j in jobs:
        name = j["name"]
        if _is_extension_job(j, tier_of):
            extensions.append(j)              # regenerated by the extension pass, not cron-tiers
            continue
        if j.get("pack"):                     # okengine#143: pack-domain provenance marker —
            domain.append(j)                  # cron-tiers `domain:` is empty by design, so the
            continue                          # marker is how split routes a pack's own crons
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
    return {ENGINE_CRONS: engine, DOMAIN_CRONS: domain, DOMAIN_PROMPTS: prompts,
            EXTENSIONS: extensions}


def merge(engine: list[dict], domain: list[dict], prompts: dict[str, str],
          tier_of: dict[str, str] | None = None,
          extensions: list[dict] | None = None) -> list[dict]:
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
    out.extend(extensions or [])              # carry extension-tier jobs through (#141)
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
            j2.setdefault("pack", pname)       # provenance marker (okengine#143)
            if j2["name"] in seen:
                errors.append(f"job-id collision: {j2['name']}")
            seen[j2["name"]] = pname
            out.append(j2)
    return out, errors


def _pack_name(pack_dir) -> str:
    """The pack's declared name (pack.yaml) for the #143 provenance marker; falls back to
    the directory name when there's no pack.yaml."""
    try:
        meta = _pack_meta().load_pack_meta(pack_dir)
    except Exception:
        meta = None
    return (meta or {}).get("name") or Path(pack_dir).name


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


def _extension_pass(pack_dir: Path, existing: list[dict]) -> tuple[list[dict], list[str]]:
    """Fold enabled-extension cron jobs into the deployed fleet (#113 composer).

    A no-op when the pack has no `.okengine/extensions.yaml` or the composer module
    is absent — so this is zero-impact until an operator enables an extension."""
    p = Path(__file__).resolve().parent / "extension_compose.py"
    if not p.is_file():
        return [], []
    import importlib.util
    spec = importlib.util.spec_from_file_location("extension_compose", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.extension_jobs(pack_dir, {j["name"] for j in existing})


def regen() -> list[dict]:
    """Generate config/cron-plus-jobs.json from the engine half + the domain pack +
    any enabled extensions (#113). Fail-loud before writing on a composition error."""
    engine = json.loads(ENGINE_CRONS_FILE.read_text())
    domain = json.loads((PACK_DIR / "crons" / DOMAIN_CRONS).read_text())
    pname = _pack_name(PACK_DIR)
    for j in domain:                          # provenance marker (okengine#143) so split/dump
        j.setdefault("pack", pname)           # can route a pack's own crons back to domain
    prompts = json.loads((PACK_DIR / "crons" / DOMAIN_PROMPTS).read_text())
    merged = merge(engine, domain, prompts, tier_of=_tier_map(TIERS))
    ext_jobs, ext_errors = _extension_pass(PACK_DIR, merged)
    if ext_errors:
        raise SystemExit("extension composition errors (not deploying):\n  "
                         + "\n  ".join(ext_errors))
    merged = merged + ext_jobs
    _, order_errors = validate_ordering(merged)        # okengine#129: fail-loud on broken/cyclic after:
    if order_errors:
        raise SystemExit("cron ordering errors (not deploying):\n  " + "\n  ".join(order_errors))
    JOBS.write_text(_dump_jobs(merged), encoding="utf-8")
    print(f"regen: {len(engine)} engine-half + {len(domain)} domain + {len(prompts)} "
          f"prompts + {len(ext_jobs)} extension -> {JOBS.name} ({len(merged)} jobs)")
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


def validate_ordering(jobs: list[dict]) -> tuple[list[str], list[str]]:
    """Validate cross-job `after:` dependencies (okengine#129): every `after` target must name a
    job in the fleet, and the dependency graph must be acyclic. Returns (topological order of job
    names, errors). A non-empty errors list means the fleet's ordering is unsound — do not deploy.

    This is the enforcement GATE for the ordering contract: `after:` is a HARD dependency (a lane
    that consumes another lane's output), unlike `tier:` (an advisory kickstart-stage hint).
    Runtime ordering (staggered schedules / wake-gate freshness) is a later phase; this catches a
    broken or circular dependency before it ships."""
    from collections import deque
    by_name = {j["name"]: j for j in jobs}
    errors: list[str] = []
    adj: dict[str, list[str]] = {n: [] for n in by_name}
    indeg: dict[str, int] = {n: 0 for n in by_name}
    for j in jobs:
        for a in (j.get("after") or []):
            if not isinstance(a, str):
                continue
            if a == j["name"]:
                errors.append(f"job {j['name']!r} declares after: itself")
                continue
            if a not in by_name:
                errors.append(f"job {j['name']!r} declares after: {a!r} but no such job in the fleet")
                continue
            adj[a].append(j["name"])
            indeg[j["name"]] += 1
    q = deque(sorted(n for n, d in indeg.items() if d == 0))
    order: list[str] = []
    while q:
        n = q.popleft()
        order.append(n)
        for m in sorted(adj[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    if len(order) != len(by_name):
        cyc = sorted(n for n in by_name if n not in set(order))
        errors.append(f"cyclic after: dependency among jobs: {cyc}")
    return order, errors


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
        ext_file = d / EXTENSIONS
        extensions = json.loads(ext_file.read_text()) if ext_file.exists() else []
        merged = merge(engine, domain, prompts, extensions=extensions)
        text = json.dumps({"jobs": merged}, indent=2) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"merged {len(merged)} jobs -> {args.out}")
        else:
            sys.stdout.write(text)
        return 0

    # check: round-trip. Extension-tier jobs aren't a cron-tiers source — they're folded fresh
    # by the composer at deploy — so split routes them to their own EXTENSIONS partition, which
    # must pass back through merge unchanged for the round-trip to be lossless (#152/#141).
    jobs = _load_jobs(JOBS)
    with tempfile.TemporaryDirectory() as td:
        parts = split(jobs, tier_of)
        _write_split(parts, Path(td))
        engine = json.loads((Path(td) / ENGINE_CRONS).read_text())
        domain = json.loads((Path(td) / DOMAIN_CRONS).read_text())
        prompts = json.loads((Path(td) / DOMAIN_PROMPTS).read_text())
        extensions = json.loads((Path(td) / EXTENSIONS).read_text())
        merged = merge(engine, domain, prompts, extensions=extensions)
    ok = _canon(jobs) == _canon(merged)
    print(f"round-trip: input={len(jobs)} jobs, "
          f"split=({len(parts[ENGINE_CRONS])} engine-half / "
          f"{len(parts[DOMAIN_CRONS])} domain / {len(parts[DOMAIN_PROMPTS])} prompts), "
          f"merged={len(merged)} jobs")
    order, order_errors = validate_ordering(jobs)      # okengine#129: after: graph soundness
    if order_errors:
        print("✗ ORDERING", file=sys.stderr)
        for e in order_errors:
            print("  " + e, file=sys.stderr)
    if ok and not order_errors:
        ndeps = sum(len(j.get("after") or []) for j in jobs)
        print(f"✓ lossless — boundary is real (merge(split(x)) == x); "
              f"after: graph acyclic ({ndeps} dep edge(s))")
        return 0
    if not ok:
        a, b = set(_canon(jobs)), set(_canon(merged))
        print("✗ MISMATCH", file=sys.stderr)
        for j in list(a - b)[:3]:
            print("  only in input:", j[:200], file=sys.stderr)
        for j in list(b - a)[:3]:
            print("  only in merged:", j[:200], file=sys.stderr)
    return 1


def missing_lane_scripts(jobs, scripts_root):
    """Lane `script`s not staged under scripts_root — the deploy guard's logic. A lane whose
    script does not resolve runs the agent with NO wake-gate and writes nothing (silent no-op);
    this is exactly how an enabled extension whose scripts were never deploy-cron-scripts'd goes
    dark. Returns [(lane_name, script)] for each missing one."""
    import os
    PREFIX = "/opt/data/scripts/"
    out = []
    for j in jobs:
        s = (j.get("script") or "").strip()
        if not s:
            continue
        rel = s[len(PREFIX):] if s.startswith(PREFIX) else s
        if not os.path.isfile(os.path.join(scripts_root, rel)):
            out.append((j.get("name"), s))
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
