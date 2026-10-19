#!/usr/bin/env bash
# require_baked_validator CONTAINER UID [PATH...] — refuse to stage scripts onto a gateway whose
# BAKED engine predates them (okengine#997, #1017, #1016).
#
# Staged scripts import two things that are not staged with them but baked into the gateway
# image: the `okengine` wheel and /opt/hermes/tools (the schema validator). A stage-only run, or
# `deploy.sh --skip-build` over a stale image, would put new scripts beside an old engine and every
# cron importing a missing name would raise ImportError: an outage, not drift. deploy.sh recreates
# the gateway (step 4) before it stages (step 5), so a normal deploy passes this check.
#
# The probe is DERIVED from the PATHs about to be staged (scripts/lib/baked_imports.py): every
# `okengine.*` / `tools.*` import they make, exactly as written, plus the validator's
# `is_reserved_basename`. A hand-listed probe forgot `okengine.actor_types` in v0.15.11 (#1016); a
# derived one cannot forget a module a staged script imports. With no PATH it scans the engine's
# own staged sources (scripts/cron, the runtime compose helpers, extensions/).
#
# Probes with the gateway's own venv python from /opt/hermes, exactly how the crons import it, and
# names every import the baked engine cannot satisfy. Sourced by deploy-cron-scripts.sh; returns
# non-zero (and says why) when anything is absent.
require_baked_validator() {
    local container="$1" uid="$2"
    shift 2
    local here root imports probe missing rc
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    root="$(cd "$here/../.." && pwd)"
    if [ "$#" -eq 0 ]; then
        set -- "$root/scripts/cron" "$root/scripts/extension_compose.py" \
            "$root/scripts/extension_discovery.py" "$root/scripts/extension_manifest.py" \
            "$root/extensions"
    fi
    if ! imports="$("${PYTHON:-python3}" "$here/baked_imports.py" "$@")"; then
        echo "ERROR: could not derive the baked-engine probe from the scripts to stage." >&2
        echo "       Nothing was staged." >&2
        return 1
    fi
    probe="$( { echo "from tools.schema_validator import is_reserved_basename"
                printf '%s\n' "$imports"; } | sed '/^$/d' | sort -u)"
    missing="$(printf '%s\n' "$probe" | docker exec -i -u "$uid" "$container" sh -c \
        'cd /opt/hermes && /opt/hermes/.venv/bin/python -c "
import sys
missing = []
for statement in sys.stdin.read().splitlines():
    try:
        exec(statement, {})
    except Exception as exc:
        missing.append(statement + chr(32) + chr(40) + type(exc).__name__ + chr(41))
print(chr(10).join(missing))
sys.exit(1 if missing else 0)
"' 2>/dev/null)"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "ERROR: the gateway's baked engine (/opt/hermes) predates these scripts: it cannot" >&2
        echo "       satisfy these imports the staged scripts make:" >&2
        if [ -n "$missing" ]; then
            printf '         %s\n' "$missing" >&2
        else
            echo "         (the probe could not run in the gateway)" >&2
        fi
        echo "       Staging them would make every cron that imports one raise ImportError." >&2
        echo "       Rebuild the engine image and recreate the gateway first (deploy.sh does both," >&2
        echo "       without --skip-build), then rerun this stage. Nothing was staged." >&2
        return 1
    fi
}
