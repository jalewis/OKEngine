#!/usr/bin/env bash
# check-public-parity — is the PUBLIC (GitHub) snapshot pair consistent and current?
#
# The GitHub repos are manually-published squashed snapshots (publish-snapshot.sh in
# each repo), not mirrors — nothing else verifies they happened or that they agree.
# Found live: the public catalog sat at v0.3.5 while the working repos were at v0.9.0,
# and the (since-fixed) pin check means an external deployer now hits a FAIL whose real
# remedy is "the publisher is stale", not "fix your pack". This makes that state
# visible the day it happens. Run after any release (and after each publish push).
#
# Checks:
#   1. local engine-manifest engine_release  vs  PUBLIC engine engine-manifest
#   2. PUBLIC catalog pack engine_versions   vs  PUBLIC engine engine_release
#      (the pair external deployers actually consume together)
#
# Exit: 0 = public pair consistent AND current with this checkout · 1 = stale/skewed
#       2 = cannot fetch (network / not yet published)
#
# Env overrides (also how the tests drive it with local fixture files):
#   PUBLIC_ENGINE_MANIFEST   url-or-path (default: raw.githubusercontent jalewis/okengine)
#   PUBLIC_CATALOG           url-or-path (default: raw.githubusercontent jalewis/okpacks-library)
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUB_MANIFEST="${PUBLIC_ENGINE_MANIFEST:-https://raw.githubusercontent.com/jalewis/okengine/main/engine-manifest.yaml}"
PUB_CATALOG="${PUBLIC_CATALOG:-https://raw.githubusercontent.com/jalewis/okpacks-library/main/catalog.json}"

_get() {  # url-or-path -> stdout
    case "$1" in
        http://*|https://*) curl -fsSL --max-time 30 "$1" ;;
        *) cat "$1" ;;
    esac
}

local_rel="$(awk -F': *' '/^engine_release:/{print $2; exit}' "$ENGINE_DIR/engine-manifest.yaml" | awk '{print $1}')"
pub_manifest="$(_get "$PUB_MANIFEST")" || { echo "ERROR: cannot fetch public engine manifest ($PUB_MANIFEST)"; exit 2; }
pub_rel="$(printf '%s\n' "$pub_manifest" | awk -F': *' '/^engine_release:/{print $2; exit}' | awk '{print $1}')"
pub_catalog="$(_get "$PUB_CATALOG")" || { echo "ERROR: cannot fetch public catalog ($PUB_CATALOG)"; exit 2; }
cat_vers="$(printf '%s' "$pub_catalog" | python3 -c "
import json, sys
vs = sorted({p.get('engine_version','?') for p in json.load(sys.stdin).get('packs', [])})
print(' '.join(vs))")"

echo "local engine_release   : ${local_rel:-?}"
echo "public engine_release  : ${pub_rel:-?}"
echo "public catalog versions: ${cat_vers:-?}"

rc=0
if [ -z "$pub_rel" ]; then
    echo "FAIL: public engine manifest carries no engine_release"; rc=1
elif [ "$pub_rel" != "$local_rel" ]; then
    echo "STALE: public engine snapshot is $pub_rel but this checkout is $local_rel — run the engine publish"; rc=1
fi
for v in $cat_vers; do
    if [ "$v" != "$pub_rel" ]; then
        echo "SKEW: public catalog pins $v but the public engine is ${pub_rel:-?} — publish the pair together"; rc=1
        break
    fi
done
[ "$rc" = 0 ] && echo "OK: public snapshot pair is consistent and current"
exit "$rc"
