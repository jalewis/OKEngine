#!/usr/bin/env bash
# Bring deployed OKEngine gateways up ONE AT A TIME, in a caller-supplied order.
#
# Why this exists: `restart: unless-stopped` is the required policy (three prior
# incidents), and it means the Docker daemon starts every gateway on the same tick at
# boot. Each one then wakes a scheduler holding hours of overdue lanes, and they all
# fire at once. On 2026-08-14 that took a 12-core host to loadavg 63, the kernel OOM
# killer ran, the host rebooted, and two containers that had already been killed were
# never restored -- a vault went 43 hours without an update and nothing said so.
#
# So the policy stays `unless-stopped` (a crashed gateway must still self-recover) and
# this script imposes the ORDER instead: stop the ones that should wait, then start
# them in sequence, each gated on the previous being genuinely ready.
#
# DEPLOYMENT ORDER IS THE CALLER'S, not the engine's: pass deployment dirs in priority
# order, exactly as reconcile-fleet-ownership.sh takes them. The engine ships no
# deployment-specific paths.
#
# Usage: staggered-start.sh <deployment-dir> [...]     # in PRIORITY ORDER
# Env:   STAGGER_READY_TIMEOUT (default 300s per gateway)
#        STAGGER_SPACING       (default 60s between the non-priority tail)
set -euo pipefail

READY_TIMEOUT="${STAGGER_READY_TIMEOUT:-300}"
SPACING="${STAGGER_SPACING:-60}"

[ "$#" -gt 0 ] || {
  echo "usage: staggered-start.sh <deployment-dir> [...]   # in PRIORITY ORDER" >&2
  exit 2
}

# Serialize: a @reboot run and a manual run must never interleave stops and starts.
exec 9>"/tmp/okengine-staggered-start.lock"
flock -n 9 || { echo "staggered-start: another run holds the lock; exiting" >&2; exit 0; }

log() { echo "staggered-start: $(date -Is) $*"; }

# Resolve a deployment's gateway by COMPOSE LABEL, never by container name. Container
# names collide across hosts and stacks -- two machines can each hold a container of the
# same name, and `docker ps` cannot tell them apart. The label is the identity that
# actually ties a container to THIS directory.
#
# Compare PHYSICAL paths, not the label string. Compose records whatever spelling the
# caller used, and this host reaches the same tree by two of them (~/Source is a symlink
# to ~/MEGA/Source). An exact `--filter label=` match on the wrong spelling finds nothing
# and the deployment is silently skipped — unordered, unstopped, and unreported. So list
# the gateways and match on readlink -f, which is true for either spelling.
gateway_of() {
  local dir="$1" want cid label
  want="$(readlink -f "$dir" 2>/dev/null)" || return 1
  [ -n "$want" ] || return 1
  for cid in $(docker ps -aq --filter "label=com.docker.compose.service=gateway" 2>/dev/null); do
    label="$(docker inspect -f \
      '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid" 2>/dev/null)"
    [ -n "$label" ] || continue
    if [ "$(readlink -f "$label" 2>/dev/null)" = "$want" ]; then
      echo "$cid"
      return 0
    fi
  done
  return 1
}

tick_mtime() {
  local tick="$1/.hermes-data/cron-plus/.tick.lock"
  [ -f "$tick" ] && stat -c %Y "$tick" 2>/dev/null || echo 0
}

# Ready is NOT "docker says running", and it is NOT "a tick file exists with a recent
# mtime" either. The tick file SURVIVES the stop: a gateway restarted seconds later
# still has a file stamped from before it went down, so an age check passes without a
# single new tick — the gate reports ready having verified nothing, and the next gateway
# starts into a host that is still busy.
#
# So readiness is a tick that is NEWER THAN THE ONE WE SAW BEFORE STOPPING IT. That can
# only be produced by the process we just started.
ready() {
  local dir="$1" cid="$2" baseline="$3" now
  [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ] || return 1
  now="$(tick_mtime "$dir")"
  if [ "$now" -eq 0 ]; then
    # No cron-plus runtime for this deployment (e.g. a non-OKEngine gateway). "Running"
    # is the only signal available; say so rather than implying a scheduler was checked.
    return 0
  fi
  [ "$now" -gt "$baseline" ]
}

wait_ready() {
  local dir="$1" cid="$2" name="$3" baseline="$4" deadline=$(( $(date +%s) + READY_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ready "$dir" "$cid" "$baseline"; then
      if [ "$(tick_mtime "$dir")" -eq 0 ]; then
        log "READY  $name (running; no scheduler to check)"
      else
        log "READY  $name (scheduler ticked)"
      fi
      return 0
    fi
    sleep 5
  done
  # Never hang the boot on one sick deployment, and never call the timeout a success.
  log "TIMEOUT $name did not tick within ${READY_TIMEOUT}s — continuing with the rest"
  return 1
}

# Wait for the daemon itself; at @reboot this script can beat dockerd.
for _ in $(seq 1 60); do
  docker info >/dev/null 2>&1 && break
  sleep 2
done
docker info >/dev/null 2>&1 || { echo "staggered-start: docker unavailable" >&2; exit 1; }

names=() cids=() dirs=()
for dir in "$@"; do
  if [ ! -f "$dir/docker-compose.yml" ]; then
    log "SKIP   invalid deployment (no docker-compose.yml): $dir"
    continue
  fi
  cid="$(gateway_of "$dir" || true)"
  if [ -z "$cid" ]; then
    log "SKIP   no gateway container for: $dir"
    continue
  fi
  names+=("$(basename "$dir")")
  cids+=("$cid")
  dirs+=("$dir")
done

[ "${#cids[@]}" -gt 0 ] || { echo "staggered-start: no gateways resolved" >&2; exit 1; }

# If this script dies between the stop and the start, every gateway it stopped would
# stay down -- `unless-stopped` does not undo a deliberate stop. Guarantee they are all
# started again on ANY exit path.
restore() {
  local rc=$?
  for cid in "${cids[@]}"; do
    [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" = "true" ] \
      || docker start "$cid" >/dev/null 2>&1 || true
  done
  exit "$rc"
}
trap restore EXIT INT TERM

log "ordering ${#cids[@]} gateway(s): ${names[*]}"

# Record each scheduler's last tick BEFORE anything is stopped. This is the baseline a
# restarted gateway must beat to be called ready; without it the surviving tick file
# makes every gate pass instantly.
baselines=()
for i in "${!cids[@]}"; do
  baselines+=("$(tick_mtime "${dirs[$i]}")")
done

# Hold everything after the first, so the priority deployment gets the host to itself.
for i in "${!cids[@]}"; do
  [ "$i" -eq 0 ] && continue
  docker stop "${cids[$i]}" >/dev/null 2>&1 || true
done

for i in "${!cids[@]}"; do
  dir="${dirs[$i]}"
  name="${names[$i]}"
  cid="${cids[$i]}"
  [ "$i" -eq 0 ] || { log "SPACE  ${SPACING}s before $name"; sleep "$SPACING"; }
  log "START  $name"
  docker start "$cid" >/dev/null 2>&1 || log "WARN   docker start failed for $name"
  wait_ready "$dir" "$cid" "$name" "${baselines[$i]}" || true
done

log "done — all gateways started in order"
