"""Typed read access to the optional PostgreSQL projection (okengine#566)."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date
from pathlib import Path

READ_DSN = os.environ.get("OKENGINE_PROJECTION_READER_DSN", "")
MAX_AGE_HOURS = float(os.environ.get("OKENGINE_PROJECTION_MAX_AGE_HOURS", "6"))
MAX_LIMIT = int(os.environ.get("OKENGINE_PROJECTION_MAX_LIMIT", "200"))

_pool = None
_pool_loop = None


class ProjectionUnavailable(RuntimeError):
    """The projection cannot safely answer the requested query."""


def as_date(value, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ProjectionUnavailable(f"{field} must be YYYY-MM-DD, got {value!r}") from exc


async def get_pool():
    global _pool, _pool_loop
    loop = asyncio.get_running_loop()
    if _pool is not None and _pool_loop is not loop:
        try:
            _pool.terminate()
        finally:
            _pool = _pool_loop = None
    if _pool is None:
        if not READ_DSN:
            raise ProjectionUnavailable(
                "OKENGINE_PROJECTION_READER_DSN is not configured for this deployment")
        import asyncpg
        _pool = await asyncpg.create_pool(READ_DSN, min_size=1, max_size=4,
                                          command_timeout=30)
        _pool_loop = loop
    return _pool


async def projection_status(*, checked: bool = False) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM v_projection_health")
    if row is None:
        raise ProjectionUnavailable(
            "the projection has never completed; run framework rebuild --projection")
    result = dict(row)
    state = Path(os.environ.get("WIKI_PATH") or "/opt/vault") / ".okengine/corpus"
    if (state / "active.json").exists():
        raise ProjectionUnavailable("corpus mutation is in progress or awaiting recovery")
    try:
        current_epoch = int((state / "epoch").read_text(encoding="ascii").strip())
    except FileNotFoundError:
        current_epoch = 0
    except (OSError, ValueError) as exc:
        raise ProjectionUnavailable("live corpus epoch is unreadable; results withheld") from exc
    projected_epoch = int(result.get("corpus_epoch") or 0)
    result["current_corpus_epoch"] = current_epoch
    result["corpus_lag"] = max(0, current_epoch - projected_epoch)
    age = int(result.get("age_seconds") or 0)
    result["stale"] = age > MAX_AGE_HOURS * 3600
    result["max_age_seconds"] = int(MAX_AGE_HOURS * 3600)
    if checked and result["stale"]:
        raise ProjectionUnavailable(
            f"projection is {age / 3600:.1f}h old (limit {MAX_AGE_HOURS:g}h; "
            f"last successful epoch {result['epoch']} at {result['finished_at']}); "
            "results withheld—investigate the projection rebuild lane")
    if checked and result["corpus_lag"]:
        raise ProjectionUnavailable(
            f"projection lags the canonical corpus by {result['corpus_lag']} epoch(s) "
            f"(projected {projected_epoch}, current {current_epoch}); results withheld")
    return result


def envelope(health: dict, rows: list[dict], matched: int, limit: int,
             filters: dict, object_classes: list[str]) -> dict:
    return {
        "results": rows,
        "matched": matched,
        "returned": len(rows),
        "truncated": matched > len(rows),
        "filters_applied": {key: value for key, value in filters.items()
                            if value not in (None, "")},
        "object_classes_searched": object_classes,
        "projection_epoch": health["epoch"],
        "projection_age_seconds": health["age_seconds"],
        "limit": limit,
    }


async def _query(row_sql: str, count_sql: str, args: list, limit: int,
                 filters: dict, object_classes: list[str]) -> dict:
    started = time.monotonic()
    health = await projection_status(checked=True)
    pool = await get_pool()
    bounded = max(1, min(int(limit), MAX_LIMIT))
    matched = int(await pool.fetchval(count_sql, *args))
    records = await pool.fetch(f"{row_sql} LIMIT {bounded}", *args)
    result = envelope(health, [dict(record) for record in records], matched, bounded,
                      filters, object_classes)
    result["query_latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    result["projection_hits"] = len(records)
    result["pages_scanned"] = 0
    result["bytes_parsed"] = 0
    result["corpus_lag"] = health["corpus_lag"]
    return result


def _page_where(namespace: str, type_: str, status: str, include_tombstoned: bool,
                fm_filters=None, published_after="", published_before="",
                updated_after="", updated_before=""):
    where, args = ["TRUE"], []
    if not include_tombstoned:
        where.append("NOT is_tombstoned")
    for column, value in (("namespace", namespace), ("type", type_), ("status", status)):
        if value:
            args.append(value)
            where.append(f"{column} = ${len(args)}")
    for key, value in sorted((fm_filters or {}).items()):
        if not key or not all(ch.isalnum() or ch == "_" for ch in key):
            raise ProjectionUnavailable(f"invalid frontmatter filter key: {key!r}")
        args.append(str(value))
        where.append(f"fm ->> '{key}' = ${len(args)}")
    for column, value, field in (("published", published_after, "published_after"),
                                 ("published", published_before, "published_before"),
                                 ("updated", updated_after, "updated_after"),
                                 ("updated", updated_before, "updated_before")):
        if value:
            args.append(as_date(value, field))
            operator = ">=" if field.endswith("after") else "<="
            where.append(f"{column} {operator} ${len(args)}")
    return " AND ".join(where), args


async def count_pages(namespace: str = "", type: str = "", status: str = "",
                      include_tombstoned: bool = False,
                      frontmatter=None, published_after="",
                      published_before="", updated_after="", updated_before="") -> dict:
    started = time.monotonic()
    health = await projection_status(checked=True)
    pool = await get_pool()
    where, args = _page_where(namespace, type, status, include_tombstoned, frontmatter,
                              published_after, published_before, updated_after, updated_before)
    # Clause fragments are selected from fixed column/operator allowlists; values stay parameterized.
    count = int(await pool.fetchval(f"SELECT count(*) FROM pages WHERE {where}", *args))  # nosec B608
    return {
        "count": count,
        "complete": True,
        "filters_applied": {"namespace": namespace, "type": type, "status": status,
                            "include_tombstoned": include_tombstoned,
                            "frontmatter": frontmatter or {},
                            "published_after": published_after,
                            "published_before": published_before,
                            "updated_after": updated_after, "updated_before": updated_before},
        "object_classes_searched": ["pages"],
        "projection_epoch": health["epoch"],
        "projection_age_seconds": health["age_seconds"],
        "corpus_lag": health["corpus_lag"], "projection_hits": 1,
        "pages_scanned": 0, "bytes_parsed": 0,
        "query_latency_ms": round((time.monotonic() - started) * 1000, 3),
    }


async def find_pages(namespace: str = "", type: str = "", status: str = "",
                     include_tombstoned: bool = False,
                     frontmatter=None, published_after="",
                     published_before="", updated_after="", updated_before="",
                     order: str = "path", limit: int = 40) -> dict:
    where, args = _page_where(namespace, type, status, include_tombstoned, frontmatter,
                              published_after, published_before, updated_after, updated_before)
    fields = ("path,namespace,canonical_id,slug,type,title,status,is_tombstoned,"
              "published,ingested,updated,fm,indexed_at")
    filters = {"namespace": namespace, "type": type, "status": status,
               "include_tombstoned": include_tombstoned, "frontmatter": frontmatter or {},
               "published_after": published_after, "published_before": published_before,
               "updated_after": updated_after, "updated_before": updated_before, "order": order}
    orders = {"path": "path", "updated_desc": "updated DESC NULLS LAST,path",
              "published_desc": "published DESC NULLS LAST,path"}
    if order not in orders:
        raise ProjectionUnavailable(f"invalid page order: {order!r}")
    order_sql = orders[order]
    # Fields and clauses are code-owned allowlists; caller values are positional parameters.
    return await _query(f"SELECT {fields} FROM pages WHERE {where} ORDER BY {order_sql}",  # nosec B608
                        f"SELECT count(*) FROM pages WHERE {where}", args, limit,  # nosec B608
                        filters, ["pages"])


async def get_page_meta(path_or_id: str) -> dict:
    started = time.monotonic()
    health = await projection_status(checked=True)
    pool = await get_pool()
    path = path_or_id if path_or_id.endswith(".md") else f"{path_or_id}.md"
    rows = await pool.fetch(
        "SELECT path,namespace,canonical_id,slug,type,title,status,is_tombstoned,"
        "superseded_by,published,ingested,updated,fm,body_chars,content_digest,fm_error,"
        "indexed_at FROM pages "
        "WHERE path=$1 OR canonical_id=$2 ORDER BY path LIMIT 2", path, path_or_id)
    if not rows:
        return {"found": False, "query": path_or_id, "projection_epoch": health["epoch"],
                "projection_age_seconds": health["age_seconds"]}
    if len(rows) > 1:
        raise ProjectionUnavailable(f"canonical id {path_or_id!r} is ambiguous")
    result = dict(rows[0])
    result.update(found=True, projection_epoch=health["epoch"],
                  projection_age_seconds=health["age_seconds"],
                  corpus_lag=health["corpus_lag"], projection_hits=1,
                  pages_scanned=0, bytes_parsed=0,
                  query_latency_ms=round((time.monotonic() - started) * 1000, 3))
    return result


async def find_links(target: str = "", source: str = "", resolution: str = "",
                     limit: int = 40) -> dict:
    where, args = ["TRUE"], []
    for column, value in (("target_path", target), ("src_path", source),
                          ("resolution", resolution)):
        if value:
            if column in {"target_path", "src_path"} and not value.endswith(".md"):
                value = f"{value}.md"
            args.append(value)
            where.append(f"{column} = ${len(args)}")
    clause = " AND ".join(where)
    filters = {"target": target, "source": source, "resolution": resolution}
    return await _query(
        f"SELECT src_path,target_ref,target_path,resolution,section FROM links "  # nosec B608
        f"WHERE {clause} ORDER BY src_path,target_ref,section",
        f"SELECT count(*) FROM links WHERE {clause}", args, limit, filters, ["links"])  # nosec B608
