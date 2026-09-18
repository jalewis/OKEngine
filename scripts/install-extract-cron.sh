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

# This installs a HOST crontab entry. extract-raw.sh resolves its raw root as
# ${WIKI_PATH:-/opt/vault}/raw — and /opt/vault is a CONTAINER path that doesn't exist on the host,
# so a WIKI_PATH-less install writes a cron that logs "raw root not found" and no-ops every 15 min,
# silently. Refuse rather than install a dead schedule (invariant-audit B7.2).
if [ -z "${WIKI_PATH:-}" ]; then
    echo "ERROR: WIKI_PATH is not set. This installs a host cron; without it the scheduled" >&2
    echo "       extract-raw.sh falls back to the container-only /opt/vault/raw and silently" >&2
    echo "       no-ops every run. Set WIKI_PATH to the vault root on THIS host and re-run." >&2
    exit 1
fi
RAW_ROOT="$WIKI_PATH/raw"
if ! mkdir -p "$RAW_ROOT"; then
    echo "ERROR: cannot create host extraction root: $RAW_ROOT" >&2
    echo "       Run this installer as the user that owns the vault/raw tree." >&2
    exit 1
fi

# Do not install a schedule that can read inputs but cannot publish companions.
# Probe the root and every existing source-bearing directory with a real create +
# unlink, rather than mode-bit inference (`test -w` is unreliable across ACLs and
# mapped/fixed UIDs). This also catches a mixed-owner subtree under a writable root.
probe_write_dir() {
    local dir="$1" probe
    if ! probe="$(mktemp "$dir/.okengine-extract-write-check.XXXXXX" 2>/dev/null)"; then
        echo "ERROR: host extraction user $(id -u):$(id -g) cannot create companions in: $dir" >&2
        echo "       Reconcile vault ownership or run the host cron as the owning user; cron not installed." >&2
        return 1
    fi
    if ! rm -f -- "$probe" || [ -e "$probe" ]; then
        echo "ERROR: host extraction write probe could not be removed: $probe" >&2
        return 1
    fi
}

probe_write_dir "$RAW_ROOT"
while IFS= read -r -d '' source_dir; do
    [ "$source_dir" = "$RAW_ROOT" ] || probe_write_dir "$source_dir"
done < <(find "$RAW_ROOT" -type f \( -iname '*.pdf' -o -iname '*.html' -o -iname '*.htm' \
    -o -iname '*.docx' -o -iname '*.pptx' -o -iname '*.xlsx' -o -iname '*.rtf' \
    -o -iname '*.doc' \) -printf '%h\0' | sort -zu)

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
