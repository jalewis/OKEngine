#!/usr/bin/env python3
"""Audit how `select_raw_batch.py` derives the year for each raw file.

Surfaces:
  - Counts by derivation reason (path-hint vs mtime-fallback)
  - High-risk files: mtime-fallback with implausible year (<2018 or >2026)
  - Disagreement: path year and mtime year differ by >2 years
  - Per-top-level-subdir breakdown

Writes `wiki/dashboards/year-derivation-audit.md`. No cron; run on demand to
check whether raising/lowering `MIN_YEAR` would catch mis-classified files.

Reuses logic from `select_raw_batch` so the audit's verdict matches what the
ingest cron actually uses.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Sibling-script import — both live in /opt/data/scripts/ after deploy.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tz_lib  # noqa: E402
from select_raw_batch import (  # noqa: E402
    BULK_IMPORT_MTIMES,
    BULK_IMPORT_SENTINEL_YEAR,
    LEAF_EXTS,
    YEAR_RE,
    _YEAR_INDEX,
    derive_year,
    extract_processed_paths,
    normalize_path,
    path_tier,
)

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
RAW = VAULT / "raw"
SOURCES = VAULT / "wiki" / "sources"
OUT = VAULT / "wiki" / "dashboards" / "year-derivation-audit.md"

NOW = datetime.now(timezone.utc)
THIS_YEAR = NOW.year
PLAUSIBLE_MIN = 2018
PLAUSIBLE_MAX = THIS_YEAR + 1  # allow a tiny slack for future-dated paths


def fmt_int(n: int) -> str:
    return f"{n:,}"


def scan() -> dict:
    """Walk raw/, replicating select_raw_batch's file filter, and classify each."""
    processed: set[str] = set()
    if SOURCES.is_dir():
        for src in SOURCES.rglob("*.md"):   # rglob: sources may be sharded (sources/<year>/<month>/)
            try:
                processed |= extract_processed_paths(src.read_text(errors="replace"))
            except OSError:
                continue

    files: list[dict] = []
    bogus = 0
    skipped_pdf = 0
    skipped_orphan_link = 0
    for p in RAW.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in LEAF_EXTS:
            continue
        if any(part.startswith(".") for part in p.parts):
            continue
        if any("\\" in part for part in p.parts):
            bogus += 1
            continue
        if p.suffix.lower() == ".pdf" and (p.parent / (p.name + ".txt")).is_file():
            skipped_pdf += 1
            continue
        if p.name.lower() == "link.md" and not (p.parent / "content.txt").is_file():
            skipped_orphan_link += 1
            continue
        rel = str(p.relative_to(VAULT))
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        # Replicate derive_year's priority chain and capture WHICH SOURCE fired.
        years_in_path = [int(y) for y in YEAR_RE.findall(rel) if 2018 <= int(y) <= 2026]
        mtime_year = datetime.fromtimestamp(mtime).year
        indexed = _YEAR_INDEX.get(rel)
        bulk_demoted = False
        if indexed is not None:
            derived = indexed
            reason = "index"
        elif years_in_path:
            derived = max(years_in_path)
            reason = "path"
        elif int(mtime) in BULK_IMPORT_MTIMES:
            derived = BULK_IMPORT_SENTINEL_YEAR
            reason = "mtime-bulk-demoted"
            bulk_demoted = True
        else:
            derived = mtime_year
            reason = "mtime"
        files.append({
            "rel": rel,
            "mtime": mtime,
            "mtime_year": mtime_year,
            "path_years": years_in_path,
            "derived": derived,
            "reason": reason,
            "bulk_demoted": bulk_demoted,
            "processed": normalize_path(rel) in processed,
            "top": rel.split("/", 2)[1] if rel.startswith("raw/") and "/" in rel[4:] else "(root)",
            "tier": path_tier(rel),
        })
    return {
        "files": files,
        "bogus_paths": bogus,
        "skipped_pdf_with_companion": skipped_pdf,
        "skipped_orphan_link_md": skipped_orphan_link,
    }


def render(data: dict) -> str:
    files = data["files"]
    total = len(files)
    unprocessed = [f for f in files if not f["processed"]]

    # Reason breakdown (unprocessed only — already-processed is moot)
    reason_unproc = Counter(f["reason"] for f in unprocessed)

    # Year × reason matrix
    year_reason: dict[int, Counter[str]] = defaultdict(Counter)
    for f in unprocessed:
        year_reason[f["derived"]][f["reason"]] += 1

    # High-risk: mtime-fallback files with implausible derived year
    high_risk = [
        f for f in unprocessed
        if f["reason"] == "mtime" and (f["derived"] < PLAUSIBLE_MIN or f["derived"] > PLAUSIBLE_MAX)
    ]
    high_risk.sort(key=lambda f: (f["derived"], f["rel"]))

    # Disagreement: path year present AND mtime year differs by >2
    disagree = [
        f for f in unprocessed
        if f["reason"] == "path" and abs(f["derived"] - f["mtime_year"]) > 2
    ]
    disagree.sort(key=lambda f: (abs(f["derived"] - f["mtime_year"]), f["rel"]), reverse=True)

    # Per-subdir breakdown (unprocessed)
    sub_total: Counter[str] = Counter()
    sub_index: Counter[str] = Counter()
    sub_path: Counter[str] = Counter()
    sub_mtime: Counter[str] = Counter()
    sub_year: dict[str, Counter[int]] = defaultdict(Counter)
    for f in unprocessed:
        t = f["top"]
        sub_total[t] += 1
        if f["reason"] == "index":
            sub_index[t] += 1
        elif f["reason"] == "path":
            sub_path[t] += 1
        else:
            sub_mtime[t] += 1
        sub_year[t][f["derived"]] += 1

    lines: list[str] = []
    lines.append("---")
    lines.append("type: dashboard")
    lines.append("title: Year-derivation audit")
    lines.append(f"generated: {NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("---")
    lines.append("")
    lines.append("# Year-derivation audit")
    lines.append("")
    lines.append(f"> Generated: {tz_lib.deployment_now().strftime('%Y-%m-%d %H:%M %Z')}  ")  # okengine#301
    lines.append("> One-shot diagnostic of how `select_raw_batch.derive_year()` classifies each raw file.")
    lines.append("> Re-run via `bash scripts/cron-plus.sh` (no cron) — purely informational.")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total raw files scanned: **{fmt_int(total)}**")
    lines.append(f"- Already processed (raw: dedupe match): **{fmt_int(total - len(unprocessed))}**")
    lines.append(f"- Unprocessed: **{fmt_int(len(unprocessed))}**")
    if data["bogus_paths"]:
        lines.append(f"- Skipped (literal `\\` in path — malformed-path guard): {fmt_int(data['bogus_paths'])}")
    if data["skipped_pdf_with_companion"]:
        lines.append(f"- Skipped (PDF with `.pdf.txt` companion): {fmt_int(data['skipped_pdf_with_companion'])}")
    if data.get("skipped_orphan_link_md"):
        lines.append(f"- Skipped (orphan `link.md` — no sibling `content.txt`): {fmt_int(data['skipped_orphan_link_md'])}")
    lines.append("")
    lines.append("**Unprocessed derivation reason:**")
    lines.append("")
    lines.append("| Reason | Count | Confidence |")
    lines.append("|---|---|---|")
    lines.append(f"| index (`raw/.year_index.json` from `enrich_raw_years.py`) | {fmt_int(reason_unproc.get('index', 0))} | high — extracted from HTML metadata |")
    lines.append(f"| path token (`YYYY` in path) | {fmt_int(reason_unproc.get('path', 0))} | high |")
    lines.append(f"| mtime fallback | {fmt_int(reason_unproc.get('mtime', 0))} | medium-low |")
    lines.append(f"| mtime bulk-demoted (cluster → {BULK_IMPORT_SENTINEL_YEAR}) | {fmt_int(reason_unproc.get('mtime-bulk-demoted', 0))} | n/a — synthetic sentinel |")
    lines.append("")

    # Year × reason
    lines.append("## Unprocessed by derived year")
    lines.append("")
    lines.append("| Year | Index | Path | Mtime | Total |")
    lines.append("|---|---|---|---|---|")
    for year in sorted(year_reason.keys(), reverse=True):
        row = year_reason[year]
        idx_n = row.get("index", 0)
        path_n = row.get("path", 0)
        mtime_n = row.get("mtime", 0)
        marker = "  ⚠" if year < PLAUSIBLE_MIN or year > PLAUSIBLE_MAX else ""
        lines.append(f"| {year}{marker} | {fmt_int(idx_n)} | {fmt_int(path_n)} | {fmt_int(mtime_n)} | {fmt_int(idx_n + path_n + mtime_n)} |")
    lines.append("")

    # High-risk
    lines.append(f"## High-risk: mtime-fallback with implausible year ({fmt_int(len(high_risk))})")
    lines.append("")
    lines.append(
        f"Files with no `YYYY` token in their path AND derived year outside "
        f"[{PLAUSIBLE_MIN}, {PLAUSIBLE_MAX}]. Likely mis-classified — mtime may "
        "reflect a sync/import operation, not content date. Sample up to 30:"
    )
    lines.append("")
    if not high_risk:
        lines.append("_None — no implausible mtime-derived years in the unprocessed set._")
    else:
        lines.append("| Year | Path |")
        lines.append("|---|---|")
        for f in high_risk[:30]:
            lines.append(f"| {f['derived']} | `{f['rel']}` |")
        if len(high_risk) > 30:
            lines.append(f"| … | _and {fmt_int(len(high_risk) - 30)} more_ |")
    lines.append("")

    # Disagreement
    lines.append(f"## Disagreement: path year vs mtime year (>2 years apart) ({fmt_int(len(disagree))})")
    lines.append("")
    lines.append(
        "Files where a `YYYY` token in the path resolved to one year but mtime "
        "says another. Path year wins per `derive_year`; this section shows the "
        "ones worth eyeballing to confirm the path year is the *content* date "
        "and not e.g. an identifier year embedded in a slug. Sample up to 30:"
    )
    lines.append("")
    if not disagree:
        lines.append("_None — every path-derived year is within 2 of mtime year._")
    else:
        lines.append("| Path year | Mtime year | Δ | Path |")
        lines.append("|---|---|---|---|")
        for f in disagree[:30]:
            delta = f["derived"] - f["mtime_year"]
            lines.append(f"| {f['derived']} | {f['mtime_year']} | {delta:+d} | `{f['rel']}` |")
        if len(disagree) > 30:
            lines.append(f"| … | … | … | _and {fmt_int(len(disagree) - 30)} more_ |")
    lines.append("")

    # Per-subdir
    lines.append("## Unprocessed by top-level subdir")
    lines.append("")
    lines.append("Surfaces curation decisions: \"backfill all of X\" vs \"skip all of Y\".")
    lines.append("")
    lines.append("| Subdir | Unprocessed | Index | Path | Mtime | Most common year |")
    lines.append("|---|---|---|---|---|---|")
    for sub, n in sub_total.most_common():
        most_common_year = sub_year[sub].most_common(1)[0][0] if sub_year[sub] else "?"
        lines.append(
            f"| `{sub}` | {fmt_int(n)} | {fmt_int(sub_index[sub])} | {fmt_int(sub_path[sub])} | "
            f"{fmt_int(sub_mtime[sub])} | {most_common_year} |"
        )
    lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    if not RAW.is_dir():
        print(f"ERROR: raw/ not found at {RAW}", file=sys.stderr)
        return 1

    data = scan()
    body = render(data)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        bak = OUT.parent / (OUT.name + ".bak")   # single overwritten sidecar, not indexed (#165 sweep)
        shutil.copy2(OUT, bak)
    OUT.write_text(body)
    print(f"wrote {OUT} ({len(body)} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
