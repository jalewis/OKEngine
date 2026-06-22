#!/usr/bin/env bash
# Stage-1 mechanical extraction: turn PDFs in the vault's raw/ tree into text.
#
# Each `foo.pdf` gets a `foo.pdf.txt` companion next to it (pdftotext). The
# raw-backfill selector (scripts/cron/select_raw_batch.py) then prefers the .txt
# companion over the binary PDF, so the ingest agent reads clean text instead of
# an unreadable binary. Run this on the host (or wherever the raw tree lives)
# before / alongside ingestion. Domain-agnostic — knows nothing but "PDF in,
# text out".
#
# Idempotent: skips a PDF whose `.pdf.txt` companion is newer than the source;
# re-extracts when the source PDF has been touched since. Empty output (a scanned
# PDF with no text layer) is flagged for OCR and the empty companion removed.
#
# Usage:
#   bash scripts/extract-pdfs.sh                  # default: $WIKI_PATH/raw (else /opt/vault/raw)
#   bash scripts/extract-pdfs.sh /path/to/raw     # explicit raw root
#   bash scripts/extract-pdfs.sh --dry-run [root] # show what would run
#
# Requires poppler-utils (`pdftotext`): apt-get install poppler-utils.

set -euo pipefail

DRY_RUN=0
RAW_ROOT="${1:-${WIKI_PATH:-/opt/vault}/raw}"
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN=1
    RAW_ROOT="${2:-${WIKI_PATH:-/opt/vault}/raw}"
fi

if [ ! -d "$RAW_ROOT" ]; then
    echo "ERROR: raw root not found: $RAW_ROOT" >&2
    exit 1
fi

if ! command -v pdftotext >/dev/null 2>&1; then
    echo "ERROR: pdftotext not on PATH. Install: apt-get install poppler-utils" >&2
    exit 2
fi

extracted=0
skipped=0
failed=0
total=0

# -print0 + IFS/-d safely handles paths with spaces, parentheses, unicode.
while IFS= read -r -d '' pdf; do
    total=$((total + 1))
    txt="${pdf}.txt"
    if [ -f "$txt" ] && [ "$txt" -nt "$pdf" ]; then
        skipped=$((skipped + 1))
        continue
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "DRY: $pdf -> $txt"
        extracted=$((extracted + 1))
        continue
    fi
    # -layout preserves columnar layout reasonably well for simple cases.
    # -nopgbrk strips form-feed chars that confuse downstream readers.
    # -enc UTF-8 normalizes encoding.
    if pdftotext -layout -nopgbrk -enc UTF-8 "$pdf" "$txt" 2>/dev/null; then
        # Empty output usually means a scanned PDF with no text layer.
        if [ ! -s "$txt" ]; then
            echo "WARN: empty output (likely scanned, needs OCR): $pdf" >&2
            rm -f "$txt"
            failed=$((failed + 1))
            continue
        fi
        extracted=$((extracted + 1))
        if [ $((extracted % 100)) -eq 0 ]; then
            printf "  ... %d extracted, %d skipped, %d failed\n" "$extracted" "$skipped" "$failed"
        fi
    else
        echo "WARN: pdftotext failed: $pdf" >&2
        rm -f "$txt"
        failed=$((failed + 1))
    fi
done < <(find "$RAW_ROOT" -type f -iname '*.pdf' -print0)

echo
printf "Total: %d PDFs scanned\n" "$total"
printf "  extracted: %d\n" "$extracted"
printf "  skipped (companion newer): %d\n" "$skipped"
printf "  failed (no text layer / errored): %d\n" "$failed"
