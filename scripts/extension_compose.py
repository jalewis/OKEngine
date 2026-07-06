#!/usr/bin/env python3
"""extension_compose — synthesize cron jobs from enabled extensions (#113 composer).

The composer half of the extension lifecycle (docs/design/extension-lifecycle.md).
It consumes the #134 discovery scanner + enabled-state and turns each enabled
`operation` extension into a namespaced cron job, fail-loud on conflicts — the
§9 invariants: generated-from-source, namespaced (`<id>`), fail-before-runtime.

MVP scope (forced by undone dependencies, each flagged at its skip):
  - only `kind: operation`, `trust: in-gateway`, script entrypoint synthesizes a job;
  - `sidecar` trust / image entrypoint is deferred to #135 (WARN, no job);
  - the synthesized job references the script by basename — staging it into the
    gateway's /opt/data/scripts/ is deploy-time work owned by #128;
  - `operation.timeout` enforcement is owned by #135, so it is NOT emitted onto the
    job yet (cron-plus has no timeout field today — sidecar-contract.md §1).

Returns ``(jobs, errors, warnings)`` throughout — a non-empty errors list is a hard
gate (do not enable / do not deploy).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Surfaces the synthesized job opts into: read via the query MCP, write via the
# enforced okengine-write path (the §4 MCP-client contract).
_DEFAULT_TOOLSETS = ["okengine", "okengine-write"]
_WORKDIR = "/opt/vault"
# Where the gateway runs cron scripts from. Extension scripts are staged into a
# per-extension subdir <SCRIPTS_ROOT>/<id>/ (#128) — namespaced so two extensions'
# run.py can't collide, and isolated from the flat engine/pack scripts dir.
SCRIPTS_ROOT = "/opt/data/scripts"
TRIGGER_NAME = "trigger.sh"            # the generated sidecar launcher (#135)


def _discovery_mod():
    p = _HERE / "extension_discovery.py"
    spec = importlib.util.spec_from_file_location("extension_discovery", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _job_id(ext_id: str) -> str:
    """Deterministic 12-hex job id from the extension id — reproducible from the
    manifest (no clock/random), so regeneration is stable."""
    return hashlib.sha1(ext_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]  # deterministic id, not security


def _iter_ops(m: dict) -> tuple[list[tuple[str | None, dict]], str | None]:
    """The operations an extension declares -> (ops, error).

    Back-compat: a singular ``operation:`` block -> one op named ``None`` (job
    named ``<id>``). A plural ``operations:`` map -> one op per entry (jobs named
    ``<id>:<op>``, realizing the discovery-spec §3.5 ``<id>:<local>`` namespacing).
    Exactly one of the two forms; ``error`` is set on a malformed shape."""
    has_single = isinstance(m.get("operation"), dict)
    has_multi = isinstance(m.get("operations"), dict)
    if has_single and has_multi:
        return [], "declares both 'operation' and 'operations' — use exactly one"
    if has_multi:
        ops = m["operations"]
        if not ops:
            return [], "'operations' map is empty"
        out: list[tuple[str | None, dict]] = []
        for name in sorted(ops):
            if not isinstance(name, str) or not name.strip():
                return [], f"operations key {name!r} must be a non-empty string"
            if not isinstance(ops[name], dict):
                return [], f"operations[{name!r}] must be a block"
            out.append((name, ops[name]))
        return out, None
    if has_single:
        return [(None, m["operation"])], None
    return [], "kind=operation requires an 'operation' or 'operations' block"


def _ops_from_dropins(ext_dir) -> tuple[list[tuple[str, dict]], str | None]:
    """`crons/*.cron.json` drop-in op files -> [(op_name, block)] (#63 P1, the drop-in model).

    Each file is ONE operation block (same shape as an `operations:` entry — schedule /
    entrypoint / prompt_file / toolsets); the op name is the filename stem minus `.cron.json`
    (so `crons/grade.cron.json` -> op `grade` -> job `<id>:grade`). Collected forward-only;
    an extension drops a file in rather than editing a central manifest block."""
    if not ext_dir:
        return [], None
    d = Path(ext_dir) / "crons"
    if not d.is_dir():
        return [], None
    out: list[tuple[str, dict]] = []
    for f in sorted(d.glob("*.cron.json")):  # glob-ok: crons/ is a flat per-extension dir, not a sharded namespace
        op_name = f.name[: -len(".cron.json")]
        if not op_name:
            return [], f"invalid cron drop-in filename: crons/{f.name}"
        try:
            block = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:                                  # noqa: BLE001
            return [], f"crons/{f.name}: invalid JSON ({e})"
        if not isinstance(block, dict):
            return [], f"crons/{f.name}: must be a JSON object (an operation block)"
        out.append((op_name, block))
    return out, None


def _collect_ops(m: dict, ext_dir) -> tuple[list[tuple[str | None, dict]], str | None]:
    """Operations from the manifest (`operation:`/`operations:`) PLUS `crons/*.cron.json`
    drop-ins, merged. At least one source must provide an op; a name appearing in both is a
    fail-loud collision. The drop-in form is what new extensions use (#63); the manifest form
    stays supported for back-compat."""
    manifest_ops: list[tuple[str | None, dict]] = []
    if m.get("operation") is not None or m.get("operations") is not None:
        manifest_ops, err = _iter_ops(m)
        if err:
            return [], err
    drop_ops, err = _ops_from_dropins(ext_dir)
    if err:
        return [], err
    if not manifest_ops and not drop_ops:
        return [], ("kind=operation requires an 'operation'/'operations' block "
                    "or crons/*.cron.json drop-in files")
    seen: set[str] = set()
    combined: list[tuple[str | None, dict]] = []
    for name, blk in [*manifest_ops, *drop_ops]:
        key = name or ""
        if key in seen:
            return [], f"duplicate operation {name!r} (manifest + crons/ collision)"
        seen.add(key)
        combined.append((name, blk))
    return combined, None


def _resolve_prompt(op: dict, ext_dir) -> tuple[str | None, str | None]:
    """The operation's prompt -> (prompt | None, error | None). An inline ``prompt``
    wins; else a bundled ``prompt_file`` is loaded relative to the extension dir (so
    long grading prompts live as files, not crammed into YAML). Pack/operator
    overrides are applied later, by job name, over whatever this returns."""
    p = op.get("prompt")
    if isinstance(p, str) and p.strip():
        return p, None
    pf = op.get("prompt_file")
    if isinstance(pf, str) and pf.strip():
        if not ext_dir:
            return None, f"prompt_file {pf!r} given but the extension dir is unknown"
        path = Path(ext_dir) / pf
        if not path.is_file():
            return None, f"prompt_file not found: {pf}"
        return path.read_text(encoding="utf-8"), None
    return None, None


def _synthesize_one(ext_id: str, m: dict, trust, op_name: str | None, op: dict,
                    ext_dir=None) -> tuple[dict | None, list[str], list[str]]:
    """One operation -> (job | None, errors, warnings). The job is namespaced
    ``<id>`` (singular form) or ``<id>:<op>`` (multi-op), so its id/name are unique."""
    errors: list[str] = []
    warnings: list[str] = []
    name = ext_id if op_name is None else f"{ext_id}:{op_name}"
    label = ext_id if op_name is None else f"{ext_id} op {op_name!r}"

    entrypoint = op.get("entrypoint")
    sched = op.get("schedule")
    sched_ok = (isinstance(sched, dict) and sched.get("kind") == "cron"
                and bool(sched.get("expr")))

    # sidecar / image entrypoint -> a TRIGGER job whose script is the generated
    # wrapper that launches the container (okengine#135). The wrapper + the compose
    # service are materialized by the deploy from sidecar_specs().
    if trust == "sidecar" or (isinstance(entrypoint, dict) and "image" in entrypoint):
        if not (isinstance(entrypoint, dict) and "image" in entrypoint):
            errors.append(f"{label}: trust=sidecar requires operation.entrypoint.image")
            return None, errors, warnings
        if not sched_ok:
            errors.append(f"{label}: operation.schedule must be {{kind: cron, expr: ...}}")
            return None, errors, warnings
        job = {
            "id": _job_id(name),
            "name": name,
            "enabled": True,
            "schedule": {"kind": "cron", "expr": sched["expr"]},
            "workdir": _WORKDIR,
            "script": f"{SCRIPTS_ROOT}/{ext_id}/{TRIGGER_NAME}",   # generated wrapper (#135)
            "prompt": None,
            "no_agent": True,
            "deliver": "local",
        }
        return job, errors, warnings

    # in-gateway op. Two flavors, discriminated by a `prompt`:
    #   - no_agent: a deterministic script (entrypoint REQUIRED), runs to completion.
    #   - agent:    has a `prompt` (inline) or a bundled `prompt_file`; the agent runs
    #               with the okengine toolsets. The entrypoint, if present, is the
    #               wake-gate selector; omit it to wake the agent on every schedule tick.
    prompt, perr = _resolve_prompt(op, ext_dir)
    if perr:
        errors.append(f"{label}: {perr}")
        return None, errors, warnings
    is_agent = isinstance(prompt, str) and bool(prompt.strip())

    script = None
    if isinstance(entrypoint, str):
        script = entrypoint
    elif isinstance(entrypoint, dict) and "script" in entrypoint:
        script = entrypoint["script"]
    elif entrypoint is not None:
        errors.append(f"{label}: operation.entrypoint must be a script name "
                      "(string or {{script: ...}}) for in-gateway operations")
        return None, errors, warnings
    has_script = isinstance(script, str) and bool(script.strip())
    if script is not None and not has_script:
        errors.append(f"{label}: operation.entrypoint script is empty")
        return None, errors, warnings
    if not is_agent and not has_script:
        errors.append(f"{label}: a no_agent operation needs an entrypoint script "
                      "(or add a 'prompt' to make it an agent operation)")
        return None, errors, warnings
    if not sched_ok:
        errors.append(f"{label}: operation.schedule must be {{kind: cron, expr: ...}}")
        return None, errors, warnings

    job = {
        "id": _job_id(name),
        "name": name,                        # namespaced by construction; globally unique
        "enabled": True,
        "schedule": {"kind": "cron", "expr": sched["expr"]},
        "workdir": _WORKDIR,
        "prompt": prompt if is_agent else None,
        "no_agent": not is_agent,
        "deliver": "local",
        "enabled_toolsets": list(op.get("toolsets") or _DEFAULT_TOOLSETS),
    }
    if has_script:
        # Absolute, namespaced path the staging step (#128) lands the script at.
        # no_agent: the work script; agent: the wake-gate selector.
        job["script"] = f"{SCRIPTS_ROOT}/{ext_id}/{Path(script).name}"
    tier = op.get("tier")               # okengine#129: kickstart-order hint (a stage name)
    if isinstance(tier, str) and tier.strip():
        job["tier"] = tier.strip()
    model = op.get("model")             # per-operation model override: the cron scheduler
    if isinstance(model, str) and model.strip():   # honors job["model"] over the config default,
        job["model"] = model.strip()    # so a low-stakes lane can run on a free/cheap model.
    after = op.get("after")             # okengine#129: hard cross-job dependency(ies)
    if isinstance(after, list):
        deps = [a.strip() for a in after if isinstance(a, str) and a.strip()]
        if deps:
            job["after"] = deps
    return job, errors, warnings


def synthesize_ops(record: dict) -> tuple[list[dict], list[str], list[str]]:
    """One enabled extension -> (jobs, errors, warnings) — one job per declared
    operation. Supports both the singular ``operation:`` and plural ``operations:``
    forms (multi-operation extensions). ``record`` is a discovery record."""
    errors: list[str] = []
    warnings: list[str] = []
    ext_id = record["id"]
    m = record["manifest"]

    if m.get("kind") != "operation":
        # Only operation contributes crons in MVP; other kinds bind to other contracts.
        warnings.append(f"{ext_id}: kind {m.get('kind')!r} contributes no cron (MVP: operation)")
        return [], errors, warnings

    trust = m.get("trust")
    ext_dir = record.get("dir")
    ops, err = _collect_ops(m, ext_dir)         # manifest operation(s) + crons/*.cron.json (#63 P1)
    if err:
        errors.append(f"{ext_id}: {err}")
        return [], errors, warnings

    jobs: list[dict] = []
    local_seen: set[str] = set()
    for op_name, op in ops:
        job, errs, warns = _synthesize_one(ext_id, m, trust, op_name, op, ext_dir)
        errors.extend(errs)
        warnings.extend(warns)
        if job is None:
            continue
        job["extension"] = ext_id               # provenance marker (okengine#141): lets the
        # cron split/dump tooling recognize extension-tier jobs (they regenerate from the
        # extension pass, not from cron-tiers.yaml) instead of failing them as unclassified.
        if job["name"] in local_seen:           # defensive — keys are unique by construction
            errors.append(f"{ext_id}: duplicate operation job name {job['name']}")
        local_seen.add(job["name"])
        jobs.append(job)
    return jobs, errors, warnings


def synthesize_job(record: dict) -> tuple[dict | None, list[str], list[str]]:
    """Back-compat single-job shim — returns the one job a single-operation extension
    yields (or the first, if several). New callers use ``synthesize_ops``."""
    jobs, errors, warnings = synthesize_ops(record)
    return (jobs[0] if jobs else None), errors, warnings


def synthesize_jobs(resolved: dict[str, dict]) -> tuple[list[dict], list[str], list[str]]:
    """Resolved enabled extensions -> (jobs, errors, warnings), with a cross-extension
    job-name collision check (mirrors merge_packs' fail-loud `seen` guard)."""
    jobs: list[dict] = []
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for ext_id in sorted(resolved):
        ext_jobs, errs, warns = synthesize_ops(resolved[ext_id])
        errors.extend(errs)
        warnings.extend(warns)
        for job in ext_jobs:
            if job["name"] in seen:
                errors.append(f"job-id collision among extensions: {job['name']}")
            seen.add(job["name"])
            jobs.append(job)
    return jobs, errors, warnings


def compose(resolved: dict[str, dict], existing_names=None
            ) -> tuple[list[dict], list[str], list[str]]:
    """Synthesize extension jobs and check them against an ``existing_names`` set
    (engine + pack job names) so an extension can't shadow an engine/pack job."""
    jobs, errors, warnings = synthesize_jobs(resolved)
    existing = set(existing_names or ())
    for j in jobs:
        if j["name"] in existing:
            errors.append(f"extension job '{j['name']}' collides with an "
                          "engine/pack job of the same name")
    return jobs, errors, warnings


def effective_ids(pack_dir) -> tuple[list[str], list[str]]:
    """ALL effective extension ids (explicit opt-ins ∪ core-not-disabled) — the set
    About reports. Broader than staging_targets: includes extensions that stage no
    gateway scripts (sidecar/UI-only)."""
    disc = _discovery_mod()
    resolved, errors = disc.resolve_for_pack(Path(pack_dir))
    return sorted(resolved), errors


def effective_records(pack_dir) -> tuple[list[dict], list[str]]:
    """Effective set WITH manifest identity for human surfaces (the About panel):
    [{id, name, description}] — the extension.yaml already says what each one is;
    surfacing it makes About self-answering ('what is completeness?')."""
    disc = _discovery_mod()
    resolved, errors = disc.resolve_for_pack(Path(pack_dir))
    out = []
    for ext_id in sorted(resolved):
        man = (resolved[ext_id].get("manifest") or {})
        out.append({"id": ext_id,
                    "name": str(man.get("name") or ext_id),
                    "description": " ".join(str(man.get("description") or "").split())})
    return out, errors


def staging_targets(pack_dir) -> tuple[list[dict], list[str]]:
    """Which extensions need their scripts staged into the gateway (#128).

    Returns ``(targets, errors)`` where each target is ``{id, dir}`` for an enabled,
    discovered, in-gateway operation that synthesizes a job — i.e. exactly the set
    whose ``script`` path the deploy must materialize at ``<SCRIPTS_ROOT>/<id>/``.
    sidecar/non-operation extensions (which synthesize no in-gateway job) are
    excluded — they don't run from the gateway scripts dir."""
    disc = _discovery_mod()
    pack_dir = Path(pack_dir)
    resolved, errors = disc.resolve_for_pack(pack_dir)   # explicit ∪ core-not-disabled
    targets: list[dict] = []
    for ext_id in sorted(resolved):
        rec = resolved[ext_id]
        if _is_sidecar(rec):
            continue                          # sidecars get a wrapper + compose service, not *.py staging
        job, errs, _ = synthesize_job(rec)
        errors.extend(errs)
        if job is None:
            continue                          # non-operation -> nothing to stage
        targets.append({"id": ext_id, "dir": rec["dir"]})
    return targets, errors


# --- sidecar materialization (okengine#135) -------------------------------

def _is_sidecar(record: dict) -> bool:
    m = record["manifest"]
    if m.get("trust") == "sidecar":
        return True
    ops, _ = _iter_ops(m)                     # singular or plural form
    return any(isinstance(op.get("entrypoint"), dict) and "image" in op["entrypoint"]
               for _, op in ops)


def image_ref(image: dict) -> str:
    """Build a docker image ref from the manifest image block, digest-pinned:
    `<registry>[:<tag>]@<digest>` (tag informational; digest is the wall)."""
    reg = str(image.get("registry") or "").strip()
    tag = str(image.get("tag") or "").strip()
    digest = str(image.get("digest") or "").strip()
    ref = reg
    if tag:
        ref += f":{tag}"
    if digest:
        ref += f"@{digest}"
    return ref


def sidecar_specs(pack_dir) -> tuple[list[dict], list[str]]:
    """Enabled sidecar extensions -> (specs, errors). Each spec carries what the deploy
    needs to materialize a compose service + trigger wrapper + token injection."""
    disc = _discovery_mod()
    pack_dir = Path(pack_dir)
    resolved, errors = disc.resolve_for_pack(pack_dir)   # explicit ∪ core-not-disabled
    specs: list[dict] = []
    for ext_id in sorted(resolved):
        rec = resolved[ext_id]
        if not _is_sidecar(rec):
            continue
        m = rec["manifest"]
        ops, err = _iter_ops(m)               # one sidecar service per image operation
        if err:
            errors.append(f"{ext_id}: {err}")
            continue
        cfg = m.get("config") or {}
        for op_name, op in ops:
            spec_id = ext_id if op_name is None else f"{ext_id}:{op_name}"
            ep = op.get("entrypoint") if isinstance(op.get("entrypoint"), dict) else {}
            image = ep.get("image")
            if not isinstance(image, dict) or not image.get("digest"):
                errors.append(f"{spec_id}: sidecar requires a digest-pinned entrypoint.image")
                continue
            specs.append({
                "id": spec_id,
                "image": image_ref(image),
                "command": image.get("command"),
                "timeout": op.get("timeout"),
                "config": {k: (v.get("default") if isinstance(v, dict) else v)
                           for k, v in cfg.items()},
            })
    return specs, errors


def render_trigger_wrapper(ext_id: str, compose_file: str, project: str) -> str:
    """The `<SCRIPTS_ROOT>/<id>/trigger.sh` wrapper cron-plus runs each tick: launch the
    sidecar once and clean it up (`--rm`). Requires docker reachable from the gateway
    (socket mount) — see docs/design/sidecar-contract.md §3.4."""
    return (
        "#!/usr/bin/env bash\n"
        f"# generated by okengine#135 for extension {ext_id} — DO NOT EDIT\n"
        "set -euo pipefail\n"
        f"exec docker compose -f {compose_file} -p {project} run --rm -T "
        f"{ext_id}-sidecar\n"
    )


def render_sidecar_service(spec: dict, mcp_url: str, write_url: str,
                           read_token: str, write_token: str) -> dict:
    """A HARDENED compose service dict for a sidecar (okengine#124).

    A sidecar is the boundary for UNTRUSTED third-party extension code, so it runs confined:
    the image is digest-pinned (enforced in sidecar_specs — content integrity), it joins the
    per-pack bridge (okengine#138) to reach the MCP endpoints by service name (NOT host net), and
    the container drops all Linux capabilities, forbids privilege escalation, runs a read-only
    rootfs (writable scratch only on a /tmp tmpfs), and is pid/memory/cpu-capped — so untrusted
    code can neither escalate, touch the host, nor exhaust it. Its only write surface is the
    scoped write MCP (#132); env injects the endpoints + scoped tokens + the extension id."""
    env = {
        "OKENGINE_EXTENSION_ID": spec["id"],
        "OKENGINE_MCP_URL": mcp_url,
        "OKENGINE_WRITE_MCP_URL": write_url,
        "OKENGINE_READ_TOKEN": read_token,
        "OKENGINE_WRITE_TOKEN": write_token,
    }
    for k, v in (spec.get("config") or {}).items():
        env[f"OKENGINE_CONFIG_{str(k).upper()}"] = str(v)
    svc = {
        "image": spec["image"],
        "container_name": f"{spec['id']}-sidecar",
        "environment": [f"{k}={v}" for k, v in env.items()],
        "restart": "no",                 # one-shot; the wrapper runs it with --rm
        # --- OS sandbox (okengine#124): confine untrusted third-party code ---
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "read_only": True,
        "tmpfs": ["/tmp"],
        "pids_limit": int((spec.get("limits") or {}).get("pids", 256)),
        "mem_limit": str((spec.get("limits") or {}).get("memory", "1024m")),
        "cpus": float((spec.get("limits") or {}).get("cpus", 1.0)),
        # joins the compose default (per-pack) bridge via `-p <project>` at run — reaches the MCP
        # endpoints by service name; no host network (okengine#138).
    }
    if spec.get("command"):
        svc["command"] = spec["command"]
    return svc


# --- composed schema (okengine#90 P3 / #133) ------------------------------

def _schema_lib():
    p = _HERE / "cron" / "schema_lib.py"
    spec = importlib.util.spec_from_file_location("schema_lib", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def collect_reader_panels(resolved: dict) -> tuple[dict, list[str]]:
    """Compose enabled extensions' `reader_panels` bindings into a {page_type: binding} map for the
    reader to stage + consume (okengine#160). Each binding carries its owning `extension`. Fail-loud
    on two enabled extensions binding the SAME page type (an ambiguous renderer must not be silent).
    Returns ({type: binding}, errors)."""
    out: dict = {}
    errors: list[str] = []
    for ext_id in sorted(resolved):
        m = resolved[ext_id]["manifest"]
        for b in (m.get("reader_panels") or []):
            if not isinstance(b, dict) or not b.get("type"):
                continue
            t = str(b["type"])
            if t in out:
                errors.append(f"reader_panels: type {t!r} bound by both "
                              f"{out[t]['extension']} and {ext_id}")
                continue
            out[t] = {**b, "extension": ext_id}
    return out, errors


def _fragments_from_resolved(resolved: dict) -> tuple[list, list[str]]:
    """Load each enabled extension's schema fragment file(s) (manifest `schema:`, paths
    relative to the extension dir) -> [(owner, fragment)] + errors."""
    import yaml
    frags: list = []
    errors: list[str] = []
    for ext_id in sorted(resolved):
        rec = resolved[ext_id]
        m = rec["manifest"]
        extdir = Path(rec["dir"])
        for sf in (m.get("schema") or []):
            p = extdir / sf
            if not p.is_file():
                errors.append(f"{ext_id}: schema fragment not found: {sf}")
                continue
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception as e:
                errors.append(f"{ext_id}: unparseable schema fragment {sf}: {e}")
                continue
            if not isinstance(data, dict):
                errors.append(f"{ext_id}: schema fragment {sf} is not a mapping")
                continue
            frags.append((f"ext:{ext_id}", data))
    return frags, errors


def _compose(pack_dir) -> tuple[dict, list, list[str]]:
    disc = _discovery_mod()
    pack_dir = Path(pack_dir)
    resolved, rerr = disc.resolve_for_pack(pack_dir)     # explicit ∪ core-not-disabled
    frags, frag_err = _fragments_from_resolved(resolved)
    composed, comp_err = _schema_lib().compose_schema(pack_dir, frags)
    return composed, frags, list(rerr) + frag_err + comp_err


def compose_check(pack_dir, resolved: dict) -> list[str]:
    """Dry-run the schema composition for a given resolved set (e.g. current-enabled +
    a candidate) -> errors. Used by `enable` to fail-before-runtime on a bad fragment."""
    frags, frag_err = _fragments_from_resolved(resolved)
    _, comp_err = _schema_lib().compose_schema(Path(pack_dir), frags)
    return frag_err + comp_err


def composed_schema(pack_dir) -> tuple[dict, list[str]]:
    composed, _frags, errors = _compose(pack_dir)
    return composed, errors


def write_composed_schema(pack_dir) -> list[str]:
    """Generate <pack>/.okengine/composed-schema.yaml from enabled extensions that bring
    schema, OR remove a stale artifact when none do (so the pack schema.yaml governs).
    Returns errors; writes nothing on error (fail-before-runtime, §9)."""
    import yaml
    pack_dir = Path(pack_dir)
    composed, frags, errors = _compose(pack_dir)
    artifact = pack_dir / ".okengine" / "composed-schema.yaml"
    if errors:
        return errors
    if not frags:
        if artifact.is_file():
            artifact.unlink()
        return []
    artifact.parent.mkdir(parents=True, exist_ok=True)
    composed = dict(composed)
    composed["_generated"] = "framework extensions (okengine#133) — do not edit"
    artifact.write_text(yaml.safe_dump(composed, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
    return []


def purge_targets(pack_dir, ext_id: str) -> list[str]:
    """Vault pages stamped `extension_id: <ext_id>` — the provenance the write path
    applies (okengine#132). Returns sorted wiki-relative .md paths. The basis for
    `extensions purge` (okengine#127): an extension's OWNED pages, identifiable because
    okengine-write stamped them."""
    import re
    wiki = Path(pack_dir) / "wiki"
    if not wiki.is_dir():
        return []
    fm_re = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
    stamp_re = re.compile(rf"^extension_id:[ \t]*['\"]?{re.escape(ext_id)}['\"]?[ \t]*$", re.M)
    out: list[str] = []
    for p in wiki.rglob("*.md"):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        m = fm_re.match(head)
        if m and stamp_re.search(m.group(1)):
            out.append(p.relative_to(wiki).as_posix())
    return sorted(out)


def _read_secrets(pack_dir) -> dict:
    import json
    p = Path(pack_dir).joinpath(".okengine", "extension-secrets.json")
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def sidecar_compose_override(pack_dir, mcp_url: str = "http://localhost:8830/mcp",
                             write_url: str = "http://localhost:8731/mcp",
                             project: str = "okengine"
                             ) -> tuple[dict, dict, list[str]]:
    """Assemble the deploy artifacts for enabled sidecars: a compose-override dict
    ({services: {<id>-sidecar: ...}}) and {id: trigger-wrapper-text}, with the scoped
    token (#132) injected from the vault secrets file. Returns (override, wrappers,
    errors). A sidecar with no minted token yet -> error (enable mints it)."""
    specs, errors = sidecar_specs(pack_dir)
    secrets = _read_secrets(pack_dir)
    services: dict = {}
    wrappers: dict = {}
    for s in specs:
        token = secrets.get(s["id"], "")
        if not token:
            errors.append(f"{s['id']}: no minted token (re-run `extensions enable`)")
            continue
        # one token per extension; the read/write surfaces scope it differently (#132).
        services[f"{s['id']}-sidecar"] = render_sidecar_service(
            s, mcp_url, write_url, token, token)
        wrappers[s["id"]] = render_trigger_wrapper(s["id"], "docker-compose.yml", project)
    return {"services": services}, wrappers, errors


def extension_jobs(pack_dir, existing_names=None) -> tuple[list[dict], list[str]]:
    """Deploy-path entry point: discover + resolve enabled-state for ``pack_dir`` and
    return ``(jobs, errors)``. A no-op ``([], [])`` when no extensions.yaml exists, so
    folding this into cron regen is zero-impact until an operator enables something."""
    disc = _discovery_mod()
    pack_dir = Path(pack_dir)
    resolved, errors = disc.resolve_for_pack(pack_dir)   # explicit ∪ core-not-disabled
    if not resolved and not errors:
        return [], []                        # nothing active -> nothing to compose
    jobs, comp_err, _ = compose(resolved, existing_names)
    errors.extend(comp_err)
    errors.extend(_apply_prompt_overrides(jobs, pack_dir))
    errors.extend(_apply_model_overrides(jobs, pack_dir))
    errors.extend(_apply_schedule_overrides(jobs, pack_dir))
    return jobs, errors


def _apply_model_overrides(jobs: list[dict], pack_dir: Path) -> list[str]:
    """Apply operator model overrides over the composed extension jobs, by job name (#151).

    Source: ``<pack>/.okengine/extension-models.json`` (a ``{job_name: model}`` map), parallel
    to extension-prompts.json. An extension lane ships model-agnostic (a pack/extension does NOT
    pin a model — that's a deployment choice); the operator routes a lane here without forking
    the manifest. The value is either a literal model name (``qwen3.5:9b``) or a profile
    reference (``@reasoning``) resolved at deploy by model_profiles.expand_jobs.
    Returns errors (unknown job names — fail-loud, likely a stale key)."""
    f = Path(pack_dir) / ".okengine" / "extension-models.json"
    if not f.is_file():
        return []
    try:
        overrides = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return [f"extension-models.json: {e}"]
    if not isinstance(overrides, dict):
        return ["extension-models.json must be a {job_name: model} map"]
    by_name = {j["name"]: j for j in jobs}
    errors: list[str] = []
    for name, model in overrides.items():
        job = by_name.get(name)
        if job is None:
            errors.append(f"extension-models.json: no extension job named {name!r} "
                          "(stale override key?)")
            continue
        if not isinstance(model, str) or not model.strip():
            errors.append(f"extension-models.json[{name!r}]: model must be a non-empty string")
            continue
        job["model"] = model.strip()
    return errors


def _apply_schedule_overrides(jobs: list[dict], pack_dir: Path) -> list[str]:
    """Apply operator cron-schedule overrides over the composed extension jobs, by job name.

    Source: ``<pack>/.okengine/extension-schedules.json`` (a ``{job_name: cron_expr}`` map),
    parallel to extension-models.json / extension-prompts.json. An extension lane ships a
    generic default cadence in its manifest (e.g. okengine.lacuna weekly); the operator
    retunes it PER-DEPLOYMENT here (e.g. daily) without forking the manifest — so the change
    survives a cron regen. Returns errors (unknown job names or malformed exprs — fail-loud)."""
    f = Path(pack_dir) / ".okengine" / "extension-schedules.json"
    if not f.is_file():
        return []
    try:
        overrides = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return [f"extension-schedules.json: {e}"]
    if not isinstance(overrides, dict):
        return ["extension-schedules.json must be a {job_name: cron_expr} map"]
    by_name = {j["name"]: j for j in jobs}
    errors: list[str] = []
    for name, expr in overrides.items():
        job = by_name.get(name)
        if job is None:
            errors.append(f"extension-schedules.json: no extension job named {name!r} "
                          "(stale override key?)")
            continue
        if not isinstance(expr, str) or len(expr.split()) != 5:
            errors.append(f"extension-schedules.json[{name!r}]: expr must be a 5-field cron string")
            continue
        job["schedule"] = {"kind": "cron", "expr": expr.strip()}
    return errors


def _apply_prompt_overrides(jobs: list[dict], pack_dir: Path) -> list[str]:
    """Apply pack/operator prompt overrides over the composed jobs, by job name.

    Source: ``<pack>/.okengine/extension-prompts.json`` (a ``{job_name: prompt}`` map) —
    the deployment's customization point, parallel to the engine-template
    `engine-template-prompts.json`. So a first-party extension ships generic default
    prompts (bundled `prompt_file`) and a pack keeps its tuned ones, keyed by the
    namespaced job name (`<id>:<op>`). Overriding a job's prompt makes it an agent op.
    Returns a list of errors (unknown job names — fail-loud, likely a stale key)."""
    f = Path(pack_dir) / ".okengine" / "extension-prompts.json"
    if not f.is_file():
        return []
    try:
        overrides = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return [f"extension-prompts.json: {e}"]
    if not isinstance(overrides, dict):
        return ["extension-prompts.json must be a {job_name: prompt} map"]
    by_name = {j["name"]: j for j in jobs}
    errors: list[str] = []
    for name, prompt in overrides.items():
        job = by_name.get(name)
        if job is None:
            errors.append(f"extension-prompts.json: no extension job named {name!r} "
                          "(stale override key?)")
            continue
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"extension-prompts.json[{name!r}]: prompt must be a non-empty string")
            continue
        job["prompt"] = prompt
        job["no_agent"] = False
        job.setdefault("enabled_toolsets", list(_DEFAULT_TOOLSETS))
    return errors
