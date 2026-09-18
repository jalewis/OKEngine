"""The projection service's database orchestration — the 56% that reported no number (#600).

`okengine-projection/service.py` was outside the coverage source list, so it produced no
measurement at all. tests/test_projection_service.py already owned its pure decision functions
(`classify_sampled_page`, `churn_since`, `require_basic_health`, `require_reproducible`); what had
never been executed under measurement was everything that touches a connection — `bootstrap`,
`rebuild`, `health_check`, `verify_reproducible` — plus the service loop and CLI around them.

Two layers, deliberately, because neither alone is honest:

* **Correctness of the SQL** is proved by `ci/postgres_projection_integration.py`, which runs
  these same functions against a real PostgreSQL 17 in the required `postgres-projection-
  integration` job. A fake connection cannot validate a query; it can only agree with it.
* **Reachability of every line and branch** is what this file adds, so an edit that breaks the
  orchestration around those queries fails in the unit lane rather than waiting for a job with a
  database attached.

The connection double is `create_autospec(asyncpg.Connection)`, not a bare Mock: if asyncpg
renames or drops `fetchrow`/`fetchval`/`fetch`/`execute`, these tests fail at the double instead
of staying green against a contract that no longer exists. The vault scan is the REAL
`scan_vault` over a real temporary tree — it is pure filesystem code, and faking it would replace
the thing under test with a restatement of it.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import create_autospec

import asyncpg
import pytest

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)


@pytest.fixture
def service():
    spec = importlib.util.spec_from_file_location(
        "projection_service_paths", REPO / "okengine-projection/service.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # The service now rejects missing/public writer credentials before it can
    # create a role. Exercise orchestration with a valid disposable DSN.
    module.WRITER_DSN = "postgresql://writer:strong@localhost/okengine"
    module.READER_PASSWORD = "different"
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def vault(tmp_path):
    """A real two-page vault, scanned by the real scanner."""
    wiki = tmp_path / "wiki/entities"
    wiki.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {}\n", encoding="utf-8")
    (wiki / "acme.md").write_text(
        "---\nid: entity-acme\ntype: company\ntitle: Acme\n---\nbody\n", encoding="utf-8")
    (wiki / "beta.md").write_text(
        "---\nid: entity-beta\ntype: company\ntitle: Beta\n---\nbody\n", encoding="utf-8")
    return tmp_path


def connection(answers: dict[str, object]):
    """An autospec'd asyncpg connection that answers by SQL fragment, longest fragment first.

    An unrecognised query is an AssertionError rather than a None: a double that silently answers
    a question it was never taught is how a test keeps passing after the code starts asking
    something else.
    """
    conn = create_autospec(asyncpg.Connection, instance=True)
    ordered = sorted(answers.items(), key=lambda item: -len(item[0]))

    def answer(sql, *args, **kwargs):
        for fragment, value in ordered:
            if fragment in sql:
                return value(*args) if callable(value) else value
        raise AssertionError(f"the double was asked an unexpected query: {sql!r}")

    conn.fetchval.side_effect = answer
    conn.fetchrow.side_effect = answer
    conn.fetch.side_effect = answer
    conn.execute.side_effect = lambda sql, *a, **k: None
    return conn


def configure(service, vault, tmp_path, conn, **projector):
    """Point the module at the temp vault and the double, keeping the real scanner."""
    schema = tmp_path / "projection-schema.sql"
    schema.write_text("CREATE TABLE IF NOT EXISTS pages ();\n", encoding="utf-8")
    service.SCHEMA = schema
    service.WRITER_DSN = "postgresql://writer:strong@localhost/okengine"
    service.READER_PASSWORD = "a-real-password"
    service.projector.VAULT = vault

    async def connect(dsn):
        return conn

    asyncpg.connect = connect          # restored by the monkeypatch fixture in each test
    for name, value in projector.items():
        setattr(service.projector, name, value)


@pytest.fixture(autouse=True)
def restore_asyncpg():
    """`configure` assigns `asyncpg.connect` directly, because the service imports asyncpg INSIDE
    each function and so resolves it from the live module every call. Put the real one back
    afterwards, or a later test inherits an earlier test's connection double."""
    real = asyncpg.connect
    yield
    asyncpg.connect = real


def epoch_stats(**overrides):
    stats = {"epoch": 7, "files_seen": 2, "links": 0, "unchanged": 0, "reparsed": 2}
    stats.update(overrides)
    return stats


async def run_epoch_stub(conn, scan, dry_run=False):
    return epoch_stats()


async def load_state_stub(conn):
    return {}, {}


# --- bootstrap -----------------------------------------------------------------------------------

def test_bootstrap_refuses_to_run_without_its_schema_file(service, tmp_path):
    service.SCHEMA = tmp_path / "absent.sql"
    with pytest.raises(RuntimeError, match="projection schema missing"):
        asyncio.run(service.bootstrap(connection({})))


def test_bootstrap_refuses_a_non_utf8_server(service, tmp_path):
    """A LATIN1 cluster silently mangles every title it stores; recreating PGDATA is the only
    fix, so this has to fail before anything is written."""
    service.SCHEMA = tmp_path / "s.sql"
    service.SCHEMA.write_text("SELECT 1;\n", encoding="utf-8")
    conn = connection({"SHOW server_encoding": "LATIN1"})
    with pytest.raises(RuntimeError, match="server_encoding is LATIN1"):
        asyncio.run(service.bootstrap(conn))


@pytest.mark.parametrize("exists, expected", [(True, "ALTER ROLE"), (False, "CREATE ROLE")])
def test_bootstrap_creates_the_reader_role_once_and_resets_its_password_after(
        service, tmp_path, exists, expected):
    """Restarting the container must not fail because the role survived the last run, and must
    still apply a rotated password."""
    service.SCHEMA = tmp_path / "s.sql"
    service.SCHEMA.write_text("SELECT 1;\n", encoding="utf-8")
    service.READER_PASSWORD = "a-real-password"
    conn = connection({
        "SHOW server_encoding": "UTF8",
        "FROM pg_roles": exists,
        "quote_literal": "'a-real-password'",
    })
    asyncio.run(service.bootstrap(conn))
    statements = [call.args[0] for call in conn.execute.call_args_list]
    assert any(expected in text for text in statements), statements
    assert any("GRANT SELECT ON ALL TABLES" in text for text in statements)
    assert any("ALTER DEFAULT PRIVILEGES" in text for text in statements)


def test_bootstrap_refuses_the_shipped_default_password(service, tmp_path):
    """The template ships a placeholder; starting with it exposes the read role on every
    deployment that never changed it."""
    service.SCHEMA = tmp_path / "s.sql"
    service.SCHEMA.write_text("SELECT 1;\n", encoding="utf-8")
    service.READER_PASSWORD = "okengine-reader-local"
    conn = connection({"SHOW server_encoding": "UTF8"})
    with pytest.raises(RuntimeError, match="OKENGINE_PROJECTION_READER_PASSWORD"):
        asyncio.run(service.bootstrap(conn))


# --- rebuild -------------------------------------------------------------------------------------

def test_rebuild_without_a_writer_dsn_is_a_named_failure(service):
    service.WRITER_DSN = ""
    with pytest.raises(RuntimeError, match="OKENGINE_PROJECTION_WRITER_DSN is not set"):
        asyncio.run(service.rebuild())


def test_rebuild_bootstraps_then_projects_and_always_closes_the_connection(
        service, vault, tmp_path):
    async def project(conn, selected_vault, *, force_reconcile=False):
        assert selected_vault == vault
        assert force_reconcile is False
        return {"epoch": 7}
    conn = connection({
        "SHOW server_encoding": "UTF8", "FROM pg_roles": True, "quote_literal": "'x'",
    })
    configure(service, vault, tmp_path, conn, project=project)
    assert asyncio.run(service.rebuild())["epoch"] == 7
    conn.close.assert_awaited()


def test_explicit_rebuild_forces_filesystem_reconciliation(service, vault, tmp_path):
    async def project(conn, selected_vault, *, force_reconcile=False):
        assert selected_vault == vault
        return {"force_reconcile": force_reconcile}
    conn = connection({
        "SHOW server_encoding": "UTF8", "FROM pg_roles": True, "quote_literal": "'x'",
    })
    configure(service, vault, tmp_path, conn, project=project)
    assert asyncio.run(service.rebuild(force_reconcile=True)) == {"force_reconcile": True}
    conn.close.assert_awaited()


def test_rebuild_closes_the_connection_even_when_the_epoch_fails(service, vault, tmp_path):
    """A leaked connection per failed cycle exhausts max_connections in a day of retries."""
    async def explode(conn, selected_vault, *, force_reconcile=False):
        raise RuntimeError("epoch exploded")

    conn = connection({
        "SHOW server_encoding": "UTF8", "FROM pg_roles": True, "quote_literal": "'x'",
    })
    configure(service, vault, tmp_path, conn, project=explode)
    with pytest.raises(RuntimeError, match="epoch exploded"):
        asyncio.run(service.rebuild())
    conn.close.assert_awaited()


# --- health_check --------------------------------------------------------------------------------

def health_answers(service, vault, *, rows=2, sample=(), age=10, finished=None):
    # The fixture writes its pages NOW, so an epoch stamped in the past legitimately counts both
    # of them as churn. Default the epoch to just after the vault was built; the churn cases pass
    # an explicitly older `finished` to make the corpus move under the projection on purpose.
    finished = finished or datetime.now(timezone.utc) + timedelta(hours=1)
    return {
        "OKENGINE_PROJECTION_WRITER_DSN": None,
        "v_projection_health": {"epoch": 7, "age_seconds": age, "finished_at": finished},
        "FROM projection_runs": {"epoch": 7, "ok": True, "error": None,
                                 "finished_at": finished},
        "SELECT count(*) FROM pages": rows,
        "ORDER BY random()": list(sample),
    }


def test_health_check_without_a_writer_dsn_is_a_named_failure(service):
    service.WRITER_DSN = ""
    with pytest.raises(RuntimeError, match="OKENGINE_PROJECTION_WRITER_DSN is not set"):
        asyncio.run(service.health_check())


def test_health_check_reports_the_facts_when_everything_agrees(service, vault, tmp_path):
    digest = __import__("hashlib").sha256(
        (vault / "wiki/entities/acme.md").read_bytes()).hexdigest()
    conn = connection(health_answers(service, vault, sample=[
        {"path": "entities/acme.md", "content_digest": digest,
         "indexed_at": datetime.now(timezone.utc) + timedelta(hours=1)}]))
    configure(service, vault, tmp_path, conn)
    report = asyncio.run(service.health_check())
    assert report == {"epoch": 7, "age_seconds": 10, "projected": 2, "eligible_files": 2,
                      "comparable": 1, "written_since_epoch": 0, "deleted_since_epoch": 0,
                      "scan_errors": 0}
    conn.close.assert_awaited()


def test_health_check_counts_a_deleted_page_as_churn_rather_than_corruption(
        service, vault, tmp_path):
    """okengine#600's sibling defect, already fixed and now measured: a page removed after the
    epoch is one row behind reality, not a digest mismatch. 412 of 48,968 rows on a live vault
    were reported as `digest drift` when nothing had been compared at all."""
    conn = connection(health_answers(service, vault, sample=[
        {"path": "entities/vanished.md", "content_digest": "0" * 64,
         "indexed_at": datetime.now(timezone.utc)}]))
    configure(service, vault, tmp_path, conn)
    with pytest.raises(RuntimeError, match="comparable=0"):
        asyncio.run(service.health_check())


def test_health_check_still_fails_on_a_page_whose_bytes_changed(service, vault, tmp_path):
    conn = connection(health_answers(service, vault, sample=[
        {"path": "entities/acme.md", "content_digest": "0" * 64,
         "indexed_at": datetime.now(timezone.utc) + timedelta(hours=1)}]))
    configure(service, vault, tmp_path, conn)
    with pytest.raises(RuntimeError, match="digest drift: 1 of 0"):
        asyncio.run(service.health_check())


def test_health_check_fails_when_the_row_count_cannot_be_explained_by_churn(
        service, vault, tmp_path):
    conn = connection(health_answers(service, vault, rows=99))
    configure(service, vault, tmp_path, conn)
    with pytest.raises(RuntimeError, match="unexplained"):
        asyncio.run(service.health_check())


def test_health_check_rejects_zero_digest_sample(service, vault, tmp_path, monkeypatch):
    conn = connection(health_answers(service, vault))
    configure(service, vault, tmp_path, conn)
    monkeypatch.setenv("OKENGINE_PROJECTION_DIGEST_SAMPLE", "0")
    with pytest.raises(RuntimeError, match="must be at least 1"):
        asyncio.run(service.health_check())


def test_health_check_skips_a_page_written_since_the_epoch(service, vault, tmp_path):
    """`newer` proves nothing either way, so it must not count as comparable OR as a mismatch."""
    conn = connection(health_answers(
        service, vault,
        sample=[{"path": "entities/acme.md", "content_digest": "0" * 64,
                 "indexed_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}],
        finished=datetime(2000, 1, 1, tzinfo=timezone.utc)))
    configure(service, vault, tmp_path, conn)
    with pytest.raises(RuntimeError, match="comparable=0"):
        asyncio.run(service.health_check())


# --- verify_reproducible -------------------------------------------------------------------------

def verify_answers(*, comparable=2, rows=(), links=()):
    # Keyed on what makes each query DIFFERENT, not on a prefix they share: all three select from
    # `public.pages live JOIN scratch.pages rebuilt`, so a fragment covering that join matches the
    # count query as well and hands it a list.
    return {
        "live.namespace": list(rows),               # the row-level column comparison
        "WITH comparable AS": list(links),          # the link aggregate
        "SELECT count(*) FROM public.pages": comparable,
    }


def test_verify_reproducible_reports_a_clean_rebuild_and_drops_its_scratch_schema(
        service, vault, tmp_path):
    conn = connection(verify_answers())
    configure(service, vault, tmp_path, conn, run_epoch=run_epoch_stub)
    assert asyncio.run(service.verify_reproducible()) == {
        "comparable": 2, "row_mismatches": 0, "link_mismatches": 0, "scratch_epoch": 7}
    dropped = [call.args[0] for call in conn.execute.call_args_list
               if "DROP SCHEMA" in call.args[0]]
    assert len(dropped) == 2, "the scratch schema is dropped before AND after the rebuild"
    conn.close.assert_awaited()


def test_verify_reproducible_fails_when_the_same_bytes_project_differently(
        service, vault, tmp_path):
    conn = connection(verify_answers(rows=[{"path": "entities/acme.md"}]))
    configure(service, vault, tmp_path, conn, run_epoch=run_epoch_stub)
    with pytest.raises(RuntimeError, match="non-deterministic: 1 row and 0 link"):
        asyncio.run(service.verify_reproducible())


def test_verify_reproducible_drops_the_scratch_schema_even_when_it_fails(
        service, vault, tmp_path):
    """A left-behind scratch schema makes the NEXT verification compare against stale rows."""
    conn = connection(verify_answers(comparable=0))
    configure(service, vault, tmp_path, conn, run_epoch=run_epoch_stub)
    with pytest.raises(RuntimeError, match="comparable=0"):
        asyncio.run(service.verify_reproducible())
    assert any("DROP SCHEMA" in call.args[0] for call in conn.execute.call_args_list)
    conn.close.assert_awaited()


# --- the service loop ------------------------------------------------------------------------------

def test_run_service_once_prints_the_epoch_and_succeeds(service, capsys):
    async def rebuild():
        return epoch_stats()

    service.rebuild = rebuild
    assert asyncio.run(service.run_service(True)) == 0
    assert json.loads(capsys.readouterr().out)["epoch"] == 7


def test_run_service_once_reports_a_failed_rebuild_on_stderr_and_exits_nonzero(service, capsys):
    async def rebuild():
        raise RuntimeError("no database")

    service.rebuild = rebuild
    assert asyncio.run(service.run_service(True)) == 1
    assert "projection rebuild failed: RuntimeError: no database" in capsys.readouterr().err


def test_the_long_running_loop_survives_a_failed_cycle_and_sleeps_before_retrying(
        service, capsys, monkeypatch):
    """A rebuild that raises must not kill the service — the next interval is the retry."""
    attempts = []

    async def rebuild():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return epoch_stats()

    class Stop(Exception):
        pass

    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            raise Stop

    service.rebuild = rebuild
    service.INTERVAL_SECONDS = 3600
    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    with pytest.raises(Stop):
        asyncio.run(service.run_service(False))
    assert len(attempts) == 2 and slept == [3600, 3600]
    assert "transient" in capsys.readouterr().err


# --- the CLI ---------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def restore_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def test_main_health_prints_the_report_and_returns_zero(service, capsys):
    async def health_check():
        return {"epoch": 7}

    service.health_check = health_check
    assert service.main(["--health"]) == 0
    assert json.loads(capsys.readouterr().out) == {"epoch": 7}


def test_main_verify_prints_the_report_and_returns_zero(service, capsys):
    async def verify_reproducible():
        return {"comparable": 2}

    service.verify_reproducible = verify_reproducible
    assert service.main(["--verify"]) == 0
    assert json.loads(capsys.readouterr().out) == {"comparable": 2}


def test_main_defaults_to_running_the_service(service, capsys):
    async def rebuild():
        return epoch_stats()

    service.rebuild = rebuild
    assert service.main(["--once"]) == 0


def test_main_reconcile_forces_a_single_full_scan(service, capsys):
    calls = []

    async def rebuild(*, force_reconcile=False):
        calls.append(force_reconcile)
        return epoch_stats()

    service.rebuild = rebuild
    assert service.main(["--reconcile"]) == 0
    assert calls == [True]


def test_main_turns_any_failure_into_a_named_nonzero_exit(service, capsys):
    """The container's restart policy reads the exit code; a traceback on stdout is not one."""
    async def health_check():
        raise RuntimeError("no database")

    service.health_check = health_check
    assert service.main(["--health"]) == 1
    assert "projection operation failed: RuntimeError: no database" in capsys.readouterr().err


def test_main_installs_a_sigterm_handler_that_exits_cleanly(service):
    """Compose sends SIGTERM on `down`; the default action would report an abnormal exit."""
    async def rebuild():
        return epoch_stats()

    service.rebuild = rebuild
    service.main(["--once"])
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    with pytest.raises(SystemExit) as exited:
        handler(signal.SIGTERM, None)
    assert exited.value.code == 0
