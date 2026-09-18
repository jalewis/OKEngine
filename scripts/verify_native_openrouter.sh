#!/usr/bin/env bash
# v0.21.3 OpenRouter is upstream-owned; reject a replacement or patch drift.
set -euo pipefail

[ "$#" -eq 1 ] || { echo "ERROR: expected Hermes checkout" >&2; exit 2; }
HERMES_ROOT="$1"
PROFILE="$HERMES_ROOT/plugins/model-providers/openrouter/__init__.py"
MANIFEST="$HERMES_ROOT/plugins/model-providers/openrouter/plugin.yaml"
EXPECTED_SHA256="6c31774782821e179fe9c2637538afa0d0bcd2827dc1a7dc9d68edfb9ba32174"
EXPECTED_MANIFEST_SHA256="750ce8f956d222108c2f4eb8dc8f994c066628d16605b123fa0c3e90ffa8a226"

if [ ! -f "$PROFILE" ] || [ ! -f "$MANIFEST" ]; then
  echo "ERROR: native v0.21.3 OpenRouter provider is missing" >&2
  exit 1
fi
if ! printf '%s  %s\n' "$EXPECTED_SHA256" "$PROFILE" | sha256sum -c - >/dev/null 2>&1; then
  echo "ERROR: native v0.21.3 OpenRouter provider drifted or was replaced" >&2
  exit 1
fi
if ! printf '%s  %s\n' "$EXPECTED_MANIFEST_SHA256" "$MANIFEST" | sha256sum -c - >/dev/null 2>&1; then
  echo "ERROR: native v0.21.3 OpenRouter manifest drifted or was replaced" >&2
  exit 1
fi
printf '%s\n' "native v0.21.3 OpenRouter provider verified"
