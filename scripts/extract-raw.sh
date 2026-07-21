#!/usr/bin/env bash
# Stage-1 mechanical extraction — run every shipped extractor over the raw/ tree.
#
# Each binary/markup source in raw/ gets a plain-text `.txt` companion next to it;
# select_raw_batch.py then prefers the companion over the raw file. This wrapper
# runs the extractors the engine ships, each idempotent (skips companions newer
# than their source) and corpus-safe (a per-file failure is logged, never aborts):
#
#   scripts/extract-pdfs.sh   .pdf            -> .pdf.txt    (pdftotext / poppler-utils)
#   scripts/extract-html.py   .html .htm      -> .html.txt   (trafilatura/readability/stdlib)
#   scripts/extract-docs.py   .docx .pptx .xlsx .rtf .doc -> .<ext>.txt
#                             (python-docx/pptx, openpyxl, striprtf, antiword;
#                              optional per-format — skips a format if its dep is absent)
#
# This MUST run on the HOST, not the gateway container — see
# docs/ingest-extraction.md for the ownership/tooling rationale. Schedule it on
# the host crontab with scripts/install-extract-cron.sh (definition in the repo,
# never hand-typed).
#
# Usage (manual):  bash scripts/extract-raw.sh [raw-root]
#   raw-root defaults to $WIKI_PATH/raw (else /opt/vault/raw).
#   EXTRACT_PYTHON overrides the python used for the .py extractors (default python3).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW="${1:-${WIKI_PATH:-/opt/vault}/raw}"
PY="${EXTRACT_PYTHON:-python3}"
ts() { date -u +%FT%TZ; }

# Single-flight PER RAW ROOT: the first full pass over a large corpus can be long; never overlap
# against the SAME corpus. A single global /tmp lock made unrelated vaults and concurrent pytest
# processes block one another (okengine#204). cksum is POSIX and keeps the filename short.
LOCK_KEY="$(printf '%s' "$RAW" | cksum | awk '{print $1}')"
LOCK_FILE="${EXTRACT_LOCK_FILE:-${TMPDIR:-/tmp}/okengine-extract-raw-${LOCK_KEY}.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(ts) skip: previous extract-raw still running for raw=$RAW"
    exit 0
fi

if [ ! -d "$RAW" ]; then
    echo "$(ts) ERROR: raw root not found: $RAW" >&2
    exit 1
fi

echo "$(ts) extract-raw start (raw=$RAW)"

# Run each extractor independently so a failure (or a missing host tool) in one
# still lets the others proceed. extract-pdfs.sh exits non-zero when pdftotext is
# absent; that is a warning here, not a hard stop.
bash "$REPO/scripts/extract-pdfs.sh" "$RAW"   || echo "$(ts) WARN: extract-pdfs.sh exited non-zero"
"$PY" "$REPO/scripts/extract-html.py" "$RAW"  || echo "$(ts) WARN: extract-html.py exited non-zero"

# Office/legacy docs (docx/pptx/xlsx/rtf/doc): optional per-format deps — the script
# no-ops gracefully if none are present. Guarded by -f so a trimmed deployment runs.
if [ -f "$REPO/scripts/extract-docs.py" ]; then
    "$PY" "$REPO/scripts/extract-docs.py" "$RAW" || echo "$(ts) WARN: extract-docs.py exited non-zero"
fi

echo "$(ts) extract-raw done"
