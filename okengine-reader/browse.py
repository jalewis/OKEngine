"""Browse-tree and page-resolution domain services."""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException


def directory_is_derived(paths: list[Path], *, split_frontmatter, derived_types) -> bool:
    """Classify a namespace from a bounded frontmatter sample."""
    seen = derived = 0
    for path in paths[:8]:
        try:
            frontmatter, _ = split_frontmatter(
                path.read_text(encoding="utf-8", errors="replace")[:2000]
            )
        except OSError:
            continue
        page_type = str(frontmatter.get("type") or "").strip().lower()
        if page_type:
            seen += 1
            derived += page_type in derived_types
    return seen > 0 and derived == seen


def tree(*, wiki, excluded_dirs, skip, reserved, namespaces, is_derived, rail_top) -> dict:
    directories = []
    if wiki.is_dir():
        excluded = excluded_dirs()
        for directory in sorted(wiki.iterdir()):
            if not directory.is_dir() or skip(directory.name) or directory.name in excluded:
                continue
            pages = [
                path
                for path in directory.rglob("*.md")
                if not skip(path.name)
                and not reserved(path)
                and not (namespaces(path) & excluded)
            ]
            if pages:
                directories.append(
                    {
                        "dir": directory.name,
                        "count": len(pages),
                        "derived": is_derived(pages),
                    }
                )
    label, featured = rail_top()
    present = {directory["dir"] for directory in directories}
    return {
        "vault": str(wiki),
        "dirs": directories,
        "top_section": {
            "label": label,
            "namespaces": [name for name in featured if name in present],
        },
    }


def groups(*, display_groups, pages_of_types) -> dict:
    output = []
    for label, types in display_groups():
        count = len(pages_of_types(types))
        if count:
            output.append({"label": label, "count": count})
    return {"groups": output}


def pages(directory: str, group: str, *, display_groups, pages_of_types, namespace_about, scan):
    if group:
        for label, types in display_groups():
            if label == group:
                return {"group": group, "pages": pages_of_types(types)}
        raise HTTPException(404, "unknown group")
    if "/" in directory or ".." in directory or directory.startswith((".", "/")):
        raise HTTPException(400, "bad dir")
    return {"dir": directory, "about": namespace_about(directory), "pages": scan(directory)}


def namespace_about(directory: str, *, wiki: Path, within, split_frontmatter, render) -> str:
    if not directory:
        return ""
    path = (wiki / directory / "_about.md").resolve()
    if not (path.is_file() and within(wiki, path)):
        return ""
    try:
        _, body = split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        return render(body)
    except OSError:
        return ""


def resolve_page(path: str, *, wiki: Path, skip, excluded_dirs, namespaces, within,
                 split_frontmatter) -> Path:
    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "bad path")
    candidate = wiki / (path + ".md")

    def tombstoned(item: Path) -> bool:
        try:
            fm, _ = split_frontmatter(item.read_text(encoding="utf-8", errors="replace")[:4000])
        except OSError:
            return False
        return str(fm.get("status") or "").strip().lower() == "tombstoned"

    # A full wiki-relative path names one exact page. Basename fallback below
    # must not redirect it to a deeper same-name shard (or claim ambiguity)
    # after the link writer has already grounded that explicit path.
    if candidate.is_file() and not tombstoned(candidate):
        resolved = candidate.resolve()
        if not within(wiki, resolved) or skip(resolved.name):
            raise HTTPException(403, "blocked")
        return resolved
    name = Path(path).name + ".md"
    parts = Path(path).parts
    search_root = wiki / parts[0] if len(parts) > 1 and (wiki / parts[0]).is_dir() else wiki
    hits = [item for item in search_root.rglob(name) if not skip(item.name)] \
        if search_root.is_dir() else []

    if hits:
        live = [item for item in hits if not tombstoned(item)]
        hits = live or hits
        if search_root == wiki and len(hits) > 1:
            excluded = excluded_dirs()
            preferred = [item for item in hits if not (namespaces(item) & excluded)] or hits
            entities = [item for item in preferred if "entities" in namespaces(item)]
            hits = entities or preferred
        deepest = max(len(item.relative_to(wiki).parts) for item in hits)
        hits = [item for item in hits if len(item.relative_to(wiki).parts) == deepest]
        if len(hits) > 1:
            raise HTTPException(409, "ambiguous page basename; use the full wiki-relative path")
        candidate = hits[0]
    elif not candidate.is_file():
        candidate = None
    if not candidate:
        raise HTTPException(404, "page not found")
    resolved = candidate.resolve()
    if not within(wiki, resolved) or skip(resolved.name):
        raise HTTPException(403, "blocked")
    return resolved
