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
import hashlib
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
RUNTIME_FIELDS = {"next_run_at", "last_run_at", "last_run_success", "last_completed_at",
                  "last_error", "last_delivery_error", "after_claim", "after_consumed"}
# A runtime PAUSE (budget-guard cost cap, or a manual `cron-plus pause`) is stored IN jobs.json as
# {enabled: false, paused_at: ...}. `paused_at` is the marker that distinguishes a pause from an
# intentional source-level `enabled: false` (a ship-disabled placeholder).
PAUSE_MARKERS = {"paused_at", "paused_reason", "paused_by"}


def sanitize(jobs: list[dict]) -> list[dict]:
    out = []
    for j in jobs:
        sj = {k: v for k, v in j.items() if k not in RUNTIME_FIELDS}
        # A runtime pause must NOT be captured as source truth: `dump` writes the sanitized jobs into
        # config/engine-crons.json + the pack's domain-crons.json, and regen() drops every
        # enabled:false job from the deployed artifact — so a `dump` run while the budget guard has
        # cost-bearing crons paused would SILENTLY REMOVE them from the fleet on the next deploy
        # (invariant-audit HIGH). A pause is runtime state; un-pause it on capture. TRUTHINESS, not
        # key-presence: cron-plus RESUME sets paused_at back to None but KEEPS the key, so a
        # paused-then-resumed job carries paused_at:null forever — a key-presence check would then
        # force-flip a later OPERATOR-disabled (plain enabled:false) job back to enabled on dump
        # (re-verify regression). Only a job actually paused NOW has a truthy paused_at.
        if sj.get("paused_at"):
            for m in PAUSE_MARKERS:
                sj.pop(m, None)
            sj["enabled"] = True
        else:
            sj.pop("paused_at", None)          # drop a lingering paused_at:null (never source truth)
        if isinstance(sj.get("repeat"), dict) and "completed" in sj["repeat"]:
            sj["repeat"] = {k: v for k, v in sj["repeat"].items() if k != "completed"}
        out.append(sj)
    return out


def _by_name(jobs: list[dict]) -> list[dict]:
    return sorted(jobs, key=lambda j: j.get("name", ""))


def _normalize_schedule(job: dict) -> dict:
    """cron-plus requires `schedule` to be a DICT ({kind:cron, expr:...}); the pack-authoring docs
    AND framework_validate._cron_expr also accept a BARE STRING ("0 13 * * SUN") and a TOP-LEVEL
    `expr` (no schedule key). Normalize BOTH looser shapes into the dict HERE — the single chokepoint
    that writes the deployed jobs.json — so a job that VALIDATES GREEN can't deploy in a shape
    cron-plus silently never fires:
      - bare string → 'str has no attribute get' crashes every tick, stalling the fleet (audit #1);
      - top-level {expr} → deploys with NO schedule key, so compute_next_run returns None,
        next_run_at stays null, and Phase 2 skips the job every tick forever, no error logged
        (audit HIGH #5)."""
    s = job.get("schedule")
    if isinstance(s, str) and s.strip():
        job = dict(job)
        job["schedule"] = {"kind": "cron", "expr": s.strip()}
    elif s is None and isinstance(job.get("expr"), str) and job["expr"].strip():
        job = dict(job)
        job["schedule"] = {"kind": "cron", "expr": job["expr"].strip()}
    return job


def _ensure_id(job: dict) -> dict:
    """Every deployed cron MUST carry an `id`. The pinned cron-plus scheduler's null-next_run_at heal
    logs via `job.get("name", job["id"])` — Python evaluates the default arg EAGERLY, so an id-less
    job raises KeyError('id') on the FIRST tick (even with a name present), the exception escapes the
    claim before anything persists, and the ticker re-crashes every 60s FOREVER — stalling the WHOLE
    fleet, engine lanes included, with every gate green (invariant-audit CRITICAL). The pack-authoring
    docs never require id, so mint a stable one from the (already pack-prefixed, unique) name at this
    single deploy chokepoint — no id-less job can reach the deployed jobs.json."""
    if not str(job.get("id") or "").strip():
        job = dict(job)
        # Stable non-security identifier; collision resistance is not an auth boundary.
        job["id"] = hashlib.sha1(
            str(job.get("name") or "").encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:12]
    return job


def _dump_jobs(jobs: list[dict]) -> str:
    """Canonical cron-plus-jobs.json text — name-sorted. DISABLED jobs
    (`enabled: false` placeholders) are NOT written to the deployed artifact:
    cron-plus would otherwise validate their never-fires sentinel expr (`0 0 30 2 *`)
    every tick and log 'invalid cron expr' noise (#27). They stay in the SOURCE
    (the pack's domain-crons.json); flip enabled:true + set a real expr to deploy
    one. The in-memory merge keeps them, so split/compose stay lossless."""
    live = [_ensure_id(_normalize_schedule(j)) for j in jobs if j.get("enabled", True)]
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
    if j.get("pack"):
        # A pack-provenance-marked job (okengine#143) is a DOMAIN cron, never an extension — even if
        # its name contains '.'/':'. Without this, a pack cron like `acme.fetch` (pack: okpack-x)
        # was routed to the EXTENSIONS partition and then silently ERASED by dump_from_live, which
        # only writes engine/domain/prompts (invariant-audit #24).
        return False
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
        # Build the pack's FULL rename map first: engine-template stubs the pack drives ->
        # <job>@<pack>, domain jobs -> <pack>:<job>. An intra-pack `after:` target names a SIBLING that
        # was ALSO renamed (a domain job OR a driven engine-template lane) — rewrite it against this map
        # so the dependency resolves to the renamed fleet member instead of dangling at the bare
        # pre-rename name (which validate_ordering then rejects, making the pack undeployable). A target
        # naming a PURE-engine job stays bare — those ship unrenamed (invariant-audit + batch-3 re-verify).
        template_driven = {jn for jn in (pk.get("prompts") or {})
                           if engine_by_name.get(jn) is not None and tier_of.get(jn) == "engine-template"}
        domain_bare = {j.get("name") for j in (pk.get("domain") or [])}
        # The rename map is keyed by BARE name across three namespaces (pure-engine / driven-template /
        # domain); a domain job whose bare name shadows an engine or driven-template lane makes an
        # intra-pack after: target ambiguous — it would silently rebind to the domain twin instead of
        # the engine/template lane the author meant (a silently misordered dependency). Fail loud rather
        # than guess (invariant-audit batch-3 re-verify).
        for nm in sorted(domain_bare & (template_driven | set(engine_by_name))):
            errors.append(f"{pname}: domain job {nm!r} shadows an engine/engine-template lane of the "
                          "same name — rename the domain job so after: targets are unambiguous")
        rename: dict[str, str] = {}
        for jn in template_driven:
            rename[jn] = f"{jn}@{pname}"
        for j in (pk.get("domain") or []):
            rename[j.get("name")] = f"{pname}:{j.get('name')}"

        def _rewrite_after(job: dict) -> dict:
            if job.get("after"):
                job["after"] = [rename.get(a, a) for a in job["after"]]
            return job

        for jobname, prompt in (pk.get("prompts") or {}).items():
            base = engine_by_name.get(jobname)
            if base is None or tier_of.get(jobname) != "engine-template":
                errors.append(f"{pname}: prompt for '{jobname}' which is not an "
                              "engine-template job")
                continue
            inst = _rewrite_after(dict(base))   # the inherited engine after: may name a lane this pack drives
            inst["name"] = f"{jobname}@{pname}"
            inst["prompt"] = prompt
            if inst["name"] in seen:
                errors.append(f"job-id collision: {inst['name']}")
            seen[inst["name"]] = pname
            out.append(inst)
        for j in (pk.get("domain") or []):
            j2 = _rewrite_after(dict(j))
            j2["name"] = f"{pname}:{j.get('name')}"
            j2.setdefault("pack", pname)        # provenance marker (okengine#143)
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
    _, order_errors = validate_ordering(jobs)          # #129: the composed path skipped this gate
    if order_errors:                                   # (single-pack regen had it) — re-verify M-ordering
        raise SystemExit("cron ordering errors (not deploying):\n  " + "\n  ".join(order_errors))
    id_errors = validate_unique_ids(jobs)              # M37: final safety net after N-way compose
    if id_errors:
        raise SystemExit("cron id/name collisions (not deploying):\n  " + "\n  ".join(id_errors))
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
    # A pack with ONLY engine crons has no crons/ dir; tolerate its absence (empty domain set) so
    # regen can run for EVERY pack. Otherwise the deploy skipped regen for such packs and shipped a
    # stale leftover — possibly another pack's job set on a multi-pack host (invariant-audit #12).
    _dc, _dp = PACK_DIR / "crons" / DOMAIN_CRONS, PACK_DIR / "crons" / DOMAIN_PROMPTS
    domain = json.loads(_dc.read_text()) if _dc.is_file() else []
    pname = _pack_name(PACK_DIR)
    for j in domain:                          # provenance marker (okengine#143) so split/dump
        j.setdefault("pack", pname)           # can route a pack's own crons back to domain
    prompts = json.loads(_dp.read_text()) if _dp.is_file() else []
    merged = merge(engine, domain, prompts, tier_of=_tier_map(TIERS))
    ext_jobs, ext_errors = _extension_pass(PACK_DIR, merged)
    if ext_errors:
        raise SystemExit("extension composition errors (not deploying):\n  "
                         + "\n  ".join(ext_errors))
    merged = merged + ext_jobs
    _, order_errors = validate_ordering(merged)        # okengine#129: fail-loud on broken/cyclic after:
    if order_errors:
        raise SystemExit("cron ordering errors (not deploying):\n  " + "\n  ".join(order_errors))
    id_errors = validate_unique_ids(merged)            # M37: fail-loud on a colliding deployed id/name
    if id_errors:
        raise SystemExit("cron id/name collisions (not deploying):\n  " + "\n  ".join(id_errors))
    JOBS.write_text(_dump_jobs(merged), encoding="utf-8")
    print(f"regen: {len(engine)} engine-half + {len(domain)} domain + {len(prompts)} "
          f"prompts + {len(ext_jobs)} extension -> {JOBS.name} ({len(merged)} jobs)")
    return merged


def _restore_source_reprs(jobs: list[dict]) -> list[dict]:
    """dump-from-live reads the DEPLOYED jobs.json, whose schedules + models were EXPANDED by the
    deploy-only transform (deploy-cron-plus-jobs.sh): cron_jitter turned `@jitter:*`/`@morning[:MM]`
    into a concrete PER-INSTALL cron expr, and model_profiles expanded an `@profile` model ref into
    concrete provider/base_url/model/ollama_num_ctx. Writing those resolved values back to the SHARED
    source would bake ONE install's jitter minute + endpoint into everyone's source, permanently
    destroying the sentinel / profile indirection (invariant-audit M20). For every job that still
    exists in the current source, restore the source's sentinel schedule + `@profile` model (and drop
    the deploy-baked profile fields), so a live->source round-trip is idempotent for the deploy-only
    transforms. Non-transformed edits (enabled, prompt, after, new jobs) are still captured from live."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cron_jitter
        import model_profiles
    except Exception:
        return jobs                                     # resolver libs absent -> capture live verbatim
    src_by_name: dict[str, dict] = {}
    src_by_id: dict[str, dict] = {}
    for f in (ENGINE_CRONS_FILE, PACK_DIR / "crons" / DOMAIN_CRONS):
        try:
            for j in json.loads(f.read_text()):
                src_by_name.setdefault(j.get("name"), j)
                if j.get("id"):
                    src_by_id.setdefault(j.get("id"), j)
        except Exception:
            pass
    out = []
    for j in jobs:
        s = src_by_name.get(j.get("name")) or src_by_id.get(j.get("id"))   # also match a RENAMED job by id
        if s:
            j = dict(j)
            # Schedule: use cron_jitter._job_expr — it reads ALL THREE documented shapes (dict,
            # bare-string, top-level `expr`); a hand-rolled `schedule.expr` read misses the top-level
            # shape and re-bakes its per-install jitter (re-verify). Restore the source's schedule
            # representation verbatim (drop the live resolved fields first).
            if cron_jitter.is_sentinel(cron_jitter._job_expr(s) or "") \
                    or cron_jitter.is_morning_sentinel(cron_jitter._job_expr(s) or ""):
                j.pop("schedule", None)
                j.pop("expr", None)
                if "schedule" in s:
                    j["schedule"] = s["schedule"]
                if "expr" in s:
                    j["expr"] = s["expr"]
            # Model: restore the @profile ref, but only DROP a profile field the deploy actually baked
            # (absent from source); a field the SOURCE set independently of the profile is preserved.
            if model_profiles.is_ref(s.get("model") or ""):
                j["model"] = s["model"]
                for pf in model_profiles.PROFILE_FIELDS:
                    if pf == "model":
                        continue
                    if pf in s:
                        j[pf] = s[pf]                   # source-set field -> keep source's value
                    else:
                        j.pop(pf, None)                 # deploy-baked endpoint field -> drop
        out.append(j)
    return out


def dump_from_live(livefile: str) -> None:
    """Capture live scheduler state -> engine half + pack, then regenerate."""
    live = json.loads(Path(livefile).read_text())
    jobs = sanitize(live["jobs"] if isinstance(live, dict) else live)
    jobs = _restore_source_reprs(jobs)   # keep source sentinels/@profiles, not the deploy-baked values (M20)
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


def validate_unique_ids(jobs: list[dict]) -> list[str]:
    """cron-plus keys every job by `id` (minted from `name` when absent — see _ensure_id), so a
    duplicate deployed id — or name, which mints a colliding id — means one job silently runs the
    OTHER's definition and the twin NEVER fires: a whole lane vanishes with every gate green
    (invariant-audit M37). The multi-pack composer prefixes names to avoid this, but single-pack
    merge + the extension pass have no gate, and validate_ordering/_by_name both collapse a dup name
    into one dict entry (so they can't see it). Check the FINAL deployed shape (post _ensure_id) over
    the ENABLED jobs (disabled placeholders never deploy). Returns errors — non-empty = do not deploy."""
    errors: list[str] = []
    seen_id: dict[str, str] = {}
    seen_name: dict[str, str] = {}
    for j in (_ensure_id(x) for x in jobs if x.get("enabled", True)):
        jid, nm = j.get("id"), j.get("name")
        if jid in seen_id:
            errors.append(f"duplicate cron id {jid!r}: {seen_id[jid]!r} and {nm!r} "
                          "(cron-plus keys by id — one runs the other's def, the twin never fires)")
        else:
            seen_id[jid] = nm
        if nm in seen_name:
            errors.append(f"duplicate cron name {nm!r} (mints a colliding id)")
        else:
            seen_name[nm] = jid
    return errors


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
        _, order_errors = validate_ordering(jobs)          # same gates as regen_composed — the CLI
        errors = errors + order_errors + validate_unique_ids(jobs)   # subcommand bypassed both (re-verify)
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
    id_errors = validate_unique_ids(jobs)              # M37: the self-test guarding the committed
    order_errors = order_errors + id_errors            # artifact must catch a colliding id/name too
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
