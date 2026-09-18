#!/usr/bin/env python3
"""Rebuild the canonical Markdown vault into OKEngine's PostgreSQL read projection."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from okengine.corpus_transaction import read_epoch as corpus_epoch

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
DSN = os.environ.get("OKENGINE_PROJECTION_WRITER_DSN", "")
MAX_DELETE_PCT = float(os.environ.get("OKENGINE_PROJECTION_MAX_DELETE_PCT", "2"))
MAX_DELETE_ABS = int(os.environ.get("OKENGINE_PROJECTION_MAX_DELETE_ABS", "50"))
BATCH = int(os.environ.get("OKENGINE_PROJECTION_BATCH", "2000"))

_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)", re.S)
_WIKILINK = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
_RESERVED = {"index.md", "log.md", "agents.md", "hot.md", "bundle.md", "health.md"}


def _monotonic():
    return time.monotonic()


@dataclass(frozen=True)
class Link:
    target_ref: str
    section: str


@dataclass
class Scan:
    pages: list[dict]
    links: dict[str, list[Link]]
    skipped_dirs: list[str]
    unreadable: list[str]
    excluded: int = 0
    # When the walk STARTED reading files. `indexed_at` is stamped from this rather than from the
    # moment each row is written, because those are minutes apart on a large vault and consumers
    # ask "was this file changed after we looked at it?" — see run_epoch.
    scanned_at: "datetime | None" = None
    bytes_parsed: int = 0


def _norm(value):
    text = str(value).strip() if value is not None else ""
    return text or None


def _aliases(fm: dict) -> list[str]:
    values = fm.get("aliases") or []
    if isinstance(values, str):
        values = [part.strip() for part in values.split(",")]
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _to_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def parse_page(rel: str, raw: bytes) -> tuple[dict, list[Link]]:
    """Parse one page without dropping malformed frontmatter or frontmatter links."""
    text = raw.decode("utf-8", errors="replace")
    match = _FM.match(text)
    fm: dict = {}
    fm_error = None
    body = text
    header = ""
    if match:
        header = match.group(1)
        body = text[match.end():]
        try:
            loaded = yaml.safe_load(header)
            if isinstance(loaded, dict):
                fm = loaded
            elif loaded is not None:
                fm_error = f"frontmatter is {type(loaded).__name__}, not a mapping"
        except yaml.YAMLError as exc:
            fm_error = str(exc)[:500]
    else:
        fm_error = "no frontmatter block"

    status = _norm(fm.get("status"))
    row = {
        "path": rel,
        "namespace": rel.split("/", 1)[0] if "/" in rel else "",
        "canonical_id": _norm(fm.get("id")),
        "slug": Path(rel).stem,
        "type": _norm(fm.get("type")),
        "title": _norm(fm.get("title") or fm.get("name")),
        "status": status,
        "is_tombstoned": (status or "").lower() == "tombstoned",
        "superseded_by": _norm(fm.get("superseded_by")),
        "published": _to_date(fm.get("published")),
        "ingested": _to_date(fm.get("ingested")),
        "updated": _to_date(fm.get("updated") or fm.get("last_updated")),
        "fm": json.dumps(fm, default=str, sort_keys=True),
        "body_chars": len(body),
        "fm_error": fm_error,
        "aliases": _aliases(fm),
    }
    links = [Link(m.group(1).strip(), "(frontmatter)")
             for m in _WIKILINK.finditer(header) if m.group(1).strip()]
    section = ""
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading:
            section = heading.group(1)[:200]
            continue
        links.extend(Link(m.group(1).strip(), section)
                     for m in _WIKILINK.finditer(line) if m.group(1).strip())
    return row, list(dict.fromkeys(links))


def selected_namespaces(vault: Path) -> set[str]:
    schema = {}
    for schema_path in (vault / "wiki" / "schema.yaml", vault / "schema.yaml"):
        try:
            schema = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
            break
        except (OSError, yaml.YAMLError):
            continue
    excluded = {"operational", "dashboards"}
    for value in schema.get("exclude") or []:
        parts = str(value).strip("/").split("/")
        namespace = parts[1] if parts and parts[0] == "wiki" and len(parts) > 1 else parts[-1]
        if namespace:
            excluded.add(namespace)
    declared = (schema.get("partitioning") or {}).get("namespaces")
    names = (set(declared) if isinstance(declared, dict) else set()) - excluded
    if names:
        return names
    wiki = vault / "wiki"
    return {path.name for path in wiki.iterdir()
            if path.is_dir() and not path.name.startswith((".", "_"))
            and path.name not in excluded}


def scan_vault(vault: Path = VAULT, *, known=None, prior_links=None) -> Scan:
    wiki = vault / "wiki"
    if not wiki.is_dir():
        raise ValueError(f"vault not found at {wiki}; refusing to project an empty corpus")
    namespaces = selected_namespaces(vault)
    scan = Scan([], {}, [], [], scanned_at=datetime.now(timezone.utc))
    for path in sorted(wiki.rglob("*.md")):
        rel = path.relative_to(wiki).as_posix()
        if not path.is_file():
            scan.skipped_dirs.append(rel)
            continue
        if rel.split("/", 1)[0] not in namespaces or path.name.lower() in _RESERVED \
                or path.name.startswith((".", "_")) or ".bak." in path.name:
            scan.excluded += 1
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            scan.unreadable.append(f"{rel}: {type(exc).__name__}")
            continue
        scan.bytes_parsed += len(raw)
        digest = hashlib.sha256(raw).hexdigest()
        previous = (known or {}).get(rel)
        if previous and previous.get("content_digest") == digest:
            fm = previous.get("fm") or {}
            if isinstance(fm, str):
                fm = json.loads(fm)
            row = {key: previous.get(key) for key in (
                "path", "namespace", "canonical_id", "slug", "type", "title", "status",
                "is_tombstoned", "superseded_by", "published", "ingested", "updated",
                "body_chars", "fm_error", "file_mtime")}
            row.update(fm=json.dumps(fm, default=str, sort_keys=True), aliases=_aliases(fm),
                       content_digest=digest, _unchanged=True)
            links = list((prior_links or {}).get(rel, []))
            scan.pages.append(row)
            scan.links[rel] = links
            continue
        row, links = parse_page(rel, raw)
        row["content_digest"] = digest
        row["_unchanged"] = False
        try:
            row["file_mtime"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            row["file_mtime"] = None
        scan.pages.append(row)
        scan.links[rel] = links
    return scan


async def load_projection_state(conn):
    fields = ("path,namespace,canonical_id,slug,type,title,status,is_tombstoned,"
              "superseded_by,published,ingested,updated,fm,body_chars,content_digest,"
              "fm_error,file_mtime")
    # The field list is a code-owned constant, never deployment or caller input.
    known = {row["path"]: dict(row)
             for row in await conn.fetch(f"SELECT {fields} FROM pages")}  # nosec B608
    links: dict[str, list[Link]] = {}
    for row in await conn.fetch("SELECT src_path,target_ref,section FROM links"):
        links.setdefault(row["src_path"], []).append(Link(row["target_ref"], row["section"]))
    return known, links


def identity_indexes(pages: Iterable[dict]):
    paths, by_id, by_alias, by_slug = set(), {}, {}, {}
    for row in pages:
        path = row["path"]
        paths.add(path)
        if row.get("canonical_id"):
            by_id.setdefault(row["canonical_id"], []).append(path)
        for alias in row.get("aliases") or []:
            by_alias.setdefault(alias, []).append(path)
        by_slug.setdefault(row["slug"], []).append(path)
    return paths, by_id, by_alias, by_slug


def resolve_link(ref: str, indexes):
    paths, by_id, by_alias, by_slug = indexes
    candidate = ref if ref.endswith(".md") else f"{ref}.md"
    if candidate in paths:
        return candidate, "exact"
    for method, index in (("id", by_id), ("alias", by_alias)):
        hits = index.get(ref, [])
        if len(hits) == 1:
            return hits[0], method
        if len(hits) > 1:
            return None, "ambiguous"
    hits = by_slug.get(Path(ref).stem, [])
    if len(hits) > 1 and "/" in ref:
        namespace = ref.split("/", 1)[0]
        hits = [path for path in hits if path.split("/", 1)[0] == namespace]
    if len(hits) == 1:
        return hits[0], "slug"
    return (None, "ambiguous") if hits else (None, "unresolved")


def page_identity(row) -> str:
    """What makes a page the SAME page across a move: its canonical id, else its bytes.

    A page with no `id:` cannot be tracked by identity, so it falls back to its content digest —
    a reshelve renames the file without touching a byte, so the digest follows it. Only a page
    whose id AND bytes both leave the vault is genuinely gone.
    """
    return row["canonical_id"] or f"digest:{row['content_digest']}"


def classify_removals(known_rows, stale_paths: set, scanned: list) -> tuple[list, list]:
    """Split stale PATHS into pages that VANISHED and pages that merely MOVED.

    The deletion guard below exists for one failure: a vault that did not mount looks like a vault
    whose every page was deleted, and projecting that would drop the whole table. It bounded that
    by counting stale PATHS — and a reshelve renames a file, which to a path-keyed diff is
    indistinguishable from a deletion.

    Measured on a live deployment (okengine#605): a partition deepening from one-letter to
    two-letter shards moved 160 of 2,749 pages, the guard read 5.8% deletion and refused, and the
    projection stopped rebuilding for hours while every post-deploy check FAILed on the resulting
    count drift. All 160 were present at their new paths; nothing had been deleted. The failure
    text sent the operator to "verify the vault mount", which was healthy the whole time.

    This is the same lesson `classify_sampled_page` learned in the projection service one layer
    up — *a page deleted after the epoch is churn, not corruption* — applied to its sibling:
    **a page moved is not a page removed.** An unmounted vault still loses every IDENTITY, so the
    guard keeps the teeth it was given.
    """
    live = {page_identity(row) for row in scanned}
    vanished, moved = [], []
    for row in known_rows:
        if row["path"] in stale_paths:
            (moved if page_identity(row) in live else vanished).append(row["path"])
    return vanished, moved


async def run_epoch(conn, scan: Scan, *, dry_run: bool = False,
                    source_epoch: int | None = None, mode: str = "reconcile") -> dict:
    started = _monotonic()
    known_rows = await conn.fetch("SELECT path, content_digest, canonical_id FROM pages")
    known = {row["path"]: row["content_digest"] for row in known_rows}
    seen = {row["path"] for row in scan.pages}
    stale_paths = set(known) - seen
    vanished, moved = classify_removals(known_rows, stale_paths, scan.pages)
    pct = 100 * len(vanished) / len(known) if known else 0.0
    if known and len(vanished) > MAX_DELETE_ABS and pct > MAX_DELETE_PCT:
        raise RuntimeError(
            f"projection would remove {len(vanished)} of {len(known)} pages ({pct:.1f}%) whose id "
            f"and content are both absent from the vault; {len(moved)} other row(s) only moved "
            f"and are not counted here. Verify the vault mount and deletion intent")
    stats = {
        "files_seen": len(scan.pages), "unchanged": 0, "reparsed": 0,
        "removed": len(stale_paths), "moved": len(moved), "vanished": len(vanished),
        "links": 0, "fm_errors": 0,
        "skipped_dirs": scan.skipped_dirs, "unreadable": scan.unreadable,
        "excluded": scan.excluded,
        "pages_scanned": len(scan.pages), "projection_hits": 0,
        "bytes_parsed": scan.bytes_parsed, "mode": mode,
    }
    if dry_run:
        stats["elapsed_seconds"] = round(_monotonic() - started, 3)
        return stats

    # The snapshot's as-of time: when the walk began READING, not when these rows land. On a
    # 50,000-page vault those differ by minutes, and stamping the write time made every page
    # modified mid-scan look like corruption to the digest check — the file was read before it
    # changed, so the stored digest is one epoch behind, which is churn rather than drift. Falls
    # back to now() for a Scan built without one (a hand-assembled scan in a test or a caller).
    snapshot_at = scan.scanned_at or datetime.now(timezone.utc)
    source_epoch = corpus_epoch(VAULT) if source_epoch is None else source_epoch
    epoch = await conn.fetchval(
        "INSERT INTO projection_runs (corpus_epoch,mode) VALUES ($1,$2) RETURNING epoch",
        source_epoch, mode)
    indexes = identity_indexes(scan.pages)
    async with conn.transaction():
        for row in scan.pages:
            unchanged = bool(row.get("_unchanged",
                                     known.get(row["path"]) == row["content_digest"]))
            stats["unchanged" if unchanged else "reparsed"] += 1
            stats["fm_errors"] += bool(row["fm_error"])
            values = [row[name] for name in (
                "path", "namespace", "canonical_id", "slug", "type", "title", "status",
                "is_tombstoned", "superseded_by", "fm", "body_chars", "content_digest",
                "fm_error", "file_mtime")]
            await conn.execute(
                "INSERT INTO pages (path,namespace,canonical_id,slug,type,title,status,"
                "is_tombstoned,superseded_by,published,ingested,updated,fm,body_chars,"
                "content_digest,fm_error,file_mtime,epoch,indexed_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15,$16,$17,$18,$19) "
                "ON CONFLICT (path) DO UPDATE SET namespace=EXCLUDED.namespace,"
                "canonical_id=EXCLUDED.canonical_id,slug=EXCLUDED.slug,type=EXCLUDED.type,"
                "title=EXCLUDED.title,status=EXCLUDED.status,is_tombstoned=EXCLUDED.is_tombstoned,"
                "superseded_by=EXCLUDED.superseded_by,published=EXCLUDED.published,"
                "ingested=EXCLUDED.ingested,updated=EXCLUDED.updated,fm=EXCLUDED.fm,"
                "body_chars=EXCLUDED.body_chars,"
                "content_digest=EXCLUDED.content_digest,fm_error=EXCLUDED.fm_error,"
                "file_mtime=EXCLUDED.file_mtime,epoch=EXCLUDED.epoch,indexed_at=$19",
                *values[:9], row["published"], row["ingested"], row["updated"], *values[9:], epoch,
                snapshot_at)
        await conn.execute("DELETE FROM pages WHERE epoch < $1", epoch)
        await conn.execute("DELETE FROM links")
        link_rows = []
        for src, links in scan.links.items():
            for link in links:
                target, resolution = resolve_link(link.target_ref, indexes)
                link_rows.append((src, link.target_ref, target, resolution, link.section))
        stats["links"] = len(link_rows)
        for offset in range(0, len(link_rows), BATCH):
            await conn.executemany(
                "INSERT INTO links (src_path,target_ref,target_path,resolution,section) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                link_rows[offset:offset + BATCH])
        stats["projection_hits"] = stats["unchanged"]
        stats["pages_scanned"] = stats["reparsed"]
        stats["elapsed_seconds"] = round(_monotonic() - started, 3)
        await conn.execute("UPDATE projection_runs SET finished_at=now(),ok=true,stats=$2::jsonb "
                           "WHERE epoch=$1", epoch, json.dumps(stats, sort_keys=True))
    stats["epoch"] = epoch
    stats["corpus_epoch"] = source_epoch
    return stats


def journal_delta(vault: Path, after_epoch: int) -> tuple[int, set[str]] | None:
    """Return a contiguous hash-journal delta, or None when reconciliation is required."""
    current = corpus_epoch(vault)
    if current == after_epoch:
        return current, set()
    journal = vault / ".okengine/corpus/journal.jsonl"
    try:
        records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
    except (OSError, ValueError, TypeError):
        return None
    relevant = [row for row in records if int(row.get("epoch", -1)) > after_epoch]
    epochs = sorted({int(row.get("epoch", -1)) for row in relevant})
    if epochs != list(range(after_epoch + 1, current + 1)):
        return None
    paths = {str(item.get("path")) for row in relevant
             for item in row.get("affected_paths", [])
             if str(item.get("path", "")).startswith("wiki/")
             and str(item.get("path", "")).endswith(".md")}
    return current, paths


def scan_changed(vault: Path, paths: set[str]) -> Scan:
    """Parse only journal-identified live pages; absent paths are deletion tombstones."""
    scan = Scan([], {}, [], [], scanned_at=datetime.now(timezone.utc))
    namespaces = selected_namespaces(vault)
    for relative in sorted(paths):
        path = vault / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        scan.bytes_parsed += len(raw)
        rel = path.relative_to(vault / "wiki").as_posix()
        if rel.split("/", 1)[0] not in namespaces or path.name.lower() in _RESERVED \
                or path.name.startswith((".", "_")) or ".bak." in path.name:
            scan.excluded += 1
            continue
        row, links = parse_page(rel, raw)
        row.update(content_digest=hashlib.sha256(raw).hexdigest(), _unchanged=False,
                   file_mtime=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
        scan.pages.append(row)
        scan.links[rel] = links
    return scan


async def project(conn, vault: Path = VAULT, *, force_reconcile: bool = False) -> dict:
    """Apply a journal-fed epoch when possible, retaining full reconciliation as the backstop."""
    async def reconcile() -> dict:
        known, links = await load_projection_state(conn)
        state = vault / ".okengine/corpus"
        for _attempt in range(3):
            before = corpus_epoch(vault)
            if (state / "active.json").exists():
                continue
            scan = scan_vault(vault, known=known, prior_links=links)
            after = corpus_epoch(vault)
            if before == after and not (state / "active.json").exists():
                return await run_epoch(conn, scan, source_epoch=after)
        raise RuntimeError("canonical corpus changed during projection reconciliation; retry")

    if force_reconcile:
        return await reconcile()

    checkpoint = await conn.fetchval(
        "SELECT corpus_epoch FROM projection_runs WHERE ok AND finished_at IS NOT NULL "
        "ORDER BY epoch DESC LIMIT 1")
    current = corpus_epoch(vault)
    if checkpoint is None or (int(checkpoint) == 0 and current > 0):
        return await reconcile()
    delta = journal_delta(vault, int(checkpoint))
    if delta is None:
        return await reconcile()
    source_epoch, affected = delta
    known, prior_links = await load_projection_state(conn)
    # Seed from database rows, then replace only journal-touched paths with filesystem parses.
    relative = {path.removeprefix("wiki/") for path in affected}
    pages = []
    links = {}
    for path, previous in known.items():
        if path in relative:
            continue
        fm = previous.get("fm") or {}
        if isinstance(fm, str):
            fm = json.loads(fm)
        row = dict(previous)
        row.update(fm=json.dumps(fm, default=str, sort_keys=True), aliases=_aliases(fm),
                   _unchanged=True)
        pages.append(row)
        links[path] = list(prior_links.get(path, []))
    changed = scan_changed(vault, affected)
    pages.extend(changed.pages)
    links.update(changed.links)
    combined = Scan(pages, links, changed.skipped_dirs, changed.unreadable,
                    excluded=changed.excluded, scanned_at=changed.scanned_at,
                    bytes_parsed=changed.bytes_parsed)
    return await run_epoch(conn, combined, source_epoch=source_epoch, mode="incremental")


async def _run(args) -> int:
    if not DSN:
        print("OKENGINE_PROJECTION_WRITER_DSN is not set", file=sys.stderr)
        return 2
    import asyncpg
    conn = await asyncpg.connect(DSN)
    try:
        if args.dry_run:
            known, links = await load_projection_state(conn)
            stats = await run_epoch(conn, scan_vault(known=known, prior_links=links), dry_run=True)
        else:
            stats = await project(conn)
    finally:
        await conn.close()
    print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        print(f"projection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
