"""Filesystem path and display primitives for the vault reader."""
from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path

import yaml

from okengine.schema_exclusions import excluded_namespace


def _namespace(value) -> str:
    """Normalize backlink-drop entries, which retain their separate legacy grammar."""
    segment = str(value).strip().strip("/")
    if segment.startswith("wiki/"):
        segment = segment[len("wiki/") :]
    return segment.strip("/").split("/")[0]


@lru_cache(maxsize=32)
def _declared_namespaces(schema_path: str, mtime_ns: int) -> frozenset[str]:
    try:
        schema = yaml.safe_load(Path(schema_path).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return frozenset()
    partitioned = (schema.get("partitioning") or {}).get("namespaces") or {}
    names = set(partitioned) if isinstance(partitioned, dict) else set()
    for value in schema.get("exclude") or []:
        try:
            names.add(excluded_namespace(value))
        except ValueError:
            continue
    return frozenset(names)


def _root_namespaces(wiki: Path) -> frozenset[str]:
    # The composed schema is authoritative for extension/bundle namespaces. Reading only the raw
    # pack schema makes a valid flat namespace look undeclared and causes every nested folder name
    # to be treated as a namespace.
    for candidate in (
        wiki.parent / ".okengine" / "composed-schema.yaml",
        wiki.parent / "schema.yaml",
        wiki / "schema.yaml",
    ):
        try:
            if candidate.is_file():
                return _declared_namespaces(str(candidate), candidate.stat().st_mtime_ns)
        except OSError:
            continue
    return frozenset()


def namespace_dirs(path: Path, wiki: Path) -> frozenset:
    try:
        parts = path.relative_to(wiki).parts[:-1]
    except ValueError:
        return frozenset()
    if not parts:
        return frozenset()
    declared = _root_namespaces(wiki)
    # A walk-up sub-domain is identifiable by its own schema, not by the absence of its name from
    # the root schema. For a real subdomain prefix, select its first governed child namespace.
    # Otherwise the top-level directory is itself the namespace, including undeclared legacy ones.
    top_schema = wiki / parts[0] / "schema.yaml"
    if top_schema.is_file() and len(parts) > 1:
        for segment in parts[1:]:
            if segment in declared:
                return frozenset((segment,))
        return frozenset((parts[1],))
    return frozenset((parts[0],))


def is_reserved_segment(segment: str) -> bool:
    return len(segment) > 1 and segment.startswith(("_", "."))


def reserved_path(path: Path, wiki: Path) -> bool:
    try:
        parts = path.relative_to(wiki).parts[:-1]
    except ValueError:
        return False
    return any(is_reserved_segment(segment) for segment in parts)


def display_timestamp(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else text


def read_head(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def page_meta(
    path: Path, *, wiki: Path, split_frontmatter, read, h1_re, display_time
) -> dict:
    relative = str(path.relative_to(wiki.resolve()))
    relative = relative[:-3] if relative.endswith(".md") else relative
    frontmatter, body = split_frontmatter(read(path))
    title = str(frontmatter.get("title") or frontmatter.get("name") or "").strip()
    if not title:
        heading = h1_re.search(body)
        title = heading.group(0).lstrip("# ").strip() if heading else Path(relative).name
    try:
        stat = path.stat()
        revision = f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        revision = ""
    return {
        "path": relative,
        "title": title,
        "type": str(frontmatter.get("type") or "").strip(),
        "status": str(frontmatter.get("status") or "").strip(),
        "updated": display_time(
            frontmatter.get("last_updated")
            or frontmatter.get("updated")
            or frontmatter.get("created")
        ),
        "revision": revision,
    }


def scan_directory(
    subdirectory: str,
    *,
    force: bool,
    wiki: Path,
    cache: dict,
    ttl: float,
    excluded_dirs,
    within,
    skip,
    reserved,
    namespaces,
    metadata,
) -> list[dict]:
    excluded = excluded_dirs()
    if subdirectory in excluded:
        return []
    now = time.monotonic()
    hit = cache.get(subdirectory)
    if not force and hit and now - hit[0] < ttl:
        return hit[1]
    base = (wiki / subdirectory).resolve()
    output: list[dict] = []
    if base.is_dir() and within(wiki, base):
        for path in base.rglob("*.md"):
            if skip(path.name) or reserved(path) or (namespaces(path) & excluded):
                continue
            output.append(metadata(path.resolve()))
    output.sort(key=lambda row: (row["title"].lower(), row["path"]))
    cache[subdirectory] = (now, output)
    return output


def revision_inventory(
    *, wiki, cache: tuple, ttl: float, excluded_dirs, skip, reserved, namespaces
) -> tuple[dict, tuple]:
    now = time.monotonic()
    if now - cache[0] < ttl:
        return {"pages": cache[1]}, cache
    output = []
    excluded = excluded_dirs()
    root = wiki.resolve()
    for path in wiki.rglob("*.md"):
        if skip(path.name) or reserved(path) or (namespaces(path) & excluded):
            continue
        try:
            resolved = path.resolve()
            stat = resolved.stat()
            relative = str(resolved.relative_to(root))[:-3]
        except (OSError, ValueError):
            continue
        output.append({"path": relative, "revision": f"{stat.st_mtime_ns}:{stat.st_size}"})
    output.sort(key=lambda row: row["path"])
    updated = (now, output)
    return {"pages": output}, updated


def excluded_namespaces(*, cache: tuple, ttl: float, schema_path, yaml_module, surfaced):
    now = time.monotonic()
    if now - cache[0] < ttl:
        return cache[1], cache
    output: set[str] = set()
    path = schema_path()
    if path.is_file():
        try:
            schema = yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}
            values = schema.get("exclude") or []
            if isinstance(values, list):
                for raw in values:
                    try:
                        output.add(excluded_namespace(raw))
                    except ValueError:
                        # Authoring validation rejects malformed entries. The reader
                        # remains available and retains other valid exclusions.
                        continue
        except Exception:
            pass
    value = frozenset(output) - surfaced
    return value, (now, value)


def backlink_drop_namespaces(*, cache: tuple, ttl: float, schema_path, yaml_module):
    now = time.monotonic()
    if cache[1] is not None and now - cache[0] < ttl:
        return cache[1], cache
    output = {"sources"}
    path = schema_path()
    if path.is_file():
        try:
            schema = yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}
            if "backlink_drop" in schema:
                output = {
                    segment
                    for value in schema.get("backlink_drop") or []
                    if (segment := _namespace(value))
                }
        except Exception:
            pass
    value = frozenset(output)
    return value, (now, value)


def display_groups(*, cache: tuple, ttl: float, schema_path, yaml_module):
    now = time.monotonic()
    if now - cache[0] < ttl:
        return cache[1], cache
    output: list[tuple[str, frozenset[str]]] = []
    path = schema_path()
    if path.is_file():
        try:
            groups = (yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}).get(
                "display_groups"
            ) or {}
            if isinstance(groups, dict):
                for label, types in groups.items():
                    normalized = frozenset(
                        str(value).strip().lower()
                        for value in types or []
                        if str(value).strip()
                    )
                    if str(label).strip() and normalized:
                        output.append((str(label).strip(), normalized))
        except Exception:
            pass
    return output, (now, output)


def rail_top(*, cache: tuple, ttl: float, schema_path, yaml_module, wiki):
    now = time.monotonic()
    if now - cache[0] < ttl:
        return cache[1], cache
    label, namespaces = "", ()
    path = schema_path()
    if path.is_file():
        try:
            value = (yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}).get(
                "rail_top_section"
            ) or {}
            if isinstance(value, dict):
                label = str(value.get("label") or "").strip()
                namespaces = tuple(
                    str(item).strip()
                    for item in value.get("namespaces") or []
                    if str(item).strip()
                )
        except Exception:
            pass
    if not label and (wiki / "briefings").is_dir():
        label, namespaces = "Briefs", ("briefings",)
    result = (label, namespaces)
    return result, (now, result)
