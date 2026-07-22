#!/usr/bin/env python3
"""deployment_validate.py — daily in-gateway deployment self-validation. no_agent.

The gap it closes: pack-side validators (`framework validate`, validate_merged) run at
authoring/deploy time on the HOST — nothing re-checked the LIVE deployment as it mutated
(pin bumps, co-installs, rule merges). Two real drifts shipped that way in one week: a
stale engine.version pin, and a type_alias shadowing a co-installed pack's type.

Checks (deterministic, against the deployed/staged state the gateway actually runs):
  1. version pins    engine.version (vault) vs the runtime stamp (/opt/data/engine-runtime.yaml)
  2. schema contract composed via the STAGED schema_lib: parses; type_aliases never shadow
                     canonical types; alias targets exist; partitioning namespaces exist
  3. sub-domains     every wiki/**/schema.yaml parses and declares types
  4. cron fleet      deployed jobs.json parses; script refs exist; no duplicate ids/names
  5. rules files     config/*rules*.yaml parse; rule ids unique
  6. extensions      .okengine/extensions.yaml parses; enabled extensions have staged scripts
  7. auth posture    trust: private + non-loopback bind -> password must be set; the Agent Chat
                     (api_server) toolset lockdown; and, when OKENGINE_HARDENED=1, the full
                     fail-closed safe profile (real MCP token, reader auth-or-public, rate limits
                     on, exports off if public) — okengine#78

Writes wiki/operational/deployment-validation.md. EXITS 1 when any FAIL exists — the lane
shows ERRORED in fleet health, which is the attention mechanism (a failed validation that
scrolls by silently is worse than none).

Env: WIKI_PATH (/opt/vault) · OKENGINE_DATA (/opt/data)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Sibling cron lib (staged into DATA/scripts alongside this script; scripts/cron/ in the repo).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from hardening_lib import hardened_posture_violations, is_hardened, is_editing  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
DATA = Path(os.environ.get("OKENGINE_DATA", "/opt/data"))
# The Hermes INSTALL dir (baked code + the okengine#192 version marker), NOT HERMES_HOME — which
# Hermes sets to the DATA dir (/opt/data). Fixed /opt/hermes; overridable for tests.
HERMES = Path(os.environ.get("OKENGINE_HERMES_DIR", "/opt/hermes"))
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


def check_crons():
    jf = DATA / "cron-plus" / "jobs.json"
    if not jf.is_file():
        add("FAIL", "crons", "deployed jobs.json missing — dead scheduler?")
        return
    # okengine#197: a root `docker exec` that rewrites the store re-owns it root:root — the
    # uid-service scheduler then silently stops firing EVERYTHING (44-min live all-lane outage).
    # Two red signals, at the earliest gate: (a) the store/pidfiles owned by someone other than
    # the uid running this validator (the service uid — deployment-validate runs as the gateway
    # service), (b) the scheduler's own .scheduler-stalled sentinel (cron-plus drops it when it
    # cannot load the store; a successful load clears it).
    me = os.geteuid()
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
        elif s and not Path(s).is_file():
            add("FAIL", "crons", f"job '{j.get('name')}' references missing script {s}")
    # NB: backlinks-refresh no longer needs iwe (okengine#179 — it builds the graph with an
    # in-process link-scanner), so there is no iwe binary dependency to validate here. iwe is
    # still used by the MCP's graph tools (kb_graph: find_references/retrieve_context/stats),
    # but that lives in the MCP container, not this gateway cron surface.


_DAILY_RE = re.compile(r"^\s*(\S+)\s+(\d+)\s+\*\s+\*\s+\*\s*$")


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
      TZ-aware cron-plus): a fresh deploy clones the old scheduler and TZ does nothing."""
    tz = (os.environ.get("TZ") or "").strip()
    tz_set = bool(tz) and tz.upper() != "UTC"
    jf = DATA / "cron-plus" / "jobs.json"
    if not jf.is_file():
        return
    try:
        jobs = json.loads(jf.read_text()).get("jobs", [])
    except Exception:
        return  # check_crons already FAILs on an unparseable jobs.json
    daily = [j.get("name") or "?" for j in jobs
             if j.get("enabled", True)
             and _DAILY_RE.match((j.get("schedule") or {}).get("expr") or "")]
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
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:
        return
    stale = []
    for j in jobs:
        if not j.get("enabled", True):
            continue
        m = _DAILY_RE.match((j.get("schedule") or {}).get("expr") or "")
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
                "jobs.json — its cron lanes may not have been folded (run deploy-cron-plus-jobs)")


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
                if (p.is_file() or p.is_dir()) and p.stat().st_uid != me:
                    kind = "dir" if p.is_dir() else "file"
                    strays.append(f"{p.relative_to(VAULT)} ({kind}, uid {p.stat().st_uid})")
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
        if not d.is_dir():
            continue
        try:
            if d.stat().st_uid != me:
                strays.append(f"{rel} (uid {d.stat().st_uid})")
        except OSError:
            strays.append(f"{rel} (unstattable)")
    # The single most critical FILE: cron-plus/jobs.json mis-owned (e.g. root:0600 from a deploy run
    # without the pack's HERMES_UID) is UNREADABLE by the lane uid, so the scheduler goes dark even
    # though the cron-plus DIR above is correctly owned — the exact fleet-stall poison, hit live
    # (okengine#193). The dir-level check misses a mis-owned file inside a well-owned dir; stat it.
    jf = DATA / "cron-plus" / "jobs.json"
    if jf.is_file():
        try:
            if jf.stat().st_uid != me:
                strays.append(f"cron-plus/jobs.json (uid {jf.stat().st_uid}) — the scheduler can't "
                              "read it and the WHOLE fleet stalls (okengine#193)")
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
_API_SERVER_SAFE_TS = {"okengine", "okengine-write", "web", "no_mcp"}


def check_auth():
    trust = os.environ.get("OKENGINE_TRUST", "private")
    bind = os.environ.get("OKENGINE_BIND", "127.0.0.1")
    pw = os.environ.get("OKENGINE_READER_PASSWORD", "")
    if trust == "private" and bind not in ("", "127.0.0.1", "localhost") and not pw:
        add("FAIL", "auth", "private vault exposed beyond loopback with NO password")

    # config.yaml is seeded ONCE from the template and never reconciled (ensure-runtime leaves an
    # existing one untouched). The secure `platform_toolsets.api_server: [okengine, okengine-write]`
    # lockdown was added to the template later (v0.10.7) — so a deployment seeded before it that then
    # enables Agent Chat inherits the BROAD default toolset (terminal / code_execution / file /
    # computer_use) on a network-exposed endpoint, and nothing validated it (invariant-audit HIGH).
    # Chat enables on ANY of: API_SERVER_ENABLED, an API_SERVER_KEY (Hermes enables on the key alone),
    # or platforms.api_server.enabled (re-verify: the first two were unchecked).
    enabled = (str(os.environ.get("API_SERVER_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")
               or bool(str(os.environ.get("API_SERVER_KEY", "")).strip()))
    cfg = _yaml(DATA / "config.yaml") if (DATA / "config.yaml").is_file() else None
    if cfg:
        plats = cfg.get("platforms") or {}
        if isinstance(plats.get("api_server"), dict) and plats["api_server"].get("enabled"):
            enabled = True
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
                    "[okengine, okengine-write] (see config/config.yaml.template).")
            elif (bad := {str(x).strip() for x in ts} - _API_SERVER_SAFE_TS):
                add("FAIL", "auth", f"Agent Chat (api_server) toolset has non-allowlisted member(s) "
                    f"{sorted(bad)} — a composite/alias can expand to terminal/code_execution/file. "
                    f"Restrict platform_toolsets.api_server to {sorted(_API_SERVER_SAFE_TS)}.")
            # okengine#257: the OKENGINE_EDITING switch must AGREE with the live toolset. Editing off is
            # enforced by DROPPING okengine-write here (ensure-runtime). If the flag says off but the
            # write MCP is still present, the switch never took (config not reconciled / gateway not
            # recreated) — UI editing (reader Chat write-back) is still exposed.
            if isinstance(ts, list) and not is_editing(os.environ) and \
                    "okengine-write" in {str(x).strip() for x in ts}:
                add("FAIL", "auth", "OKENGINE_EDITING is off but okengine-write is STILL in the "
                    "api_server toolset — UI editing (reader Chat write-back) is still exposed. Re-run "
                    "ensure-runtime and recreate the gateway so the switch applies (okengine#257).")

    # OKENGINE_HARDENED (okengine#78): a single opt-in profile. When set, the deployment ASSERTS it
    # must be safe to expose, so every unsafe setting is a FAIL (fail-closed — the profile never mints
    # secrets or flips values silently). hardened_posture_violations is the shared source of truth.
    if is_hardened(os.environ):
        viols = hardened_posture_violations(os.environ)
        for msg in viols:
            add("FAIL", "hardening", msg)
        if not viols:
            add("INFO", "hardening", "OKENGINE_HARDENED posture satisfied "
                "(real MCP token, reader auth-or-public, rate limits on, exports safe).")


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
                    f"lib. Rebuild the gateway image (build-engine-image.sh, or the id-index overlay); "
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


def check_provenance_env():
    """invariant-audit M14/#750: composition provenance (maintained_by / discovered_by) is stamped
    by the enforced write path ONLY when the gateway carries OKENGINE_PACK (deployment-pinned, never
    client-supplied). A pack whose compose predates the OKENGINE_PACK env (pre-2026-06-26 skeleton)
    silently stops stamping it on every write — undetectable until you ask 'which pack wrote this'.
    This validator runs IN the gateway, so its own env reflects what the write path sees."""
    if not os.environ.get("OKENGINE_PACK", "").strip():
        add("WARN", "provenance", "OKENGINE_PACK is not set in the gateway env — the write path "
            "stamps no composition provenance (maintained_by/discovered_by). Add "
            "`OKENGINE_PACK=<pack>` to the gateway service in docker-compose.yml and recreate it "
            "(regenerate from the current skeleton, or copy the line).")


def main() -> int:
    for c in (check_pins, check_schema, check_subdomains, check_crons,
              check_timezone, check_partition_dups, check_rules, check_extensions,
              check_ownership, check_runtime_ownership, check_auth, check_write_path_libs,
              check_provenance_env):
        try:
            c()
        except Exception as e:
            add("FAIL", "validator", f"{c.__name__} crashed: {e}")
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    F.sort(key=lambda x: (order[x[0]], x[1]))
    fails = sum(1 for l, _, _ in F if l == "FAIL")
    warns = sum(1 for l, _, _ in F if l == "WARN")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Deployment validation"', f"updated: {now}", "---",
         "", f"# Deployment validation — {now}", "",
         f"_Daily in-gateway self-check of the LIVE deployment (pins, composed schema, "
         f"sub-domains, cron fleet, rules, extensions, auth). A FAIL marks this lane ERRORED "
         f"in fleet health on purpose._", "",
         f"**{'FAIL' if fails else 'PASS'}** — {fails} fail · {warns} warn", ""]
    if F:
        L += ["| Level | Area | Finding |", "|---|---|---|"]
        L += [f"| {l} | {a} | {m} |" for l, a, m in F]
    L.append("")
    out = VAULT / "wiki" / "operational" / "deployment-validation.md"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(L), encoding="utf-8")
        dest = "-> wiki/operational/deployment-validation.md"
    except OSError as e:
        # The report file (or its dir) is foreign-owned (root, from a bare `docker exec` write), so
        # the lane uid can't overwrite it — the exact uid-desync condition check_ownership exists to
        # catch. A raw PermissionError here would crash ON THE VALIDATOR'S OWN OUTPUT, swallowing the
        # FAIL diagnosis it just computed (which names that very file). Fail loud with the remedy but
        # STILL print the findings below, so the diagnosis is never lost (okengine#178 peer pattern).
        print(f"deployment-validate: ERROR cannot write {out}: {e} — likely a foreign-owned (root) "
              "report file. Repair: scripts/fix-vault-ownership.sh <deployment-dir>", file=sys.stderr)
        dest = "(report unwritable — see stderr)"
    for l, a, m in F:
        print(f"  {l:<4} [{a}] {m}")
    print(f"deployment-validate: {'FAIL' if fails else 'PASS'} ({fails} fail, {warns} warn) {dest}")
    print(json.dumps({"wakeAgent": False}))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
