#!/usr/bin/env bash
# Run the Qwen backfill qualification matrix strictly serially over the requested packs.
# Serial execution matches the shared local profile's model_concurrency=1 and
# avoids turning qualification itself into model-slot starvation.
set -uo pipefail

# No deployment default: the engine ships no operator filesystem layout. Set
# PACK_ROOT (or OKENGINE_PACK_ROOT) to the directory holding the pack checkouts.
PACK_ROOT="${PACK_ROOT:-${OKENGINE_PACK_ROOT:-}}"
if [ -z "$PACK_ROOT" ]; then
  echo "ERROR: set PACK_ROOT (or OKENGINE_PACK_ROOT) to the directory containing the pack checkouts" >&2
  exit 2
fi
ENGINE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
generation="${QUALIFICATION_GENERATION:-}"
generation="${generation:-g19}"
IMAGE="${QUALIFICATION_IMAGE:-hermes-agent}"
uid="${HERMES_UID:-1003}"
gid="${HERMES_GID:-1003}"
failures=0
prepared=0

# Fleet composition is OPERATOR INPUT, not an engine literal (okengine#510). These arrays
# held five deployment names and their gateway containers — one operator's fleet — inside a
# domain-agnostic engine, so the tool silently did nothing on anyone else's. The pack↔gateway
# PAIRING is the thing that cannot be discovered: nothing on disk distinguishes a deployment
# from an archived checkout or a pack source repo (13 have pack.yaml on the author's host,
# only 5 were targets). So both lists are supplied, positionally paired, and required.
#
#   QUALIFICATION_PACKS=<pack-dir-a>,<pack-dir-b> \
#   QUALIFICATION_GATEWAYS=<gateway-a>,<gateway-b> \
#     bash scripts/run_backfill_qualification_matrix.sh
IFS=',' read -r -a packs <<<"${QUALIFICATION_PACKS:-}"
IFS=',' read -r -a gateways <<<"${QUALIFICATION_GATEWAYS:-}"
if (( ${#packs[@]} == 0 )) || [ -z "${packs[0]}" ]; then
  echo "ERROR: set QUALIFICATION_PACKS to a comma-separated list of pack directory names" >&2
  exit 2
fi
if (( ${#gateways[@]} != ${#packs[@]} )); then
  echo "ERROR: QUALIFICATION_GATEWAYS must list one gateway container per pack, in the same order (got ${#gateways[@]} for ${#packs[@]} packs)" >&2
  exit 2
fi
# Export for prepare/cleanup, which take the same list and also ship no default.
export OKENGINE_QUALIFICATION_PACKS="${QUALIFICATION_PACKS}"

# Qualification artifacts live in the deployed corpus, so ordinary scheduled
# runs could otherwise claim them between matrix lanes. Pause only the lanes
# under test, wait for any already-claimed run to finish, and always restore
# them on exit. Direct runner invocation intentionally remains valid while a
# schedule is paused.
qualification_job_ids=(
  715b6e8b89be 9f869c8f7181 1e64bed86dff dd2ef37f8067
  da91e3a7b8ca 1550d7fa513e 9111a022aaa2
)
restore_schedules() {
  local gateway job_id
  for gateway in "${gateways[@]}"; do
    for job_id in "${qualification_job_ids[@]}"; do
      docker exec -u "$uid:$gid" "$gateway" python3 \
        /opt/data/plugins/cron-plus/cli.py resume "$job_id" >/dev/null 2>&1 || true
    done
  done
}
cleanup_fixtures() {
  if (( prepared == 1 )); then
    python3 "$ENGINE_ROOT/scripts/cleanup_backfill_qualification.py" \
      --pack-root "$PACK_ROOT" --generation "$generation" --apply
  fi
}
finish() {
  cleanup_fixtures
  restore_schedules
}
trap finish EXIT
trap 'exit 130' INT TERM
for gateway in "${gateways[@]}"; do
  for job_id in "${qualification_job_ids[@]}"; do
    docker exec -u "$uid:$gid" "$gateway" python3 \
      /opt/data/plugins/cron-plus/cli.py pause "$job_id" >/dev/null 2>&1 || true
  done
done
quiesce_seconds="${QUALIFICATION_QUIESCE_SECONDS:-900}"
for ((waited = 0; waited < quiesce_seconds; waited++)); do
  active=0
  for i in "${!packs[@]}"; do
    for job_id in "${qualification_job_ids[@]}"; do
      if [[ "$(docker exec -u "$uid:$gid" "${gateways[$i]}" python3 -c \
        "import sys;sys.path.insert(0,'/opt/data/plugins/cron-plus');import scheduler;print(int(scheduler._job_is_running('$job_id')))" \
        2>/dev/null)" == 1 ]]; then
        active=1
      fi
    done
  done
  (( active == 0 )) && break
  sleep 1
done
if (( active != 0 )); then
  echo "ERROR: qualification lanes did not quiesce after ${quiesce_seconds}s" >&2
  exit 113
fi
if [[ -n "${QUALIFICATION_GENERATION:-}" && "${QUALIFICATION_PREPARE:-1}" == 1 ]]; then
  # Set this before preparation so EXIT cleanup also removes a partially
  # written generation if fixture creation fails midway.
  prepared=1
  python3 "$ENGINE_ROOT/scripts/prepare_backfill_qualification.py" \
    "$generation" --pack-root "$PACK_ROOT" || exit $?
fi

pack_has_predictions() {
  # Enablement is vault-level state in the pack's .okengine/extensions.yaml, so parse it
  # rather than matching on a deployment's name. Host python3 already needs PyYAML for
  # prepare_backfill_qualification.py, so this adds no new dependency.
  python3 - "$PACK_ROOT/$1/.okengine/extensions.yaml" <<'PY'
import sys
try:
    import yaml
    data = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception:
    sys.exit(1)
enabled = data.get("enabled") if isinstance(data, dict) else None
sys.exit(0 if isinstance(enabled, dict) and "okengine.predictions" in enabled else 1)
PY
}

run_lane() {
  local pack="$1" gateway="$2" lane="$3" job_id="$4"
  shift 4
  local stamp log rc
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  log="$PACK_ROOT/$pack/.hermes-data/logs/cron-plus/$lane-$stamp.log"
  mkdir -p "$(dirname "$log")"
  docker run --rm --volumes-from "$gateway" --network "container:$gateway" \
    --entrypoint python3 \
    -e HERMES_UID="$uid" -e HERMES_GID="$gid" \
    -e CRON_PLUS_DISABLED=1 -e CRON_DEFER_UTC_HOURS= \
    -e WIKI_PATH=/opt/vault -e HERMES_HOME=/opt/data \
    "$@" "$IMAGE" /opt/data/plugins/cron-plus/runner.py \
    --job-id "$job_id" >"$log" 2>&1
  rc=$?
  printf '=== %s/%s rc=%s\n' "$pack" "$lane" "$rc"
  grep -E 'Inference transport selected|verified completion receipt|runner completed|invalid completion|ERROR' \
    "$log" | tail -8 || tail -12 "$log"
  # The receipt verdict must distinguish "the log says no" from "we could not
  # read the log".
  #
  # This previously shelled out to `rg`, which is NOT a binary everywhere -- on
  # the operator host it is an interactive shell function, and a script run as
  # `bash script.sh` inherits no interactive functions. So every call exited
  # "command not found", matched nothing, and the `! rg -q` test read that empty
  # result as proof of a bad receipt: 29 lanes that had each logged
  # {'selected': 1, 'accepted': 1, 'undisposed': 0} were reported as 29
  # failures. That is the empty-parse-is-not-a-negative trap, and it is the
  # second time it has cost this project a day. grep -E is POSIX and always
  # present.
  if (( rc == 0 )); then
    if [ ! -s "$log" ]; then
      printf 'UNVERIFIABLE: %s/%s wrote no log — receipt state UNKNOWN, not a pass\n' \
        "$pack" "$lane"
      rc=113
    elif ! grep -Eq \
        "verified completion receipt: .*'selected': 1.*'accepted': 1.*'undisposed': 0" \
        "$log"; then
      printf 'ERROR: %s/%s did not prove one accepted, fully disposed receipt\n' \
        "$pack" "$lane"
      rc=112
    fi
  fi
  if (( rc != 0 )); then
    failures=$((failures + 1))
  fi
}

run_group() {
  local lane="$1" job_id="$2"
  shift 2
  local i
  for i in "${!packs[@]}"; do
    run_lane "${packs[$i]}" "${gateways[$i]}" "$lane" "$job_id" "$@"
  done
}

run_group source-quality-backfill 715b6e8b89be \
  -e QUALITY_BACKFILL_BATCH_SIZE=1 \
  -e QUALITY_BACKFILL_TARGET="${QUALITY_BACKFILL_TARGET:-wiki/sources/2026/07/27/qwen-final-control-$generation.md}"
run_group entity-backfill 9f869c8f7181 \
  -e ENTITY_BACKFILL_TARGET="${ENTITY_BACKFILL_TARGET:-sources/2026/07/27/qwen-final-control-$generation.md}"
run_group concept-backfill 1e64bed86dff \
  -e CONCEPT_BACKFILL_MIN_INBOUND=1 \
  -e CONCEPT_BACKFILL_BATCH_SIZE=1 \
  -e CONCEPT_BACKFILL_TARGET="${CONCEPT_BACKFILL_TARGET:-qwen-final-local-backfill-control-$generation}"
run_group page-quality-enrich dd2ef37f8067 \
  -e PQ_ENRICH_QUEUE="wiki/operational/qwen-page-quality-qualification-$generation.json" \
  -e ENRICH_COOLDOWN_DAYS=0
run_group raw-backfill da91e3a7b8ca \
  -e RAW_BATCH_SIZE=1 \
  -e RAW_BACKFILL_TARGET="${RAW_BACKFILL_TARGET:-raw/qualification/qwen-final-control-$generation.md}"

# The prediction lane runs only where okengine.predictions is ENABLED. This was
# `for i in 0 1 3` — literal indices into a fixed five-pack array, which silently selects the
# wrong packs (or dies under `set -u`) the moment the pack list is supplied rather than
# hardcoded. Ask the pack what it enables instead (okengine#510).
for i in "${!packs[@]}"; do
  if pack_has_predictions "${packs[$i]}"; then
    run_lane "${packs[$i]}" "${gateways[$i]}" \
      okengine.predictions_prediction-structural-backfill 1550d7fa513e \
      -e PSB_BATCH_SIZE=1 \
      -e PSB_TARGET="predictions/qwen-qualification-$generation"
  fi
done

# An optional pack-specific lane, supplied as pack:gateway:lane:job-id and skipped when
# unset. This was a hardcoded pack name, gateway and lane — domain knowledge in a
# domain-agnostic engine, and it failed for anyone whose fleet lacked that deployment.
if [ -n "${QUALIFICATION_EXTRA_LANE:-}" ]; then
  IFS=':' read -r xpack xgateway xlane xjob <<<"$QUALIFICATION_EXTRA_LANE"
  if [ -z "$xpack" ] || [ -z "$xgateway" ] || [ -z "$xlane" ] || [ -z "$xjob" ]; then
    echo "ERROR: QUALIFICATION_EXTRA_LANE must be pack:gateway:lane:job-id" >&2
    exit 2
  fi
  run_lane "$xpack" "$xgateway" "$xlane" "$xjob"
fi

printf 'qualification matrix complete: failures=%s\n' "$failures"
exit "$failures"
