#!/usr/bin/env python3
"""schema-drift-lint — corpus-wide OKF/domain conformance monitor (#3 / OKF).

The write-time guard (tools/file_operations) and the vault pre-commit gate stop
NEW non-conformant pages at their two entry points. But files can still land out
of band — direct host writes, MEGA sync from another machine, bulk scripts — so a
periodic full-corpus sweep is the durable backstop that proves conformance isn't
silently regressing.

This is the SAME validator the guard uses (tools.schema_validator), run across the
whole vault. It is report-only: it writes a stable dashboard to
wiki/operational/schema-conformance.md (operational/ is itself excluded from
validation) and NEVER mutates a page — backfilling the residual is the job of the
deterministic backfills + the enrichment crons, not the monitor.

Pure script (no_agent): emits wakeAgent=false always. A non-zero exit (its own
failure) is delivered verbatim as an alert per the cron failure path.

Env:
  WIKI_PATH               vault root (default /opt/vault)
  SCHEMA_DRIFT_BASELINE   if set to an int, exit non-zero when the live count
                          exceeds it (turns the monitor into a ratchet/alarm).
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# tools.schema_validator ships in the Hermes image at /opt/hermes/tools/.
for _p in ("/opt/hermes", str(Path(__file__).resolve().parents[2])):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from tools.schema_validator import schema_reject_reason  # type: ignore[import]

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
REPORT = WIKI / "operational" / "schema-conformance.md"

# Skip non-page artifacts: scratch dirs the validator ignores + backup/restore
# debris (gitignored, kept on disk) so the count reflects curated pages, not junk.
_SKIP_SUBSTR = ("/.git/", "/raw/", "/_archive/", "/_patches/")
_SKIP_NAME_SUBSTR = (".bak", ".was-broken", ".restored", ".corrupt",
                     ".recovered", ".backup", ".accidental-overwrite",
                     "_test_", "/test.md", "test-write", "test_write")


def _is_artifact(sp: str) -> bool:
    if any(s in sp for s in _SKIP_SUBSTR):
        return True
    name = sp.rsplit("/", 1)[-1]
    return any(s in name for s in _SKIP_NAME_SUBSTR)


def _bucket(reason: str) -> str:
    if reason.startswith("missing YAML"):
        return "no-frontmatter"
    if reason.startswith("missing required"):
        return "typeless (no type:)"
    if reason.startswith("type '"):
        return "missing required field(s)"
    if "not valid YAML" in reason or "not a YAML mapping" in reason:
        return "broken frontmatter"
    return "other"


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki dir not found at {WIKI}", file=sys.stderr)
        return 1

    total = scanned = bad = 0
    by_bucket = Counter()
    by_type = Counter()
    by_field = Counter()
    worst: list[tuple[str, str]] = []

    for p in WIKI.rglob("*.md"):
        sp = p.as_posix()
        if _is_artifact(sp):
            continue
        total += 1
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        reason = schema_reject_reason(str(p), content)
        scanned += 1
        if not reason:
            continue
        bad += 1
        by_bucket[_bucket(reason)] += 1
        if reason.startswith("type '"):
            by_type[reason.split("'")[1]] += 1
            for f in reason.split("field(s): ")[1].split(", "):
                by_field[f] += 1
        if len(worst) < 40:
            worst.append((p.relative_to(VAULT).as_posix(), reason))

    pct = 100.0 * (scanned - bad) / scanned if scanned else 100.0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "---", "type: dashboard", 'title: "Schema Conformance (OKF + domain)"', "---",
        "", f"# Schema Conformance — {now}", "",
        f"> Source of truth: `schema.yaml`. Validator: `tools/schema_validator.py` "
        f"(same one the write-guard uses). Report-only; this monitor never edits pages.",
        "",
        f"- **In-scope pages scanned:** {scanned:,}",
        f"- **Non-conformant:** {bad:,}",
        f"- **Conformance:** {pct:.2f}%",
        "",
        "## By violation class", "",
        "| class | count |", "|---|---:|",
    ]
    for k, v in by_bucket.most_common():
        lines.append(f"| {k} | {v} |")
    if by_type:
        lines += ["", "## Missing-required-field pages by type", "",
                  "| type | count |", "|---|---:|"]
        for k, v in by_type.most_common():
            lines.append(f"| {k} | {v} |")
    if by_field:
        lines += ["", "## Most-missing fields", "",
                  "| field | count |", "|---|---:|"]
        for k, v in by_field.most_common():
            lines.append(f"| {k} | {v} |")
    if worst:
        lines += ["", "## Sample (first 40)", ""]
        for rel, reason in worst:
            lines.append(f"- `{rel}` — {reason}")
    lines.append("")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")

    print(f"schema-drift-lint: {bad:,}/{scanned:,} non-conformant "
          f"({pct:.2f}% conformant) -> {REPORT.relative_to(VAULT)}")

    baseline = os.environ.get("SCHEMA_DRIFT_BASELINE")
    if baseline and baseline.isdigit() and bad > int(baseline):
        print(f"ERROR: conformance regressed — {bad} non-conformant exceeds "
              f"baseline {baseline}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
