"""Filesystem path and display primitives for the vault reader."""
from __future__ import annotations

import re
import time
from functools import lru_cache
from pathlib import Path

import yaml

from okengine.file_fingerprint import fingerprint
from okengine.schema_exclusions import excluded_namespace

# file_fingerprint's key is the stat identity (five fields), then the content digest while unsettled.
_DIGEST = 5


def _namespace(value) -> str:
    """Normalize backlink-drop entries, which retain their separate legacy grammar."""
    segment = str(value).strip().strip("/")
    if segment.startswith("wiki/"):
        segment = segment[len("wiki/") :]
    return segment.strip("/").split("/")[0]


@lru_cache(maxsize=32)
def _declared_namespaces(schema_path: str, key) -> frozenset[str]:
    """Declared namespaces of one schema; ``key`` (the file fingerprint) only keys the cache."""
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
                # Fingerprint, not mtime_ns: a schema edited within the same timestamp tick must not
                # keep the previous namespace set (okengine#1017).
                return _declared_namespaces(str(candidate), fingerprint(candidate))
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


# A frontmatter envelope whose closing delimiter is in hand: the opener line, then the earliest
# `\n---` after it (the reader's split, rendering._FM_RE, closes on the same delimiter).
_ENVELOPE_CLOSED = re.compile(rb"[^\n]*\n.*?\n---", re.S)


def read_head(path: Path, limit: int) -> str:
    """The page's first `limit` bytes, extended through its whole frontmatter envelope.

    A plain head cut a long envelope off: a tombstone with ~58 kB of frontmatter (a merged actor's
    sources list) parsed as {}, so the reader listed and served it while every generator hid it
    (okengine#993). Only a page whose envelope outruns `limit` reads further, so the listing scan
    stays a head read for ordinary pages."""
    try:
        with path.open("rb") as handle:
            head = handle.read(limit)
            if head.startswith(b"---"):
                head = _through_envelope(handle, head)
            return head.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _through_envelope(handle, head: bytes) -> bytes:
    """Extend `head` until it holds the envelope's closing delimiter (or the file ends).

    The opener is `---` plus the rest of its first line, so the closing delimiter is looked for
    only after the first newline, as the reader's frontmatter split does. Each read doubles the
    head, so an envelope of any length costs reads linear in its size."""
    while not _ENVELOPE_CLOSED.match(head):
        more = handle.read(len(head))
        if not more:
            return head
        head += more
    return head


TOMBSTONED = "tombstoned"


def is_tombstoned(path: Path, *, split_frontmatter, read, limit: int = 16384) -> bool:
    """True when the page's frontmatter says `status: tombstoned`.

    A tombstone is a retired page the write path keeps on disk as history: the reader hides it
    everywhere a reader would find it (listings, counts, search, backlinks, direct links). The
    head read extends through the whole envelope, so a `status` below a long `sources:` list is
    seen however long the list is.
    """
    frontmatter, _ = split_frontmatter(read(path, limit))
    return str(frontmatter.get("status") or "").strip().lower() == TOMBSTONED


def tombstone_check(split_frontmatter):
    """`is_tombstoned` bound to a frontmatter parser: the one predicate every reader surface uses."""
    return lambda path: is_tombstoned(path, split_frontmatter=split_frontmatter, read=read_head)


def _revision(path: Path, stat, now_ns: int | None = None) -> str:
    """A page's edit identity for clients that skip unchanged pages (render-lint), which store it
    and compare it for equality. "" when the page vanished mid-read.

    A SETTLED page (its timestamps older than the file_fingerprint settle window) gets exactly
    ``mtime_ns:size``. Any later write must stamp a newer mtime, so that is exact, and it is the
    format render-lint stored before v0.15.10: a different format would invalidate every stored
    check (okengine#1023). It deliberately leaves out the inode, device and ctime, so a restore, a
    vault move or a ``chmod -R`` that changes no content does not re-check the whole vault.

    An UNSETTLED page also carries its content digest: a same-size rewrite in the same timestamp
    tick leaves ``mtime_ns:size`` unchanged, and only the content tells the versions apart
    (okengine#1017). Whether a page has settled is file_fingerprint's decision: its key carries the
    digest exactly while the file is unsettled. A page therefore changes token at most once as it
    settles."""
    key = fingerprint(path, stat, now_ns)
    if key is None:
        return ""
    # key[_DIGEST:] is () for a settled page and (digest,) for an unsettled one.
    return ":".join([str(stat.st_mtime_ns), str(stat.st_size), *key[_DIGEST:]])


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
    except OSError:
        stat = None
    revision = _revision(path, stat) if stat is not None else ""
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
            row = metadata(path.resolve())
            if row["status"].lower() != TOMBSTONED:
                output.append(row)
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
        revision = _revision(resolved, stat)
        if not revision:
            continue
        output.append({"path": relative, "revision": revision})
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
