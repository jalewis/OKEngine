#!/usr/bin/env bash
# Seed a pack's runtime dir (.hermes-data) BEFORE `docker compose up`, and make
# sure the gateway uid can actually write it.
#
# A library pack fetched from git has no .hermes-data/ — it's gitignored runtime
# state, seeded by `framework init`/`pull`. A plain `git clone` (the common case
# for a catalog pack) skips that, and the gateway bind-mounts
# `./.hermes-data:/opt/data`. If the dir is missing at compose-up Docker
# auto-creates it as ROOT; and even when present, the gateway runs as
# HERMES_UID:HERMES_GID (default: the invoking user's uid, so a clone-as-yourself
# tree is writable out of the box) — if you instead PIN a uid the tree isn't
# writable by, the gateway can't `mkdir /opt/data/logs` and stays unhealthy while
# showing as Up (issue #16, okengine#102).
#
# So this: (1) seeds .hermes-data (config.yaml from the engine template + qmd/ +
# logs/), idempotently (an existing config is left untouched); (2) ensures the
# tree is writable by HERMES_UID — and if it isn't, FAILS before compose with an
# actionable message (or fixes it with --fix-perms).
#
# Usage:
#   bash $ENGINE_DIR/scripts/ensure-runtime.sh [pack-dir] [--fix-perms]
#   CRON_PACK_DIR=/path/to/pack bash .../ensure-runtime.sh
# Env: HERMES_UID/HERMES_GID (default: your uid/gid), FIX_PERMS=1 (same as --fix-perms).
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACK_ARG=""; FIX_PERMS="${FIX_PERMS:-0}"
for a in "$@"; do
    case "$a" in
        --fix-perms) FIX_PERMS=1 ;;
        -*) echo "unknown flag: $a" >&2; exit 2 ;;
        *) PACK_ARG="$a" ;;
    esac
done
PACK="$(cd "${PACK_ARG:-${CRON_PACK_DIR:-$PWD}}" && pwd)"
TMPL="$ENGINE_DIR/config/config.yaml.template"
RT="$PACK/.hermes-data"
CFG="$RT/config.yaml"
HUID="${HERMES_UID:-$(id -u)}"; HGID="${HERMES_GID:-$(id -g)}"

mkdir -p "$RT/qmd" "$RT/logs"
[ -f "$RT/.gitkeep" ] || : > "$RT/.gitkeep"

if [ -f "$CFG" ]; then
    echo "ok: $CFG already present (left untouched)"
elif [ -f "$TMPL" ]; then
    cp "$TMPL" "$CFG"
    echo "seeded: $CFG  <- $TMPL"
    echo "  review it (model provider, delivery) before deploy; secrets go in .env"
else
    echo "ERROR: engine config template not found: $TMPL" >&2
    exit 1
fi

# --- runtime version marker: stamp the ACTUAL engine/Hermes being deployed ---
# The reader's About reads this so it reports what's RUNNING, not the pack's DECLARED
# engine.version pins (which can be stale/wrong vs the deployed engine — a public pack pinned
# to an older engine still deploys on a newer one, and its hermes_pin then lies). okengine#119.
MANIFEST="$ENGINE_DIR/engine-manifest.yaml"
if [ -f "$MANIFEST" ]; then
    _rel="$(awk -F': *' '/^engine_release:/{print $2; exit}' "$MANIFEST" | awk '{print $1}')"
    _htag="$(awk -F': *' '/pinned_tag:/{print $2; exit}' "$MANIFEST" | awk '{print $1}')"
    _hsha="$(awk -F': *' '/pinned_sha:/{print $2; exit}' "$MANIFEST" | awk '{print $1}')"
    _esha="$(git -C "$ENGINE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    {
        printf 'engine_release: %s\n' "${_rel:-unknown}"
        printf 'hermes_pin: %s\n'     "${_htag:-unknown}"
        printf 'hermes_sha: %s\n'     "${_hsha:-unknown}"
        printf 'engine_sha: %s\n'     "$_esha"
    } > "$RT/engine-runtime.yaml"
    echo "stamped: $RT/engine-runtime.yaml  (engine ${_rel:-?} · Hermes ${_htag:-?})"
fi

# --- MCP auth: keep the gateway's read-MCP client header in sync with the token ---
# The read server requires `Bearer <OKENGINE_MCP_TOKEN>`, falling back to the
# built-in "okengine-local" when that env var is unset. The seeded config.yaml
# ships "okengine-local"; if the operator set a real OKENGINE_MCP_TOKEN in .env,
# rewrite the header to match or every read MCP call 401s (okengine#32). Runs even
# when CFG already existed, so a token added after the first deploy is picked up.
if [ -f "$CFG" ]; then
    MTOK=""
    if [ -f "$PACK/.env" ]; then
        _t="$(grep -E '^[[:space:]]*OKENGINE_MCP_TOKEN[[:space:]]*=' "$PACK/.env" | tail -1 | cut -d= -f2-)"
        _t="$(printf '%s' "$_t" | sed -E 's/^[[:space:]"'\'']+//; s/[[:space:]"'\'']+$//')"
        [ -n "$_t" ] && MTOK="$_t"
    fi
    # The read server REFUSES to serve the built-in "okengine-local" default on its
    # (container) 0.0.0.0 bind — it's public/well-known — so a fresh deploy with no real
    # token leaves the mcp crash-looping. Generate a secret and persist it to .env so the
    # mcp boots out of the box; the header rewrite below matches it (okengine#105).
    if [ -z "$MTOK" ] || [ "$MTOK" = "okengine-local" ]; then
        MTOK="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
        ENVF="$PACK/.env"
        if [ -f "$ENVF" ] && grep -qE '^[[:space:]]*OKENGINE_MCP_TOKEN[[:space:]]*=' "$ENVF"; then
            MTOK="$MTOK" ENVF="$ENVF" python3 - <<'PY'
import os, re, pathlib
f = pathlib.Path(os.environ["ENVF"])
f.write_text(re.sub(r'(?m)^[ \t]*OKENGINE_MCP_TOKEN[ \t]*=.*$',
                    "OKENGINE_MCP_TOKEN=" + os.environ["MTOK"], f.read_text(encoding="utf-8")),
             encoding="utf-8")
PY
        else
            printf 'OKENGINE_MCP_TOKEN=%s\n' "$MTOK" >> "$ENVF"
        fi
        echo "  generated a secret OKENGINE_MCP_TOKEN in .env (the read MCP refuses the built-in default on its bind)"
    fi
    # Python rewrite (no sed-escaping pitfalls with token punctuation); touches
    # only the okengine read-server Authorization header. Handles both the quoted
    # (`"Bearer x"`) and unquoted (`Bearer x`) YAML forms, preserving the quoting.
    _auth_status="$(CFG="$CFG" MTOK="$MTOK" python3 - <<'PY'
import os, re, pathlib
cfg = pathlib.Path(os.environ["CFG"]); tok = os.environ["MTOK"]
t = cfg.read_text(encoding="utf-8")
new = re.sub(r'(Authorization:[ \t]*"?)Bearer [^\n"]*("?)[ \t]*$',
             lambda m: m.group(1) + "Bearer " + tok + m.group(2),
             t, flags=re.M)
if new != t:
    cfg.write_text(new, encoding="utf-8")
    print("changed")
else:
    print("nochange")
PY
)"
    if [ "$_auth_status" = "changed" ]; then
        if [ "$MTOK" = "okengine-local" ]; then
            echo "mcp auth: read-MCP header reset to Bearer okengine-local (built-in local default)"
        else
            echo "mcp auth: read-MCP header synced to OKENGINE_MCP_TOKEN from .env"
        fi
    fi
fi

# --- writability: the gateway (uid HUID) must be able to write the runtime tree ---
_writable_by() {  # <dir> <uid> <gid> — true if uid/gid can write <dir>
    local d="$1" u="$2" g="$3" ou og perm
    ou="$(stat -c '%u' "$d")"; og="$(stat -c '%g' "$d")"; perm="$(stat -c '%A' "$d")"
    { [ "$ou" = "$u" ] && [ "${perm:2:1}" = "w" ]; } && return 0   # owner-write
    { [ "$og" = "$g" ] && [ "${perm:5:1}" = "w" ]; } && return 0   # group-write
    [ "${perm:8:1}" = "w" ] && return 0                            # other-write
    return 1
}

if _writable_by "$RT" "$HUID" "$HGID"; then
    : # the container uid can write the runtime — good
elif [ "$FIX_PERMS" = "1" ]; then
    chmod -R a+rwX "$PACK"
    echo "fix-perms: made $PACK group/other-writable so uid $HUID can write it"
    echo "  (local-deploy convenience — the tree is now world-writable; for a shared host"
    echo "   prefer running the whole stack as your own uid:"
    echo "   export HERMES_UID=\$(id -u) HERMES_GID=\$(id -g) — avoids both world-write and the"
    echo "   chown-vs-deploy.sh conflict, see okengine#33)"
else
    cat >&2 <<MSG
ERROR: the gateway runs as uid $HUID:$HGID, but $PACK is owned by $(id -un) (uid $(id -u))
       and is not writable by $HUID — the container cannot create /opt/data/logs and
       will fail to start (it may still show as 'Up'). Fix one of:
  - export HERMES_UID=\$(id -u) HERMES_GID=\$(id -g)  (recommended: run the whole stack as
                                                       yourself. deploy.sh writes the tree as you
                                                       and the gateway remaps to your uid — no
                                                       chown, no conflict)
  - re-run with --fix-perms                          (quick local: chmod -R a+rwX \$PACK — the
                                                       tree becomes world-writable)
  - sudo chown -R $HUID:$HGID "$PACK"                 (ONLY if you ALSO run deploy.sh as uid $HUID:
                                                       chowning to $HUID while you deploy as
                                                       $(id -un) breaks deploy.sh, which must write
                                                       the tree — okengine#33)
MSG
    exit 1
fi
