#!/usr/bin/env python3
"""Real-PostgreSQL contract test for the optional read projection (okengine#566)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[1]
WRITER_DSN = os.environ["OKENGINE_PROJECTION_WRITER_DSN"]
READER_DSN = os.environ["OKENGINE_PROJECTION_READER_DSN"]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def concurrent_projection_contract(projector, admin, vault, query):
    """Bounded, real lock-order reproduction, followed by the serialized public path."""
    a, b = await asyncpg.connect(WRITER_DSN), await asyncpg.connect(WRITER_DSN)
    tasks = []
    try:
        await a.execute("SET application_name='a'")
        await b.execute("SET application_name='b'")
        # AFTER the first UPSERT has acquired its row lock, hold each writer at
        # an independent fixture gate. Opposite scans then lock the other's row.
        await admin.execute("""
            CREATE FUNCTION fixture_projection_gate() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF (current_setting('application_name')='a' AND NEW.path='entities/a/acme.md')
                 OR (current_setting('application_name')='b' AND NEW.path='sources/report.md') THEN
                PERFORM pg_advisory_lock(950, CASE current_setting('application_name') WHEN 'a' THEN 1 ELSE 2 END);
                PERFORM pg_advisory_unlock(950, CASE current_setting('application_name') WHEN 'a' THEN 1 ELSE 2 END);
              END IF;
              RETURN NEW;
            END $$;
            CREATE TRIGGER fixture_projection_gate AFTER INSERT OR UPDATE ON pages
            FOR EACH ROW EXECUTE FUNCTION fixture_projection_gate();
        """)
        # Notifications are delivered only at commit, so observe the fixture
        # advisory waits directly instead of depending on a timing delay.
        await admin.execute("SELECT pg_advisory_lock(950,1),pg_advisory_lock(950,2)")
        cold = projector.scan_vault(vault)
        reverse = projector.Scan(list(reversed(cold.pages)), cold.links, [], [],
                                 scanned_at=cold.scanned_at)
        tasks = [asyncio.create_task(projector._run_epoch(a, cold)),
                 asyncio.create_task(projector._run_epoch(b, reverse))]
        async def both_waiting():
            for _ in range(1000):
                await admin.execute("SELECT pg_stat_clear_snapshot()")
                n = await admin.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid=ANY($1::int[]) "
                    "AND wait_event='advisory'", [a.get_server_pid(), b.get_server_pid()])
                if n == 2:
                    return
            raise AssertionError("writers did not reach both fixture lock barriers")
        await asyncio.wait_for(both_waiting(), 10)
        await admin.execute("SELECT pg_advisory_unlock(950,1),pg_advisory_unlock(950,2)")
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 15)
        assert sum(isinstance(result, asyncpg.DeadlockDetectedError) for result in results) == 1
        assert sum(isinstance(result, dict) for result in results) == 1
        print(json.dumps({"negative_lock_order": "deadlock_detected",
                          "statements": ["UPSERT pages acme/report in opposite order",
                                         "DELETE stale pages", "DELETE links"]}))
        await admin.execute("DROP TRIGGER fixture_projection_gate ON pages; DROP FUNCTION fixture_projection_gate()")
        async with projector.mutation_lock(admin):
            tasks = [asyncio.create_task(projector.project(a, vault, force_reconcile=True)),
                     asyncio.create_task(projector.project(b, vault))]
            await asyncio.wait_for(both_waiting(), 10)
            assert await admin.fetchval("SELECT count(*) FROM pages") == 3
            await admin.execute("UPDATE pages SET title=title WHERE path='entities/a/acme.md'")
            read = await asyncio.wait_for(query.count_pages(include_tombstoned=True), 5)
            assert read["count"] == 3 and read["complete"], "writer lock must not pause readers"
            tasks[0].cancel()
            try:
                await tasks[0]
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("waiting reconciliation did not cancel")
            tasks[0] = asyncio.create_task(projector.project(a, vault, force_reconcile=True))
            await asyncio.wait_for(both_waiting(), 10)
        results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert all(result["files_seen"] == 3 for result in results)
        # Both consumers run after both transactions have committed, then repeat.
        for _ in range(2):
            result = await query.count_pages(include_tombstoned=True)
            assert result["count"] == 3 and result["complete"] and result["corpus_lag"] == 0
            for row in cold.pages:
                found = await query.get_page_meta(row["path"])
                assert found["found"] and found["content_digest"] == row["content_digest"]
        print(json.dumps({"serialized_concurrency": "passed", "population": 3,
                          "epochs": [r["epoch"] for r in results], "late_reads": 2}))
    finally:
        await admin.execute("SELECT pg_advisory_unlock_all()")
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await a.close()
        await b.close()


async def owner_readability_contract(service, admin, vault):
    """A real 0600 page, real different kernel UIDs, and real PostgreSQL consumers."""
    import subprocess
    assert os.geteuid() == 0, "required UID boundary fixture needs a root disposable CI job"
    vault.chmod(0o755)
    page = vault / "wiki/entities/private.md"
    page.write_text("---\nid: private-page\ntype: company\ntitle: Private\n---\nowner-only\n")
    os.chown(page, 31003, 31003)
    page.chmod(0o600)
    child = r"""
import asyncio,importlib.util,json,os,sys
from pathlib import Path
from okengine.projection import projector
from okengine.mcp import projection
spec=importlib.util.spec_from_file_location('owner_service',sys.argv[1])
service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)
async def run():
    if os.getuid()==31004:
        for operation in (lambda: service.rebuild(force_reconcile=True), service.health_check):
            try: await operation()
            except RuntimeError as exc:
                assert 'unreadable' in str(exc),str(exc)
            else: raise AssertionError('unreadable population certified as successful')
        scan=projector.scan_changed(Path(os.environ['WIKI_PATH']), {'wiki/entities/private.md'})
        try: projector.require_readable_scan(scan)
        except RuntimeError as exc: assert 'unreadable' in str(exc),str(exc)
        else: raise AssertionError('journal refresh dropped an owner-only page')
        print(json.dumps({'different_uid':'rejected','incremental_unknown':'rejected'}))
    else:
        for _ in range(2):
            stats=await service.rebuild(force_reconcile=True)
            health=await service.health_check()
            found=await projection.get_page_meta('entities/private.md')
            count=await projection.count_pages(include_tombstoned=True)
            assert stats['files_seen']==4 and health['scan_errors']==0 and health['projected']==4
            assert found['found'] and count['count']==4 and count['complete']
        await projection._pool.close()
        print(json.dumps({'owner_uid':'passed','population':4,'late_reads':2}))
asyncio.run(run())
"""
    env = {**os.environ, "WIKI_PATH": str(vault), "PGSSLMODE": "disable",
           "OKENGINE_PROJECTION_READER_PASSWORD": "reader-test",
           "OKENGINE_PROJECTION_SCHEMA": str(REPO / "db/projection-schema.sql")}
    before = await admin.fetchval("SELECT max(epoch) FROM projection_runs WHERE ok")
    for uid in (31004, 31003):
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-c", child, str(REPO / "okengine-projection/service.py")],
            env=env, user=uid, group=uid, extra_groups=[], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        print(result.stdout.strip())
        if uid == 31004:
            assert await admin.fetchval("SELECT count(*) FROM pages") == 3
            assert await admin.fetchval("SELECT max(epoch) FROM projection_runs WHERE ok") == before


async def main() -> None:
    projector = load("postgres_projection_integration",
                     REPO / "src/okengine/projection/projector.py")
    query = load("projection_query_integration", REPO / "src/okengine/mcp/projection.py")
    service = load("projection_service_integration", REPO / "okengine-projection/service.py")
    query.READ_DSN = READER_DSN
    service.WRITER_DSN = WRITER_DSN
    service.READER_PASSWORD = "reader-test"
    service.SCHEMA = REPO / "db/projection-schema.sql"

    admin = await asyncpg.connect(WRITER_DSN)
    try:
        assert await admin.fetchval("SHOW server_encoding") == "UTF8"
        await service.bootstrap(admin)
        # Reproduce the upgrade shape from okengine#643: an older deployment has a
        # health view without corpus_epoch. PostgreSQL refuses CREATE OR REPLACE
        # when inserting that column into the existing view, so a second bootstrap
        # must replace the derived view while preserving every projection table.
        await admin.execute("""
            DROP VIEW v_projection_health;
            CREATE VIEW v_projection_health AS
                SELECT epoch, mode, started_at, finished_at, stats,
                       EXTRACT(epoch FROM now() - finished_at)::bigint AS age_seconds
                FROM projection_runs
                WHERE ok AND finished_at IS NOT NULL
                ORDER BY epoch DESC
                LIMIT 1
        """)
        old_columns = await admin.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='v_projection_health' ORDER BY ordinal_position")
        assert "corpus_epoch" not in {row["column_name"] for row in old_columns}
        await service.bootstrap(admin)
        upgraded_columns = await admin.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='v_projection_health' ORDER BY ordinal_position")
        assert "corpus_epoch" in {row["column_name"] for row in upgraded_columns}
        assert await admin.fetchval("SELECT count(*) FROM projection_runs") == 0

        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / "wiki/entities/a").mkdir(parents=True)
            (vault / "wiki/sources").mkdir()
            (vault / "schema.yaml").write_text(
                "partitioning:\n  namespaces:\n    entities: {}\n    sources: {}\n")
            (vault / "wiki/entities/a/acme.md").write_text(
                "---\nid: entity-acme\ntype: company\ntitle: Acme — Labs\n"
                "published: 2026-08-01\naliases: [old-acme]\n"
                "sources: ['[[sources/report]]']\n---\n## Peers\n[[entity-beta]]\n")
            (vault / "wiki/entities/a/beta.md").write_text(
                "---\nid: entity-beta\ntype: company\ntitle: Beta\n---\n")
            (vault / "wiki/sources/report.md").write_text(
                "---\nid: source-report\ntype: source\ntitle: Report\n---\n[[old-acme]]\n")
            service.projector.VAULT = vault

            cold = projector.scan_vault(vault)
            first = await projector.run_epoch(admin, cold)
            assert first["files_seen"] == 3 and first["links"] == 3
            known, links = await projector.load_projection_state(admin)
            warm = projector.scan_vault(vault, known=known, prior_links=links)
            second = await projector.run_epoch(admin, warm)
            assert second["unchanged"] == 3 and second["reparsed"] == 0
            assert await admin.fetchval(
                "SELECT published FROM pages WHERE canonical_id='entity-acme'") is not None
            methods = await admin.fetch(
                "SELECT DISTINCT resolution FROM links ORDER BY resolution")
            assert {row["resolution"] for row in methods} == {"alias", "exact", "id"}

            reader = await asyncpg.connect(READER_DSN)
            try:
                assert await reader.fetchval("SELECT count(*) FROM pages") == 3
                try:
                    await reader.execute("DELETE FROM pages")
                except asyncpg.InsufficientPrivilegeError:
                    pass
                else:
                    raise AssertionError("okengine_reader unexpectedly mutated projected state")
            finally:
                await reader.close()

            count = await query.count_pages(type="company", published_after="2026-01-01")
            assert count["count"] == 1 and count["complete"] is True
            found = await query.find_pages(namespace="entities", limit=1)
            assert found["matched"] == 2 and found["truncated"] is True
            await query._pool.close()
            query._pool = query._pool_loop = None

            checksum = await admin.fetchval(
                "SELECT md5(string_agg(path||':'||content_digest, ',' ORDER BY path)) FROM pages")
            assert checksum
            await concurrent_projection_contract(projector, admin, vault, query)
            healthy = await service.health_check()
            verified = await service.verify_reproducible()
            assert healthy["projected"] == 3 and verified["comparable"] == 3
            print(json.dumps({"first": first, "second": second, "checksum": checksum,
                              "health": healthy, "verify": verified},
                             default=str, sort_keys=True))
            await owner_readability_contract(service, admin, vault)
    finally:
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
