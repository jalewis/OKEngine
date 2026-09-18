#!/usr/bin/env python3
"""Reconcile version-locked review records with the current wiki.

Open records for deleted pages, superseded page versions, pages whose review flag
was cleared, or duplicate requests for the same current page are closed as
``dismissed``.  The command is a dry run unless ``--apply`` is supplied.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile

import yaml


OPEN = {"open", "in-review", "changes-requested", "rejected"}


def _page_state(path: Path) -> tuple[int, str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = {}
    if text.startswith("---"):
        try:
            fm = yaml.safe_load(text.split("---", 2)[1]) or {}
        except yaml.YAMLError:
            fm = {}
    try:
        version = int(fm.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    return version, hashlib.sha256(text.encode()).hexdigest(), fm.get("needs_review") is True


def _informative(record: dict) -> bool:
    reasons = record.get("reasons") or []
    return any(isinstance(reason, dict) and reason.get("code") != "legacy-unspecified"
               for reason in reasons)


def _queue_reasons(wiki: Path) -> dict[str, str]:
    """Return the newest historical queue explanation for each subject."""
    queue = wiki / "_review-queue.md"
    if not queue.is_file():
        return {}
    found: dict[str, str] = {}
    pattern = re.compile(r"^- \d{4}-\d{2}-\d{2} \*\*([^*]+)\.md\*\* — (.*)$")
    for line in queue.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            found.setdefault(match.group(1), match.group(2))
    return found


def _reason_code(detail: str) -> str:
    lowered = detail.lower()
    if "unresolvable wikilink" in lowered:
        return "broken-link"
    if "degenerate" in lowered or "repetition" in lowered:
        return "agent-draft"
    if "slug id collision" in lowered:
        return "id-collision"
    if "immutable field change reverted" in lowered:
        return "rejected-write"
    if "field `" in lowered:
        return "owner-conflict"
    return "manual"


def _write(path: Path, record: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".review-reconcile-", delete=False) as handle:
        yaml.safe_dump(record, handle, sort_keys=False, allow_unicode=True)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    wiki = args.pack.expanduser().resolve() / "wiki"
    store = wiki / "operational" / "reviews"
    historical_reasons = _queue_reasons(wiki)
    rows: list[tuple[Path, dict, str | None]] = []
    current_groups: dict[tuple[str, int, str], list[tuple[Path, dict]]] = defaultdict(list)

    for path in sorted(store.rglob("*.yaml")):
        try:
            record = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(record, dict) or str(record.get("state")) not in OPEN:
            continue
        subject = str(record.get("subject") or "")
        page = wiki / f"{subject}.md"
        reason = None
        if not page.is_file():
            reason = "subject page no longer exists"
        else:
            version, digest, flagged = _page_state(page)
            if record.get("subject_version") != version or record.get("subject_hash") != digest:
                reason = "superseded by a newer subject version"
            elif not flagged:
                reason = "subject no longer requests review"
            else:
                current_groups[(subject, version, digest)].append((path, record))
        rows.append((path, record, reason))

    duplicate_paths: dict[Path, str] = {}
    for group in current_groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: (_informative(item[1]),
                                                  str(item[1].get("requested_at") or "")),
                         reverse=True)
        for path, _ in ordered[1:]:
            duplicate_paths[path] = "duplicate request for the current subject version"

    counts = Counter()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for path, record, reason in rows:
        reason = reason or duplicate_paths.get(path)
        if not reason:
            counts["kept-open"] += 1
            subject = str(record.get("subject") or "")
            detail = historical_reasons.get(subject)
            if detail and not _informative(record):
                counts["reasons-hydrated"] += 1
                if args.apply:
                    record["reasons"] = [{"code": _reason_code(detail), "detail": detail}]
                    record["version"] = int(record.get("version") or 1) + 1
                    record.setdefault("history", []).append({
                        "action": "hydrate-reason", "at": stamp,
                        "detail": "restored latest explanation from the historical review queue",
                        "service": "maintenance",
                    })
                    _write(path, record)
            continue
        counts[reason] += 1
        if not args.apply:
            continue
        record["state"] = "dismissed"
        record["decision_by"] = "review-record-reconciler"
        record["decision_at"] = stamp
        record["decision_note"] = reason
        record["decision_service"] = "maintenance"
        record["version"] = int(record.get("version") or 1) + 1
        record.setdefault("history", []).append({
            "decision": "dismiss", "state": "dismissed",
            "decision_by": "review-record-reconciler", "decision_at": stamp,
            "decision_note": reason, "service": "maintenance",
        })
        _write(path, record)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"review-record reconciliation {mode}: " +
          ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
