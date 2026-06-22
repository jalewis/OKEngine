#!/usr/bin/env bash
# Regenerate the deployed cron artifact (config/cron-plus-jobs.json) from its
# two sources (two-repo split, slice 2):
#   engine half  -> config/engine-crons.json
#   domain half  -> $CRON_PACK_DIR/crons/{domain-crons,engine-template-prompts}.json
#
# Run after editing either source (an engine cron def, or a domain pack cron /
# prompt). The result is name-sorted and byte-stable. Then deploy with
# scripts/deploy-cron-plus-jobs.sh.
#
# Override the pack location with CRON_PACK_DIR (default: the pack location).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRON_PACK_DIR="${CRON_PACK_DIR:-/path/to/pack}" \
    python3 "$REPO_ROOT/scripts/cron_pack_split.py" regen
echo "Done. Deploy with: bash scripts/deploy-cron-plus-jobs.sh"
