#!/usr/bin/env bash
# fix-vault-ownership — chown foreign-owned vault files back to the vault uid.
# The repair half of the ownership guardrails (detection: deployment-validate's
# ownership FAIL; prevention: vault-exec.sh). Runs the chown INSIDE the gateway
# as container-root — the same context that creates the strays is the one that
# can fix them, no host sudo needed.
#   fix-vault-ownership.sh <deployment-dir> [--dry-run]
set -euo pipefail
D="${1:?usage: fix-vault-ownership.sh <deployment-dir> [--dry-run]}"
DRY="${2:-}"
GW="$(docker compose --project-directory "$D" ps -q gateway 2>/dev/null | head -1)"
[ -n "$GW" ] || { echo "no running gateway for $D" >&2; exit 1; }
# Vault uid: .env pin, else the gateway's own HERMES_UID (running truth), else the image default.
# NEVER a hardcoded operator uid — a fixed personal uid would chown the ENTIRE vault to the WRONG owner
# on any other host, killing every gateway write (the opposite of this script's purpose).
UIDG="$(grep -oE '^HERMES_UID=[0-9]+' "$D/.env" 2>/dev/null | cut -d= -f2 || true)"
[ -n "$UIDG" ] || UIDG="$(docker exec "$GW" sh -c 'printf %s "${HERMES_UID:-10000}"' 2>/dev/null || true)"
UIDG="${UIDG:-10000}"
docker exec "$GW" sh -c "
  find /opt/vault/wiki /opt/vault/raw /opt/vault/config -not -uid $UIDG -type f 2>/dev/null | head -200 > /tmp/.strays
  N=\$(wc -l < /tmp/.strays)
  if [ \"\$N\" = 0 ]; then echo 'ownership clean'; exit 0; fi
  echo \"\$N stray file(s):\"; head -10 /tmp/.strays
  if [ '$DRY' = '--dry-run' ]; then echo '(dry run — not fixing)'; exit 0; fi
  xargs -r chown $UIDG:$UIDG < /tmp/.strays
  echo \"chowned \$N file(s) to $UIDG:$UIDG\""
