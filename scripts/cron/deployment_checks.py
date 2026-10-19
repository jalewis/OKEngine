#!/usr/bin/env python3
"""deployment_checks.py — shared LIVE-deployment checks library (okengine#405).

The deterministic checks below were extracted from `deployment_validate.py` so that the daily
in-gateway validator, `framework status`/`framework doctor` (live host diagnostics), and
`post_deploy_verify.sh` all run the SAME checks instead of three partial re-implementations.

Each check reads the configured paths (VAULT/DATA/HERMES — set by `configure()`, defaulting to the
in-gateway env) and appends findings to the module-level `F` via `add()`. Callers use `run(names)`:
it resets `F`, runs the selected checks in canonical order (isolating a crash as a FAIL, never
propagating), and returns the findings list. The checks are written against module globals by design;
`configure()` rebinds those globals rather than threading a context through every signature.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess  # nosec B404 - runs the baked gateway interpreter on a fixed engine script
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import yaml
from yaml import YAMLError

# Sibling cron lib (staged into DATA/scripts alongside this script; scripts/cron/ in the repo).
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Source-tree-only helpers such as extension_compose live one directory above cron/. In a
# deployment DATA/scripts is inserted at check time below; adding the source parent makes the
# same full-composition check deterministic in host tests and direct source execution instead of
# depending on some earlier test having imported extension_compose into sys.modules.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hardening_lib import (  # noqa: E402
    ACTORLESS_WRITER,
    CHAT_WRITER,
    OPERATOR_SURFACES,
    actorless_writer_bindings,
    actorless_writers,
    STORED_CREDENTIAL_PLATFORMS,
    hardened_posture_violations,
    is_editing,
    is_hardened,
    platform_config_state,
    platform_toolset_bindings,
    read_auth_json,
)
from dotenv_lib import compose_default, read_compose_env, read_dotenv  # noqa: E402
from deepseek_policy import (  # noqa: E402
    MODEL_ENV_KEY, env_model_values, model_values,
    legacy_deepseek_model as _legacy_deepseek_model,
)

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
DATA = Path(os.environ.get("OKENGINE_DATA", "/opt/data"))
# The Hermes INSTALL dir (baked code + the okengine#192 version marker), NOT HERMES_HOME — which
# Hermes sets to the DATA dir (/opt/data). Fixed /opt/hermes; overridable for tests.
HERMES = Path(os.environ.get("OKENGINE_HERMES_DIR", "/opt/hermes"))
#: The gateway container's environment, which the auth, timezone and provenance checks judge. In the
#: gateway it IS this process's environment (compose env_file + environment:). A host run
#: (`framework status/doctor`, post-deploy [9]) is not: there `configure(env=host_env(dep))` points
#: the checks at what compose hands the gateway from the pack .env, not at the operator's shell
#: (okengine#994 [16]: a pack-.env-only API_SERVER_ENABLED was invisible to the host check).
ENV: Mapping[str, str] = os.environ
F: list[tuple[str, str, str]] = []

def add(level, area, msg):
    F.append((level, area, msg))


def _yaml(p: Path):
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        add("FAIL", "parse", f"{p.name}: unparseable ({e})")
        return None


def check_pins():
    ev = _yaml(VAULT / "engine.version") if (VAULT / "engine.version").is_file() else None
    rt_path = DATA / "engine-runtime.yaml"
    rt = _yaml(rt_path) if rt_path.is_file() else None
    # okengine#192: the runtime stamp is written only by ensure-runtime at INITIAL deploy, so an
    # image ROLL (rebuild+recreate) without a re-stamp leaves it stale — the About panel + the pin
    # check below then report a version the deployment is NOT running. Compare the stamp to the
    # RUNNING engine baked into the image ($HERMES/.okengine_release, written by build-engine-image);
    # on desync, SELF-HEAL the stamp so the About is correct on the next read, and WARN so the
    # missing re-stamp step in the roll is visible. No marker (pre-#192 image) -> skip silently.
    baked = HERMES / ".okengine_release"
    running = baked.read_text(encoding="utf-8").strip() if baked.is_file() else ""
    if running and rt is not None:
        stamped = str(rt.get("engine_release", ""))
        if stamped and stamped != running:
            rt["engine_release"] = running
            try:
                rt_path.write_text("".join(f"{k}: {v}\n" for k, v in rt.items()), encoding="utf-8")
                add("WARN", "pins", f"runtime stamp said {stamped} but the running engine is {running} "
                                    "— an image roll didn't re-stamp; auto-refreshed. Fold "
                                    "`ensure-runtime` into the roll so the stamp never lags.")
            except OSError:
                add("FAIL", "pins", f"runtime stamp {stamped} != running engine {running} and the "
                                    "stamp is not writable — About reports a version not running")
    # The HERMES half of the same #192 desync: the engine check above never covered the stamp's
    # hermes_pin, so a Hermes-bump image roll left About claiming the OLD Hermes with nothing
    # catching it (found live on the v0.18.2 canary). build-engine-image now bakes the pin as
    # $HERMES/.hermes_pin; compare + self-heal exactly like the engine release. Pre-marker image
    # -> skip silently.
    baked_hp = HERMES / ".hermes_pin"
    running_hp = baked_hp.read_text(encoding="utf-8").strip() if baked_hp.is_file() else ""
    # okengine#1044 [29]: "pre-marker image" is not a silent pass when the image IS visible. Every
    # gateway build-engine-image.sh has produced since #192 carries both markers; a Hermes tree
    # without them is an image this engine did not build (an upstream tag, a hand-built image),
    # which may not carry the patch-01 write guard. Host-side runs point HERMES at a path that does
    # not exist (no image to see), and stay quiet.
    if HERMES.is_dir() and not (running or running_hp):
        add("WARN", "pins", f"{HERMES} carries no .okengine_release/.hermes_pin marker: this gateway "
                            "image was not built by build-engine-image.sh, so the carried Hermes "
                            "patches (incl. the patch-01 write guard) are unverified. Re-run deploy.sh "
                            "--rebuild; post_deploy_verify step [3b2] checks the patches themselves.")
    if running_hp and rt is not None:
        stamped_hp = str(rt.get("hermes_pin", ""))
        if stamped_hp and stamped_hp != running_hp:
            rt["hermes_pin"] = running_hp
            try:
                rt_path.write_text("".join(f"{k}: {v}\n" for k, v in rt.items()), encoding="utf-8")
                add("WARN", "pins", f"runtime stamp said Hermes {stamped_hp} but the running gateway "
                                    f"is {running_hp} — an image roll didn't re-stamp; auto-refreshed. "
                                    "Fold `ensure-runtime` into the roll so the stamp never lags.")
            except OSError:
                add("FAIL", "pins", f"runtime stamp Hermes {stamped_hp} != running {running_hp} and "
                                    "the stamp is not writable — About reports a Hermes not running")
    if not ev or not rt:
        add("WARN", "pins", "engine.version or runtime stamp missing — pin drift undetectable")
        return
    # ensure-runtime.sh writes `engine_release:` / `hermes_pin:` — read THOSE keys.
    # (This originally read `engine`/`hermes`, which the stamp never contained, so
    # both comparisons were silently vacuous — a stale v0.6.1 pin sailed through.
    # If no spelling yields a value, say so instead of comparing empty strings.)
    pin = str(ev.get("version", ""))
    run = str(rt.get("engine_release", rt.get("engine", rt.get("engine_version", ""))))
    if not run:
        add("WARN", "pins", "runtime stamp carries no engine_release — pin drift undetectable")
    elif pin and pin.lstrip("v").split(".")[0:2] != run.lstrip("v").split(".")[0:2]:
        add("FAIL", "pins", f"engine.version pins {pin} but the runtime stamp says {run} — "
                            "re-validate the pack against the running engine and bump the pin")
    hp = str(ev.get("hermes_pin", ""))
    hr = str(rt.get("hermes_pin", rt.get("hermes", rt.get("hermes_tag", ""))))
    if hp and not hr:
        # M22 one-sided-drift rule (also enforced on the engine_release leg above and in
        # check_write_path_libs / post_deploy_verify): a pin present on only ONE side is
        # UNDETECTABLE, never a silent pass. An older/partial ensure-runtime stamp can carry
        # engine_release but no hermes_pin (the key predates okengine#119), so a stale Hermes pin
        # would sail through here vacuously — WARN instead (invariant-audit completeness sweep).
        add("WARN", "pins", "runtime stamp carries no hermes_pin — Hermes pin drift undetectable")
    elif hp and hr and hp != hr:
        add("FAIL", "pins", f"hermes_pin {hp} != runtime Hermes {hr}")

    expected_cp_path = DATA / "config" / "cron-plus.pin"
    plugin_head_path = DATA / "plugins" / "cron-plus" / ".git" / "HEAD"
    if not expected_cp_path.is_file() and not plugin_head_path.is_file():
        pass  # pre-marker deployment; the next cron-script stage installs the continuous detector
    elif not expected_cp_path.is_file() or not plugin_head_path.is_file():
        add("WARN", "pins", "cron-plus expected pin or installed HEAD is missing — scheduler pin "
                            "drift undetectable; re-run deploy-cron-scripts + install-cron-plus")
    else:
        expected_cp = expected_cp_path.read_text(encoding="utf-8", errors="replace").strip()
        installed_cp = plugin_head_path.read_text(encoding="utf-8", errors="replace").strip()
        if installed_cp.startswith("ref:"):
            ref = installed_cp.split(":", 1)[1].strip()
            ref_path = plugin_head_path.parent / ref
            installed_cp = (ref_path.read_text(encoding="utf-8", errors="replace").strip()
                            if ref_path.is_file() else "")
        if not expected_cp or not installed_cp:
            add("WARN", "pins", "cron-plus expected/installed revision is unreadable — scheduler "
                                "pin drift undetectable")
        elif installed_cp != expected_cp:
            add("FAIL", "pins", f"cron-plus installed HEAD {installed_cp[:12]} != engine manifest "
                                f"pin {expected_cp[:12]} — run scripts/install-cron-plus.sh and "
                                "recreate/restart the gateway")


def _artifact_missing_pack_governance(live, disk) -> bool:
    """True iff `disk` (the on-disk composed artifact = base ⊕ pack ⊕ ENABLED EXTENSIONS) is MISSING
    or DISAGREES on something `live` (a fresh base ⊕ pack recompose, WITHOUT extension fragments)
    produces. A SUBSET test, not equality: the artifact legitimately carries extra extension-owned
    types/namespaces/owners and extension-added enum values, which an equality check wrongly read as
    drift — false-flagging STALE on EVERY schema-bringing-extension deployment (lacuna/frontier/…).
    Trade-off: a pack that REMOVES a type/enum without regenerating the artifact isn't caught here
    (rare; surfaced by the type_alias-shadow / orphaned-namespace checks)."""
    if isinstance(live, dict):
        if not isinstance(disk, dict):
            return True
        return any(k not in disk or _artifact_missing_pack_governance(v, disk[k]) for k, v in live.items())
    if isinstance(live, list):
        if not isinstance(disk, list):
            return True
        return any(x not in disk for x in live)     # every base⊕pack value must survive in the artifact
    return live != disk


def _schema_documents_equal(fresh, disk) -> bool:
    """Compare governing schema content, ignoring generated provenance only."""
    if not isinstance(fresh, dict) or not isinstance(disk, dict):
        return False
    clean_fresh = {k: v for k, v in fresh.items() if not str(k).startswith("_")}
    clean_disk = {k: v for k, v in disk.items() if not str(k).startswith("_")}
    return clean_fresh == clean_disk


def check_schema():
    sys.path.insert(0, str(DATA / "scripts"))
    try:
        import schema_lib
        composed, errors = schema_lib.compose_schema(VAULT)
        for e in errors or []:
            add("FAIL", "schema", f"composition: {e}")
    except Exception as e:
        add("WARN", "schema", f"staged schema_lib compose unavailable ({e}) — falling back to root-only checks")
        composed = _yaml(VAULT / "schema.yaml") or {}
    types = set((composed or {}).get("types") or {})
    root = _yaml(VAULT / "schema.yaml") or {}
    aliases = root.get("type_aliases") or {}
    for a, target in aliases.items():
        if a in types:
            add("FAIL", "schema", f"type_alias '{a}: {target}' SHADOWS canonical type '{a}' "
                                  "(normalization drains silently retype pages) — retire the alias")
        if types and target not in types:
            add("WARN", "schema", f"type_alias target '{target}' not a composed type")
    for ns in ((root.get("partitioning") or {}).get("namespaces") or {}):
        if not (VAULT / "wiki" / ns).is_dir():
            add("WARN", "schema", f"partitioned namespace wiki/{ns}/ does not exist")
    # STALENESS: prefer a fresh FULL runtime composition (engine + pack + enabled
    # extension source fragments) and compare it exactly with the enforced artifact.
    # deploy-cron-scripts stages the composer and engine extension tier into
    # /opt/data for this check. This catches additions, removals and changed enums
    # without trusting the artifact's own recorded _fragments as source-of-truth.
    art = VAULT / ".okengine" / "composed-schema.yaml"
    if art.is_file() and composed and not any(l == "FAIL" and c == "schema" for l, c, _ in F):
        on_disk = _yaml(art) or {}
        try:
            import extension_compose
        except (ImportError, FileNotFoundError):
            fresh, full_errors = None, []
        else:
            try:
                fresh, full_errors = extension_compose.composed_schema(VAULT)
            except Exception as exc:
                add("FAIL", "schema", f"fresh full runtime composition crashed: {exc}")
                fresh, full_errors = {}, []
        if fresh is not None:
            for error in full_errors or []:
                add("FAIL", "schema", f"full runtime composition: {error}")
            if not full_errors and not _schema_documents_equal(fresh, on_disk):
                add("FAIL", "schema", "composed-schema.yaml DIVERGES from a fresh full composition "
                    "of engine + schema.yaml + enabled extension source fragments. The write path "
                    "and UI use the artifact; redeploy to regenerate it before accepting writes.")
        else:
            # Upgrade-safe fallback for a pre-#277 runtime that has not staged the
            # composer yet. It can detect pack additions/changes but not fragment
            # removals; therefore WARN rather than claiming full validation.
            try:
                live_bp, _bp_err = schema_lib.compose_schema(VAULT, fragments=[])
            except Exception:
                live_bp = composed
            keys = ("types", "enums", "field_enums", "partitioning", "permissions", "review")
            if any(_artifact_missing_pack_governance(live_bp.get(k), on_disk.get(k)) for k in keys):
                add("WARN", "schema", "composed-schema.yaml is STALE against base+pack governance; "
                    "runtime full-composition validation is unavailable until the next deploy.")


def check_subdomains():
    for sub in (VAULT / "wiki").rglob("schema.yaml"):
        d = _yaml(sub)
        if d is None:
            continue
        if not (d.get("types") or {}):
            add("WARN", "sub-domains", f"{sub.relative_to(VAULT)} declares no types")
        else:
            add("INFO", "sub-domains", f"domain {sub.parent.relative_to(VAULT / 'wiki')}/: "
                                       f"{len(d['types'])} type(s)")


def _model_identity(job, cfg):
    """The concrete inference endpoint a job shares with others. Mirrors model_slots.model_identity.

    Kept in step with that function deliberately: this check exists to compare jobs the RUNNER
    will treat as sharing one lock set, so it has to resolve identity the same way the runner does.
    """
    if job.get("no_agent") is True:
        return None
    provider = str(job.get("provider") or cfg.get("provider") or "default")
    base_url = str(job.get("base_url") or cfg.get("base_url") or "default")
    model = str(job.get("model") or cfg.get("default") or "default")
    return f"{provider}|{base_url}|{model}"


def _check_slot_concurrency_coherence(jobs):
    """One endpoint, one slot count. Jobs sharing an identity must agree on model_concurrency.

    model_concurrency describes THE ENDPOINT -- how many inference slots the server actually
    serves -- but it is declared PER JOB, so nothing stops two jobs on one endpoint disagreeing.
    When they do, the lower value does not throttle that job to N concurrent runs: model_slot()
    only ever probes lock indices 0..limit-1, so a limit=1 job competes for index 0 ALONE and
    never looks at index 1 even while it sits free. It starves against spare capacity, and the
    endpoint runs under-used at the same time.

    Live instance this was written from: one identity carried 8 lanes at limit=1 and 9 at
    limit=2, and a limit=1 lane burned its whole slot-wait budget queuing on index 0 --
    `model slot unavailable after 300.1s (identity=custom|...|qwen3-coder:30b, limit=1)`.
    """
    # Read directly rather than via _yaml(): that helper RECORDS a "parse" FAIL for anything it
    # cannot load, including a file that is simply absent. config.yaml is optional here -- a
    # deployment without one still has coherent identities (everything falls back together) --
    # so borrowing _yaml would manufacture a failure out of a legitimate layout.
    cfg = {}
    try:
        raw = yaml.safe_load((DATA / "config.yaml").read_text(encoding="utf-8", errors="replace"))
        model = (raw or {}).get("model") if isinstance(raw, dict) else None
        cfg = model if isinstance(model, dict) else {}
    except (OSError, YAMLError, AttributeError):
        cfg = {}
    limits = {}
    for j in jobs:
        if not j.get("enabled", True):
            continue
        identity = _model_identity(j, cfg)
        if identity is None:
            continue
        try:
            limit = max(1, int(j.get("model_concurrency", 1)))
        except (TypeError, ValueError):
            limit = 1
        limits.setdefault(identity, {}).setdefault(limit, []).append(j.get("name"))
    for identity, by_limit in sorted(limits.items()):
        if len(by_limit) < 2:
            continue
        lowest = min(by_limit)
        detail = ", ".join(f"{lim}: {len(names)} lane(s)" for lim, names in sorted(by_limit.items()))
        starved = sorted(n for n in by_limit[lowest] if n)[:4]
        add("FAIL", "crons",
            f"model_concurrency disagrees on one endpoint ({identity}) — {detail}. "
            f"Slot count is a property of the ENDPOINT, not of a lane: the {lowest}-slot lanes "
            f"only ever probe lock indices 0..{lowest - 1} and will queue there while higher "
            f"indices sit free (e.g. {', '.join(starved)}"
            f"{'…' if len(by_limit[lowest]) > 4 else ''}). Set every lane on this endpoint to "
            f"what the server actually serves.")


def _check_model_routing(jobs):
    """A lane must not claim one model/provider and be served by another.

    `model_slots.model_identity` resolves provider, base_url and model INDEPENDENTLY, each falling
    back to config.yaml. So a lane that names only some of them silently inherits the rest, and the
    result is a request that goes somewhere its own configuration does not describe. Nothing
    downstream objects: a local inference server ignores the model name in the request body and
    serves whatever weights it has loaded, and the run receipt records the model that was ASKED
    for, not the one that answered. The lane, its receipts and any cost accounting all agree with
    each other and all disagree with reality.

    Both rules below are stated WITHOUT vendor names -- the engine ships no domain knowledge, so
    this cannot be a list of known providers. Each instead uses the deployment's own declarations
    as the reference, which is stronger anyway: it stays correct for providers that did not exist
    when this was written.

    Live instances this was written from (okengine#616):
      * a lane declaring only `model: <remote-model>` inherited the local provider AND base_url,
        so it ran on the local box for five runs while its config, receipts and cost attribution
        all named a paid hosted model.
      * 30 lanes whose profile named a provider but no base_url inherited the LOCAL one, giving
        every one of them a slot identity naming a host they never contact.
    """
    cfg = {}
    try:
        raw = yaml.safe_load((DATA / "config.yaml").read_text(encoding="utf-8", errors="replace"))
        model = (raw or {}).get("model") if isinstance(raw, dict) else None
        cfg = model if isinstance(model, dict) else {}
    except (OSError, YAMLError, AttributeError):
        cfg = {}
    default_provider = str(cfg.get("provider") or "")
    default_base = str(cfg.get("base_url") or "")

    # RULE 1 -- a provider override with no endpoint override.
    # Declaring a different provider while inheriting the default endpoint sends that provider's
    # request to the default provider's host. If the override were intentional the endpoint would
    # have moved with it.
    if default_provider and default_base:
        stranded = sorted(j.get("name") for j in jobs
                          if j.get("enabled", True) and j.get("no_agent") is not True
                          and j.get("provider") and str(j["provider"]) != default_provider
                          and not j.get("base_url"))
        if stranded:
            add("FAIL", "crons",
                f"{len(stranded)} lane(s) override `provider` but inherit the default `base_url` "
                f"({default_base}) — the request is addressed to the DEFAULT provider's endpoint "
                f"under another provider's name. Set base_url explicitly on the profile: "
                f"{', '.join(stranded[:5])}{'…' if len(stranded) > 5 else ''}")

    # RULE 2 -- one model name served by two different providers in one deployment.
    # The deployment itself is the authority on which provider owns a model name: if any lane
    # declares `provider: X` for a model, a second lane resolving that same model under a
    # different provider is asking the wrong host for it.
    owners = {}
    for j in jobs:
        if not j.get("enabled", True) or j.get("no_agent") is True:
            continue
        model = str(j.get("model") or cfg.get("default") or "")
        provider = str(j.get("provider") or default_provider or "")
        if not model or not provider:
            continue
        owners.setdefault(model, {}).setdefault(provider, []).append(j.get("name"))
    for model, by_provider in sorted(owners.items()):
        if len(by_provider) < 2:
            continue
        detail = "; ".join(
            f"{prov}: {', '.join(sorted(n for n in names if n)[:3])}"
            f"{'…' if len(names) > 3 else ''}"
            for prov, names in sorted(by_provider.items()))
        add("FAIL", "crons",
            f"model '{model}' is claimed by {len(by_provider)} providers in one deployment "
            f"({detail}). A local endpoint will serve the request under whatever weights it has "
            f"loaded and the receipt will still record '{model}', so this is invisible at "
            f"runtime — give the odd lane an explicit provider.")


def scheduler_uid() -> int:
    """The uid cron-plus runs as: the gateway's HERMES_UID from the judged environment, else this
    process's euid. In the gateway the two agree (deployment-validate runs as the service uid). A
    host-side run (post_deploy_verify step [9], `framework doctor`) runs as the deployer, or as 0
    under sudo, so its euid says nothing about the scheduler; comparing the store's owner to it
    told a second operator to chown jobs.json to themselves (okengine#1044 [9]). An absent,
    malformed or root value falls back to the euid: the gateway never runs as root (#185)."""
    raw = str(ENV.get("HERMES_UID") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return os.geteuid()


def check_crons():
    environments = [("gateway environment", ENV.items())]
    for dotenv in sorted({VAULT / ".env", DATA / ".env"}):
        if dotenv.is_file():
            try:
                environments.append((str(dotenv), list(env_model_values(
                    dotenv.read_text(encoding="utf-8")))))
            except (OSError, ValueError):
                add("FAIL", "DeepSeek model policy", f"{dotenv}: cannot inspect model assignments")
    for source, entries in environments:
        for key, value in entries:
            if MODEL_ENV_KEY.fullmatch(key) and (legacy := _legacy_deepseek_model(value)):
                add("FAIL", "DeepSeek model policy", f"{source} {key} selects legacy model "
                    f"'{legacy}' — use deepseek-flash")
    jf = DATA / "cron-plus" / "jobs.json"
    if not jf.is_file():
        add("FAIL", "crons", "deployed jobs.json missing — dead scheduler?")
        return
    # okengine#197: a root `docker exec` that rewrites the store re-owns it root:root — the
    # uid-service scheduler then silently stops firing EVERYTHING (44-min live all-lane outage).
    # Two red signals, at the earliest gate: (a) the store/pidfiles owned by someone other than
    # the scheduler's uid, (b) the scheduler's own .scheduler-stalled sentinel (cron-plus drops it
    # when it cannot load the store; a successful load clears it).
    me = scheduler_uid()
    try:
        st = jf.stat()
        if st.st_uid != me:
            add("FAIL", "crons", f"jobs.json owned by uid {st.st_uid}, scheduler runs as uid {me} "
                                 f"— a root `docker exec` write poisoned it; every lane will "
                                 f"silently stop. chown it back (fix-vault-ownership / "
                                 f"`chown {me} {jf}`)")
    except OSError:
        pass
    pid_dir = DATA / "cron-plus" / "pids"
    if pid_dir.is_dir():
        bad = [p.name for p in pid_dir.glob("*.pid") if p.stat().st_uid != me]  # glob-ok: runtime pids/ is a flat dir, not a sharded content namespace
        if bad:
            add("WARN", "crons", f"{len(bad)} pidfile(s) owned by another uid ({', '.join(bad[:5])}"
                                 f"{'…' if len(bad) > 5 else ''}) — root `docker exec` residue; "
                                 f"the scheduler may fail to clear them")
    sent = DATA / "cron-plus" / ".scheduler-stalled"
    if sent.is_file():
        detail = ""
        try:
            detail = json.loads(sent.read_text()).get("error", "")
        except Exception:
            pass
        why = detail or "unreadable job store"
        add("FAIL", "crons", f"scheduler STALLED sentinel present ({why}) — cron-plus cannot "
                             f"load jobs.json; NO lanes are firing")
    try:
        jobs = json.loads(jf.read_text()).get("jobs", [])
    except Exception as e:
        add("FAIL", "crons", f"jobs.json unparseable ({e})")
        return
    legacy_jobs = sorted({
        (str(j.get("name") or j.get("id") or "<unnamed>"), model)
        for j in jobs if j.get("enabled", True)
        for value in model_values(j)
        if (model := _legacy_deepseek_model(value))
    })
    for name, model in legacy_jobs:
        add("FAIL", "DeepSeek model policy", f"enabled job '{name}' selects legacy model "
            f"'{model}' — redeploy it with deepseek-flash")

    runtime_path = DATA / "config.yaml"
    runtime = _yaml(runtime_path) if runtime_path.is_file() else {}
    runtime = runtime or {}
    model_config = runtime.get("model") if isinstance(runtime, dict) else {}
    if not isinstance(model_config, dict):
        model_config = {}
    if legacy_default := _legacy_deepseek_model(model_config.get("default")):
        add("FAIL", "DeepSeek model policy", f"config.yaml default selects legacy model "
            f"'{legacy_default}' — use deepseek-flash")
    fallbacks = runtime.get("fallback_providers", []) if isinstance(runtime, dict) else []
    for index, fallback in enumerate(fallbacks if isinstance(fallbacks, list) else []):
        if isinstance(fallback, dict) and (
            legacy_fallback := _legacy_deepseek_model(fallback.get("model"))
        ):
            add("FAIL", "DeepSeek model policy", f"config.yaml fallback_providers[{index}] "
                f"selects legacy model '{legacy_fallback}' — use deepseek-flash")
    seen_id, seen_name = set(), set()
    for j in jobs:
        if j.get("id") in seen_id:
            add("FAIL", "crons", f"duplicate job id {j.get('id')}")
        if j.get("name") in seen_name:
            add("FAIL", "crons", f"duplicate job name {j.get('name')}")
        seen_id.add(j.get("id")); seen_name.add(j.get("name"))
        s = j.get("script")
        if s and not s.startswith("/"):
            if not ((DATA / "scripts" / s).is_file() or list((DATA / "scripts").glob(f"*/{s}"))):  # glob-ok: deliberate one-level lookup in the runtime scripts dir, not a sharded content namespace
                add("FAIL", "crons", f"job '{j.get('name')}' references missing script {s}")
        elif s:
            script_path = Path(s)
            try:
                runtime_relative = script_path.relative_to("/opt/data")
            except ValueError:
                candidate = script_path
            else:
                candidate = DATA / runtime_relative
            if not candidate.is_file():
                add("FAIL", "crons", f"job '{j.get('name')}' references missing script {s}")
    _check_slot_concurrency_coherence(jobs)
    _check_model_routing(jobs)
    # NB: backlinks-refresh no longer needs iwe (okengine#179 — it builds the graph with an
    # in-process link-scanner), so there is no iwe binary dependency to validate here. iwe is
    # still used by the MCP's graph tools (kb_graph: find_references/retrieve_context/stats),
    # but that lives in the MCP container, not this gateway cron surface.


_FIXED_HOUR_RE = re.compile(r"^\s*(\S+)\s+(\d+)\s+\S+\s+\S+\s+\S+\s*\Z")


def _cron_plus_tz_aware():
    """Whether the INSTALLED cron-plus plugin interprets cron fields in CRON_TZ/TZ. A plugin
    pinned before the TZ-aware commit (engine-manifest cron-plus pinned_sha) silently computes
    every schedule in UTC — so a set TZ is ignored and @morning/OKENGINE_BRIEF_HOUR misfire.
    Returns True/False, or None if the plugin isn't readable (check_crons/post-deploy cover that)."""
    jp = DATA / "plugins" / "cron-plus" / "jobs.py"
    try:
        return "CRON_TZ" in jp.read_text()
    except OSError:
        return None


def check_timezone():
    """The structural catch for the recurring 'briefs run at the wrong time' bug (okengine#177),
    in two failure modes:

    - TZ UNSET + fixed-hour daily lanes -> WARN. Every daily lane interprets in UTC, so
      @morning/OKENGINE_BRIEF_HOUR fires hours off local (07:00 UTC = 03:00 US-Eastern). UTC is a
      legitimate zero-config choice (the .env.example documents it), so this is advisory.
    - TZ SET to a real zone but the installed cron-plus is UTC-NAIVE (stale pin) -> FAIL. The
      operator declared local-time intent and the plugin silently ignores it — a misconfiguration,
      not a choice. This is exactly the stale-pin regression (manifest pinned one commit before the
      TZ-aware cron-plus): a fresh deploy clones the old scheduler and TZ does nothing.
    - TZ SET to an invalid IANA zone -> FAIL before inspecting jobs. Falling back to UTC would make
      every schedule and human-facing date silently disagree with the operator's declaration."""
    tz = (ENV.get("TZ") or "").strip()
    tz_set = bool(tz) and tz.upper() != "UTC"
    zone = None
    if tz_set:
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(tz)
        except Exception as exc:
            add("FAIL", "timezone",
                f"TZ={tz!r} is not a valid IANA timezone ({exc}) — refusing the silent UTC "
                "fallback; correct TZ in .env and recreate every service")
            return
    _check_writer_timezones(tz)
    jf = DATA / "cron-plus" / "jobs.json"
    if not jf.is_file():
        return
    try:
        jobs = json.loads(jf.read_text()).get("jobs", [])
    except Exception:
        return  # check_crons already FAILs on an unparseable jobs.json
    daily = [j.get("name") or "?" for j in jobs
             if j.get("enabled", True)
             and _FIXED_HOUR_RE.match((j.get("schedule") or {}).get("expr") or "")]
    if not daily:
        return
    listed = f"{', '.join(daily[:4])}{' …' if len(daily) > 4 else ''}"
    if not tz_set:
        add("WARN", "timezone",
            f"TZ is unset (schedules interpret as UTC) but {len(daily)} fixed-hour daily "
            f"lane(s) are enabled ({listed}) — these fire in UTC, not the operator's local "
            "morning. If local-morning briefs were intended, set TZ (+ OKENGINE_BRIEF_HOUR) in "
            ".env and recreate the gateway, THEN null the stale next_run_at in "
            ".hermes-data/cron-plus/jobs.json (cron-plus only self-heals NULL next_run_at; a "
            "populated one computed under the OLD tz is honored as-is until it lapses).")
        return
    if _cron_plus_tz_aware() is False:
        add("FAIL", "timezone",
            f"TZ={tz} is set but the installed cron-plus plugin is UTC-naive (no CRON_TZ/TZ "
            f"handling) — the {len(daily)} daily lane(s) ({listed}) run in UTC, ignoring TZ "
            "(07:00 local fires 07:00 UTC). The plugin pin is stale: bump engine-manifest "
            "cron-plus pinned_sha to the TZ-aware commit, remove .hermes-data/plugins/cron-plus, "
            "re-run ensure-runtime, recreate the gateway, THEN null the stale next_run_at in "
            ".hermes-data/cron-plus/jobs.json (it persists across the recreate under the old tz; "
            "cron-plus only recomputes a NULL next_run_at).")
        return
    # TZ is a real zone AND cron-plus is tz-aware — but a persisted next_run_at can STILL be stale if
    # the TZ VALUE changed since it was computed (America/New_York -> America/Chicago, or UTC -> a
    # zone). cron-plus only recomputes a NULL next_run_at, so a populated one keeps firing at the OLD
    # local hour until it lapses — the prior branches only covered unset->set and the naive plugin
    # (#326 [29]). Detect it directly: the next fire, viewed in the CURRENT tz, must land on the
    # schedule's declared hour.
    assert zone is not None
    stale = []
    for j in jobs:
        if not j.get("enabled", True):
            continue
        m = _FIXED_HOUR_RE.match((j.get("schedule") or {}).get("expr") or "")
        nra = j.get("next_run_at")
        if not m or not nra:
            continue
        try:
            want_hour = int(m.group(2))
            dt = datetime.fromisoformat(str(nra))
            if dt.tzinfo is None:
                continue                       # a naive stamp has no tz to compare — leave it
            local_hour = dt.astimezone(zone).hour
        except (ValueError, TypeError):
            continue
        if local_hour != want_hour:
            stale.append(f"{j.get('name') or '?'} (next fire {local_hour:02d}:xx local, "
                         f"schedule {want_hour:02d}:xx)")
    if stale:
        add("WARN", "timezone",
            f"TZ={tz} is valid and cron-plus is tz-aware, but {len(stale)} daily lane(s) carry a "
            "next_run_at computed under a DIFFERENT timezone (they fire at the wrong local hour): "
            f"{', '.join(stale[:4])}{' …' if len(stale) > 4 else ''}. cron-plus honors a populated "
            "next_run_at as-is (only a NULL one is recomputed), so null the stale next_run_at in "
            ".hermes-data/cron-plus/jobs.json to adopt the current TZ.")


#: The behavioral DST self-test (staged beside this file) and how long the gateway interpreter gets.
_DST_SELFTEST = Path(__file__).resolve().parent / "croniter_dst_selftest.py"
_DST_SELFTEST_TIMEOUT = 60
#: The self-test's exit status -> finding level. Anything else (a crash, a missing PASS line, no tz
#: database, a timeout) means the scheduler's croniter was not judged: WARN, never a pass.
_DST_VERDICTS = {("PASS", 0): "INFO", ("FAIL", 10): "FAIL", ("MISSING", 11): "FAIL"}
_DST_REMEDY = {
    "INFO": "",
    "FAIL": ". Lanes fire an hour off (or twice) on the next DST transition. Rebuild the gateway "
            "image (build-engine-image.sh installs the pinned croniter) and recreate the gateway, "
            "then null any next_run_at computed across a transition in "
            ".hermes-data/cron-plus/jobs.json.",
}


def check_scheduler_dst():
    """cron-plus computes next_run_at with the GATEWAY venv's croniter. Hermes's frozen uv.lock
    installs croniter 6.0.0, which shifts a next fire that crosses DST by an hour (fall-back fires
    an hour late; spring-forward fires an hour early and again on time), while CI and the host lock
    6.2.4, so no test saw it. The image build pins 6.2.4 (build-engine-image.sh); this re-checks the
    running gateway by BEHAVIOR, running croniter_dst_selftest.py under the interpreter cron-plus
    runs in (HERMES/.venv/bin/python, isolated), never under this process's interpreter: a host run
    would judge the host's croniter. Host side there is no baked venv, so it is undetectable."""
    interp = HERMES / ".venv" / "bin" / "python"
    if not interp.is_file():
        add("WARN", "scheduler-dst", f"no gateway interpreter at {interp}, so the croniter cron-plus "
            "schedules with is not checked here (undetectable, not a pass). Run inside the gateway.")
        return
    if not _DST_SELFTEST.is_file():
        add("WARN", "scheduler-dst", f"{_DST_SELFTEST} is absent, so the gateway's croniter DST "
            "behavior is unchecked (undetectable, not a pass). Restage the cron scripts.")
        return
    # -B: verify runs this as root in the gateway; bytecode must not land in the plugin tree (#1064).
    argv = [str(interp), "-I", "-B", str(_DST_SELFTEST)]
    plugin = DATA / "plugins" / "cron-plus"
    if (plugin / "jobs.py").is_file():
        # Also judge the deployed plugin's fall-back policy (okengine#1055, #1061 [4]); a missing
        # plugin is reported by the cron-plus checks, so croniter alone is still judged here.
        argv += ["--cron-plus", str(plugin)]
    try:
        proc = subprocess.run(argv,  # nosec B603 - fixed argv
                              capture_output=True, text=True, timeout=_DST_SELFTEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        add("WARN", "scheduler-dst", f"the croniter DST self-test timed out after "
            f"{_DST_SELFTEST_TIMEOUT}s under {interp} (undetectable, not a pass)")
        return
    except OSError as exc:
        add("WARN", "scheduler-dst", f"cannot run {interp} ({exc}) (undetectable, not a pass)")
        return
    line = (proc.stdout.strip().splitlines() or [""])[-1]
    verdict, _, detail = line.partition("\t")
    level = _DST_VERDICTS.get((verdict, proc.returncode))
    if level is None:
        why = detail or (proc.stderr.strip().splitlines() or ["no output"])[-1]
        add("WARN", "scheduler-dst", f"the croniter DST self-test under {interp} did not reach a "
            f"verdict (exit {proc.returncode}: {why}) (undetectable, not a pass)")
    else:
        add(level, "scheduler-dst", detail + _DST_REMEDY[level])


def check_partition_dups():
    """A partitioned namespace must hold each slug ONCE. The KEV/NVD importers wrote CVEs to the
    flat `cves/CVE-X.md` root while the reshelve drain files them at `cves/YYYY/MM/CVE-X.md`, so a
    partition-unaware importer re-created a flat copy every cycle → the same slug at two paths,
    doubling every count built on it (okengine#54). Generic guard for ALL partitioned namespaces
    across ALL domains: any slug appearing at more than one path is a FAIL (the earliest gate the
    dup can be caught before it poisons the cockpit/indices). Flat namespaces are exempt — a slug
    can only live at one place there anyway."""
    wiki = VAULT / "wiki"
    if not wiki.is_dir():
        return
    # every non-flat namespace declared by the root schema + any sub-domain schema
    nss: list[str] = []
    def _collect(sp: Path, prefix: str):
        d = _yaml(sp)
        for leaf, cfg in (((d or {}).get("partitioning") or {}).get("namespaces") or {}).items():
            if (cfg or {}).get("strategy", "flat") != "flat":
                nss.append(f"{prefix}{leaf}")
    if (VAULT / "schema.yaml").is_file():
        _collect(VAULT / "schema.yaml", "")
    for sd in sorted(p for p in wiki.iterdir() if p.is_dir()):
        if (sd / "schema.yaml").is_file():
            _collect(sd / "schema.yaml", f"{sd.name}/")
    for ns in nss:
        base = wiki / ns
        if not base.is_dir():
            continue
        seen: dict[str, list[str]] = {}
        for p in base.rglob("*.md"):
            slug = p.stem
            # skip generated per-dir artifacts (INDEX, paginated INDEX-p02/03, _* scaffolding) —
            # the same stem legitimately recurs in every shard dir; matches okf_migrate's skip.
            if slug.startswith("_") or slug.startswith("INDEX") or slug in ("index", "log", "README"):
                continue
            # A tombstoned page is intentionally superseded (e.g. a same-story dedup loser left at
            # its old shard path with superseded_by) — it inflates no count (the counting lanes skip
            # it) and is not a live occupant of the slug. Only LIVE copies at 2+ paths are a #54 dup.
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                continue
            fm_end = head.find("\n---", 3)
            fm_head = head[:fm_end] if fm_end != -1 else head
            if re.search(r"(?im)^status:\s*[\"']?tombstoned\b", fm_head):
                continue
            seen.setdefault(slug, []).append(p.relative_to(wiki).as_posix())
        dups = {s: paths for s, paths in seen.items() if len(paths) > 1}
        if dups:
            sample = "; ".join(f"{s} @ {', '.join(sorted(ps))}" for s, ps in list(dups.items())[:3])
            add("FAIL", "partition-dups",
                f"namespace '{ns}' has {len(dups)} slug(s) at multiple paths — partition-unaware "
                f"writer duplicated pages (okengine#54); every count over this namespace is "
                f"inflated. Re-file to the canonical shard and drop the stale copy. e.g. {sample}"
                + (" …" if len(dups) > 3 else ""))



def check_reserved_basenames():
    """No content page may sit under a reserved basename (okengine#997).

    The write path refuses those names and the validator, id-index and INDEX builders skip them,
    so such a page can be neither maintained through the enforced write path nor listed — it
    silently drops out of its INDEX.md. The detector is `okf_migrate.reserved_name_pages`, whose
    predicate is `schema_validator.is_reserved_basename` — the one the minting guard and the write
    path's refusal also use. A LIVE page under an OKF-reserved or dot-leading name is a FAIL
    (unmaintainable content). A WARN when the only offenders are tombstones (a dead redirect) or
    scaffold-shaped pages (`_template.md`, `INDEX-x.md`): a pack may read those by name, so they
    are a review item, not an automatic rename."""
    if not (VAULT / "wiki").is_dir():
        return
    import okf_migrate
    hits = okf_migrate.reserved_name_pages(VAULT)
    if not hits:
        return
    scaffold = okf_migrate._reserved_contract().is_engine_generated
    live = [rel for rel, tombstoned in hits if not tombstoned]
    content = [rel for rel in live if not scaffold(Path(rel).name)]
    # the FAIL-class pages first, so tombstones and templates never crowd a real offender out
    ordered = content + [rel for rel, _ in hits if rel not in content]
    sample = ", ".join(ordered[:3]) + (" …" if len(ordered) > 3 else "")
    add("FAIL" if content else "WARN", "reserved-basenames",
        f"{len(hits)} content page(s) ({len(live)} live, {len(live) - len(content)} "
        f"scaffold-shaped) sit under a reserved basename — the write path refuses them and "
        f"INDEX.md omits them (okengine#997): {sample}. Rename them and rewrite their links with "
        "the malformed-slugs repair: dry-run `repair_malformed_slugs.py --vault <deployment>`, "
        "then `framework repair plan|apply` with `--repairs malformed-slugs`.")


def check_rules():
    cdir = VAULT / "config"
    for f in cdir.glob("*rules*.yaml") if cdir.is_dir() else []:  # glob-ok: vault config/ is a flat dir, not a sharded content namespace
        d = _yaml(f)
        if d is None:
            continue
        ids = [r.get("id") for r in (d.get("rules") or []) if isinstance(r, dict)]
        dup = sorted({i for i in ids if ids.count(i) > 1} - {None})
        if dup:
            add("FAIL", "rules", f"{f.name}: duplicate rule id(s) {dup}")


def check_extensions():
    jf = DATA / "cron-plus" / "jobs.json"
    jobs = []
    if jf.is_file():
        try:
            jobs = json.loads(jf.read_text()).get("jobs", [])
        except Exception:
            pass  # check_crons already FAILs on an unparseable jobs.json
    # Drive off the STAGED extension dirs (/opt/data/scripts/<id>/), NOT the extensions.yaml `enabled`
    # map: that map lists OPT-IN extensions only, while deploy-cron-scripts stages the EFFECTIVE active
    # set (explicit ∪ core-not-disabled). A core default-on lane extension (okengine.contradictions /
    # timeline) is active + staged yet absent from `enabled`, so an enabled-map loop misses exactly the
    # worst under-reported case (invariant-audit M-B4.2, re-verify). A staged dir with a *.py IS a lane
    # extension (deploy-cron-scripts only creates a dir + stages *.py for job/lane extensions; panels/
    # sidecar/schema-only stage nothing), so the WARN can't false-positive on those.
    sroot = DATA / "scripts"
    staged = sorted(p.name for p in sroot.iterdir() if p.is_dir()) if sroot.is_dir() else []
    referenced = set()
    for j in jobs:
        mm = re.search(r"/scripts/([^/]+)/", j.get("script") or "")
        if mm:
            referenced.add(mm.group(1))
    # FAIL: a deployed job references an extension dir that isn't staged — the lane is dead.
    for ext in sorted(referenced):
        if not (sroot / ext).is_dir():
            add("FAIL", "extensions", f"cron job references /scripts/{ext}/ but no staged scripts "
                                      "dir — its lane is dead; run deploy-cron-scripts")
    # WARN: an extension staged lane scripts (*.py) but NO job references it — the fold never ran
    # (deploy-cron-plus-jobs skipped), so the extension is active while its lanes never schedule:
    # silently inert with every gate green. WARN, not FAIL (never hard-block a deploy on this).
    for ext in staged:
        if ext not in referenced and any((sroot / ext).glob("*.py")):  # glob-ok: a staged extension scripts dir is FLAT, never a sharded namespace
            add("WARN", "extensions", f"extension '{ext}' staged scripts but has NO lane in "
                "jobs.json — its cron lanes may not have been folded (run deploy-cron-plus-jobs), "
                "or it is no longer enabled and its dir is stale (deploy-cron-scripts.sh "
                "--prune-stale-extensions removes it)")


def _owned_kind(mode):
    """The ownership-relevant kind of an LSTAT mode, or None for kinds the checks ignore (sockets,
    fifos, devices). Symlinks count, judged by the link's own owner — never the target's (#881).
    Path.rglob does not descend through symlinked directories, so a link to a foreign tree
    (e.g. /usr) is judged once, as a link, and its target tree is never walked."""
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return None


def check_ownership():
    """Foreign-owned files under the vault silently block the lanes that maintain it
    (root-owned INDEX files; a root-owned dashboard — both shipped by bare `docker exec`
    which defaults to root). The lane runs AS the vault uid, so: anything it couldn't
    overwrite is a FAIL. Repair: scripts/fix-vault-ownership.sh (host) or ensure-runtime."""
    me = os.geteuid()
    strays = []
    # .okengine holds the write-path-critical composed-schema.yaml (write_server PREFERS it; a
    # root-owned copy that a lane can't regenerate silently degrades the write path to base+pack) plus
    # extensions/connectors runtime — all lane-maintained, so it belongs in the ownership scan (#326
    # [14]). Its snapshots/ + backups/ are transient transaction artifacts (framework upgrade/install/
    # backup), not lane-maintained runtime, so skip them to avoid false FAILs.
    for base in ("wiki", "raw", "config", ".okengine"):
        d = VAULT / base
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if base == ".okengine" and {"snapshots", "backups"} & set(p.relative_to(d).parts):
                continue
            try:
                # Flag stray DIRECTORIES too, not just files (invariant-audit #8): a root-owned dir
                # (from a bare root `docker exec`) is unwritable by the lane uid, so the atomic-write
                # pattern (write tmp + os.replace) can't create new pages in it — yet the file-only
                # detection/repair pair converged 'green' with the dir still root-owned.
                # stat(follow_symlinks=False): judge a symlink by ITS OWN owner, never its target's
                # (okengine#881) — a lane-owned link to a root-owned system file is not a stray.
                info = p.stat(follow_symlinks=False)
                kind = _owned_kind(info.st_mode)
                if kind and info.st_uid != me:
                    strays.append(f"{p.relative_to(VAULT)} ({kind}, uid {info.st_uid})")
            except OSError:
                strays.append(f"{p.relative_to(VAULT)} (unstattable)")
            if len(strays) > 25:
                break
    if strays:
        add("FAIL", "ownership", f"{len(strays)}{'+' if len(strays) > 25 else ''} path(s) not "
                                 f"owned by the lane uid ({me}) — lanes cannot maintain them: "
                                 + "; ".join(strays[:5])
                                 + (" …" if len(strays) > 5 else ""))


def check_runtime_ownership():
    """The cron-plus scheduler and every lane run AS the gateway's HERMES_UID and must OWN the
    runtime dir to write it (/opt/data/cron-plus/.tick.lock + jobs.json, the plugin, scripts,
    config). A runtime tree owned by a DIFFERENT uid — the classic muddle: brought up with the
    compose default 10000 while the mounted .hermes-data is the operator's uid — makes the ticker
    die on a PermissionError acquiring .tick.lock, and NOTHING schedules. Complements check_ownership
    (which covers the VAULT); this covers /opt/data. Flag any runtime path the lane uid can't own."""
    me = os.geteuid()
    if me == 0:
        return  # root writes regardless of owner — no muddle. The muddle is a NON-root lane uid
                # that doesn't match the tree (e.g. compose default 10000 vs an operator's 1003).
    strays = []
    # qmd (the search index bind-source) and state/ join the runtime tree: a root-recreated
    # .hermes-data/qmd or state/ is unwritable by the lane uid, so the index rebuild / stateful lanes
    # silently die the same way jobs.json does — check them too (invariant-audit M-B4.3).
    for rel in ("cron-plus", "plugins/cron-plus", "scripts", "config", "qmd", "state"):
        d = DATA / rel
        try:
            info = d.stat()
            if not stat.S_ISDIR(info.st_mode):
                continue
            if info.st_uid != me:
                strays.append(f"{rel} (uid {info.st_uid})")
        except FileNotFoundError:
            continue
        except OSError:
            strays.append(f"{rel} (unstattable)")
            continue
        for child in d.rglob("*"):
            if child == DATA / "cron-plus" / "jobs.json":
                continue  # retained below with its scheduler-specific failure guidance
            try:
                # A venv's bin/python3 -> /usr/bin/python3 is a lane-owned LINK to a root-owned
                # target; following it reported a false FAIL on a live deployment (okengine#881).
                info = child.stat(follow_symlinks=False)
                kind = _owned_kind(info.st_mode)
                if kind and info.st_uid != me:
                    strays.append(
                        f"{child.relative_to(DATA)} ({kind}, uid {info.st_uid})"
                    )
            except OSError:
                strays.append(f"{child.relative_to(DATA)} (unstattable)")
            if len(strays) > 25:
                break
        if len(strays) > 25:
            break
    # The single most critical FILE: cron-plus/jobs.json mis-owned (e.g. root:0600 from a deploy run
    # without the pack's HERMES_UID) is UNREADABLE by the lane uid, so the scheduler goes dark even
    # though the cron-plus DIR above is correctly owned — the exact fleet-stall poison, hit live
    # (okengine#193). The dir-level check misses a mis-owned file inside a well-owned dir; stat it.
    jf = DATA / "cron-plus" / "jobs.json"
    try:
        info = jf.stat()
        if stat.S_ISREG(info.st_mode):
            if info.st_uid != me:
                strays.append(f"cron-plus/jobs.json (uid {info.st_uid}) — the scheduler can't "
                              "read it and the WHOLE fleet stalls (okengine#193)")
    except FileNotFoundError:
        pass
    except OSError:
        strays.append("cron-plus/jobs.json (unstattable)")
    if strays:
        add("FAIL", "runtime-ownership",
            f"runtime dir(s) under /opt/data not owned by the lane uid ({me}) — the cron-plus "
            "ticker cannot write here and the scheduler dies (.tick.lock PermissionError, nothing "
            f"runs): {'; '.join(strays)}. Pin HERMES_UID in .env to the tree owner and recreate; "
            "repair with a root `docker run -v <pack>:/p alpine chown -R <uid>:<gid> /p/.hermes-data`.")


# ALLOWLIST, not a dangerous-name blocklist: chat's api_server toolset may ONLY contain these. A
# blocklist missed the ways broad tools get in — a composite alias (`hermes-api-server`) that Hermes
# expands to terminal/code_execution, or a non-list value that Hermes treats as unset -> broad
# default (re-verify). Anything outside this set (or a non-list, or unset while enabled) FAILs.
# okengine#983: the chat writes ONLY through its actor-bound `okengine-write-chat`; the actor-less
# `okengine-write` (admin to the write path) is no longer allowlisted.
_API_SERVER_SAFE_TS = {"okengine", CHAT_WRITER, "web", "no_mcp"}


def _gateway_env_and_chat(cfg) -> tuple[dict, bool]:
    """(the env Hermes enables platforms from, whether the Agent Chat (api_server) is on).

    Chat enables on ANY of: API_SERVER_ENABLED, an API_SERVER_KEY (Hermes enables on the key
    alone), or an api_server block enabled in config.yaml in any spelling Hermes reads
    (platform_config_state, okengine#993). okengine#991: Hermes loads $HERMES_HOME/.env
    (= DATA/.env) over the container environment (override=True), and `hermes setup` writes
    credentials there, so a key set only in that file still enables its platform. A `docker exec`
    run of this check does not inherit that load."""
    env = {**ENV, **read_dotenv(DATA / ".env")}
    enabled = (str(env.get("API_SERVER_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")
               or bool(str(env.get("API_SERVER_KEY", "")).strip()))
    if isinstance(cfg, dict) and platform_config_state(cfg, "api_server")[0]:
        enabled = True
    return env, enabled


def check_auth():
    trust = ENV.get("OKENGINE_TRUST", "private")
    bind = ENV.get("OKENGINE_BIND", "127.0.0.1")
    pw = ENV.get("OKENGINE_READER_PASSWORD", "")
    if trust == "private" and bind not in ("", "127.0.0.1", "localhost") and not pw:
        add("FAIL", "auth", "private vault exposed beyond loopback with NO password")

    # config.yaml is seeded ONCE from the template and never reconciled (ensure-runtime leaves an
    # existing one untouched). The secure `platform_toolsets.api_server: [okengine, okengine-write-chat]`
    # lockdown was added to the template later (v0.10.7) — so a deployment seeded before it that then
    # enables Agent Chat inherits the BROAD default toolset (terminal / code_execution / file /
    # computer_use) on a network-exposed endpoint, and nothing validated it (invariant-audit HIGH).
    # Chat enablement and the gateway env: _gateway_env_and_chat.
    cfg = _yaml(DATA / "config.yaml") if (DATA / "config.yaml").is_file() else None
    env, enabled = _gateway_env_and_chat(cfg)
    if enabled:
        if cfg is None:
            add("WARN", "auth", "Agent Chat (api_server) appears enabled but config.yaml is unreadable "
                "here — the api_server toolset lockdown is UNVERIFIABLE (undetectable, not a pass).")
        else:
            ts = ((cfg.get("platform_toolsets") or {}).get("api_server"))
            if not isinstance(ts, list):
                add("FAIL", "auth", "Agent Chat (api_server) is enabled but platform_toolsets.api_server "
                    f"is not a list ({type(ts).__name__}) — Hermes then uses the broad default toolset "
                    "(terminal/code_execution/file/computer_use) on a network endpoint. Set it to "
                    f"[okengine, {CHAT_WRITER}] (see config/config.yaml.template).")
            elif (bad := {str(x).strip() for x in ts} - _API_SERVER_SAFE_TS):
                add("FAIL", "auth", f"Agent Chat (api_server) toolset has non-allowlisted member(s) "
                    f"{sorted(bad)} — a composite/alias can expand to terminal/code_execution/file. "
                    f"Restrict platform_toolsets.api_server to {sorted(_API_SERVER_SAFE_TS)}.")
            # okengine#257: the OKENGINE_EDITING switch must AGREE with the live toolset. Editing off is
            # enforced by DROPPING the chat writer here (ensure-runtime). If the flag says off but the
            # write MCP is still present, the switch never took (config not reconciled / gateway not
            # recreated) — UI editing (reader Chat write-back) is still exposed.
            if isinstance(ts, list) and not is_editing(ENV) and \
                    CHAT_WRITER in {str(x).strip() for x in ts}:
                add("FAIL", "auth", f"OKENGINE_EDITING is off but {CHAT_WRITER} is STILL in the "
                    "api_server toolset — UI editing (reader Chat write-back) is still exposed. Re-run "
                    "ensure-runtime and recreate the gateway so the switch applies (okengine#257).")
    # okengine#983: no model-facing platform may reach the actor-less writer, which the write path
    # treats as admin. Checked whether or not chat is enabled (any configured platform counts);
    # the CLI is exempt by NAME (OPERATOR_SURFACES), never by omission.
    if isinstance(cfg, dict):
        # okengine#991: the gateway env is what Hermes enables messaging platforms from;
        # okengine#993: photon also enables from credentials stored in $HERMES_HOME/auth.json.
        auth, auth_error = read_auth_json(DATA / "auth.json")
        for msg in actorless_writer_bindings(cfg, chat_enabled=enabled, env=env, auth=auth):
            add("FAIL", "auth", msg)
        servers = cfg.get("mcp_servers")
        if auth_error and isinstance(servers, dict) and actorless_writers(servers):
            add("WARN", "auth", f"{auth_error}: whether {sorted(STORED_CREDENTIAL_PLATFORMS)} "
                "enable from stored credentials, and so reach the actor-less writer, is "
                "undetectable here (not a pass). Run the check as the gateway user.")
        if isinstance(servers, dict) and actorless_writers(servers):
            for platform, why in sorted(OPERATOR_SURFACES.items()):
                add("INFO", "auth", f"platform {platform!r} is an operator surface, exempt from "
                    "the actor-less writer check: " + why)

    # OKENGINE_HARDENED (okengine#78): a single opt-in profile. When set, the deployment ASSERTS it
    # must be safe to expose, so every unsafe setting is a FAIL (fail-closed — the profile never mints
    # secrets or flips values silently). hardened_posture_violations is the shared source of truth.
    if is_hardened(ENV):
        viols = hardened_posture_violations(ENV)
        for msg in viols:
            add("FAIL", "hardening", msg)
        if not viols:
            add("INFO", "hardening", "OKENGINE_HARDENED posture satisfied "
                "(real MCP token, reader auth-or-public, rate limits on, exports safe).")


def check_chat_capability():
    """okengine#991: a chat writer ensure-runtime has bound must have its capability in the BAKED
    policy catalog. The binding is config (stage + recreate); the capability is baked into the
    gateway image. A stage-only roll onto an image that predates it rejects every chat write-back
    ('caller has no declared write capability') while every other gate stays green. Only the engine
    catalog can introduce a chat capability (a pack may only replace one: policy_plane
    .compose_documents), so the baked catalog is the complete answer. Bound means some platform
    Hermes runs (or that has a platform_toolsets entry) is handed the writer: named in its list,
    or BY DEFAULT, when the platform has no list or a list naming no registered server
    (okengine#993 N2: with no actor-less writer registered, nothing else catches a default
    binding)."""
    path = DATA / "config.yaml"
    cfg = _yaml(path) if path.is_file() else None
    if not isinstance(cfg, dict):
        return
    servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
    chat = {}
    for name, spec in servers.items():
        env = (spec or {}).get("env") if isinstance(spec, dict) else None
        actor = str((env or {}).get("OKENGINE_WRITE_ACTOR") or "").strip() \
            if isinstance(env, dict) else ""
        if actor.startswith("chat:"):
            chat[str(name)] = actor
    env, enabled = _gateway_env_and_chat(cfg)
    auth, _error = read_auth_json(DATA / "auth.json")   # an unreadable file is check_auth's WARN
    bindings = platform_toolset_bindings(cfg, chat_enabled=enabled, env=env, auth=auth)
    reached = set().union(*(names for names, _toolset in bindings.values()))
    bound = {name: actor for name, actor in chat.items() if name in reached}
    if not bound:
        return
    catalog_path = HERMES / "config" / "policy" / "catalog.yaml"
    if not catalog_path.is_file():
        add("WARN", "chat-capability", f"chat writer(s) {sorted(bound)} are bound but the baked "
            f"policy catalog {catalog_path} is absent here, so their capability is unverifiable "
            "(undetectable, not a pass). Run inside the gateway.")
        return
    recorded = len(F)
    catalog = _yaml(catalog_path)
    if len(F) > recorded:           # unparseable: _yaml already recorded the FAIL
        return
    # An empty (or comment-only) catalog parses to None: it declares nothing, so it falls through
    # to the no-capabilities FAIL below rather than reading as nothing to check.
    capabilities = catalog.get("capabilities") if isinstance(catalog, dict) else None
    if not isinstance(capabilities, dict):
        add("FAIL", "chat-capability", f"the baked policy catalog {catalog_path} has no "
            "capabilities mapping, so no bound chat writer can write. Rebuild the gateway image.")
        return
    for name, actor in sorted(bound.items()):
        if actor in capabilities:
            add("INFO", "chat-capability", f"{name} is bound and the baked catalog declares {actor}")
        else:
            add("FAIL", "chat-capability", f"{name} is bound (actor {actor}) but the gateway's baked "
                f"policy catalog declares no {actor} capability, so every chat write-back is "
                "rejected. The image predates the config ensure-runtime staged: rebuild the gateway "
                "image (build-engine-image.sh) and recreate the gateway; a stage-only roll misses it.")


# The libs the enforced okengine-write MCP (write_server.py) imports from the BAKED scripts/cron tree
# (HERMES/scripts/cron) AND that deploy-cron-scripts.sh ALSO stages to DATA/scripts. Only libs present
# in BOTH places have a baked-vs-staged drift surface: a stage-only deploy leaves the WRITE PATH on the
# OLD baked lib while the cron fleet + this validator (which import the staged copy) run the NEW one —
# it ships green while the write guard is stale (invariant-audit).
#   NB converge.py is NOT here: it lives only in okengine-mcp/ (baked beside write_server.py, imported
#   from its own dir) and is never staged to scripts/cron, so it has no baked-vs-staged pair to compare
#   — it's image-only like scope.py (a change needs an image rebuild, caught by the version stamp, not
#   this drift check). Listing it here made check_write_path_libs hit the both-absent branch and never
#   actually compare it, and the M23 pin enshrined the wrong bucket (invariant-audit round-2 re-verify).
_WRITE_PATH_LIBS = ("schema_lib.py", "id_lib.py", "id_index.py", "okf_migrate.py")


def check_write_path_libs():
    baked_dir, staged_dir = HERMES / "scripts" / "cron", DATA / "scripts"
    if not baked_dir.is_dir() or not staged_dir.is_dir():
        add("WARN", "write-path", f"cannot compare baked vs staged write-path libs — "
            f"{baked_dir if not baked_dir.is_dir() else staged_dir} absent (undetectable, not a pass)")
        return
    for name in _WRITE_PATH_LIBS:
        baked, staged = baked_dir / name, staged_dir / name
        if not (baked.is_file() and staged.is_file()):
            # Present on one side, absent on the other = a real drift (a lib newly added to
            # _WRITE_PATH_LIBS that a rebuild/stage didn't carry across), NOT a pass. Only when
            # BOTH are absent is there nothing to compare. Silent `continue` here masked exactly
            # the drift this check exists to catch (invariant-audit M22 — missing key is a WARN
            # "undetectable", never a vacuous pass).
            if baked.is_file() != staged.is_file():
                present, absent = (baked, staged) if baked.is_file() else (staged, baked)
                add("WARN", "write-path", f"{name} exists in {present.parent} but is MISSING from "
                    f"{absent.parent} — cannot verify the write path runs the current lib "
                    f"(undetectable, not a pass). Rebuild the gateway image and re-stage.")
            continue
        try:
            if baked.read_bytes() != staged.read_bytes():
                add("FAIL", "write-path", f"{name} DIFFERS between the baked write path ({baked}) and "
                    f"the staged copy ({staged}) — the enforced MCP write server is running a STALE "
                    f"lib. Rebuild the gateway image (build-engine-image.sh, as a patch release); "
                    f"staging alone does not reach write_server. See docs/id-index-runbook.md.")
        except OSError as e:
            add("WARN", "write-path", f"{name}: cannot compare ({e})")
    # The write server ALSO validates against the engine-owned base-schema layer, which it loads from
    # the BAKED HERMES/config — same stale-vs-staged trap as the libs above: a schema field-shape
    # change staged to DATA/config but not rebuilt into the image leaves the write guard enforcing the
    # OLD shape (invariant-audit M5). Compare the two copies the same way.
    baked_bs, staged_bs = HERMES / "config" / "base-schema.yaml", DATA / "config" / "base-schema.yaml"
    if baked_bs.is_file() and staged_bs.is_file():
        try:
            if baked_bs.read_bytes() != staged_bs.read_bytes():
                add("FAIL", "write-path", f"base-schema.yaml DIFFERS between the baked write path "
                    f"({baked_bs}) and the staged copy ({staged_bs}) — the enforced MCP write server "
                    f"validates against a STALE base schema. Rebuild the gateway image; staging alone "
                    f"does not reach write_server. See docs/id-index-runbook.md.")
        except OSError as e:
            add("WARN", "write-path", f"base-schema.yaml: cannot compare ({e})")
    elif baked_bs.is_file() != staged_bs.is_file():
        # Same one-sided-drift hole as the libs: present baked-only or staged-only is undetectable,
        # not a pass (invariant-audit M22).
        present, absent = (baked_bs, staged_bs) if baked_bs.is_file() else (staged_bs, baked_bs)
        add("WARN", "write-path", f"base-schema.yaml exists in {present.parent} but is MISSING from "
            f"{absent.parent} — cannot verify the write path validates against the current base "
            f"schema (undetectable, not a pass).")
    # tools/schema_validator.py is the OKF conformance validator — BAKED at HERMES/tools and imported
    # by BOTH the write-guard hook AND the staged importer_guard/schema_drift_lint crons. It is
    # image-only, so a validator change staged (its reference copy lands in DATA/config) without an
    # image rebuild leaves the write path + those crons enforcing the OLD rules. Compare the baked copy
    # against the staged reference the same way (okengine#326 [15]).
    baked_sv = HERMES / "tools" / "schema_validator.py"
    staged_sv = DATA / "config" / "schema_validator.py"
    if baked_sv.is_file() and staged_sv.is_file():
        try:
            if baked_sv.read_bytes() != staged_sv.read_bytes():
                add("FAIL", "write-path", f"schema_validator.py DIFFERS between the baked validator "
                    f"({baked_sv}) and the staged reference ({staged_sv}) — the write-guard hook and "
                    f"the importer_guard/schema_drift_lint crons run a STALE validator. Rebuild the "
                    f"gateway image; staging alone does not reach the baked tools/. See "
                    f"docs/id-index-runbook.md.")
        except OSError as e:
            add("WARN", "write-path", f"schema_validator.py: cannot compare ({e})")
    elif baked_sv.is_file() != staged_sv.is_file():
        present, absent = (baked_sv, staged_sv) if baked_sv.is_file() else (staged_sv, baked_sv)
        add("WARN", "write-path", f"schema_validator.py exists in {present.parent} but is MISSING "
            f"from {absent.parent} — cannot verify the write path/guards run the current validator "
            f"(undetectable, not a pass).")


def _gateway_pack() -> str:
    """OKENGINE_PACK as the gateway carries it: the process env in the gateway, else (host-side
    doctor/post-deploy) the gateway service declaration in docker-compose.yml."""
    pack = ENV.get("OKENGINE_PACK", "").strip()
    if pack:
        return pack
    compose = _yaml(VAULT / "docker-compose.yml") \
        if (VAULT / "docker-compose.yml").is_file() else {}
    gateway = (((compose or {}).get("services") or {}).get("gateway") or {})
    environment = gateway.get("environment") or {}
    if isinstance(environment, dict):
        return str(environment.get("OKENGINE_PACK") or "").strip()
    if isinstance(environment, list):
        for entry in environment:
            key, separator, value = str(entry).partition("=")
            if key == "OKENGINE_PACK" and separator:
                return value.strip()
    return ""


def check_provenance_env():
    """invariant-audit M14/#750, re-pointed by okengine#1032 [20]: composition provenance
    (maintained_by / discovered_by) is stamped with the write path's pack principal. Since
    okengine#1027 that is the ``name`` in <vault>/pack.yaml (okengine.pack_principal), with
    OKENGINE_PACK only as a fallback read from the WRITER's own environment. Hermes never passes the
    gateway's OKENGINE_PACK to a served write server, so the gateway value proves nothing about
    what is stamped. This check therefore judges the real source:

      - pack.yaml declares no ``name``: WARN. Provenance then depends on an OKENGINE_PACK in each
        writer's own ``env:`` map, which write-principal judges per server;
      - the gateway's OKENGINE_PACK disagrees with pack.yaml: WARN. The write path stamps the
        pack.yaml name and ignores the gateway value, which only misleads whoever reads it.
    """
    try:
        declared = _pack_principal().declared_pack(VAULT)
    except (ImportError, AttributeError):
        add("WARN", "provenance", "okengine.pack_principal is not importable from this engine, so "
            "the pack the write path stamps as provenance cannot be determined (undetectable, not a "
            "pass). Rebuild the gateway image, or run from a current engine checkout.")
        return
    gateway = _gateway_pack()
    if not declared:
        add("WARN", "provenance", f"{VAULT / 'pack.yaml'} declares no `name`, so the write path "
            "has no deployment pack to stamp as composition provenance (maintained_by/"
            "discovered_by). The gateway's OKENGINE_PACK does not reach the write servers; only an "
            "OKENGINE_PACK in a writer's own env: map stands in (see write-principal). Give "
            "pack.yaml its `name`.")
    elif gateway and gateway != declared:
        add("WARN", "provenance", f"the gateway's OKENGINE_PACK={gateway} disagrees with the name "
            f"{declared!r} in pack.yaml. The write path stamps {declared!r} and ignores the gateway "
            "value; correct or drop OKENGINE_PACK in docker-compose.yml so it stops misleading.")


def _pack_principal():
    """The write server's own startup rule, from THIS engine. On the host (framework doctor,
    deploy.sh's pre-recreate gate, post_deploy_verify) this file sits in an engine checkout whose
    own src/ must win: an installed or editable `okengine` from another worktree may predate
    okengine#1027. In the gateway the staged copy has no checkout beside it, and the baked wheel
    is the write server's own."""
    checkout = Path(__file__).resolve().parents[2] / "src" / "okengine" / "pack_principal.py"
    if checkout.is_file():
        import importlib.util
        spec = importlib.util.spec_from_file_location("okengine_pack_principal_checkout", checkout)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    import engine_package  # noqa: F401 — `okengine` importable when this tree is loaded by path
    from okengine import pack_principal
    return pack_principal


def _writer_envs(cfg: dict) -> dict[str, dict[str, str]]:
    """`{server: env map}` for every write server registered in the gateway `cfg`: an
    `okengine-write*` name or any server that runs write_server.py."""
    servers = cfg.get("mcp_servers") if isinstance(cfg.get("mcp_servers"), dict) else {}
    writers = {}
    for name, spec in servers.items():
        spec = spec if isinstance(spec, dict) else {}
        if str(name).startswith(ACTORLESS_WRITER) or any(
                "write_server.py" in str(arg) for arg in (spec.get("args") or [])):
            env = spec.get("env")
            writers[str(name)] = {str(k): str(v) for k, v in env.items()} \
                if isinstance(env, dict) else {}
    return writers


# The writers ensure-runtime reconciles (its `^  okengine-write(-…)?:` selector).
_BOUND_WRITER = re.compile(r"okengine-write(?:-[A-Za-z0-9_.-]+)?$")


def _check_writer_timezones(tz: str) -> None:
    """okengine#1042 [36]: a write server dates its wiki/log.md ledger lines with the zone in its
    OWN `env:` map, because Hermes never passes the gateway's TZ to a stdio server. A writer whose
    map lacks the gateway's zone (or carries another) writes a different calendar day from the
    in-process writers and the review/rotation lanes for part of every day. ensure-runtime binds
    it; this catches a config.yaml that ensure-runtime has not reconciled since."""
    path = DATA / "config.yaml"
    cfg = _yaml(path) if path.is_file() else None
    if not isinstance(cfg, dict):
        return
    want = "UTC" if not tz or tz.upper() == "UTC" else tz
    off = []
    for name, env in sorted(_writer_envs(cfg).items()):
        if not _BOUND_WRITER.match(name):
            continue        # ensure-runtime binds only okengine-write*; its advice could not clear it
        have = (env.get("TZ") or "").strip()
        have = "UTC" if not have or have.upper() == "UTC" else have
        if have != want:
            off.append(f"{name} ({have})")
    if off:
        add("WARN", "timezone",
            f"the gateway runs TZ={want} but {len(off)} write server(s) date the ledger in another "
            f"zone: {', '.join(off[:4])}{' …' if len(off) > 4 else ''}. Hermes does not pass the "
            "gateway's TZ to a stdio writer, so its wiki/log.md lines name a different day around "
            "midnight. Re-run ensure-runtime (it binds TZ into every writer's env:), then recreate "
            "the gateway.")


def check_write_principal():
    """okengine#1027: every registered write server must start with a pack principal. Hermes hands
    a stdio MCP server only a safe baseline plus its own `env:` map, so the gateway's OKENGINE_PACK
    never reaches it; the server derives the pack from the vault's pack.yaml (OKENGINE_PACK from its
    `env:` map only where pack.yaml declares none) and refuses to start without one or on a
    disagreement. This applies the server's own startup rule (okengine.pack_principal) to each
    server's env map, so a writer that would refuse to start, and leave its lane with no write
    tools, FAILs here before the gateway is recreated."""
    path = DATA / "config.yaml"
    cfg = _yaml(path) if path.is_file() else None
    if not isinstance(cfg, dict):
        return
    try:
        startup_refusal = _pack_principal().startup_refusal
    except (ImportError, AttributeError):
        add("WARN", "write-principal", "okengine.pack_principal is not importable from this engine "
            "(an engine checkout or a gateway image that predates okengine#1027), so this check "
            "cannot judge the write servers (undetectable, not a pass). Rebuild the gateway image, "
            "or run from a current engine checkout.")
        return

    writers = _writer_envs(cfg)
    refused = {name: why for name, env in sorted(writers.items())
               if (why := startup_refusal(VAULT, env))}
    for name, why in refused.items():
        add("FAIL", "write-principal", f"{name} would refuse to start: {why}. Its lane gets no "
            "write tools. Give the pack.yaml a `name` (and drop or correct a stale OKENGINE_PACK in "
            "that server's env:), re-run ensure-runtime, then recreate the gateway.")
    if writers and not refused:
        add("INFO", "write-principal", f"{len(writers)} write server(s) start with a pack principal")


def _source_url_key(url: str) -> str:
    """The grouping key for a source URL: the write path's own identity rule
    (`state._source_identity_url`, which `deduplication._norm_url` also calls), so this check can
    never group what the write path keeps apart. "" for a non-address or a bare service endpoint."""
    import engine_package  # noqa: F401 — `okengine` importable when this tree is loaded by path
    from okengine.write_services.state import _source_identity_url
    return _source_identity_url(url)


def check_source_identity():
    """Identity drift the write path cannot see after the fact (okengine#886), counted — never
    repaired — so the class stays visible between reviews:

      - live SOURCE urls held by more than one page (the same document at several seats; merge
        with scripts/cron/dedup_same_url.py, dry-run by default);
      - PLACEHOLDER source urls (`abs/[pending]`, `{id}`, `/tbd`, example.com — the write path's
        own `_PLACEHOLDER_URL`); one live vault filed 20 different papers under one;
      - live pages with NO `id`, per namespace. The write path can neither update nor find such a
        page by identity: ~600 importer-written sources were invisible to converge and duplicated.
      - bare SERVICE-ENDPOINT source urls (an ArcGIS `/query`, `/MapServer/<n>`, a Socrata
        `/resource/<id>.json`, with no query string — `state._is_service_endpoint_url`). Every
        record read from the endpoint shares the URL; before okengine#901 the write path converged
        on it and fused unrelated records into whichever page claimed it first (one live vault lost
        a 38-parcel body that way). They carry no identity now, so they are never grouped as
        "shared" URLs; the count finds the pages to inspect for earlier fusions.

    WARN with counts and a few examples. Tombstoned pages, generated INDEX/_ files and history are
    excluded. URL normalization mirrors write_services.deduplication._norm_url (pinned by a test)."""
    import engine_package  # noqa: F401 — `okengine` importable when this tree is loaded by path
    from okengine.write_services.state import _PLACEHOLDER_URL, _is_service_endpoint_url
    wiki = VAULT / "wiki"
    if not wiki.is_dir():
        return
    by_url: dict[str, list[str]] = {}
    placeholders: list[str] = []
    endpoints: list[str] = []
    idless: dict[str, list[str]] = {}
    for p in sorted(wiki.rglob("*.md")):
        rel = p.relative_to(wiki).as_posix()
        parts = rel.split("/")
        if len(parts) < 2 or parts[0].startswith(("_", ".")) or p.name.startswith(("INDEX", "_")):
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            continue
        m = re.match(r"---\s*\n(.*?)\n---", head, re.S)
        fm = m.group(1) if m else ""
        if re.search(r"(?im)^status:\s*[\"']?tombstoned\b", fm):
            continue
        if not re.search(r"(?m)^id:\s*\S", fm):
            idless.setdefault(parts[0], []).append(rel)
        if parts[0] != "sources":
            continue
        um = re.search(r"(?m)^url:\s*[\"']?([^\"'\n]*?)[\"']?\s*$", fm)
        url = um.group(1).strip() if um else ""
        if url and _PLACEHOLDER_URL.search(url):
            placeholders.append(rel)
        elif _is_service_endpoint_url(url):
            endpoints.append(rel)
        elif _source_url_key(url):
            by_url.setdefault(_source_url_key(url), []).append(rel)
    shared = {u: ps for u, ps in by_url.items() if len(ps) > 1}
    if shared:
        sample = "; ".join(f"{u} ×{len(ps)}" for u, ps in list(shared.items())[:3])
        add("WARN", "source-identity",
            f"{len(shared)} source URL(s) are held by more than one live page "
            f"({sum(map(len, shared.values()))} pages) — the same document at several seats "
            f"splits citations and doubles counts. Review/merge with "
            f"scripts/cron/dedup_same_url.py (dry-run by default). e.g. {sample}")
    if placeholders:
        add("WARN", "source-identity",
            f"{len(placeholders)} source page(s) carry a placeholder URL — URL identity fuses "
            f"unrelated documents under it. Repair from their raw captures with "
            f"dedup_same_url.py --placeholders. e.g. {', '.join(placeholders[:3])}")
    if endpoints:
        add("WARN", "source-identity",
            f"{len(endpoints)} source page(s) carry a bare service-endpoint URL (an API query "
            f"endpoint with no query string, shared by every record read from it) — it is not a "
            f"document identity, and before okengine#901 the write path converged unrelated "
            f"records into such pages. Check each for a fused body (its .okengine/snapshots "
            f"history) and give it a record-specific URL. e.g. {', '.join(endpoints[:3])}")
    for ns, rels in sorted(idless.items()):
        add("WARN", "source-identity",
            f"namespace '{ns}' has {len(rels)} live page(s) with no `id` — the write path can "
            f"neither update them nor find them by identity, so writers duplicate them. "
            f"e.g. {', '.join(rels[:3])}")


# ── shared driver (okengine#405) ──────────────────────────────────────────────
def check_tool_search():
    """Hermes' progressive tool disclosure should be OFF on an OKEngine deployment (okengine#886).

    Hermes defaults `tools.tool_search.enabled` to "auto": every MCP tool — every OKEngine write
    tool — is hidden behind tool_search/tool_describe/tool_call bridges. OKEngine lanes already
    scope their tools with enabled_toolsets, so the bridge saves nothing and costs turns: on one
    live deployment 285 of 357 raw-backfill runs spent 2 of their 4 iterations finding their
    write tool. WARN (not FAIL): a deployment may opt in deliberately.
    """
    cfg = DATA / "config.yaml"
    if not cfg.is_file():
        return
    try:
        doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except (OSError, YAMLError):  # bare name: Cosmic Ray cannot mutate a dotted exception
        add("WARN", "tool search", f"{cfg}: unreadable — tool disclosure setting UNDETECTABLE")
        return
    setting = ((doc.get("tools") or {}).get("tool_search") if isinstance(doc, dict) else None)
    enabled = setting.get("enabled") if isinstance(setting, dict) else setting
    if str(enabled).strip().lower() in ("off", "false"):
        add("PASS", "tool search", "progressive tool disclosure off (lanes call their tools directly)")
        return
    add("WARN", "tool search", "tools.tool_search.enabled is not \"off\" (Hermes default \"auto\") — "
        "every lane spends turns on tool_search/describe before it can write; set it off in "
        "config.yaml (see config/config.yaml.template)")


CHECKS = {
    "pins": check_pins,
    "schema": check_schema,
    "subdomains": check_subdomains,
    "crons": check_crons,
    "timezone": check_timezone,
    "scheduler-dst": check_scheduler_dst,
    "partition-dups": check_partition_dups,
    "reserved-basenames": check_reserved_basenames,
    "source-identity": check_source_identity,
    "rules": check_rules,
    "extensions": check_extensions,
    "ownership": check_ownership,
    "runtime-ownership": check_runtime_ownership,
    "auth": check_auth,
    "write-path": check_write_path_libs,
    "chat-capability": check_chat_capability,
    "provenance": check_provenance_env,
    "write-principal": check_write_principal,
    "operations": lambda: check_operation_runs(),
    "tool-search": check_tool_search,
}
# The daily validator runs every registered check except operations (doctor-only). The list is
# pinned by test_registry_covers_the_validator_checks_plus_operations, so adding one is deliberate.
VALIDATE_CHECKS = [k for k in CHECKS if k != "operations"]
# post_deploy_verify.sh step [9] runs every registered check host-side EXCEPT these, each of which
# needs the running gateway and runs in its own in-container step there (okengine#1032 [22]). The
# step derives its list from CHECKS minus this map, so a new check runs at deploy time by default;
# tests/cron/test_post_deploy_verify_checks.py pins that each exclusion's in-container step exists.
POST_DEPLOY_HOST_EXCLUDED = {
    "write-path": "compares the BAKED /opt/hermes libs with the staged copies; step [3b] does it in "
                  "the gateway",
    "runtime-ownership": "needs the container's HERMES_UID; step [5c] stats the runtime dir in the "
                         "gateway",
    "ownership": "must run as the lane uid inside the gateway; step [5f] runs it there",
    "chat-capability": "reads the policy catalog baked into the gateway image; its own in-container "
                       "step runs it there",
    "scheduler-dst": "runs the DST self-test under the gateway's baked venv interpreter; its own "
                     "in-container step runs it there",
}


def host_env(deployment, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment compose hands the gateway of the HOST deployment dir `deployment`: its pack
    .env read as both compose's project .env and the gateway's env_file, with `environ` (default:
    this process's) as compose's invoking environment. `.hermes-data/.env`, which Hermes loads over
    it, is applied where the checks need it (_gateway_env_and_chat), exactly as in the gateway.

    TZ and HERMES_UID are the exceptions to the env_file view: the gateway service sets
    `TZ=${TZ:-UTC}` and `HERMES_UID=${HERMES_UID:-10000}` in its `environment:`, which overrides
    env_file and resolves from the invoking environment first, so they are taken as compose
    interpolates them (okengine#1042 [36] review, okengine#1044 [9]); HERMES_UID only when set."""
    environ = os.environ if environ is None else environ
    env = read_compose_env(Path(deployment) / ".env", environ)
    env["TZ"] = compose_default("TZ", environ, Path(deployment) / ".env", "UTC")
    # Same for the gateway's `HERMES_UID=${HERMES_UID:-10000}`: the uid the scheduler runs as,
    # which check_crons judges the store's owner against (okengine#1044 [9]). Only an explicit
    # value is carried: with none, check_crons keeps judging by the running uid rather than by the
    # bare-compose default, which deploy.sh and ensure-runtime never leave in effect.
    uid = compose_default("HERMES_UID", environ, Path(deployment) / ".env", "")
    if uid:
        env["HERMES_UID"] = uid
    return env


def configure(vault, data=None, hermes=None, env: Mapping[str, str] | None = None) -> None:
    """Point the checks at a specific deployment (host `framework status/doctor`). Rebinds the module
    globals the checks read; unspecified DATA/HERMES keep their env defaults. `env` is the gateway
    environment the checks judge (a host run passes host_env(deployment)); omitted, it is this
    process's environment, as in the gateway, so a later in-process configure never inherits it."""
    global VAULT, DATA, HERMES, ENV
    VAULT = Path(vault)
    ENV = os.environ if env is None else dict(env)
    if data is not None:
        DATA = Path(data)
    if hermes is not None:
        HERMES = Path(hermes)


def reset() -> None:
    F.clear()


def run(names=None):
    """Reset, run the selected checks (default: the validator set) in canonical order, and return the
    findings as (level, area, msg) tuples. A crashing check is recorded FAIL, never raised."""
    reset()
    if names is None:
        selected = list(VALIDATE_CHECKS)
    else:
        want = set(names)
        selected = [n for n in CHECKS if n in want]
        # okengine#994 [15]: a name this copy does not register never ran, which is not a pass.
        # post_deploy_verify asks the STAGED copy for a check by name; one staged before the
        # check existed used to return no findings, read as "nothing to report".
        for name in sorted(want - set(CHECKS)):
            add("FAIL", "validator", f"unknown check {name!r}: this deployment_checks.py does not "
                "register it (stale staged copy?), so it never ran")
    for name in selected:
        try:
            CHECKS[name]()
        except Exception as e:  # a single check must never sink the whole run
            add("FAIL", "validator", f"{name} crashed: {e}")
    return list(F)


def check_operation_runs():
    """okengine#325/#405 (doctor-only): surface the engine-owned operation run receipts — any run
    still `running` whose worker pid is dead (crashed/orphaned), and recent terminal failures.
    Offline-safe: reads only .okengine/operations/runs receipts under the vault."""
    base = VAULT / ".okengine" / "operations" / "runs"
    if not base.is_dir():
        return
    stuck, failed, total = [], [], 0
    for rp in sorted(base.glob("*/*.json")):  # glob-ok: receipts live at operation/run.json by contract, not a sharded content namespace
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            add("WARN", "operations", f"unreadable operation receipt {rp.relative_to(VAULT)}")
            continue
        total += 1
        status = str(r.get("status") or "")
        if status == "running":
            pid = r.get("pid")
            alive = False
            if isinstance(pid, int) and pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except (OSError, ValueError):
                    alive = False
            if not alive:
                stuck.append(f"{r.get('operation')}/{r.get('run_id')} (pid {pid} gone)")
        elif status == "failed":
            failed.append(f"{r.get('operation')}/{r.get('run_id')}")
    if stuck:
        add("WARN", "operations", f"{len(stuck)} operation run(s) stuck 'running' with a dead worker "
            f"(orphaned by a crash): {'; '.join(stuck[:5])}{' …' if len(stuck) > 5 else ''}")
    if failed:
        add("INFO", "operations", f"{len(failed)} of {total} operation run(s) ended failed: "
            f"{', '.join(failed[:5])}{' …' if len(failed) > 5 else ''}")
