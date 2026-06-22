#!/usr/bin/env bash
# Install the host-crontab entry for scripts/extract-raw.sh (idempotent).
#
# Why host crontab and NOT cron-plus: the extractors must run as the host user
# that owns raw/ (the gateway container's uid cannot write companions there) and
# need host tools (pdftotext, and python-docx/pptx once #5 lands). See
# scripts/extract-raw.sh and docs/ingest-extraction.md.
#
# Schedule: every 15 min — incremental runs are cheap (companion mtime-skip) and
# keep companions ahead of the raw-backfill selector. Re-run to (re)install; the
# entry is added once and never duplicated.
#
# Env:
#   SCHEDULE       cron schedule (default "*/15 * * * *")
#   WIKI_PATH      passed through so extract-raw.sh resolves the same raw/ root
#   EXTRACT_PYTHON python for the .py extractors (passed through)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WRAPPER="$REPO/scripts/extract-raw.sh"
LOG="${EXTRACT_LOG:-$HOME/.okengine-extract-raw.log}"
SCHEDULE="${SCHEDULE:-*/15 * * * *}"

# Carry WIKI_PATH / EXTRACT_PYTHON into the cron environment (cron has a bare env)
# so the scheduled run resolves the same raw/ root and interpreter as this install.
env_prefix=""
[ -n "${WIKI_PATH:-}" ]      && env_prefix+="WIKI_PATH=$WIKI_PATH "
[ -n "${EXTRACT_PYTHON:-}" ] && env_prefix+="EXTRACT_PYTHON=$EXTRACT_PYTHON "

ENTRY="$SCHEDULE ${env_prefix}$WRAPPER >> $LOG 2>&1"

chmod +x "$WRAPPER" 2>/dev/null || true

cur="$(crontab -l 2>/dev/null || true)"
if printf '%s\n' "$cur" | grep -qF "scripts/extract-raw.sh"; then
    echo "already installed:"
    printf '%s\n' "$cur" | grep -F "scripts/extract-raw.sh"
    exit 0
fi

{ printf '%s\n' "$cur"; echo "$ENTRY"; } | crontab -
echo "installed:"
echo "  $ENTRY"
