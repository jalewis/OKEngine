#!/usr/bin/env bash
# vault-exec — run a command inside a deployment's gateway AS THE VAULT UID.
#
# Bare `docker exec` runs as root (the gateway image must start as root for s6),
# and one root-created file under /opt/vault silently blocks the vault-uid lanes that
# maintain it — this bit twice in one week (root-owned INDEX files after a manual
# index rebuild; a root-owned validation dashboard after a manual lane pre-test).
# This wrapper makes the correct thing the easy thing:
#
#   vault-exec.sh <deployment-dir> <command...>
#
# Resolves the gateway container from the deployment's compose project and the vault
# uid from HERMES_UID (the .env pin, else the gateway's own env, else the image
# default). Use bare `docker exec` ONLY for operations that genuinely need root
# (chown repairs, package installs).
set -euo pipefail

D="${1:?usage: vault-exec.sh <deployment-dir> <command...>}"
shift
GW="$(docker compose --project-directory "$D" ps -q gateway 2>/dev/null | head -1)"
if [ -z "$GW" ]; then
    echo "vault-exec: no running gateway for $D" >&2
    exit 1
fi
# Resolve the vault uid: the pack's .env pin, else the GATEWAY's own HERMES_UID (the running
# truth), else the image default 10000. NEVER a hardcoded operator uid — a fixed personal uid
# would run as the wrong user on any other host and mint the very foreign-owned strays this prevents.
UIDG="$(grep -oE '^HERMES_UID=[0-9]+' "$D/.env" 2>/dev/null | cut -d= -f2 || true)"
[ -n "$UIDG" ] || UIDG="$(docker exec "$GW" sh -c 'printf %s "${HERMES_UID:-10000}"' 2>/dev/null || true)"
UIDG="${UIDG:-10000}"
# Resolve the vault GID separately — reusing the uid as the group runs the command with the wrong
# primary group on a gateway whose gid != uid (a pack pinning HERMES_GID to a shared group), so a file
# it writes lands group-owned by the uid-as-gid and trips the very ownership guard this wrapper exists
# to uphold (invariant-audit B8). Mirror the uid resolution: the .env HERMES_GID pin, else the
# gateway's own HERMES_GID (running truth), else fall back to the uid (unpinned == old uid==gid).
GIDG="$(grep -oE '^HERMES_GID=[0-9]+' "$D/.env" 2>/dev/null | cut -d= -f2 || true)"
[ -n "$GIDG" ] || GIDG="$(docker exec "$GW" sh -c 'printf %s "${HERMES_GID:-}"' 2>/dev/null || true)"
GIDG="${GIDG:-$UIDG}"
exec docker exec -u "$UIDG:$GIDG" "$GW" "$@"
