#!/usr/bin/env python3
"""Publish the weekly wiki health audit deterministically.

The former agent job asked a model to enumerate an entire vault, run shell
audits, rotate a report, append the log, update the index, and commit a change
baseline. Large packs repeatedly compressed context and could return fluent
prose without performing any of those writes. This script makes collection,
publication, and terminal success runner-owned.

Semantic/editorial work remains owned by the bounded specialist lanes and
dashboards surfaced in the report. The weekly audit aggregates their state
instead of launching another unbounded full-corpus agent.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lint_watcher  # noqa: E402
import rebuild_index  # noqa: E402
import tz_lib  # noqa: E402
import wiki_change_check  # noqa: E402
import wiki_schema_audit  # noqa: E402

ARTIFACT_PREFIX = "OKENGINE_ARTIFACT:"

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
LOG = WIKI / "log.md"
STATE_PATH = Path(os.environ.get(
    "WIKI_HEALTH_AUDIT_STATE",
    str(Path(os.environ.get("HERMES_HOME", "/opt/data"))
        / "scripts" / "lint-state.json"),
))
MAX_CHANGED = int(os.environ.get("WIKI_HEALTH_MAX_CHANGED", "25"))
EDITORIAL_NAME_RE = re.compile(
    r"(contradict|stale|prediction|lacuna|question|portfolio|grounding|review)",
    re.IGNORECASE,
)
QUEUE_LABELS = {
    "broken-wikilinks": "Broken wikilinks",
    "orphans": "Orphan knowledge pages",
    "publisher-drift": "Publisher drift candidates",
    "fm-parse-errors": "Frontmatter parse errors",
    "yaml-invalid": "YAML-invalid pages",
    "schema-drift": "Schema drift",
    "sources-missing-quality-scores": "Sources missing quality scores",
    "pages-missing-from-index": "Pages missing from index",
    "reference-pages": "Reference catalog pages",
    "reference-broken-wikilinks": "Reference-catalog broken links",
}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _content_files() -> list[Path]:
    if not WIKI.is_dir():
        return []
    return [
        p for p in WIKI.rglob("*.md")
        if p.is_file()
        and not wiki_change_check._skip(p.name)
        and p.name not in {"index.md", "log.md"}
        and not p.name.startswith("lint-")
        and not any(part in {"dashboards", "operational"} for part in p.relative_to(WIKI).parts)
    ]


def changed_pages(baseline: float) -> tuple[list[Path], float]:
    files = _content_files()
    if not files:
        return [], baseline
    latest = max(p.stat().st_mtime for p in files)
    changed = sorted(
        (p for p in files if p.stat().st_mtime > baseline),
        key=lambda p: (-p.stat().st_mtime, p.as_posix()),
    )
    return changed, latest


def next_report_path(today: str) -> Path:
    used: set[int] = set()
    # glob-ok: rotated lint reports are intentionally top-level operational files.
    for path in WIKI.glob(f"lint-{today}*.md"):
        match = re.fullmatch(
            rf"lint-{re.escape(today)}(?:-v(\d+))?\.md", path.name)
        if match:
            used.add(int(match.group(1) or 1))
    version = max(used, default=0) + 1
    return WIKI / f"lint-{today}-v{version}.md"


def prior_queue_counts() -> dict[str, int]:
    prior = lint_watcher.read_prior_snapshot()
    return {str(k): int(v) for k, v in prior.items()}


def editorial_dashboards(limit: int = 20) -> list[Path]:
    roots = [WIKI / "dashboards", WIKI / "operational"]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        found.extend(
            p for p in root.rglob("*.md")
            if p.is_file() and EDITORIAL_NAME_RE.search(p.name)
        )
    found.sort(key=lambda p: (-p.stat().st_mtime, p.as_posix()))
    return found[:limit]


def _queue_table(queues: dict[str, int], prior: dict[str, int]) -> list[str]:
    out = [
        "| Check | Current | Prior | Delta |",
        "|---|---:|---:|---:|",
    ]
    for name, value in queues.items():
        old = prior.get(name, 0)
        out.append(
            f"| {QUEUE_LABELS.get(name, name)} | {value} | {old} | {value - old:+d} |")
    return out


def render_report(
    report: Path,
    generated: datetime,
    changed: list[Path],
    baseline: float,
    latest: float,
    queues: dict[str, int],
    prior: dict[str, int],
    details: dict,
    schema_report: str,
) -> str:
    broken = queues.get("broken-wikilinks", 0)
    missing_index = queues.get("pages-missing-from-index", 0)
    fm_errors = queues.get("fm-parse-errors", 0) + queues.get("yaml-invalid", 0)
    summary = (
        f"Broken wikilinks: {broken} | Missing from index: {missing_index} | "
        f"Frontmatter errors: {fm_errors} | Changed pages: {len(changed)}"
    )
    lines = [
        "---",
        "type: lint",
        f'title: "Wiki health audit — {generated.date().isoformat()}"',
        f"generated_at: {generated.isoformat()}",
        "generator: wiki-health-audit",
        f"changed_pages: {len(changed)}",
        f"baseline_mtime: {baseline}",
        f"latest_content_mtime: {latest}",
        "---",
        "",
        f"# Wiki health audit — {generated.date().isoformat()}",
        "",
        f"> {summary}",
        "",
        "## Queue summary",
        "",
        *_queue_table(queues, prior),
        "",
        "## Top missing concepts",
        "",
    ]
    missing = (details.get("broken-wikilinks") or {}).get("top_missing") or []
    if missing:
        lines.extend(
            f"- `[[{item['target']}]]` — {item['inbound']} inbound reference(s)"
            for item in missing
        )
    else:
        lines.append("None.")

    lines.extend(["", "## Publisher drift", ""])
    publishers = (details.get("publisher-drift") or {}).get("candidates") or []
    if publishers:
        lines.extend(
            f"- `{item['publisher']}` — {item['sources']} source page(s)"
            for item in publishers
        )
    else:
        lines.append("None.")

    lines.extend(["", schema_report.rstrip(), "", "### Recommendations", ""])
    drift = (details.get("schema-drift") or {}).get("by_type") or {}
    if drift:
        lines.append(
            "Review declared aliases first; route repeatable migrations to "
            "`schema-type-drain` and reserve human review for genuinely new types.")
    else:
        lines.append("No schema-emergence recommendation is required.")

    lines.extend(["", "## Changed-page sample", ""])
    if changed:
        for path in changed[:MAX_CHANGED]:
            rel = path.relative_to(VAULT).as_posix()
            stamp = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            lines.append(f"- `{rel}` — {stamp}")
        if len(changed) > MAX_CHANGED:
            lines.append(f"- … and {len(changed) - MAX_CHANGED} more")
    else:
        lines.append("No content pages changed since the prior successful audit.")

    lines.extend([
        "",
        "## Editorial review ownership",
        "",
        "Semantic contradiction, staleness, prediction grading, grounding, and "
        "research-gap judgments are handled by bounded specialist lanes. Their "
        "latest generated surfaces are listed here; this audit does not rescan "
        "the entire corpus with an unconstrained model.",
        "",
    ])
    dashboards = editorial_dashboards()
    if dashboards:
        for path in dashboards:
            rel = path.relative_to(WIKI).with_suffix("").as_posix()
            lines.append(f"- [[{rel}]]")
    else:
        lines.append("No specialist editorial dashboard is present.")
    lines.extend([
        "",
        "## Audit disposition",
        "",
        f"- Report: `[[{report.stem}]]`",
        "- Collection and publication: deterministic",
        "- Model calls: 0",
        "- Terminal state: report, log, index, and baseline committed",
        "",
    ])
    return "\n".join(lines)


def append_log(generated: datetime, report: Path, queues: dict[str, int]) -> None:
    stamp = tz_lib.deployment_now().strftime("%Y-%m-%d %H:%M %Z")
    summary = (
        f"{queues.get('broken-wikilinks', 0)} broken links, "
        f"{queues.get('fm-parse-errors', 0) + queues.get('yaml-invalid', 0)} "
        "frontmatter errors"
    )
    entry = (
        f"\n## [{stamp}] lint | {summary}\n"
        f"Full report: [[{report.stem}]]\n"
    )
    current = LOG.read_text(errors="replace") if LOG.exists() else "# Wiki log\n"
    if f"Full report: [[{report.stem}]]" not in current:
        _atomic_write(LOG, current.rstrip() + "\n" + entry)


def commit_baseline(latest: float, generated: datetime) -> None:
    state = wiki_change_check.load_state()
    state["last_baseline_mtime"] = latest
    state["last_run_at"] = generated.isoformat()
    state.pop("pending_baseline_mtime", None)
    state.pop("pending_started_at", None)
    wiki_change_check.STATE_PATH = STATE_PATH
    wiki_change_check.save_state(state)


def emit_artifact(path: Path, operation: str, count: int | None = None) -> None:
    resolved = path.resolve()
    try:
        reported_path = resolved.relative_to(VAULT.resolve()).as_posix()
    except ValueError:
        reported_path = resolved.as_posix()
    artifact: dict[str, object] = {
        "path": reported_path,
        "operation": operation,
    }
    if count is not None:
        artifact["count"] = count
    print(f"{ARTIFACT_PREFIX} {json.dumps(artifact, sort_keys=True)}")


def main() -> int:
    if not WIKI.is_dir():
        print(f"wiki-health-audit: wiki missing at {WIKI}", file=sys.stderr)
        return 2
    wiki_change_check.STATE_PATH = STATE_PATH
    lint_watcher.VAULT = VAULT
    lint_watcher.OPS_DIR = WIKI / "operational"
    lint_watcher.SNAPSHOTS = lint_watcher.OPS_DIR / "queue-snapshots.md"
    rebuild_index.VAULT = VAULT
    rebuild_index.INDEX = WIKI / "index.md"
    wiki_schema_audit.VAULT = VAULT

    state = wiki_change_check.load_state()
    baseline = float(state.get("last_baseline_mtime", 0.0))
    changed, latest = changed_pages(baseline)
    if not changed:
        print("wiki-health-audit: no content changes since successful baseline")
        emit_artifact(rebuild_index.INDEX, "verify", 0)
        print(json.dumps({"wakeAgent": False, "changed": 0}))
        return 0

    generated = datetime.now(timezone.utc)
    report = next_report_path(generated.date().isoformat())
    details: dict = {}
    queues = lint_watcher.scan_queues(details)
    prior = prior_queue_counts()
    schema_parts = wiki_schema_audit.scan_wiki(WIKI)
    schema_report = wiki_schema_audit.render_report(*schema_parts)
    body = render_report(
        report, generated, changed, baseline, latest, queues, prior,
        details, schema_report,
    )

    _atomic_write(report, body)
    append_log(generated, report, queues)
    _atomic_write(rebuild_index.INDEX, rebuild_index.render_index())
    commit_baseline(latest, generated)

    emit_artifact(report, "create", len(changed))
    emit_artifact(LOG, "append", 1)
    emit_artifact(rebuild_index.INDEX, "replace")
    emit_artifact(STATE_PATH, "update")

    print(f"wiki-health-audit: wrote {report.relative_to(VAULT)}")
    print(f"wiki-health-audit: changed={len(changed)} model_calls=0")
    print(json.dumps({
        "wakeAgent": False,
        "changed": len(changed),
        "report": report.relative_to(VAULT).as_posix(),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
