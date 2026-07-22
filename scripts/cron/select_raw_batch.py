#!/usr/bin/env python3
"""Select the next batch of raw files to ingest, prioritizing 2025-2026 newest-first.

Output (stdout) is a markdown digest the agent reads to know which files to process.
Output is deterministic — same vault state → same selection → idempotent re-runs.
"""
from __future__ import annotations

import os
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Fold unicode quote variants to ASCII so dedupe matches across encodings.
# Without this, a source page with raw: "...owner\'s..." (escaped ASCII apostrophe
# from yaml.safe_load) won't match a filesystem path containing curly U+2019, and
# the raw file gets re-queued every run.
_QUOTE_FOLD = str.maketrans({
    "\u2018": "'",  # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK
    "\u201A": "'",  # SINGLE LOW-9 QUOTATION MARK
    "\u201C": chr(34),  # LEFT DOUBLE QUOTATION MARK
    "\u201D": chr(34),  # RIGHT DOUBLE QUOTATION MARK
    "\u201E": chr(34),  # DOUBLE LOW-9 QUOTATION MARK
})


def normalize_path(p: str) -> str:
    """Fold unicode quote variants + NFKC-normalize for dedupe comparison."""
    return unicodedata.normalize("NFKC", p).translate(_QUOTE_FOLD)

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
N = int(os.environ.get("BATCH_SIZE", "30"))
MIN_YEAR = int(os.environ.get("MIN_YEAR", "2025"))
LEAF_EXTS = {".md", ".pdf", ".txt", ".html", ".htm", ".json",
             ".pptx", ".docx", ".xlsx", ".rtf", ".doc"}
YEAR_RE = re.compile(r"(?:^|[^0-9])(20[12]\d)(?:[^0-9]|$)")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.S)
YEAR_INDEX_PATH = VAULT / "raw" / ".year_index.json"

# Priority-ingest directory names (relative to the raw/ tree, or vault-root for
# the curated tier). Configurable so a deployment can name its buckets to match
# its own collection layout. Defaults are neutral:
#   CURATED_DIR  — operator-curated, hand-saved content (highest signal)
#   BULK_DIR     — bulk auto-archived web pages (high volume, uneven signal)
CURATED_DIR = os.environ.get("CURATED_DIR", "clippings")
BULK_DIR = os.environ.get("BULK_DIR", "bulk")

# Bulk-import mtime clusters: epoch-second timestamps shared by many files from
# a single sync/restore operation. When derive_year falls back to mtime and the
# mtime matches a bulk-import second, the file is demoted to BULK_IMPORT_SENTINEL
# so it falls out of the priority window (MIN_YEAR). Without this, files
# mis-classified as fresh dominate the queue and displace real recent work. Any
# narrow window with hundreds of identical mtimes is a bulk operation, not real
# publication time. Populate via env (comma-separated epoch seconds) per
# deployment — leave empty if the corpus has no such clusters.
_bulk_mtimes_env = os.environ.get("BULK_IMPORT_MTIMES", "")
BULK_IMPORT_MTIMES = {
    int(s.strip()) for s in _bulk_mtimes_env.split(",") if s.strip().isdigit()
}
BULK_IMPORT_SENTINEL_YEAR = int(os.environ.get("BULK_IMPORT_SENTINEL_YEAR", "2000"))


def _load_year_index() -> dict[str, int]:
    """Per-file year overrides produced by `enrich_raw_years.py`. Keyed by
    vault-relative path. Returns {} if the index is missing or unreadable."""
    if not YEAR_INDEX_PATH.is_file():
        return {}
    try:
        import json
        data = json.loads(YEAR_INDEX_PATH.read_text())
        if not isinstance(data, dict):
            return {}
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, str))}
    except (OSError, ValueError, ImportError):
        return {}


_YEAR_INDEX = _load_year_index()

# Offer-count manifest (raw/.batch-offered.json). A raw file is "processed" only when a source page
# carries its path in `raw:`. But a DUPLICATE raw file (its story already has a source under another
# slug) never gets its own source, so it's never processed and — being newest — stays in the top-N,
# re-offered every run: the lane loops on the same batch forever instead of draining. This tracks how
# many times each still-unprocessed file has been offered; once a file has been offered STUCK_AFTER
# times without being processed it is a duplicate/low-signal the agent won't ingest, so we stop
# offering it and the selector advances. A file that later gets a source drops out (pruned below).
OFFER_MANIFEST = VAULT / "raw" / ".batch-offered.json"
STUCK_AFTER = int(os.environ.get("RAW_STUCK_AFTER", "4"))
ACCEPT_MIN_CHARS = int(os.environ.get("RAW_ACCEPT_MIN_CHARS", "80"))
MAX_CONTEXT_BYTES = int(os.environ.get("RAW_MAX_CONTEXT_BYTES", "200000"))
SELECTION_MANIFEST = Path(os.environ.get(
    "OKENGINE_SELECTION_MANIFEST", str(VAULT / "raw" / ".selection.json")))
ACCEPT_REQUIRED_FIELDS = tuple(x.strip() for x in os.environ.get(
    "RAW_ACCEPT_REQUIRED_FIELDS", "type,raw,publisher,published").split(",") if x.strip())


def _load_offered() -> dict[str, int]:
    try:
        import json
        d = json.loads(OFFER_MANIFEST.read_text())
        return {str(k): int(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, ValueError, ImportError):
        return {}


def _save_offered(d: dict[str, int]) -> None:
    try:
        import json
        OFFER_MANIFEST.write_text(json.dumps(d))
    except OSError:
        pass


def extract_processed_paths(text: str) -> set[str]:
    """Pull `raw:` field(s) out of a source page's YAML frontmatter.
    Handles single-string, single-quoted, double-quoted, and list forms.
    Returns empty set on parse failure or missing field."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return set()
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return set()
    if not isinstance(data, dict):
        return set()
    body = text[m.end():]
    meaningful = len("".join(body.split()))
    if data.get("type") != "source" or meaningful < ACCEPT_MIN_CHARS:
        return set()
    if any(data.get(key) in (None, "", [], {}) for key in ACCEPT_REQUIRED_FIELDS):
        return set()
    raw = data.get("raw")
    if isinstance(raw, str):
        return {normalize_path(raw.strip())}
    if isinstance(raw, list):
        return {normalize_path(item.strip()) for item in raw if isinstance(item, str)}
    return set()


def derive_year(rel_path: str, mtime: float) -> int:
    # Priority 1: per-file override from enrich_raw_years.py (extracted from
    # HTML metadata — authoritative when present, fixes bulk-import mtime drift).
    indexed = _YEAR_INDEX.get(rel_path)
    if indexed is not None:
        return indexed
    # Priority 2: explicit year token in the path (e.g. raw/2026-05/...).
    years = [int(y) for y in YEAR_RE.findall(rel_path) if 2018 <= int(y) <= 2026]
    if years:
        return max(years)
    # Priority 3: filesystem mtime — least reliable for bulk archives. Demote
    # known bulk-import clusters to BULK_IMPORT_SENTINEL_YEAR so they fall out
    # of the priority window rather than fraudulently dominating it.
    if int(mtime) in BULK_IMPORT_MTIMES:
        return BULK_IMPORT_SENTINEL_YEAR
    return datetime.fromtimestamp(mtime).year


def path_tier(rel_path: str) -> int:
    """Priority tier for ingest ordering. Lower = higher priority.

    Within each tier, sort still applies (year DESC, mtime DESC). Tiering only
    matters when comparing files from different buckets — within the curated
    bucket the newest is still picked first.

    Tier 0: Operator-curated, hand-saved content (CURATED_DIR). Highest
            signal — already filtered for relevance.
    Tier 2: Bulk auto-archived pages (BULK_DIR). Volume is large, signal is
            uneven. Drains last so curated material isn't blocked.
    Tier 1: Everything else — top-level files, themed collections.
            Hand-organized but not individually curated.
    """
    # The curated marker matches both `raw/<CURATED_DIR>/...` and the vault-root
    # `<CURATED_DIR>/...` tree (some clipping tools write to the vault root by
    # default). The startswith check covers the vault-root case where the
    # `/<CURATED_DIR>/` substring check misses (no leading `/`).
    if (f"/{CURATED_DIR}/" in rel_path or
        rel_path.startswith(f"{CURATED_DIR}/") or
        rel_path.startswith(f"raw/{CURATED_DIR}/")):
        return 0
    if f"/{BULK_DIR}/" in rel_path:
        return 2
    return 1


# Ingest-provenance frontmatter the compile agent must CARRY from the raw page onto the compiled
# source page (okengine#194 — it rewrote frontmatter to the schema and silently dropped these, so
# a vault could not be filtered/attributed by ingest source). Kept in ONE place; the base schema
# lists the same keys under common_optional (cross-checked by tests/cron/test_select_raw_batch.py).
PROVENANCE_KEYS = ("source_feed", "source_channel", "matched_query", "watch_lane", "quality_score")


def main() -> int:
    if not VAULT.exists():
        print(f"ERROR: vault not found at {VAULT}", file=sys.stderr)
        print(f"# Batch selection failed: vault not found at `{VAULT}`")
        return 1

    sources_dir = VAULT / "wiki" / "sources"
    raw_dir = VAULT / "raw"
    if not raw_dir.exists():
        print(f"# Batch selection failed: `{raw_dir}` does not exist")
        return 1

    processed: set[str] = set()
    if sources_dir.exists():
        for src in sources_dir.rglob("*.md"):
            try:
                processed |= extract_processed_paths(src.read_text(errors="replace"))
            except OSError:
                continue  # page moved/deleted by a concurrent lane mid-scan

    all_raw: list[tuple[str, str, int, float]] = []
    bogus_paths: list[str] = []
    # Walk raw/ (the bulk ingest tree) AND the vault-root curated tree (where
    # some clipping tools write by default — operator-curated tier-0 content
    # that may sit outside raw/). itertools.chain lets one loop handle both.
    import itertools as _itertools
    curated_root = VAULT / CURATED_DIR
    scan_iter = _itertools.chain(
        raw_dir.rglob("*"),
        curated_root.rglob("*") if curated_root.is_dir() else [],
    )
    for p in scan_iter:
        if not p.is_file() or p.suffix.lower() not in LEAF_EXTS:
            continue
        if any(part.startswith(".") for part in p.parts):
            continue
        # Detect path components containing literal backslashes. Root cause:
        # shell tool calls double-quoting paths with `\ ` escapes — inside
        # "..." in bash, `\ ` is preserved verbatim instead of collapsing to a
        # space, so a `mkdir -p "raw/Some\ Dir/..."` creates a directory
        # literally named `Some\ Dir` shadowing the real `Some Dir`.
        if any("\\" in part for part in p.parts):
            bogus_paths.append(str(p.relative_to(VAULT)))
            continue
        # Binary docs (PDF/DOCX/PPTX/XLSX/RTF/DOC, unreadable) and noisy raw HTML
        # are pre-extracted to `.<ext>.txt` companions by scripts/extract-pdfs.sh,
        # scripts/extract-html.py, and scripts/extract-docs.py on the host. When a
        # companion exists, skip the raw file — the agent ingests the `.txt` (a
        # normal text leaf) instead. Files without a companion stay queued; the
        # next extract run creates it.
        if p.suffix.lower() in (".pdf", ".html", ".htm", ".docx", ".pptx",
                                ".xlsx", ".rtf", ".doc") and \
                (p.parent / (p.name + ".txt")).is_file():
            continue
        # Orphan link.md stubs (no sibling content.txt) are tiny placeholders
        # — `[Link to the article](URL)` with no extractable content. Processing
        # them produces low-quality placeholder source pages; skip until a content
        # fetcher backfills them.
        if p.name.lower() == "link.md" and not (p.parent / "content.txt").is_file():
            continue
        rel = str(p.relative_to(VAULT))
        norm = normalize_path(rel)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue  # file removed/moved by a concurrent lane mid-scan
        all_raw.append((rel, norm, derive_year(rel, mtime), mtime))

    unprocessed_all = [(rel, year, mtime) for (rel, norm, year, mtime) in all_raw if norm not in processed]
    # Year-deferral keeps the older bulk archive out of the priority queue.
    # But tier-0 content (the curated tree) is operator-curated — saved with
    # deliberate intent, regardless of when the underlying event happened. A
    # years-old write-up clipped today is HIGH signal even though its filename
    # year token is old. Exempt tier 0 from the year gate so curated content
    # always drains.
    unprocessed = [
        (rel, year, mtime) for (rel, year, mtime) in unprocessed_all
        if year >= MIN_YEAR or path_tier(rel) == 0
    ]
    deferred_older = len(unprocessed_all) - len(unprocessed)

    # Offer counts are visibility, never completion. Invalid/empty compiled pages and rejected writes
    # remain retryable regardless of how many times they were offered.
    offered = {k: v for k, v in _load_offered().items() if k not in processed}
    repeatedly_rejected = [t for t in unprocessed
                           if offered.get(normalize_path(t[0]), 0) >= STUCK_AFTER]
    unprocessed_live = unprocessed

    def _emit_bogus_warning() -> None:
        if not bogus_paths:
            return
        warn = f"{len(bogus_paths)} raw file(s) have literal backslashes in their paths (shell-quoting artifact). Skipped from selection."
        print(f"WARNING: {warn}", file=sys.stderr)
        print(f"## WARNING — Path Hygiene\n\n{warn} These typically duplicate content that already exists at the correct (literal-space) path. Investigate and clean up:\n")
        for bp in bogus_paths[:10]:
            print(f"- `{bp}`")
        if len(bogus_paths) > 10:
            print(f"- ... ({len(bogus_paths) - 10} more not shown)")
        print()

    if not unprocessed_live:
        print(f"# Raw-backfill batch — {datetime.now(timezone.utc).isoformat()}\n")
        _emit_bogus_warning()
        msg = f"# Backfill complete\n\n0 ingestable files remaining at MIN_YEAR={MIN_YEAR} ({len(all_raw)} total in raw/, all priority files indexed in wiki/sources/)."
        if deferred_older:
            msg += f"\n\n{deferred_older} pre-{MIN_YEAR} files are deferred — lower MIN_YEAR env var to ingest them."
        msg += "\n\nAction: run `hermes cron pause raw-backfill` and append a final log entry to $WIKI_PATH/wiki/log.md."
        print(msg)
        print('{"wakeAgent": false}')
        return 0

    chosen = sorted(unprocessed_live, key=lambda t: (path_tier(t[0]), -t[1], -t[2]))[:N]
    # Record this offering. A file the agent keeps skipping accrues offers until it crosses
    # STUCK_AFTER and drops out of the live pool above; one that gets a source is pruned next run.
    for rel, _, _ in chosen:
        _np = normalize_path(rel)
        offered[_np] = offered.get(_np, 0) + 1
    _save_offered(offered)

    by_year = {}
    for _, y, _ in unprocessed_all:
        by_year[y] = by_year.get(y, 0) + 1

    print(f"# Raw-backfill batch — {datetime.now(timezone.utc).isoformat()}\n")
    _emit_bogus_warning()
    print(f"**Vault:** `{VAULT}`")
    print(f"**Total raw files:** {len(all_raw)}")
    print(f"**Already processed:** {len(processed)}")
    print(f"**Unprocessed (in scope, year>={MIN_YEAR}):** {len(unprocessed)}")
    if repeatedly_rejected:
        print(f"**Retryable (offered >={STUCK_AFTER}x without an accepted source):** "
              f"{len(repeatedly_rejected)} — still selected; inspect receipt rejection codes")
    if deferred_older:
        print(f"**Unprocessed (deferred, year<{MIN_YEAR}):** {deferred_older}")
    remaining = len(unprocessed_live) - len(chosen)
    print(f"**This batch:** {len(chosen)} of {len(unprocessed_live)} ingestable "
          f"(bounded by `BATCH_SIZE={N}`, newest-first within scope)")
    if remaining > 0:
        print(f"**Remaining after this batch:** {remaining} — they drain on the next runs "
              f"automatically; no need to wait or stop the job. Raise `BATCH_SIZE` to do more per run.")
    print()

    print("**Unprocessed by year:**")
    for y in sorted(by_year.keys(), reverse=True):
        marker = "  ← in scope" if y >= MIN_YEAR else "  (deferred)"
        print(f"- {y}: {by_year[y]}{marker}")
    print()

    by_tier = {0: 0, 1: 0, 2: 0}
    for rel, _, _ in unprocessed:
        by_tier[path_tier(rel)] += 1
    tier_labels = {0: f"curated ({CURATED_DIR})", 1: "hand-organized", 2: f"bulk archive ({BULK_DIR})"}
    print("**Unprocessed by priority tier (drains in order):**")
    for tier in (0, 1, 2):
        print(f"- tier {tier} ({tier_labels[tier]}): {by_tier[tier]}")
    print()

    print("## Files to ingest this batch (in order)\n")
    print("Process each in the order listed below. Each source page MUST set `raw:` to the relative path shown — that's the dedupe key.\n")
    print("CARRY the raw page's ingest-provenance frontmatter onto the source page VERBATIM when "
          f"present — {', '.join(f'`{k}`' for k in PROVENANCE_KEYS)}, plus any other *_score/*_id "
          "provenance keys the raw page carries. Copy what exists; never invent values. These keys "
          "are schema-legal on every type (base common_optional) — dropping them loses the vault's "
          "ability to attribute/filter by ingest source (okengine#194).\n")
    print("Keep source roles separate: `publisher` is the organization/site that published the "
          "article; `source_feed` is the repository or feed that supplied it; `source_channel` is "
          "the transport (for example `api` or `feed`); and `matched_query` is discovery context. "
          "Never put a retrieval repository/feed, a search engine, or text such as `via <feed>` "
          "in `publisher`. Preserve `published` from the raw record and omit an "
          "unknown field — never write placeholder strings such as `undefined`. If `publisher` is "
          "absent, identify it from the article URL or leave it unset; do not substitute "
          "`source_feed`, `source_channel`, or `matched_query`.\n")
    print("If a raw file DUPLICATES a story that already has a source page (different slug), do NOT "
          "create a second page — instead APPEND this raw path to that existing source's `raw:` list "
          "(via `mcp_okengine_write_update_entity`). That records it as processed so it stops being "
          "re-queued; leaving it unmarked is what made the lane loop on duplicates.\n")
    selected_keys = [rel for rel, _, _ in chosen]
    lane_id = os.environ.get("OKENGINE_LANE_ID", "")
    contract_digest = os.environ.get("OKENGINE_CONTRACT_DIGEST", "")
    manifest = {"api": 1, "selected": selected_keys,
                "input_digest": "sha256:" + hashlib.sha256(
                    json.dumps(selected_keys, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
                "lane_id": lane_id, "contract_digest": contract_digest}
    try:
        SELECTION_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        temp = SELECTION_MANIFEST.with_suffix(SELECTION_MANIFEST.suffix + ".tmp")
        temp.write_text(json.dumps(manifest, indent=2) + "\n")
        temp.replace(SELECTION_MANIFEST)
    except OSError as exc:
        print(f"ERROR: cannot write selection manifest {SELECTION_MANIFEST}: {exc}", file=sys.stderr)
        return 1
    print("## Verified receipt identity\n")
    print(f"- `lane_id`: `{lane_id}`")
    print(f"- `contract_digest`: `{contract_digest}`")
    print(f"- `input_digest`: `{manifest['input_digest']}`")
    print("Use these exact runner-owned values in the final `okengine-receipt` JSON block.\n")
    for i, (rel, year, mtime) in enumerate(chosen, 1):
        mtime_iso = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        size = (VAULT / rel).stat().st_size
        limit = (f", extraction=partial(first {MAX_CONTEXT_BYTES} bytes of {size}; receipt must "
                 "declare deferred remainder)" if size > MAX_CONTEXT_BYTES else ", extraction=complete")
        print(f"{i}. `{rel}` — derived_year={year}, mtime={mtime_iso}, bytes={size}{limit}")
    print()

    print("## After this batch\n")
    print("This lane creates **source** pages only. Entities, concepts and predictions are "
          "populated by their **own** lanes (entity-backfill / concept-backfill / "
          "prediction-*), which run after sources exist — a sources-only vault right after the "
          "first ingest is **expected**, not a stall.\n")
    print(f"The job is bounded ({len(chosen)} this run) and self-draining: it processes the "
          "backlog over successive runs and stops waking the agent once 0 remain — you don't "
          "need to babysit or kill it. To force one bounded pass now: "
          "`bash <engine>/scripts/cron-plus.sh run <raw-backfill job id>` "
          "(find the id with `cron-plus.sh list`).\n")

    return 0


if __name__ == "__main__":
    # DeepSeek off-peak deferral (CRON_DEFER_UTC_HOURS): during the configured peak UTC window
    # emit nothing — cron-plus wakes the agent only on non-empty stdout (scheduler.py), so this
    # bulk drain silently defers to the next off-peak fire (no model call at 2x price).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from offpeak import offpeak_defer
    if offpeak_defer():
        sys.exit(0)
    sys.exit(main())
