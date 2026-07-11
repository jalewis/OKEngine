#!/usr/bin/env python3
"""detect_field_loss.py — flag curated-frontmatter fields lost between vault git
revisions.

The write-guard PREVENTS an agent `write_file`/`edit`/`patch` from dropping
curated fields. This is the complementary DETECTION layer: a deterministic
backstop that catches losses the guard can't (deterministic helper scripts
write via Python file-ops, not the guarded tools; and historical losses
predate the guard). Detection only — it never edits pages; it writes a review
report so the operator can restore via the curated-entity-fields overlay.

For each curated page changed in the window, it parses the frontmatter at the
baseline revision and at HEAD, and reports any curated field present-then-absent
(plus source wikilinks dropped from the `sources:` list). A field that merely
CHANGED value (e.g. type re-classified, a curated field's value bumped) is not a
loss.

The guarded namespaces and the curated-field set are PACK inputs (schema.yaml),
read at runtime — the engine guards nothing domain-specific on its own.

Env:
  WIKI_PATH               vault git root (default /opt/vault)
  FIELD_LOSS_WINDOW_DAYS  how far back the baseline is (default 1; the one-time
                          audit uses a wider window, e.g. 35)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WINDOW_DAYS = int(os.environ.get("FIELD_LOSS_WINDOW_DAYS", "1"))
OP_DIR = VAULT / "wiki" / "operational"
SNAP = OP_DIR / "field-loss-snapshots.md"

_SCHEMA = schema_lib.governing_schema(VAULT)

# Guarded namespaces — the pack's knowledge namespaces (schema.yaml). Fallback:
# the actual top-level dirs under wiki/ that hold markdown, minus excluded /
# dot / underscore dirs. Never a hardcoded domain list.
def _guarded() -> list[str]:
    ns = schema_lib.knowledge_namespaces(_SCHEMA)
    if not ns:
        excluded = schema_lib.excluded_dirs(_SCHEMA)
        wiki = VAULT / "wiki"
        if wiki.is_dir():
            ns = {d.name for d in wiki.iterdir()
                  if d.is_dir() and not d.name.startswith((".", "_"))
                  and d.name not in excluded and any(True for _ in d.rglob("*.md"))}
    return [f"wiki/{n}" for n in sorted(ns)]


GUARDED = _guarded()

# Curated fields whose loss is a regression worth a human look. These are PACK
# inputs (schema.yaml `protected_fields:`) — the engine guards nothing
# domain-specific on its own, so the default is EMPTY (no field-loss flagged
# unless the pack declares protected fields). Prediction-schema field loss is
# covered by prediction-intake-audit (missing_* violations), so it is not
# duplicated here.
CURATED_FIELDS = schema_lib.protected_fields(_SCHEMA)

_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
_WIKILINK = re.compile(r"\[\[[^\]]+\]\]")


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(VAULT), *args],
                              capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        return ""


def _fm(text: str) -> dict | None:
    m = _FM.match(text or "")
    if not m:
        return None
    try:
        d = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return d if isinstance(d, dict) else None


def _source_links(fm: dict) -> set[str]:
    out: set[str] = set()
    for key in ("sources", "basis"):
        v = fm.get(key)
        if isinstance(v, list):
            for item in v:
                out |= set(_WIKILINK.findall(str(item)))
    return out


def detect() -> tuple[list[dict], str | None]:
    baseline = _git("rev-list", "-1", f"--before={WINDOW_DAYS} days ago", "HEAD").strip()
    if not baseline:
        # window predates the repo → audit the full history from the root commit
        baseline = _git("rev-list", "--max-parents=0", "HEAD").split()[:1]
        baseline = baseline[0].strip() if baseline else ""
    if not baseline:
        return [], None
    changed = _git("diff", "--name-only", f"{baseline}..HEAD", "--", *GUARDED).split()
    losses: list[dict] = []
    for rel in changed:
        if rel.startswith("_") or "_archive" in rel:
            continue
        old_fm = _fm(_git("show", f"{baseline}:{rel}"))
        if old_fm is None:
            continue  # baseline broken/new → nothing reliable to compare
        cur_path = VAULT / rel
        if not cur_path.exists():
            losses.append({"file": rel, "lost": ["<file deleted>"]})
            continue
        new_fm = _fm(cur_path.read_text(errors="replace"))
        if new_fm is None:
            continue  # current unparseable → corruption, handled elsewhere
        lost = sorted(f for f in CURATED_FIELDS if f in old_fm and f not in new_fm)
        # Source-drop is a meaningful regression where a page carries a curated
        # citation set. We flag it only when the pack marks `sources` as a
        # protected field — otherwise dropped sources are often legitimate
        # consolidation/dedup and would flood the signal.
        if "sources" in CURATED_FIELDS:
            dropped_sources = _source_links(old_fm) - _source_links(new_fm)
            if dropped_sources:
                lost.append(f"sources(-{len(dropped_sources)})")
        if lost:
            losses.append({"file": rel, "lost": lost})
    return losses, baseline


def write_report(losses: list[dict], baseline: str | None, today: str) -> None:
    OP_DIR.mkdir(parents=True, exist_ok=True)
    L = ["---", "type: dashboard", "title: Curated field-loss review",
         f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}", "generator: scripts/cron/detect_field_loss.py",
         "---", "", "# Curated field-loss review", "",
         f"_Window: last {WINDOW_DAYS}d (baseline `{(baseline or 'n/a')[:8]}`) → HEAD. "
         "Detection only — restore via `config/curated-entity-fields.json` overlay "
         "if a loss is unintended._", ""]
    if not losses:
        L.append("No curated-field losses detected in window. \U0001F7E2")
    else:
        L.append(f"**{len(losses)} page(s) lost curated fields:**")
        L.append("")
        L.append("| page | lost fields |")
        L.append("|---|---|")
        for x in sorted(losses, key=lambda d: d["file"]):
            page = x["file"].replace("wiki/", "").rsplit(".md", 1)[0]
            L.append(f"| [[{page}]] | {', '.join(x['lost'])} |")
    (OP_DIR / f"field-loss-{today}.md").write_text("\n".join(L) + "\n")

    # one-row/day snapshot for kb-health to read
    header = ("---\ntype: dashboard\ntitle: Field-loss snapshots\n---\n\n"
              "# Field-loss snapshots\n\n| date | losses |\n|---|---|\n")
    if not SNAP.exists():
        SNAP.write_text(header)
    rows = [ln for ln in SNAP.read_text(errors="replace").splitlines()
            if not ln.startswith(f"| {today} |")]
    SNAP.write_text("\n".join(rows).rstrip() + f"\n| {today} | {len(losses)} |\n")


def main() -> int:
    today = datetime.now().date().isoformat()  # LOCAL ledger day, not UTC (TZ-behind-UTC files tomorrow) — invariant-audit B6.2
    losses, baseline = detect()
    write_report(losses, baseline, today)
    print("=== detect-field-loss ===")
    print(f"  vault: {VAULT}  window: {WINDOW_DAYS}d  baseline: {(baseline or 'n/a')[:8]}")
    print(f"  pages with lost curated fields: {len(losses)}")
    for x in losses[:30]:
        print(f"    {x['file']}: {', '.join(x['lost'])}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
