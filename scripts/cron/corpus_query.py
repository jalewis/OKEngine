#!/usr/bin/env python3
"""Typed, streaming queries over corpus_indexer JSONL artifacts.

This is an engine library, not a domain taxonomy: any indexed namespace can be
loaded with ``load(kind)``. Convenience queries cover the engine's common
source, prediction, and event shapes while preserving the complete row.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

INDEX_DIR = Path(os.environ.get("HERMES_DATA", "/opt/data")) / "state" / "corpus-index"
EVENT_INDEX = (
    Path(os.environ.get("HERMES_DATA", "/opt/data"))
    / "state"
    / "okengine.events"
    / "event-scores.jsonl"
)
_KIND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PREDICTION_LINK_RE = re.compile(r"\[\[(?:predictions/)?([^|\]#]+)")


def available_kinds(index_dir: Path | None = None) -> set[str]:
    """Return namespace indexes currently present on disk."""
    root = index_dir or INDEX_DIR
    if not root.is_dir():
        return set()
    return {
        path.stem
        for path in root.iterdir()
        if path.is_file() and path.suffix == ".jsonl" and _KIND_RE.fullmatch(path.stem)
    }


def load(kind: str, *, index_dir: Path | None = None) -> Iterator[dict]:
    """Stream rows for one indexed namespace.

    The kind is constrained to a filename-safe namespace and must exist. Bad
    JSON fails loud with the file and line number instead of silently dropping
    evidence from an analytical query.
    """
    if not _KIND_RE.fullmatch(kind):
        raise ValueError(f"invalid corpus-index kind: {kind!r}")
    path = (index_dir or INDEX_DIR) / f"{kind}.jsonl"
    if not path.is_file():
        known = sorted(available_kinds(index_dir))
        raise FileNotFoundError(
            f"{path} not found — run corpus_indexer.py first"
            + (f" (available: {known})" if known else "")
        )
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSONL: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{lineno}: corpus row must be an object")
            yield row


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _has_prediction_basis(frontmatter: dict) -> bool:
    values = frontmatter.get("basis") or frontmatter.get("basis_in") or []
    if not isinstance(values, list):
        values = [values]
    return any(
        isinstance(value, str)
        and (
            "predictions/" in value
            or bool(_PREDICTION_LINK_RE.search(value) and "prediction" in value.lower())
        )
        for value in values
    )


def query_sources(
    *,
    signal_class: str | None = None,
    publisher: str | None = None,
    since: date | None = None,
    source_kind: str | None = None,
    has_basis_in_predictions: bool | None = None,
    index_dir: Path | None = None,
) -> Iterator[dict]:
    """Filter the conventional ``sources`` namespace by common OKF fields."""
    for row in load("sources", index_dir=index_dir):
        fm = row.get("frontmatter") or {}
        if signal_class and fm.get("signal_class") != signal_class:
            continue
        if publisher and fm.get("publisher") != publisher:
            continue
        if source_kind and fm.get("source_kind") != source_kind:
            continue
        if since:
            observed = _parse_date(fm.get("ingested") or fm.get("published"))
            if not observed or observed < since:
                continue
        if (
            has_basis_in_predictions is not None
            and _has_prediction_basis(fm) is not has_basis_in_predictions
        ):
            continue
        yield row


def query_predictions(
    *,
    status: str | None = None,
    horizon: str | None = None,
    near_due_pct: float | None = None,
    today: date | None = None,
    index_dir: Path | None = None,
) -> Iterator[dict]:
    """Filter predictions, optionally selecting those a fraction through their window."""
    if near_due_pct is not None and not 0 <= near_due_pct <= 1:
        raise ValueError("near_due_pct must be between 0 and 1")
    today = today or datetime.now(timezone.utc).date()
    for row in load("predictions", index_dir=index_dir):
        fm = row.get("frontmatter") or {}
        if status and fm.get("status") != status:
            continue
        if horizon and fm.get("horizon") != horizon:
            continue
        if near_due_pct is not None:
            made_on = _parse_date(fm.get("made_on"))
            resolves_by = _parse_date(fm.get("resolves_by"))
            if not made_on or not resolves_by or resolves_by <= made_on:
                continue
            elapsed = (today - made_on).days
            total = (resolves_by - made_on).days
            if elapsed / total < near_due_pct:
                continue
        yield row


def query_events(
    *,
    entity: str | None = None,
    event_type: str | None = None,
    since: date | None = None,
    min_score: float | None = None,
    event_index: Path | None = None,
) -> Iterator[dict]:
    """Filter the deterministic event-scoring substrate emitted by okengine.events."""
    path = event_index or EVENT_INDEX
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSONL: {exc}") from exc
            entities = row.get("entities") or row.get("related_entities") or []
            if entity and entity != row.get("entity") and entity not in entities:
                continue
            if event_type and event_type not in {
                row.get("event_type"), row.get("type"), row.get("typed_event")
            }:
                continue
            if since:
                observed = _parse_date(
                    row.get("date") or row.get("observed_at") or row.get("published")
                )
                if not observed or observed < since:
                    continue
            score = row.get("score")
            if score is None:
                score = row.get("aggregate_score")
            if score is None and isinstance(row.get("scores"), dict):
                score = row["scores"].get("materiality", row["scores"].get("signal_strength"))
            if min_score is not None and (not isinstance(score, (int, float)) or score < min_score):
                continue
            yield row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("kinds", "sources-by-class", "predictions-near-due"))
    parser.add_argument("--near-due-pct", type=float, default=0.8)
    args = parser.parse_args(argv)
    if args.command == "kinds":
        print("\n".join(sorted(available_kinds())))
        return 0
    if args.command == "sources-by-class":
        counts = Counter(
            (row.get("frontmatter") or {}).get("signal_class") or "(unset)"
            for row in load("sources")
        )
        for key, count in counts.most_common():
            print(f"{key}\t{count}")
        return 0
    rows = query_predictions(status="open", near_due_pct=args.near_due_pct)
    for row in rows:
        print(row.get("rel_path") or row.get("stem"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
