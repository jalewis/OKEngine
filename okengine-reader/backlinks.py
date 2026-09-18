"""Backlink parsing and source-filter domain services."""
from __future__ import annotations

import os
import json
import re
import time

FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]", re.DOTALL)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+?)\)")
FENCE_RE = re.compile(r"^([ \t]*)(```+|~~~+)[^\n]*\n.*?^\1\2[^\n]*$", re.DOTALL | re.MULTILINE)
INLINE_CODE_RE = re.compile(r"(`+)[^\n]*?\1")


def skip_source(
    key: str,
    *,
    skip,
    reserved_names,
    is_reserved_segment,
    excluded_dirs,
    surfaced_derived,
    backlink_drop_dirs,
    namespaces,
) -> bool:
    name = key.split("/")[-1]
    if not name.endswith(".md"):
        name += ".md"
    if skip(name) or name in reserved_names:
        return True
    parts = key.split("/")
    if any(is_reserved_segment(segment) for segment in parts[:-1]):
        return True
    # Schema exclusions are namespace-scoped, but backlink-only drops (notably
    # ``sources``) apply wherever that directory occurs, including a subdomain.
    # Keeping those two concepts separate avoids treating every nested folder as
    # a namespace while still excluding source documents from backlink sources.
    dropped_namespaces = excluded_dirs() | surfaced_derived
    if namespaces(key) & dropped_namespaces:
        return True
    return bool(set(parts[:-1]) & backlink_drop_dirs())


def source_title(source: str, *, wiki, split_frontmatter, read_head, h1_re) -> str:
    try:
        frontmatter, body = split_frontmatter(read_head(wiki / f"{source}.md"))
        title = str(frontmatter.get("title") or frontmatter.get("name") or "").strip()
        if title:
            return title
        heading = h1_re.search(body)
        if heading:
            return heading.group(0).lstrip("# ").strip()
    except OSError:
        pass
    return source.split("/")[-1].replace("-", " ").strip() or source


def strip_markdown(text: str, *, frontmatter_re, fence_re, inline_re) -> str:
    match = frontmatter_re.match(text)
    if match:
        text = text[match.end() :]
    return inline_re.sub(" ", fence_re.sub("\n", text))


def wikilink_key(inner: str):
    key = inner.split("|", 1)[0].split("\n", 1)[0].split("#", 1)[0].strip()
    if not key or key.startswith(("http://", "https://", "mailto:")):
        return None
    return key[:-3] if key.endswith(".md") else key


def markdown_key(url: str, document_dir: str):
    value = url.split("#", 1)[0].strip()
    if (
        not value
        or value.startswith(("http://", "https://", "mailto:", "#"))
        or not value.endswith(".md")
    ):
        return None
    relative = os.path.normpath(os.path.join(document_dir, value))
    return None if relative.startswith("..") else relative[:-3]


def scan_forward_refs(
    *, wiki, skip_source, strip_markdown_text, wiki_re, markdown_re, wikilink_parser, markdown_parser
) -> list:
    paths = list(wiki.rglob("*.md"))
    keys = [path.relative_to(wiki).as_posix()[:-3] for path in paths]
    keyset = set(keys)
    by_basename: dict[str, list] = {}
    for key in keys:
        by_basename.setdefault(key.rsplit("/", 1)[-1], []).append(key)
    for candidates in by_basename.values():
        candidates.sort()

    def resolve(raw: str) -> str:
        if raw in keyset:
            return raw
        candidates = by_basename.get(raw.rsplit("/", 1)[-1])
        return candidates[0] if candidates else raw

    documents = []
    for path, key in zip(paths, keys):
        if skip_source(key):
            continue
        try:
            body = strip_markdown_text(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        document_dir = key.rsplit("/", 1)[0] if "/" in key else ""
        references, seen = [], set()
        parsers = (
            (wiki_re, lambda match: wikilink_parser(match.group(1))),
            (markdown_re, lambda match: markdown_parser(match.group(1), document_dir)),
        )
        for expression, parser in parsers:
            for match in expression.finditer(body):
                target = parser(match)
                if target:
                    target = resolve(target)
                    if target != key and target not in seen:
                        seen.add(target)
                        references.append({"key": target})
        documents.append({"key": key, "references": references})
    return documents


def build(*, scan, skip_source, source_title) -> dict:
    output: dict[str, list] = {}
    for document in scan():
        source = document.get("key")
        if not source or skip_source(source):
            continue
        title = source_title(source)
        for reference in document.get("references") or []:
            target = reference.get("key")
            if not target or target == source or skip_source(target):
                continue
            output.setdefault(target, []).append({"key": source, "title": title})
    for target, rows in output.items():
        seen, unique = set(), []
        for row in rows:
            if row["key"] in seen:
                continue
            seen.add(row["key"])
            unique.append(row)
        unique.sort(key=lambda row: row["title"].lower())
        output[target] = unique
    return output


def artifact_map(*, wiki, max_age: int, cache: dict) -> dict | None:
    path = wiki / ".backlinks.json"
    try:
        stat = path.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > max_age:
        return None
    if cache["mtime"] == stat.st_mtime and cache["map"] is not None:
        return cache["map"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    mapping = data.get("backlinks")
    if not isinstance(mapping, dict):
        return None
    cache["map"] = mapping
    cache["mtime"] = stat.st_mtime
    return mapping


def refresh_async(*, lock, state: dict, ttl: int, load, threading_module) -> None:
    if not lock.acquire(blocking=False):
        return
    try:
        stale = state["map"] is None or time.monotonic() - state["ts"] > ttl
    finally:
        lock.release()
    if stale:
        threading_module.Thread(
            target=lambda: load(blocking=True), daemon=True
        ).start()


def load_map(
    *, blocking: bool, artifact, state: dict, ttl: int, lock, build, refresh
) -> dict:
    mapping = artifact()
    if mapping is not None:
        return mapping
    now = time.monotonic()
    if state["map"] is not None and now - state["ts"] <= ttl:
        return state["map"]
    if not blocking:
        refresh()
        return state["map"] or {}
    with lock:
        now = time.monotonic()
        if state["map"] is None or now - state["ts"] > ttl:
            mapping = build()
            if mapping or state["map"] is None:
                state["map"] = mapping
                state["ts"] = now
    return state["map"] or {}


def prewarm(*, load, threading_module) -> None:
    threading_module.Thread(target=load, daemon=True).start()
