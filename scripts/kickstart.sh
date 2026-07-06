#!/usr/bin/env bash
# kickstart — fully populate a freshly-deployed vault NOW instead of waiting for the schedule.
#
# A fresh install ships inert and the fleet is scheduled across hours/days (feeds ~2h,
# backfills hourly, dashboards daily, brief weekly), so the wiki + dashboards stay empty for
# a long time and a new operator can't see that it works (okengine#109). This walks the WHOLE
# build/maintenance fleet ONCE, in dependency order, through cron-plus (each cron runs exactly
# as configured), waiting for each cron to finish before the next so later lanes see the work
# the earlier lanes produced:
#
#   ingest -> compile -> score -> entities -> schema/repair -> graph -> concepts ->
#   canonical -> predictions -> quality/audit -> index+dashboards -> brief
#
# Opt-in (the agent lanes spend on the model): `deploy.sh --kickstart`, or directly:
#   CRON_PACK_DIR=<pack> bash scripts/kickstart.sh <pack-dir>
set -euo pipefail

PACK_DIR="${1:-${CRON_PACK_DIR:-$PWD}}"
HUID="${HERMES_UID:-$(id -u)}"

# THIS pack's gateway via its compose project (not the first gateway on a multi-pack host, #108).
CONTAINER="$(docker compose -f "$PACK_DIR/docker-compose.yml" ps -q gateway 2>/dev/null | head -1)"
if [ -z "$CONTAINER" ]; then
    echo "ERROR: no running gateway for $PACK_DIR — deploy the stack first." >&2
    exit 1
fi

echo "==> kickstart: building the full vault now (one-time, dependency-ordered; skips the schedule)"

# Orchestrate INSIDE the gateway (cli.py + jobs.json are there and we can poll/sleep locally).
docker exec -i -u "$HUID" "$CONTAINER" /opt/hermes/.venv/bin/python - <<'PY'
import json, subprocess, sys, time
CLI  = "/opt/data/plugins/cron-plus/cli.py"
JOBS = "/opt/data/cron-plus/jobs.json"
PYBIN = sys.executable

def jobs():
    with open(JOBS, encoding="utf-8") as f:
        return {j["id"]: j for j in json.load(f)["jobs"]}

def by_name(substr):
    return [j for j in jobs().values()
            if j.get("enabled", True) and substr in j.get("name", "")]

def by_tier(label):
    # okengine#129: extension jobs that declared `tier: <label>` slot into that stage,
    # rather than guessing a wall-clock time relative to the engine fleet.
    return [j for j in jobs().values()
            if j.get("enabled", True) and j.get("tier") == label]

def cli(*args):
    subprocess.run([PYBIN, CLI, *args], capture_output=True, text=True)

def run_cron(job, timeout):
    """Trigger one cron and wait for its run to complete (last_run_at advances). Returns
    (status, detail). no_agent crons finish in the tick; agent crons run async."""
    jid = job["id"]
    before = jobs()[jid].get("last_run_at")
    cli("run", jid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        cli("tick")
        cur = jobs()[jid]
        # A run is COMPLETE only when last_run_success is set. For agent crons last_run_at
        # advances when the selector fires (seconds) but the compile finishes much later, so
        # keying on last_run_at alone declares agent lanes done prematurely (okengine#114).
        if cur.get("last_run_at") not in (None, before) and cur.get("last_run_success") is not None:
            ok = cur.get("last_run_success")
            return ("ok" if ok else "FAIL", "" if ok else str(cur.get("last_error") or "see logs"))
        time.sleep(5)
    return ("timeout", f">{timeout}s")

# (stage label, [name-substrings IN ORDER], per-cron timeout, repeats)
# Ordered so each lane sees what the previous produced. Repeats drain batch processors.
STAGES = [
    ("ingest",        ["feed-fetch", "hf-import", "hf-papers-import"], 240, 1),
    ("compile",       ["raw-backfill"],                                600, 1),
    ("score",         ["source-quality-backfill", "curated-fields-guard"], 600, 1),
    ("entities",      ["entity-backfill"],                             600, 1),
    ("schema/repair", ["normalize-entity-schema", "schema-type-drain", "schema-classify-drain",
                       "sanitize-frontmatter-updated", "repair-broken-frontmatter",
                       "detect-field-loss", "reshard-oversized",
                       "repair-yaml-propose", "repair-yaml-apply"], 420, 1),
    ("graph",         ["source-backlink-drain", "broken-wikilinks-drain", "orphans-drain"], 420, 1),
    ("concepts",      ["concept-backfill"],                            600, 1),
    ("canonical",     ["canonical-assemble", "publisher-canonical-drain"], 420, 1),
    # predictions is now the opt-in okengine.predictions extension — these run only if it's
    # enabled (by_name returns nothing otherwise, and the stage skips). See extensions/.
    ("predictions",   ["okengine.predictions:candidate-watch", "okengine.predictions:grade", "okengine.predictions:regrade"], 420, 1),
    ("quality/audit", ["source-staleness-refresh", "page-quality-audit", "page-quality-enrich",
                       "schema-drift-lint", "lint-watcher",
                       "wiki-health-audit"], 420, 1),
    ("index+dash",    ["reshelve", "tier-refresh", "corpus-indexer", "index-rebuild-daily",
                       "kb-health-refresh", "project-stats-refresh", "build-index-tree",
                       "build-hot-set", "refresh-kb-dashboards"], 420, 1),
    ("brief",         ["brief", "digest"],                            420, 1),
]

summary = {"ok": [], "FAIL": [], "timeout": [], "absent": []}
for label, names, timeout, repeats in STAGES:
    print(f"\n=== stage: {label} ===", flush=True)
    # the stage's engine/pack crons (by name) + any extension job that declared this
    # tier (#129), deduped by id so a tier-tagged job named like a stage isn't run twice.
    seen_ids, stage_jobs = set(), []
    for job in [c for s in names for c in by_name(s)] + by_tier(label):
        if job["id"] not in seen_ids:
            seen_ids.add(job["id"])
            stage_jobs.append(job)
    for job in stage_jobs:
        name = job["name"]
        last = None
        for r in range(repeats):
            status, detail = run_cron(job, timeout)
            last = status
            if status != "ok":
                break
        tag = {"ok": "✓", "FAIL": "✗", "timeout": "⏳"}.get(last, "?")
        print(f"  {tag} {name}" + (f"  [{detail}]" if last != "ok" else ""), flush=True)
        summary.setdefault(last, []).append(name)

print("\n==> kickstart summary")
print(f"   ok: {len(summary['ok'])}  |  FAIL: {len(summary['FAIL'])}  |  timeout: {len(summary['timeout'])}")
if summary["FAIL"]:
    print("   FAILED: " + ", ".join(summary["FAIL"]))
if summary["timeout"]:
    print("   TIMED OUT: " + ", ".join(summary["timeout"]))
print("==> kickstart done.")
PY
