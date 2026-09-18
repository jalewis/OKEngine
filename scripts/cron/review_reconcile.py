#!/usr/bin/env python3
"""review_reconcile — prune the operational review queue back to its stated invariant (okengine#540).

`wiki/_review-queue.md` is written by the enforced write path (`_queue_review` for flag-raising
writes, `_flag` for the agent-facing flag tool and for slug-collision notices). Its docstring
claims "a resolved page's row is removed" — nothing ever removed one, so the file is an
append-only log that grows forever. Measured on five live vaults: 5,849 rows, of which 70%
pointed at a path with no page and 53% of the remainder were already cleared. 14% were live.

This lane restores the invariant: ONE outstanding row per live, still-open page. It is
deterministic (no_agent) — no LLM judgment participates, and a row is dropped only when its
death is PROVABLE from the vault itself.

Dispositions, applied per row in this order:

  duplicate   two rows for the same page. The queue is newest-first, so the FIRST occurrence is
              kept and later ones dropped (this folds in scripts/dedupe_review_queue.py, which
              was never scheduled). Every reason survives in wiki/log.md regardless.
  phantom     the recorded path has no page, and its basename resolves nowhere under wiki/.
              Overwhelmingly a `slug id collision on create`: the create was REJECTED, so
              `_flag` queued the path that was never written. The row can never be actioned.
  relocated   the path is gone but the basename resolves to exactly ONE page — the page was
              resharded under it (okengine#336/#54). The row's path is REWRITTEN to the current
              location, then re-judged as live/cleared below.
  ambiguous   the path is gone and the basename resolves to SEVERAL pages. Unactionable, but its
              death is not provable, so the row is KEPT and counted. A persistently non-zero
              count here means this rule needs a namespace-scoped resolver.
  cleared     the page exists, no longer carries `needs_review: true`, and has no OPEN review
              record in wiki/operational/reviews. Both flag producers are covered: a
              `_queue_review` row always has a record, and a `_flag` row (collision, field-loss
              guard, immutable-field revert) is a point-in-time notice about a write that
              already landed — the durable copy is wiki/log.md.
  live        the page exists and is still flagged, or still has an open review record. Kept
              verbatim, including its original date.

Conditions that are independently re-detected each cycle are safe to drop here: broken-wikilink
rows are re-raised by broken-wikilinks-drain (every 2h) and inventoried by reference-integrity,
so a dropped row that still has broken links comes back on its own.

A page whose frontmatter does not parse is KEPT — an unreadable page is not proof of anything.

Safety: aborts without writing if wiki/ contains no pages at all (an unmounted or mid-reshard
vault must not read as "every row is dead"). Takes the same fcntl lock the write path uses
(.okengine/review-queue.lock) around the whole read-modify-write, so a concurrent prepend cannot
be lost. Keeps one generation of undo at wiki/_review-queue.md.prev.

Runs at 22:55 — after review-autoverify (22:45) so the night's auto-clears are pruned in the
same cycle, and before review-queue (23:05) which rebuilds the human dashboard.

Env: WIKI_PATH (vault root, default /opt/vault). `--dry-run` reports without writing.
Pure script (no_agent): always emits {"wakeAgent": false}.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"

# Row shape written by write_server._append_review_queue_once. The em dash is part of the format;
# a line that does not match is preamble (frontmatter, heading, prose) and is passed through.
ROW = re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2}) \*\*(?P<path>[^*]+)\*\* — (?P<reason>.*)$")
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
# reconcile_review_records.py's OPEN set — a record in any of these still wants a human.
_OPEN_STATES = {"open", "in-review", "changes-requested", "rejected"}
_STALE_DAYS = int(os.environ.get("REVIEW_RECONCILE_STALE_DAYS", "30"))


def _ledger_date() -> str:
    """The date convention wiki/log.md already uses — write_server._today().

    LOCAL date, not UTC. The gateways run TZ=America/New_York while the containers' UTC clock is
    4-5h ahead, so a UTC stamp wrote `2026-08-04` onto a line logged at 22:55 on the 3rd, one day
    off every queue row and log line around it (the host-vs-container time trap). The same
    OKENGINE_MCP_WRITE_DATE override is honoured so a pinned ledger stays internally consistent
    across both writers rather than half-pinned.
    """
    return os.environ.get("OKENGINE_MCP_WRITE_DATE") or date.today().isoformat()


def _frontmatter(text: str) -> dict | None:
    """Parsed frontmatter, or None if the page has none / it does not parse."""
    m = _FM_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _page_index(wiki: Path) -> dict[str, list[str]]:
    """basename stem -> [vault-relative path with .md, ...] for every knowledge page.

    Underscore/dot-prefixed and INDEX files are excluded for the same reason review_queue.py
    excludes them: they are structural, never the subject of a review row.
    """
    idx: dict[str, list[str]] = defaultdict(list)
    for p in wiki.rglob("*.md"):
        n = p.name
        if n.startswith(("_", ".")) or n.upper().startswith("INDEX") or ".bak" in n:
            continue
        idx[p.stem].append(p.relative_to(wiki).as_posix())
    return idx


def _open_subjects(wiki: Path) -> set[str]:
    """Subjects ('entities/q/u/quietexit', no .md) carrying at least one OPEN review record."""
    store = wiki / "operational" / "reviews"
    if not store.is_dir():
        return set()
    out = set()
    # glob-ok: write_server._review_record_path writes every record flat as <digest>.yaml here —
    # the review store is not a sharded knowledge namespace.
    for rp in store.glob("*.yaml"):  # glob-ok: flat record store
        try:
            rec = yaml.safe_load(rp.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue        # an unreadable record is not evidence that a subject is closed
        if not isinstance(rec, dict):
            continue
        if str(rec.get("state") or "").strip().lower() in _OPEN_STATES:
            subject = str(rec.get("subject") or "").strip()
            if subject:
                out.add(subject.removesuffix(".md"))
    return out


def _is_live(wiki: Path, rel: str, open_subjects: set[str]) -> bool:
    """Does this existing page still want a human? Unreadable/unparseable => yes (conservative)."""
    if rel.removesuffix(".md") in open_subjects:
        return True
    try:
        text = (wiki / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    fm = _frontmatter(text)
    if fm is None:
        return True          # no frontmatter or it does not parse — see the module docstring
    return fm.get("needs_review") is True


def reconcile(text: str, wiki: Path, index: dict[str, list[str]],
              open_subjects: set[str]) -> tuple[str, Counter, list[tuple[str, str, str]]]:
    """Return (new_text, disposition counts, [(disposition, path, reason), ...]).

    Non-row lines are emitted untouched and in place, so the frontmatter, heading and
    explanatory sentence survive exactly as write_server wrote them.
    """
    seen: set[str] = set()
    kept: list[str] = []
    counts: Counter = Counter()
    detail: list[tuple[str, str, str]] = []
    today = date.today()

    for line in text.splitlines(keepends=True):
        m = ROW.match(line.rstrip("\n"))
        if not m:
            kept.append(line)
            continue
        counts["rows"] += 1
        raw = m.group("path").strip()
        rel = raw if raw.endswith(".md") else raw + ".md"
        reason = m.group("reason")

        if rel in seen:
            counts["duplicate"] += 1
            detail.append(("duplicate", rel, reason))
            continue

        if not (wiki / rel).is_file():
            hits = index.get(Path(rel).stem) or []
            if not hits:
                counts["phantom"] += 1
                detail.append(("phantom", rel, reason))
                continue
            if len(hits) > 1:
                counts["ambiguous"] += 1
                detail.append(("ambiguous", rel, reason))
                seen.add(rel)
                kept.append(line)
                continue
            counts["relocated"] += 1
            detail.append(("relocated", f"{rel} -> {hits[0]}", reason))
            rel = hits[0]
            line = f"- {m.group('date')} **{rel}** — {reason}\n"
            if rel in seen:                      # the row it moved onto is already queued
                counts["duplicate"] += 1
                continue

        if not _is_live(wiki, rel, open_subjects):
            counts["cleared"] += 1
            detail.append(("cleared", rel, reason))
            continue

        counts["live"] += 1
        try:
            age = (today - datetime.strptime(m.group("date"), "%Y-%m-%d").date()).days
        except ValueError:
            age = 0
        if age > _STALE_DAYS:
            counts["live_stale"] += 1
        seen.add(rel)
        kept.append(line)

    return "".join(kept), counts, detail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    ap.add_argument("--verbose", action="store_true", help="print every row disposition")
    args = ap.parse_args(argv)

    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    queue = WIKI / "_review-queue.md"
    if not queue.is_file():
        print("review-reconcile: no wiki/_review-queue.md — nothing to reconcile")
        print(json.dumps({"wakeAgent": False}))
        return 0

    index = _page_index(WIKI)
    if not index:
        # An unmounted volume or a reshard mid-move presents as an empty wiki. Every row would
        # grade `phantom` and the whole queue would be deleted on a vault that is merely absent.
        print("review-reconcile: wiki/ contains no knowledge pages — REFUSING to reconcile "
              "(unmounted or mid-move vault; this is not a pass)", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1
    open_subjects = _open_subjects(WIKI)

    lock_path = VAULT / ".okengine" / "review-queue.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        original = queue.read_text(encoding="utf-8")
        cleaned, counts, detail = reconcile(original, WIKI, index, open_subjects)
        removed = counts["rows"] - counts["live"] - counts["ambiguous"]
        wrote = False
        if not args.dry_run and cleaned != original:
            queue.with_suffix(".md.prev").write_text(original, encoding="utf-8")
            queue.write_text(cleaned, encoding="utf-8")
            wrote = True

    if args.verbose:
        for disposition, path, reason in detail:
            print(f"  {disposition:10s} {path} — {reason[:90]}")

    summary = (f"{counts['rows']} rows -> {counts['live'] + counts['ambiguous']} outstanding "
               f"(removed {removed}: phantom={counts['phantom']}, cleared={counts['cleared']}, "
               f"duplicate={counts['duplicate']}) · relocated={counts['relocated']} · "
               f"ambiguous={counts['ambiguous']} · open review records={len(open_subjects)}")
    print(f"review-reconcile: {summary}")
    if counts["live_stale"]:
        print(f"review-reconcile: {counts['live_stale']} outstanding row(s) older than "
              f"{_STALE_DAYS}d — a human queue nothing is draining")
    if args.dry_run:
        print("review-reconcile: dry run; re-run without --dry-run to write")
    elif wrote:
        # Deleting rows is only safe because every reason is durable in log.md; record the
        # deletion there too, so the prune itself is auditable from the same file.
        stamp = _ledger_date()
        try:
            with (WIKI / "log.md").open("a", encoding="utf-8") as log:
                log.write(f"- {stamp} review-reconcile pruned {removed} row(s) — {summary}\n")
        except OSError as exc:      # a read-only or host-owned log must not fail the prune
            print(f"review-reconcile: could not append to wiki/log.md ({exc})", file=sys.stderr)
        print(f"review-reconcile: wrote {queue} (undo: {queue.with_suffix('.md.prev')})")

    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
