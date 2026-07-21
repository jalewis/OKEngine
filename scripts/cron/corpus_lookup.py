#!/usr/bin/env python3
"""Reusable lookup helpers for an optional pack-owned question corpus."""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def _parse_frontmatter(text: str) -> dict | None:
    match = _FM_RE.match(text)
    if not match:
        return None
    try:
        value = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return value if isinstance(value, dict) else None


def _wikilink_slugs(value) -> set[str]:
    values = value if isinstance(value, list) else [value]
    slugs: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        for match in _WIKILINK_RE.finditer(item):
            slug = match.group(1).strip().split("/")[-1].removesuffix(".md")
            if slug:
                slugs.add(slug)
    return slugs


def find_matching_questions(
    slug_set: set[str],
    *,
    asker: str | None = None,
    status: str | None = "active",
    vault: Path | None = None,
    namespace: str = "questions",
) -> list[dict]:
    """Return question pages related to any requested entity/concept slug."""
    root = vault or VAULT
    questions_dir = root / "wiki" / namespace
    if not slug_set or not questions_dir.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(questions_dir.rglob("*.md")):
        if path.name.startswith(("_", "INDEX")):
            continue
        try:
            fm = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not fm or fm.get("type") not in {"board-question", "question"}:
            continue
        if asker and str(fm.get("asker") or "") != asker:
            continue
        if status and str(fm.get("status") or "active") != status:
            continue
        related = (
            _wikilink_slugs(fm.get("related_entities"))
            | _wikilink_slugs(fm.get("related_concepts"))
        )
        matched = related & slug_set
        if not matched:
            continue
        rows.append({
            "rel": str(path.relative_to(root / "wiki")),
            "stem": path.stem,
            "question": str(fm.get("question") or "")[:200],
            "canonical_form": str(fm.get("canonical_form") or "")[:200],
            "asker": str(fm.get("asker") or ""),
            "related_matched": sorted(matched),
            "trigger_events": fm.get("trigger_events") or [],
            "last_seen": str(fm.get("last_seen") or ""),
        })
    return rows


def format_questions_for_digest(questions: list[dict], cap: int = 5) -> str:
    """Render stable Markdown suitable for insertion in a synthesis wake-gate."""
    if not questions:
        return "  (no matching questions in corpus)"
    lines: list[str] = []
    for question in questions[:cap]:
        rel = question["rel"].removesuffix(".md")
        asker = question.get("asker") or "?"
        related = ", ".join(
            (question.get("related_matched") or question.get("related_slugs") or [])[:3]
        )
        lines.append(f"  - [[{rel}]]  (asker={asker}, related={related})")
        canonical = question.get("canonical_form") or question.get("question") or ""
        if canonical:
            lines.append(f"      Q: {canonical[:140]}")
    if len(questions) > cap:
        lines.append(f"  - _... and {len(questions) - cap} more matching questions_")
    return "\n".join(lines)
