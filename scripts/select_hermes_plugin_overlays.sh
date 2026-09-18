#!/usr/bin/env bash
# Resolve one complete plugin set for the exact Hermes pin; never mix provider generations.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "ERROR: expected engine directory and Hermes tag" >&2; exit 2; }
ENGINE_ROOT="$1"
HERMES_TAG="$2"

case "$HERMES_TAG" in
  v2026.7.7.2)
    PLUGIN_ROOT="$ENGINE_ROOT/plugins"
    REQUIRED="model-providers/custom model-providers/openrouter web/serper"
    ;;
  v2026.9.14)
    PLUGIN_ROOT="$ENGINE_ROOT/overlays/hermes-v2026.9.14/plugins"
    REQUIRED="model-providers/custom web/serper"
    ;;
  *) echo "ERROR: no verified plugin overlay set for Hermes tag $HERMES_TAG" >&2; exit 1 ;;
esac

for rel in $REQUIRED; do
  if [ ! -f "$PLUGIN_ROOT/$rel/__init__.py" ] || [ ! -f "$PLUGIN_ROOT/$rel/plugin.yaml" ]; then
    echo "ERROR: incomplete $HERMES_TAG plugin overlay set: $rel" >&2
    exit 1
  fi
done

printf '%s\n' "$PLUGIN_ROOT"
