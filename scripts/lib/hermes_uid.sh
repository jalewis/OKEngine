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

_okengine_env_file_val() {   # $1=pack_dir  $2=VAR  -> stdout the pinned value, or non-zero
    [ -f "$1/.env" ] || return 1
    local v
    v="$(sed -n "s/^$2=//p" "$1/.env" | head -1 | tr -d '[:space:]')"
    [ -n "$v" ] && printf '%s' "$v"
}

_okengine_tree_owner() {     # $1=pack_dir  $2=stat_fmt (%u|%g)  -> non-root owner of the runtime tree
    local pack_dir="$1" fmt="$2" t v
    for t in "$pack_dir/.hermes-data" "$pack_dir"; do
        v="$(stat -c "$fmt" "$t" 2>/dev/null)" || continue
        if [ -n "$v" ] && [ "$v" != "0" ]; then printf '%s' "$v"; return 0; fi
    done
    return 1
}

resolve_hermes_uid() {       # $1=pack_dir  -> echoes the uid to exec as
    local pack_dir="$1" v
    if [ -n "${HERMES_UID:-}" ]; then printf '%s' "$HERMES_UID"; return; fi
    if v="$(_okengine_env_file_val "$pack_dir" HERMES_UID)"; then printf '%s' "$v"; return; fi
    if v="$(_okengine_tree_owner "$pack_dir" '%u')"; then printf '%s' "$v"; return; fi
    echo "  ⚠ HERMES_UID unresolved (no env, no .env pin, tree owner root/unknown) — defaulting to" \
         "10000; the cron runner will STALL if /opt/data isn't 10000-owned (okengine#185)" >&2
    printf '10000'
}

resolve_hermes_gid() {       # $1=pack_dir  -> echoes the gid to exec as (mirrors the uid resolution)
    local pack_dir="$1" v
    if [ -n "${HERMES_GID:-}" ]; then printf '%s' "$HERMES_GID"; return; fi
    if v="$(_okengine_env_file_val "$pack_dir" HERMES_GID)"; then printf '%s' "$v"; return; fi
    if v="$(_okengine_tree_owner "$pack_dir" '%g')"; then printf '%s' "$v"; return; fi
    printf '10000'
}
