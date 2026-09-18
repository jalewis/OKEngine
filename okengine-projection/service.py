#!/usr/bin/env python3
"""Bootstrap and periodically rebuild OKEngine's PostgreSQL read projection."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from okengine.projection import projector

APP_ROOT = Path(__file__).resolve().parent

WRITER_DSN = os.environ.get("OKENGINE_PROJECTION_WRITER_DSN", "")
READER_PASSWORD = os.environ.get("OKENGINE_PROJECTION_READER_PASSWORD", "")
INTERVAL_SECONDS = max(60, int(os.environ.get("OKENGINE_PROJECTION_INTERVAL_SECONDS", "3600")))
SCHEMA = Path(os.environ.get("OKENGINE_PROJECTION_SCHEMA", "/app/db/projection-schema.sql"))


async def bootstrap(conn) -> None:
    validate_secrets(WRITER_DSN, READER_PASSWORD)
    if not SCHEMA.is_file():
        raise RuntimeError(f"projection schema missing at {SCHEMA}")
    encoding = await conn.fetchval("SHOW server_encoding")
    if encoding != "UTF8":
        raise RuntimeError(f"server_encoding is {encoding}, expected UTF8; recreate PGDATA")
    await conn.execute(SCHEMA.read_text(encoding="utf-8"))
    exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)",
                                 "okengine_reader")
    password = await conn.fetchval("SELECT quote_literal($1)", READER_PASSWORD)
    if exists:
        await conn.execute(f"ALTER ROLE okengine_reader PASSWORD {password}")
    else:
        await conn.execute(f"CREATE ROLE okengine_reader LOGIN PASSWORD {password}")
    await conn.execute("GRANT USAGE ON SCHEMA public TO okengine_reader")
    await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO okengine_reader")
    await conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                       "GRANT SELECT ON TABLES TO okengine_reader")


def validate_secrets(writer_dsn: str, reader_password: str) -> None:
    # Old .env.example copies can be non-empty yet contain publicly shipped
    # placeholders. Reject those as well as Compose fallbacks before creating
    # the reader role; do not print a DSN or password in an error message.
    insecure_writer = {"", "okengine-projection-local", "REPLACE_BEFORE_ENABLING"}
    insecure_reader = {"", "okengine-reader-local", "REPLACE_WITH_A_DIFFERENT_VALUE"}
    if reader_password.strip() in insecure_reader or not reader_password.strip():
        raise RuntimeError("set a non-default OKENGINE_PROJECTION_READER_PASSWORD before starting "
                           "the projection")
    try:
        writer_password = unquote(urlsplit(writer_dsn).password or "")
    except ValueError:
        writer_password = ""
    if writer_password.strip() in insecure_writer or not writer_password.strip():
        raise RuntimeError("set a non-default OKENGINE_PROJECTION_WRITER_PASSWORD before starting "
                           "the projection")
    if writer_password == reader_password:
        raise RuntimeError("set distinct OKENGINE_PROJECTION_WRITER_PASSWORD and "
                           "OKENGINE_PROJECTION_READER_PASSWORD before starting the projection")


async def rebuild(*, force_reconcile: bool = False) -> dict:
    import asyncpg
    if not WRITER_DSN:
        raise RuntimeError("OKENGINE_PROJECTION_WRITER_DSN is not set")
    conn = await asyncpg.connect(WRITER_DSN)
    try:
        await bootstrap(conn)
        return await projector.project(
            conn, projector.VAULT, force_reconcile=force_reconcile)
    finally:
        await conn.close()


async def health_check() -> dict:
    """Return health facts or raise with a rate-framed actionable failure."""
    import asyncpg
    if not WRITER_DSN:
        raise RuntimeError("OKENGINE_PROJECTION_WRITER_DSN is not set")
    conn = await asyncpg.connect(WRITER_DSN)
    try:
        health = await conn.fetchrow("SELECT * FROM v_projection_health")
        max_age = float(os.environ.get("OKENGINE_PROJECTION_MAX_AGE_HOURS", "6")) * 3600
        latest = await conn.fetchrow(
            "SELECT epoch,ok,error,finished_at FROM projection_runs ORDER BY epoch DESC LIMIT 1")
        scan = projector.scan_vault(projector.VAULT)
        rows = int(await conn.fetchval("SELECT count(*) FROM pages"))
        files = len(scan.pages)
        churn = churn_since(scan.pages, health["finished_at"] if health else None)
        require_basic_health(health, max_age, latest, rows, files, churn)
        sample_size = int(os.environ.get("OKENGINE_PROJECTION_DIGEST_SAMPLE", "200"))
        if sample_size < 1:
            raise RuntimeError("OKENGINE_PROJECTION_DIGEST_SAMPLE must be at least 1")
        sample = await conn.fetch(
            "SELECT path,content_digest,indexed_at FROM pages ORDER BY random() LIMIT $1",
            sample_size)
        mismatches, comparable, vanished = [], 0, []
        for row in sample:
            verdict, detail = classify_sampled_page(
                projector.VAULT / "wiki" / row["path"], row["content_digest"], row["indexed_at"])
            if verdict == "vanished":
                vanished.append(row["path"])
            elif verdict == "mismatch":
                mismatches.append(row["path"] if detail is None else f"{row['path']} ({detail})")
            elif verdict == "comparable":
                comparable += 1
        require_digest_health(mismatches, comparable)
        return {"epoch": health["epoch"], "age_seconds": health["age_seconds"],
                "projected": rows, "eligible_files": files, "comparable": comparable,
                "written_since_epoch": churn, "deleted_since_epoch": len(vanished),
                "scan_errors": len(scan.unreadable)}
    finally:
        await conn.close()


def classify_sampled_page(path, digest: str, indexed_at) -> tuple[str, str | None]:
    """One sampled row against the file it projects: comparable | mismatch | vanished | newer.

    `vanished` is the case this function exists to separate. A page DELETED after the epoch is
    churn, not corruption — the row is one epoch behind reality and the next rebuild drops it.
    Bucketing it with digest mismatches made the health check fail on every run of any vault that
    deletes anything: 412 of 48,968 rows on one live vault, every one a page a cleanup lane had
    removed within the hour, reported as `digest drift ... (FileNotFoundError)` when nothing had
    been compared at all. The COUNT check already bounds exactly this by observed churn; this is
    the same reasoning applied to the same facts, and a projection that keeps stale rows for good
    is still caught there, by row-vs-file count.

    Present-but-unreadable stays a mismatch: a permissions or I/O error is not a deletion, and
    waving it through would hide the one case where the file is there and cannot be trusted.

    `newer` is the pre-existing skip: a file modified after `indexed_at` was legitimately written
    since the epoch, so its digest is expected to differ and proves nothing either way.
    """
    try:
        stat = path.stat()
        raw = path.read_bytes()
    except FileNotFoundError:
        return "vanished", None
    except OSError as exc:
        return "mismatch", type(exc).__name__
    if datetime.fromtimestamp(stat.st_mtime, timezone.utc) > indexed_at:
        return "newer", None
    if hashlib.sha256(raw).hexdigest() != digest:
        return "mismatch", None
    return "comparable", None


def churn_since(pages: list[dict], finished_at) -> int:
    """Eligible pages written since the projection finished — the corpus moved under it.

    `rows` is a count as of an EPOCH; `files` is a count of the filesystem NOW. On a vault
    that ingests continuously those are never taken at the same instant, so comparing them
    for equality reports drift for every file that merely arrived in between. Measured on a
    live 48,000-page vault the difference was ±1 against a projection 40 minutes into a
    one-hour cycle — a FAIL on every check, for a reason unrelated to correctness. A gate
    that fails routinely stops being read, which is the failure it exists to catch.

    So bound the discrepancy by observable churn rather than by a fixed fudge factor: a page
    written after `finished_at` cannot be in that epoch. Creations inflate `files`; a reshelve
    (delete + create at a new path) leaves a stale row and inflates `rows`; both touch mtimes,
    so this bounds either direction by how much the corpus actually moved. A discrepancy
    LARGER than the churn is real divergence and still fails.

    This mirrors the digest check below, which already skips a sampled page whose mtime is
    newer than its `indexed_at` for exactly the same reason.
    """
    if finished_at is None:
        return 0
    total = 0
    for page in pages:
        mtime = page.get("file_mtime")
        if mtime is not None and mtime > finished_at:
            total += 1
    return total


def require_basic_health(health, max_age: float, latest, rows: int, files: int,
                         churn: int = 0) -> None:
    if health is None:
        raise RuntimeError("projection has never completed a successful epoch")
    if int(health["age_seconds"] or 0) > max_age:
        raise RuntimeError(f"projection is {health['age_seconds']}s old; limit is {max_age:g}s")
    # `ok` is NOT NULL DEFAULT false, so a row is `ok=false` from the moment an epoch STARTS
    # until it completes. Reading that as a failed run reports every rebuild in progress as a
    # failure — and fires exactly when an operator is most likely to be looking, in the minutes
    # after a deploy or a container restart, while the first epoch is still running. Observed
    # live: `last projection epoch 159 failed: no error recorded` seconds after a roll, on an
    # epoch the database recorded as ok=true once it finished.
    #
    # `finished_at` is what separates the two. An epoch still running is not a failed epoch; one
    # that died without finishing leaves its row behind, but the NEXT epoch supersedes it as
    # `latest`, and a projector that never runs again is caught by the age check above.
    #
    # The old message said "no error recorded" out loud and nobody heard it: a run that failed
    # has an error, and a run with no error had not failed.
    if latest and not latest["ok"] and latest["finished_at"] is not None:
        raise RuntimeError(f"last projection epoch {latest['epoch']} failed: "
                           f"{latest['error'] or 'no error recorded'}")
    drift = abs(rows - files)
    if drift > churn:
        raise RuntimeError(f"count drift: {rows} projected vs {files} eligible files "
                           f"({drift} of {files or 1}); {churn} page(s) were written since the "
                           f"projection finished, leaving {drift - churn} unexplained")


def require_digest_health(mismatches: list[str], comparable: int) -> None:
    if mismatches:
        raise RuntimeError(f"digest drift: {len(mismatches)} of {comparable} comparable "
                           f"sampled pages; examples: {', '.join(mismatches[:3])}")
    if comparable == 0:
        raise RuntimeError("digest health verified nothing: comparable=0")


async def verify_reproducible() -> dict:
    """Rebuild in a scratch schema and compare digest-anchored semantic rows and links."""
    import asyncpg
    scratch = "okengine_verify_projection"
    conn = await asyncpg.connect(WRITER_DSN)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {scratch} CASCADE")
        await conn.execute(f"CREATE SCHEMA {scratch}")
        ddl = SCHEMA.read_text(encoding="utf-8")
        await conn.execute(f"SET search_path TO {scratch}")
        await conn.execute(ddl)
        stats = await projector.run_epoch(conn, projector.scan_vault(projector.VAULT))
        row_diff = await conn.fetch(f"""
            SELECT live.path FROM public.pages live
            JOIN {scratch}.pages rebuilt USING (path, content_digest)
            WHERE (live.namespace,live.canonical_id,live.slug,live.type,live.title,live.status,
                   live.is_tombstoned,live.superseded_by,live.fm::text,live.body_chars,live.fm_error)
              IS DISTINCT FROM
                  (rebuilt.namespace,rebuilt.canonical_id,rebuilt.slug,rebuilt.type,rebuilt.title,
                   rebuilt.status,rebuilt.is_tombstoned,rebuilt.superseded_by,rebuilt.fm::text,
                   rebuilt.body_chars,rebuilt.fm_error)
            LIMIT 20""")  # nosec B608
        link_diff = await conn.fetch(f"""
            WITH comparable AS (
              SELECT live.path FROM public.pages live
              JOIN {scratch}.pages rebuilt USING (path, content_digest))
            SELECT path FROM comparable WHERE
              (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.target_ref,x.section)
                 FROM public.links x WHERE x.src_path=comparable.path)
              IS DISTINCT FROM
              (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.target_ref,x.section)
                 FROM {scratch}.links x WHERE x.src_path=comparable.path)
            LIMIT 20""")  # nosec B608
        comparable = int(await conn.fetchval(f"""
            SELECT count(*) FROM public.pages live
            JOIN {scratch}.pages rebuilt USING (path, content_digest)"""))  # nosec B608
        require_reproducible(comparable, row_diff, link_diff)
        return {"comparable": comparable, "row_mismatches": 0, "link_mismatches": 0,
                "scratch_epoch": stats["epoch"]}
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {scratch} CASCADE")
        await conn.close()


def require_reproducible(comparable: int, row_diff, link_diff) -> None:
    if comparable == 0:
        raise RuntimeError("reproducibility verified nothing: comparable=0")
    if row_diff or link_diff:
        raise RuntimeError(f"projection is non-deterministic: {len(row_diff)} row and "
                           f"{len(link_diff)} link mismatches")


async def run_service(once: bool, *, force_reconcile: bool = False) -> int:
    while True:
        try:
            stats = (await rebuild(force_reconcile=True)
                     if force_reconcile else await rebuild())
            print(json.dumps(stats, sort_keys=True), flush=True)
        except Exception as exc:
            print(f"projection rebuild failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            if once:
                return 1
        if once:
            return 0
        await asyncio.sleep(INTERVAL_SECONDS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--reconcile", action="store_true")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        if args.health:
            print(json.dumps(asyncio.run(health_check()), sort_keys=True))
            return 0
        if args.verify:
            print(json.dumps(asyncio.run(verify_reproducible()), sort_keys=True))
            return 0
        return asyncio.run(run_service(args.once or args.reconcile,
                                       force_reconcile=args.reconcile))
    except Exception as exc:
        print(f"projection operation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
