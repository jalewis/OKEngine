"""Bounded ripgrep search service for the reader."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException


def search(request, query: str, limit: int, *, wiki, guard, semaphore, excluded_dirs,
           namespaces, subprocess_module):
    query = query.strip()
    if len(query) < 2:
        return {"q": query, "results": []}
    release = guard(request, semaphore)
    ignore = ["!*.bak.*", "!_?*", "!INDEX.md", "!index.md", "!INDEX-*", "!index-*"]
    glob_args = ["-g", "*.md"]
    for pattern in ignore:
        glob_args += ["-g", pattern]
    command = [
        "rg", "-i", "-F", "-m1", "--no-heading", "-n", "--no-messages",
        "--max-columns", "240", *glob_args, "--", query, str(wiki),
    ]
    try:
        process = subprocess_module.run(
            command, capture_output=True, timeout=12, text=True
        )
    except FileNotFoundError as exc:
        raise HTTPException(503, "ripgrep not installed") from exc
    except subprocess_module.TimeoutExpired:
        return {"q": query, "results": [], "truncated": True}
    finally:
        release()
    base = wiki.resolve()
    seen, rows = set(), []
    for line in process.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path, _line_number, text = parts
        try:
            relative = str(Path(file_path).resolve().relative_to(base))
        except (ValueError, OSError):
            continue
        relative = relative[:-3] if relative.endswith(".md") else relative
        if namespaces(Path(file_path)) & excluded_dirs():
            continue
        if relative in seen:
            continue
        seen.add(relative)
        directory = relative.split("/", 1)[0]
        rows.append(
            {
                "path": relative,
                "dir": directory,
                "title": Path(relative).name,
                "snippet": re.sub(r"\s+", " ", text).strip()[:200],
            }
        )
        if len(rows) >= 1500:
            break
    lowered = query.lower()
    rows.sort(
        key=lambda row: (
            0 if lowered in row["title"].lower() else 1,
            row["dir"],
            row["path"],
        )
    )
    return {
        "q": query,
        "total": len(rows),
        "results": rows[: max(1, min(limit, 100))],
    }
