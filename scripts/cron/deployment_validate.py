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
  7. auth posture    trust: private + non-loopback bind -> password must be set

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
    if hp and hr and hp != hr:
        add("FAIL", "pins", f"hermes_pin {hp} != runtime Hermes {hr}")


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
    # STALENESS (#12): the enforced write path (merged_schema) prefers the on-disk
    # .okengine/composed-schema.yaml UNCONDITIONALLY when present, but ONLY `framework extensions
    # enable/disable` ever regenerates it — no deploy/upgrade/edit does. So a schema.yaml edit not
    # paired with an extension toggle is silently IGNORED on the write path (the frozen artifact
    # wins). We already recomputed the live `composed` above; compare the governance-bearing
    # sections and WARN on drift (the write path is using stale rules).
    art = VAULT / ".okengine" / "composed-schema.yaml"
    if art.is_file() and composed and not any(l == "FAIL" and c == "schema" for l, c, _ in F):
        on_disk = _yaml(art) or {}
        keys = ("types", "enums", "partitioning", "permissions", "owners", "review")
        if {k: composed.get(k) for k in keys} != {k: on_disk.get(k) for k in keys}:
            add("WARN", "schema", "composed-schema.yaml is STALE — it differs from a live recompose "
                "of schema.yaml + base + extensions, and the enforced write path uses the on-disk "
                "(stale) copy, so recent schema.yaml edits are not applied. Regenerate it: re-run "
                "the deploy (it recomposes an existing artifact) or any `framework extensions "
                "enable/disable`.")


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
            ".env and recreate the gateway.")
        return
    if _cron_plus_tz_aware() is False:
        add("FAIL", "timezone",
            f"TZ={tz} is set but the installed cron-plus plugin is UTC-naive (no CRON_TZ/TZ "
            f"handling) — the {len(daily)} daily lane(s) ({listed}) run in UTC, ignoring TZ "
            "(07:00 local fires 07:00 UTC). The plugin pin is stale: bump engine-manifest "
            "cron-plus pinned_sha to the TZ-aware commit, remove .hermes-data/plugins/cron-plus, "
            "re-run ensure-runtime, and recreate the gateway.")


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
    ef = VAULT / ".okengine" / "extensions.yaml"
    if not ef.is_file():
        return
    d = _yaml(ef) or {}
    for ext in (d.get("enabled") or {}):
        if not (DATA / "scripts" / ext).is_dir():
            add("FAIL", "extensions", f"enabled extension '{ext}' has no staged scripts dir — "
                                      "its lanes are dead; run deploy-cron-scripts")


def check_ownership():
    """Foreign-owned files under the vault silently block the lanes that maintain it
    (root-owned INDEX files; a root-owned dashboard — both shipped by bare `docker exec`
    which defaults to root). The lane runs AS the vault uid, so: anything it couldn't
    overwrite is a FAIL. Repair: scripts/fix-vault-ownership.sh (host) or ensure-runtime."""
    me = os.geteuid()
    strays = []
    for base in ("wiki", "raw", "config"):
        d = VAULT / base
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            try:
                if p.is_file() and p.stat().st_uid != me:
                    strays.append(f"{p.relative_to(VAULT)} (uid {p.stat().st_uid})")
            except OSError:
                strays.append(f"{p.relative_to(VAULT)} (unstattable)")
            if len(strays) > 25:
                break
    if strays:
        add("FAIL", "ownership", f"{len(strays)}{'+' if len(strays) > 25 else ''} file(s) not "
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
    for rel in ("cron-plus", "plugins/cron-plus", "scripts", "config"):
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


def check_auth():
    trust = os.environ.get("OKENGINE_TRUST", "private")
    bind = os.environ.get("OKENGINE_BIND", "127.0.0.1")
    pw = os.environ.get("OKENGINE_READER_PASSWORD", "")
    if trust == "private" and bind not in ("", "127.0.0.1", "localhost") and not pw:
        add("FAIL", "auth", "private vault exposed beyond loopback with NO password")


def main() -> int:
    for c in (check_pins, check_schema, check_subdomains, check_crons,
              check_timezone, check_partition_dups, check_rules, check_extensions,
              check_ownership, check_runtime_ownership, check_auth):
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
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    for l, a, m in F:
        print(f"  {l:<4} [{a}] {m}")
    print(f"deployment-validate: {'FAIL' if fails else 'PASS'} ({fails} fail, {warns} warn) "
          "-> wiki/operational/deployment-validation.md")
    print(json.dumps({"wakeAgent": False}))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
