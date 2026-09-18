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
            healthy = await service.health_check()
            verified = await service.verify_reproducible()
            assert healthy["projected"] == 3 and verified["comparable"] == 3
            print(json.dumps({"first": first, "second": second, "checksum": checksum,
                              "health": healthy, "verify": verified},
                             default=str, sort_keys=True))
    finally:
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
