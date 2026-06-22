#!/usr/bin/env python3
"""Meta-watcher: scan vault for known queue depths, compare against prior
snapshot, classify each queue, alert on regressions or unhandled queues.

Closes the observability gap where drain crons watch vault state directly
but no process confirmed the drains were actually keeping up. Without this:
queue depths get reported but nothing reads them, so a queue could grow
silently for weeks before someone notices.

Architecture: this script scans the vault directly (same as the drain
crons), so it works regardless of lint-report format or schedule. Writes
two outputs:

  wiki/operational/queue-snapshots.md — append-only history (one row/day)
  wiki/operational/lint-watch-YYYY-MM-DD.md — today's classified report

Designed to be runnable as a cron-plus job (wake-gate script that writes
its reports as a side effect AND returns wakeAgent=false — no agent
invocation needed; this is pure observability, no judgment required).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
OPS_DIR = VAULT / "wiki" / "operational"
SNAPSHOTS = OPS_DIR / "queue-snapshots.md"

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?(?:#[^\]]+)?\]\]")
# Optional, domain-agnostic "Canonical names" publisher block in the vault
# CLAUDE.md: a `**Canonical names**` heading followed by a list of backtick-
# quoted names. Packs that don't curate publisher names simply omit it, in
# which case publisher-drift detection no-ops.
_CANONICAL_BLOCK_RE = re.compile(
    r"\*\*Canonical names\*\*[^`]*?\n\n((?:`[^`]+`(?:,\s*)?)+)",
    re.DOTALL,
)
_CANONICAL_NAME_RE = re.compile(r"`([^`]+)`")

OPERATIONAL_NO_FM = {"index.md", "log.md", "README.md"}
OPERATIONAL_NO_FM_PREFIXES = ("lint-", "log-")
OPERATIONAL_TYPES = {
    "dashboard", "lint", "overview", "report", "daily-brief",
}

# Mapping: queue name -> whether a drain cron is expected to own it.
# True  = a drain cron is expected to clear this queue (alert if it grows).
# False = no auto-drain expected (one-shot/manual fix only; don't alert as
#         "unhandled" on growth, but still surface depth).
# The engine no longer hardcodes specific cron NAMES here — which cron owns a
# queue is deployment-specific (pack/cron-tier config), not engine knowledge.
# The report column shows "drain" / "manual" instead of a literal cron name.
DRAIN_OWNED = {
    "broken-wikilinks": True,
    "orphans": True,
    "publisher-drift": True,
    "fm-parse-errors": False,  # rare; one-shot fix, no auto-drain
    "yaml-invalid": True,
    "schema-drift": True,
    "sources-missing-quality-scores": True,
    "pages-missing-from-index": True,
}


def parse_fm(text):
    m = _FM_RE.match(text)
    if not m:
        return None, None
    try:
        fm = yaml.safe_load(m.group(1))
        return (fm if isinstance(fm, dict) else None), m
    except yaml.YAMLError:
        return None, m


def is_operational_path(path):
    rel = path.relative_to(VAULT / "wiki")
    if len(rel.parts) > 1:
        return False
    return rel.name in OPERATIONAL_NO_FM or any(
        rel.name.startswith(p) for p in OPERATIONAL_NO_FM_PREFIXES
    )


def all_wiki_pages():
    """All .md under wiki/, excluding backup dirs and operational top-level files."""
    out = []
    for p in (VAULT / "wiki").rglob("*.md"):
        if not p.is_file():
            continue
        if any(".bak." in part or part.startswith(".") for part in p.parts):
            continue
        # Archived content (wiki/**/_archive/, wiki/_archived-*) is retained for
        # history but is NOT live vault state — exclude it from all lint/drift
        # counts (matches normalize_entity_schema's _archived skip).
        if any(part.startswith("_archive") for part in p.parts):
            continue
        out.append(p)
    return out


def knowledge_namespaces() -> set[str]:
    """Knowledge namespaces for orphan / missing-from-index checks. Schema-driven
    (schema.yaml `partitioning.namespaces`, minus `exclude:` dirs); on-disk
    top-level wiki dirs (minus excluded + dot/underscore + operational) as a
    fallback when the pack declares none. The engine ships no hardcoded list."""
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
    return names


def scan_queues():
    """Return dict[queue-name -> int depth]."""
    pages = all_wiki_pages()
    schema = schema_lib.governing_schema(VAULT)
    declared_types = schema_lib.canonical_types(schema)  # empty ⇒ accept any type
    knowledge_ns = knowledge_namespaces()
    fm_parse_errors = 0
    yaml_invalid = 0
    schema_drift = 0
    sources_missing_quality = 0
    pages_missing_index = 0
    publisher_drift = 0

    # All wikilinks → set of resolved/unresolved targets
    all_targets = set()
    inbound_count = {}  # slug → count

    # Build slug index for resolution
    valid_slugs = set()
    for p in pages:
        rel = p.relative_to(VAULT / "wiki")
        valid_slugs.add(str(rel.with_suffix("")))  # e.g., entities/foo
        valid_slugs.add(p.stem)  # bare slug (sources/foo → foo)

    # Read index.md for "missing from index" check
    try:
        index_links = set()
        for m in _WIKILINK_RE.finditer((VAULT / "wiki" / "index.md").read_text(errors="replace")):
            index_links.add(m.group(1).strip())
            index_links.add(m.group(1).strip().split("/")[-1])
    except OSError:
        index_links = set()

    # Load canonical publisher list for publisher-drift count
    try:
        claude_md = (VAULT / "CLAUDE.md").read_text(errors="replace")
        cm = _CANONICAL_BLOCK_RE.search(claude_md)
        canonical_publishers = set(_CANONICAL_NAME_RE.findall(cm.group(1))) if cm else set()
    except OSError:
        canonical_publishers = set()
    publisher_counts = {}

    for p in pages:
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue

        fm, m = parse_fm(txt)
        if not m:
            if not is_operational_path(p):
                fm_parse_errors += 1
            continue

        # FM regex passed — try YAML
        try:
            yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            yaml_invalid += 1
            continue

        if not fm:
            continue

        # Schema drift: type present but not among the pack's declared canonical
        # types (and not an operational type). When the pack declares no types,
        # `declared_types` is empty ⇒ accept any type (no drift counted).
        t = str(fm.get("type", ""))
        if declared_types and t and t not in declared_types and t not in OPERATIONAL_TYPES:
            schema_drift += 1

        # Sources: must carry quality scores (reliability + credibility) and
        # contribute to the publisher-canonicalization tally. These are generic
        # OKF source-page conventions; a pack without a `source` type simply
        # produces no source pages, so this no-ops.
        if t == "source":
            if not (fm.get("reliability") and fm.get("credibility")):
                sources_missing_quality += 1
            pub = fm.get("publisher")
            if pub and isinstance(pub, str):
                publisher_counts[pub] = publisher_counts.get(pub, 0) + 1

        # All pages: count outbound wikilinks and inbound references
        for wm in _WIKILINK_RE.finditer(txt):
            tgt = wm.group(1).strip()
            all_targets.add(tgt)
            slug = tgt.split("/")[-1]
            inbound_count[slug] = inbound_count.get(slug, 0) + 1

        # "missing from index" check — knowledge namespaces only (sources/
        # operational/dashboards excluded via knowledge_namespaces()).
        rel = p.relative_to(VAULT / "wiki")
        if rel.parts and rel.parts[0] in knowledge_ns:
            slug_paths = (str(rel.with_suffix("")), p.stem)
            if not any(s in index_links for s in slug_paths):
                pages_missing_index += 1

    # Broken wikilinks: targets not in valid_slugs
    broken_wikilinks = sum(1 for t in all_targets if t not in valid_slugs and t.split("/")[-1] not in valid_slugs)

    # Orphans: knowledge-namespace pages with 0 inbound references
    orphans = 0
    for p in pages:
        rel = p.relative_to(VAULT / "wiki")
        if rel.parts and rel.parts[0] in knowledge_ns:
            if inbound_count.get(p.stem, 0) == 0:
                orphans += 1

    # Publisher drift: publishers used ≥10 times not in canonical list
    for pub, cnt in publisher_counts.items():
        if cnt >= 10 and pub not in canonical_publishers and pub not in {"Unknown", "TBD", "N/A"}:
            publisher_drift += 1

    return {
        "broken-wikilinks": broken_wikilinks,
        "orphans": orphans,
        "publisher-drift": publisher_drift,
        "fm-parse-errors": fm_parse_errors,
        "yaml-invalid": yaml_invalid,
        "schema-drift": schema_drift,
        "sources-missing-quality-scores": sources_missing_quality,
        "pages-missing-from-index": pages_missing_index,
    }


def read_prior_snapshot():
    """Return last day's queue depths from queue-snapshots.md, or {}."""
    if not SNAPSHOTS.exists():
        return {}
    try:
        txt = SNAPSHOTS.read_text(errors="replace")
    except OSError:
        return {}
    # Each row: `| YYYY-MM-DD | q1=N1, q2=N2, ... |`
    rows = re.findall(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|\s*(.+?)\s*\|\s*$", txt, re.MULTILINE)
    if not rows:
        return {}
    last = rows[-1]
    out = {}
    for kv in last.split(","):
        kv = kv.strip()
        if "=" in kv:
            k, v = kv.split("=", 1)
            try:
                out[k.strip()] = int(v.strip())
            except ValueError:
                pass
    return out


def append_snapshot(today, queues):
    """Append today's row to queue-snapshots.md (create file if missing)."""
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    if not SNAPSHOTS.exists():
        header = (
            "---\n"
            "type: dashboard\n"
            "title: Queue snapshots\n"
            f"created: {today}\n"
            "---\n\n"
            "# Queue snapshots\n\n"
            "> Append-only daily snapshot of vault queue depths. Written by `lint_watcher.py`.\n"
            "> Use to spot regressions (queue grew despite a drain) or unhandled queues (no drain at all).\n\n"
            "| Date | Queue depths |\n"
            "|---|---|\n"
        )
        SNAPSHOTS.write_text(header)
    row = "| " + today + " | " + ", ".join(f"{k}={v}" for k, v in sorted(queues.items())) + " |\n"
    with open(SNAPSHOTS, "a") as f:
        f.write(row)


def write_today_report(today, queues, prior, alerts):
    report = OPS_DIR / f"lint-watch-{today}.md"
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    out = [
        "---",
        "type: dashboard",
        "title: Lint watch",
        f"date: {today}",
        f"generated_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"alerts: {len(alerts)}",
        "---",
        "",
        f"# Lint watch — {today}",
        "",
        f"> Daily meta-watcher. Scans vault for known queue depths, compares vs prior day,",
        f"> classifies each queue. {len(alerts)} alert(s) today.",
        "",
        "## Queue depths",
        "",
        "| Queue | Today | Yesterday | Δ | Ownership | Status |",
        "|---|---:|---:|---:|---|---|",
    ]
    for q in sorted(queues.keys()):
        depth = queues[q]
        prev = prior.get(q, 0)
        delta = depth - prev
        owned = DRAIN_OWNED.get(q, True)  # unknown queues assumed drain-owned
        ownership = "drain" if owned else "manual"
        if depth == 0:
            status = "✓ empty"
        elif not owned:
            status = "⚠ UNHANDLED — no drain cron"
        elif delta > 0:
            status = f"⚠ REGRESSION — grew by {delta}"
        elif delta < 0:
            status = f"✓ draining ({delta:+d})"
        else:
            status = "stable"
        delta_str = f"{delta:+d}" if delta != 0 else "0"
        out.append(f"| `{q}` | {depth} | {prev} | {delta_str} | {ownership} | {status} |")
    out.append("")
    if alerts:
        out.append("## Alerts")
        out.append("")
        for a in alerts:
            out.append(f"- {a}")
        out.append("")
    else:
        out.append("## Alerts")
        out.append("")
        out.append("None — all queues healthy or draining as expected.")
        out.append("")
    report.write_text("\n".join(out))
    return report


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    queues = scan_queues()
    prior = read_prior_snapshot()

    alerts = []
    is_first_run = not prior  # no prior snapshot — suppress regression alerts (everything looks like it grew from 0)
    for q, depth in queues.items():
        if depth == 0:
            continue
        owned = DRAIN_OWNED.get(q, True)
        prev = prior.get(q, 0)
        delta = depth - prev
        if not owned:
            alerts.append(f"`{q}` has {depth} items but no drain cron — UNHANDLED queue, accumulating silently.")
        elif delta > 0 and not is_first_run:
            alerts.append(f"`{q}` grew by {delta} (now {depth}) despite an expected drain cron — drain may be stuck or behind.")

    append_snapshot(today, queues)
    report = write_today_report(today, queues, prior, alerts)

    print(f"=== lint-watcher: {today} ===")
    print(f"  vault: {VAULT}")
    print(f"  queues scanned: {len(queues)}")
    print(f"  alerts: {len(alerts)}")
    print(f"  report: {report.relative_to(VAULT)}")
    print(f"  snapshot row appended to: {SNAPSHOTS.relative_to(VAULT)}")
    print()
    for q, depth in sorted(queues.items()):
        prev = prior.get(q, 0)
        delta = depth - prev
        ownership = "drain" if DRAIN_OWNED.get(q, True) else "manual"
        delta_str = f"{delta:+d}" if delta != 0 else " 0"
        print(f"  {q:42s} {depth:5d}  Δ {delta_str:>4s}  owner={ownership}")
    if alerts:
        print()
        print("ALERTS:")
        for a in alerts:
            print(f"  - {a}")
    print()
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
