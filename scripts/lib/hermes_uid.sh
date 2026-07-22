# shellcheck shell=bash
# hermes_uid.sh — resolve the uid/gid the pack's gateway remaps to, WITHOUT blindly defaulting to
# the image's 10000. A deploy that writes /opt/data as the wrong owner stalls the whole cron fleet:
# cron-plus runs as the pack uid and cannot read a root/10000-owned jobs.json, so every scheduled
# lane silently stops (okengine#185 — observed live: a deploy run without HERMES_UID exported wrote
# jobs.json root-owned and the fleet went dark until the file was chowned back).
#
# The pack's .env already pins HERMES_UID (deploy.sh writes it there so bare `docker compose` ops
# match the tree owner) — but the standalone deploy scripts only read the SHELL env, so forgetting
# `export HERMES_UID` fell through to 10000. This resolves it the way compose effectively does, in
# order of authority:
#   1. explicit HERMES_UID/HERMES_GID in the environment  (operator override wins)
#   2. the pack's .env pin                                 (what deploy.sh recorded)
#   3. the runtime tree owner                              (stat .hermes-data, else the pack dir)
#   4. last-resort 10000 + a LOUD warning                  (old behaviour, but no longer silent)

_okengine_env_file_val() {   # $1=pack_dir  $2=VAR  -> stdout dotenv value, or non-zero
    [ -f "$1/.env" ] || return 1
    python3 - "$1/.env" "$2" <<'PY'
import re
import shlex
import sys
from pathlib import Path

path, key = Path(sys.argv[1]), sys.argv[2]
rx = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=\s*(.*?)\s*$")
for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    match = rx.match(line)
    if not match:
        continue
    try:
        parts = shlex.split(match.group(1), comments=True, posix=True)
    except ValueError:
        sys.exit(1)
    if parts:
        print(" ".join(parts), end="")
        sys.exit(0)
    sys.exit(1)
sys.exit(1)
PY
}

_okengine_tree_owner() {     # $1=pack_dir  $2=stat_fmt (%u|%g)  -> non-root owner of the runtime tree
    local pack_dir="$1" fmt="$2" t v
    for t in "$pack_dir/.hermes-data" "$pack_dir"; do
        v="$(stat -c "$fmt" "$t" 2>/dev/null)" || continue
        if [ -n "$v" ] && [ "$v" != "0" ]; then printf '%s' "$v"; return 0; fi
    done
    return 1
}

resolve_hermes_uid() {       # $1=pack_dir  -> echoes the uid to exec as (never root: #185 / #326 [9])
    local pack_dir="$1" v
    # An explicit 0 (root) from env OR .env is REJECTED, never honoured — remapping the gateway to
    # root writes a root-owned jobs.json the pack-uid cron runner can't read (the fleet goes dark,
    # #185). The tree-owner tier already excludes root; the explicit tiers must too, else a stray
    # HERMES_UID=0 short-circuits the guard (#326 [9]).
    if [ -n "${HERMES_UID:-}" ]; then
        if [ "${HERMES_UID}" != "0" ]; then printf '%s' "$HERMES_UID"; return; fi
        echo "  ⚠ HERMES_UID=0 (root) ignored — the gateway must never run as root (okengine#185)" >&2
    fi
    if v="$(_okengine_env_file_val "$pack_dir" HERMES_UID)"; then
        if [ "$v" != "0" ]; then printf '%s' "$v"; return; fi
        echo "  ⚠ .env HERMES_UID=0 (root) ignored — the gateway must never run as root (okengine#185)" >&2
    fi
    if v="$(_okengine_tree_owner "$pack_dir" '%u')"; then printf '%s' "$v"; return; fi
    echo "  ⚠ HERMES_UID unresolved (env/.env root-rejected, tree owner root/unknown) — defaulting to" \
         "10000; the cron runner will STALL if /opt/data isn't 10000-owned (okengine#185)" >&2
    printf '10000'
}

resolve_hermes_gid() {       # $1=pack_dir  -> echoes the gid to exec as (mirrors the uid resolution)
    local pack_dir="$1" v
    if [ -n "${HERMES_GID:-}" ]; then
        if [ "${HERMES_GID}" != "0" ]; then printf '%s' "$HERMES_GID"; return; fi
        echo "  ⚠ HERMES_GID=0 (root) ignored — the gateway must never run as root (okengine#185)" >&2
    fi
    if v="$(_okengine_env_file_val "$pack_dir" HERMES_GID)"; then
        if [ "$v" != "0" ]; then printf '%s' "$v"; return; fi
        echo "  ⚠ .env HERMES_GID=0 (root) ignored — the gateway must never run as root (okengine#185)" >&2
    fi
    if v="$(_okengine_tree_owner "$pack_dir" '%g')"; then printf '%s' "$v"; return; fi
    echo "  ⚠ HERMES_GID unresolved (env/.env root-rejected, tree owner root/unknown) — defaulting to" \
         "10000; the cron runner will STALL if /opt/data isn't 10000-owned (okengine#185)" >&2
    printf '10000'
}
