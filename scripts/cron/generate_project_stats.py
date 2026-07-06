#!/usr/bin/env python3
"""Generate wiki/dashboards/project-stats.md — single-page snapshot of vault + ops health.

Aggregates (all domain-agnostic — namespaces are read from schema.yaml):
  - Corpus: raw/ doc count + bytes, vault wiki markdown count, git commit cadence
  - Per-namespace page counts (and a `type`-frontmatter breakdown per namespace)
  - Sources: ingest velocity (7/30/90d), counts by year, top 20 publishers
    (no-ops if the pack declares no `sources` namespace)
  - Operational: cron-plus job last-run state, Hermes session counts/tokens/cost (7d/30d)
  - Freshness: recently updated / stale counts via mtime, per namespace

Domain-specific stats (e.g. prediction status/horizon/confidence drift) are NOT
computed here — that conformance/reporting lives in the domain pack.

Usage:
    WIKI_PATH=/path/to/vault python3 scripts/cron/generate_project_stats.py [--dry-run]

Inside the gateway container, WIKI_PATH defaults to /opt/vault and
state.db / cron-plus jobs.json default to /opt/data/. On the host, override via
env vars (HERMES_DATA_DIR, GIT_BIN) — both data sources degrade gracefully if
unreadable so the script still produces a useful corpus/wiki snapshot.

Wake-gate: emits `{"wakeAgent": false}` — pure side-effect, no agent invocation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
HERMES_DATA = Path(os.environ.get("HERMES_DATA_DIR", "/opt/data"))
OUT = VAULT / "wiki" / "dashboards" / "project-stats.md"


def knowledge_namespaces() -> list[str]:
    """Knowledge namespaces to report page counts / freshness for. Schema-driven
    (schema.yaml `partitioning.namespaces`, minus `exclude:` dirs); on-disk
    top-level wiki dirs (minus excluded + dot/underscore + dashboards/operational)
    as a fallback when the pack declares none. The engine ships no hardcoded list."""
    schema = schema_lib.governing_schema(VAULT)
    excluded = schema_lib.excluded_dirs(schema) | {"operational", "dashboards"}
    names = schema_lib.knowledge_namespaces(schema) - excluded
    if not names:
        wiki = VAULT / "wiki"
        if wiki.is_dir():
            names = {
                d.name for d in wiki.iterdir()
                if d.is_dir()
                and not d.name.startswith((".", "_"))
                and d.name not in excluded
            }
    return sorted(names)

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
PUBLISHER_LINE_RE = re.compile(r"^publisher:\s*(.+?)\s*$", re.MULTILINE)
SOURCE_FNAME_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()


def parse_frontmatter(path: Path) -> dict:
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return {}
    m = FRONTMATTER_RE.match(txt)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def md_files(subdir: str) -> list[Path]:
    d = VAULT / "wiki" / subdir
    if not d.is_dir():
        return []
    # rglob: a namespace may be sharded; skip shard INDEX dashboards so counts stay accurate.
    return [p for p in d.rglob("*.md")
            if p.is_file() and not p.name.startswith("_")
            and p.name != "INDEX.md" and not p.name.startswith("INDEX-")]


# --- Corpus ---------------------------------------------------------------

def corpus_stats() -> dict:
    raw_root = VAULT / "raw"
    raw_files = 0
    raw_bytes = 0
    raw_subdirs: Counter[str] = Counter()
    if raw_root.is_dir():
        for root, _, files in os.walk(raw_root):
            rel = Path(root).relative_to(raw_root)
            top = rel.parts[0] if rel.parts else "(root)"
            for fn in files:
                fp = Path(root) / fn
                try:
                    raw_bytes += fp.stat().st_size
                except OSError:
                    continue
                raw_files += 1
                raw_subdirs[top] += 1

    wiki_md = sum(1 for p in (VAULT / "wiki").rglob("*.md")
                  if p.is_file() and not p.name.startswith("_"))

    git_info: dict = {}
    try:
        out = subprocess.run(
            ["git", "-C", str(VAULT), "rev-list", "--count", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        git_info["commits"] = int(out.stdout.strip())
        out = subprocess.run(
            ["git", "-C", str(VAULT), "log", "-1", "--format=%cI|%s"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        last = out.stdout.strip().split("|", 1)
        git_info["last_commit_at"] = last[0]
        git_info["last_commit_subject"] = last[1] if len(last) > 1 else ""
    except (subprocess.SubprocessError, ValueError, FileNotFoundError):
        pass

    return {
        "raw_files": raw_files,
        "raw_bytes": raw_bytes,
        "raw_subdirs": raw_subdirs,
        "wiki_md": wiki_md,
        "git": git_info,
    }


# --- Per-namespace page stats --------------------------------------------

def namespace_stats(namespaces: list[str]) -> dict[str, Counter]:
    """Per-namespace `type`-frontmatter breakdown. Pages with no `type` are
    bucketed as 'untyped'. Domain-agnostic: works for any namespace."""
    out: dict[str, Counter] = {}
    for ns in namespaces:
        counts: Counter[str] = Counter()
        for p in md_files(ns):
            fm = parse_frontmatter(p)
            t = str(fm.get("type") or fm.get("category") or "untyped")
            counts[t] += 1
        out[ns] = counts
    return out


# --- Sources --------------------------------------------------------------

def _read_publisher(path: Path) -> str | None:
    try:
        with path.open("r", errors="replace") as fh:
            chunk = fh.read(1024)
    except OSError:
        return None
    m = PUBLISHER_LINE_RE.search(chunk)
    if not m:
        return None
    val = m.group(1).strip().strip('"').strip("'")
    return val or None


def source_stats() -> dict:
    paths = md_files("sources")
    total = len(paths)

    by_year: Counter[str] = Counter()
    last_7 = last_30 = last_90 = 0
    d7 = TODAY - timedelta(days=7)
    d30 = TODAY - timedelta(days=30)
    d90 = TODAY - timedelta(days=90)

    publishers: Counter[str] = Counter()

    for p in paths:
        m = SOURCE_FNAME_DATE_RE.match(p.name)
        if m:
            year = m.group(1)
            by_year[year] += 1
            try:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                if d >= d7:
                    last_7 += 1
                if d >= d30:
                    last_30 += 1
                if d >= d90:
                    last_90 += 1
            except ValueError:
                pass
        pub = _read_publisher(p)
        if pub:
            publishers[pub] += 1

    return {
        "total": total,
        "by_year": by_year,
        "last_7": last_7,
        "last_30": last_30,
        "last_90": last_90,
        "top_publishers": publishers.most_common(20),
        "publishers_total": len(publishers),
    }


# --- Operational ---------------------------------------------------------

def cron_health() -> dict:
    path = HERMES_DATA / "cron-plus" / "jobs.json"
    if not path.is_file():
        return {"available": False}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return {"available": False}

    success = 0
    failure = 0
    never = 0
    disabled = 0
    failures: list[tuple[str, str]] = []  # (name, error excerpt)
    for j in jobs:
        if not j.get("enabled", True):
            disabled += 1
            continue
        last = j.get("last_run_at")
        if not last:
            never += 1
            continue
        if j.get("last_run_success") is True:
            success += 1
        else:
            failure += 1
            err = (j.get("last_error") or j.get("last_delivery_error") or "").strip()
            failures.append((j.get("name", j.get("id", "?")), err[:200]))
    return {
        "available": True,
        "total": len(jobs),
        "success": success,
        "failure": failure,
        "never": never,
        "disabled": disabled,
        "failures": failures,
    }


def hermes_session_stats() -> dict:
    db = HERMES_DATA / "state.db"
    if not db.is_file():
        return {"available": False}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return {"available": False}
    try:
        windows = {"24h": 1, "7d": 7, "30d": 30}
        result: dict = {"available": True, "windows": {}}
        cutoff_unix = lambda days: (NOW - timedelta(days=days)).timestamp()
        for label, days in windows.items():
            row = conn.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cache_read_tokens), 0),
                       COALESCE(SUM(actual_cost_usd), 0),
                       COALESCE(SUM(estimated_cost_usd), 0)
                FROM sessions
                WHERE started_at >= ?
                """,
                (cutoff_unix(days),),
            ).fetchone()
            result["windows"][label] = {
                "sessions": row[0],
                "input_tokens": row[1],
                "output_tokens": row[2],
                "cache_read": row[3],
                "actual_cost": row[4],
                "estimated_cost": row[5],
            }
        # By source (cron vs other) over 30d
        rows = conn.execute(
            """
            SELECT source, COUNT(*)
            FROM sessions
            WHERE started_at >= ?
            GROUP BY source
            ORDER BY COUNT(*) DESC
            """,
            (cutoff_unix(30),),
        ).fetchall()
        result["sources_30d"] = rows
        # By model over 30d
        rows = conn.execute(
            """
            SELECT COALESCE(model, '?'), COUNT(*), COALESCE(SUM(estimated_cost_usd), 0)
            FROM sessions
            WHERE started_at >= ?
            GROUP BY model
            ORDER BY COUNT(*) DESC
            LIMIT 10
            """,
            (cutoff_unix(30),),
        ).fetchall()
        result["models_30d"] = rows
        return result
    except sqlite3.Error as e:
        return {"available": False, "error": str(e)}
    finally:
        conn.close()


# --- Freshness ------------------------------------------------------------

def freshness_stats(namespaces: list[str]) -> dict:
    cutoff_7 = (NOW - timedelta(days=7)).timestamp()
    cutoff_90 = (NOW - timedelta(days=90)).timestamp()
    out: dict = {}
    for subdir in namespaces:
        recent = 0
        stale = 0
        for p in md_files(subdir):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m >= cutoff_7:
                recent += 1
            if m < cutoff_90:
                stale += 1
        out[subdir] = {"recent_7d": recent, "stale_90d": stale}
    return out


# --- Rendering ------------------------------------------------------------

def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_int(n: int) -> str:
    return f"{n:,}"


def render(corpus, ns_stats, sources, cron, hermes, freshness, namespaces) -> str:
    lines: list[str] = []
    lines.append("---")
    lines.append("type: dashboard")
    lines.append("title: Project Stats")
    lines.append(f"generated: {NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("---")
    lines.append("")
    lines.append("# Project Stats")
    lines.append("")
    lines.append(f"> Generated: {NOW.strftime('%Y-%m-%d %H:%M UTC')}  ")
    lines.append(f"> Regenerated daily by `scripts/cron/generate_project_stats.py` — safe to overwrite.")
    lines.append("")

    # Headline counters
    lines.append("## Headline")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|---|---|")
    for ns in namespaces:
        lines.append(f"| {ns} | {fmt_int(sum(ns_stats[ns].values()))} |")
    lines.append(f"| Wiki markdown files (all subdirs) | {fmt_int(corpus['wiki_md'])} |")
    lines.append(f"| Raw documents | {fmt_int(corpus['raw_files'])} |")
    lines.append(f"| Raw corpus size | {fmt_bytes(corpus['raw_bytes'])} |")
    if corpus["git"]:
        g = corpus["git"]
        lines.append(f"| Vault git commits | {fmt_int(g.get('commits', 0))} |")
        lines.append(f"| Vault last commit | {g.get('last_commit_at', '?')} — {g.get('last_commit_subject', '')[:60]} |")
    lines.append("")

    # Corpus
    lines.append("## Corpus")
    lines.append("")
    lines.append(f"`raw/` holds **{fmt_int(corpus['raw_files'])}** files / **{fmt_bytes(corpus['raw_bytes'])}** across {len(corpus['raw_subdirs'])} top-level subdirs (immutable sources, gitignored).")
    lines.append("")
    if corpus["raw_subdirs"]:
        lines.append("Top 15 raw/ subdirs by file count:")
        lines.append("")
        lines.append("| Subdir | Files |")
        lines.append("|---|---|")
        for name, n in corpus["raw_subdirs"].most_common(15):
            lines.append(f"| `{name}` | {fmt_int(n)} |")
        lines.append("")

    # Per-namespace type breakdowns (domain-agnostic)
    for ns in namespaces:
        counts = ns_stats[ns]
        total_ns = sum(counts.values())
        lines.append(f"## {ns} ({fmt_int(total_ns)})")
        lines.append("")
        if counts:
            lines.append("| Type | Count |")
            lines.append("|---|---|")
            for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                marker = " ⚠ untyped" if t == "untyped" else ""
                lines.append(f"| {t}{marker} | {fmt_int(n)} |")
        lines.append("")

    # Sources — citation-page analytics (ingest velocity, year, publishers).
    # Generic OKF source-page conventions; no-ops when the pack has no sources.
    if sources["total"]:
        lines.append("## Source analytics")
        lines.append("")
        lines.append("### Ingest velocity (by filename date prefix)")
        lines.append("")
        lines.append("| Window | New sources |")
        lines.append("|---|---|")
        lines.append(f"| Last 7 days | {fmt_int(sources['last_7'])} |")
        lines.append(f"| Last 30 days | {fmt_int(sources['last_30'])} |")
        lines.append(f"| Last 90 days | {fmt_int(sources['last_90'])} |")
        lines.append("")

        lines.append("### By year (last 10 years)")
        lines.append("")
        lines.append("| Year | Sources |")
        lines.append("|---|---|")
        current_year = TODAY.year
        for y in range(current_year, current_year - 10, -1):
            n = sources["by_year"].get(str(y), 0)
            lines.append(f"| {y} | {fmt_int(n)} |")
        older = sum(n for y, n in sources["by_year"].items() if y.isdigit() and int(y) < current_year - 9)
        lines.append(f"| earlier | {fmt_int(older)} |")
        lines.append("")

        lines.append(f"### Top 20 publishers (of {fmt_int(sources['publishers_total'])} distinct names)")
        lines.append("")
        lines.append("| Publisher | Sources |")
        lines.append("|---|---|")
        for pub, n in sources["top_publishers"]:
            lines.append(f"| {pub} | {fmt_int(n)} |")
        lines.append("")

    # Operational
    lines.append("## Operational")
    lines.append("")
    lines.append("### Cron-plus jobs")
    lines.append("")
    if cron.get("available"):
        lines.append("| State | Count |")
        lines.append("|---|---|")
        lines.append(f"| Last run: success | {cron['success']} |")
        lines.append(f"| Last run: failure | {cron['failure']} |")
        lines.append(f"| Never run | {cron['never']} |")
        lines.append(f"| Disabled | {cron['disabled']} |")
        lines.append(f"| Total | {cron['total']} |")
        lines.append("")
        if cron["failures"]:
            lines.append("Currently in failure state:")
            lines.append("")
            for name, err in cron["failures"]:
                err_short = err.replace("\n", " ").strip() or "(no error message)"
                lines.append(f"- `{name}` — {err_short}")
            lines.append("")
    else:
        lines.append("_cron-plus jobs.json not readable from this path — operational stats skipped._")
        lines.append("")

    lines.append("### Hermes sessions (from `state.db`)")
    lines.append("")
    if hermes.get("available"):
        lines.append("| Window | Sessions | Input tok | Output tok | Cache read | Est. cost |")
        lines.append("|---|---|---|---|---|---|")
        for label in ("24h", "7d", "30d"):
            w = hermes["windows"][label]
            lines.append(
                f"| {label} | {fmt_int(w['sessions'])} | {fmt_int(w['input_tokens'])} | "
                f"{fmt_int(w['output_tokens'])} | {fmt_int(w['cache_read'])} | "
                f"${w['estimated_cost']:.2f} |"
            )
        lines.append("")
        if hermes.get("sources_30d"):
            lines.append("Sessions by source (30d):")
            lines.append("")
            lines.append("| Source | Sessions |")
            lines.append("|---|---|")
            for src, n in hermes["sources_30d"]:
                lines.append(f"| {src or '?'} | {fmt_int(n)} |")
            lines.append("")
        if hermes.get("models_30d"):
            lines.append("Top models (30d):")
            lines.append("")
            lines.append("| Model | Sessions | Est. cost |")
            lines.append("|---|---|---|")
            for model, n, cost in hermes["models_30d"]:
                lines.append(f"| {model} | {fmt_int(n)} | ${cost:.2f} |")
            lines.append("")
    else:
        lines.append("_state.db not readable from this path — Hermes session stats skipped._")
        lines.append("")

    # Freshness
    lines.append("## Freshness (by mtime)")
    lines.append("")
    lines.append("| Subdir | Touched in last 7d | Untouched >90d |")
    lines.append("|---|---|---|")
    for sub in namespaces:
        f = freshness.get(sub)
        if not f:
            continue
        lines.append(f"| {sub} | {fmt_int(f['recent_7d'])} | {fmt_int(f['stale_90d'])} |")
    lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate project-stats.md from vault + ops state")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout without writing")
    args = parser.parse_args()

    if not (VAULT / "wiki").is_dir():
        print(f"ERROR: vault not found at {VAULT}", file=sys.stderr)
        return 1

    namespaces = knowledge_namespaces()
    corpus = corpus_stats()
    ns_stats = namespace_stats(namespaces)
    sources = source_stats()
    cron = cron_health()
    hermes = hermes_session_stats()
    freshness = freshness_stats(namespaces)

    body = render(corpus, ns_stats, sources, cron, hermes, freshness, namespaces)

    if args.dry_run:
        sys.stdout.write(body)
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        bak = OUT.parent / (OUT.name + ".bak")   # single overwritten sidecar, not indexed (#165 sweep)
        shutil.copy2(OUT, bak)
    OUT.write_text(body)
    print(f"wrote {OUT} ({len(body)} bytes)", file=sys.stderr)
    print('{"wakeAgent": false}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
