from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "projection", REPO / "src/okengine/mcp/projection.py"
)
Q = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(Q)


class Record(dict):
    pass


class Pool:
    def __init__(self, health=None, rows=None, count=0):
        self.health = health
        self.rows = rows or []
        self.count = count
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.health

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.count

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.rows


@pytest.fixture(autouse=True)
def reset():
    Q._pool = Q._pool_loop = None
    Q.MAX_AGE_HOURS = 6
    yield
    Q._pool = Q._pool_loop = None


def set_pool(pool):
    Q._pool = pool
    Q._pool_loop = asyncio.get_running_loop()


def test_status_missing_and_stale_refusal():
    async def scenario():
        set_pool(Pool())
        with pytest.raises(Q.ProjectionUnavailable, match="never completed"):
            await Q.projection_status()
        set_pool(Pool(Record(epoch=4, finished_at="then", age_seconds=25_000)))
        status = await Q.projection_status()
        assert status["stale"] is True
        with pytest.raises(Q.ProjectionUnavailable, match="results withheld"):
            await Q.projection_status(checked=True)
        set_pool(Pool(Record(epoch=5, finished_at="edge", age_seconds=21_600)))
        edge = await Q.projection_status(checked=True)
        assert edge == {"epoch": 5, "finished_at": "edge", "age_seconds": 21_600,
                        "stale": False, "max_age_seconds": 21_600,
                        "current_corpus_epoch": 0, "corpus_lag": 0}
        set_pool(Pool(Record(epoch=6, finished_at="over", age_seconds=21_601)))
        with pytest.raises(Q.ProjectionUnavailable, match="6.0h old .*limit 6h"):
            await Q.projection_status(checked=True)
        set_pool(Pool(Record(epoch=7, finished_at="later", age_seconds=25_000)))
        with pytest.raises(Q.ProjectionUnavailable, match="6.9h old"):
            await Q.projection_status(checked=True)
    asyncio.run(scenario())


def test_checked_status_fails_closed_when_corpus_epoch_is_ahead(tmp_path, monkeypatch):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    (state / "epoch").write_text("9\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))

    async def scenario():
        set_pool(Pool(Record(epoch=8, corpus_epoch=7, finished_at="now", age_seconds=1)))
        status = await Q.projection_status()
        assert status["corpus_lag"] == 2
        with pytest.raises(Q.ProjectionUnavailable, match="lags .* by 2 epoch"):
            await Q.projection_status(checked=True)

    asyncio.run(scenario())


def test_status_refuses_active_or_unreadable_corpus_state(tmp_path, monkeypatch):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))

    async def scenario():
        set_pool(Pool(Record(epoch=1, corpus_epoch=1, finished_at="now", age_seconds=1)))
        (state / "active.json").write_text("{}")
        with pytest.raises(Q.ProjectionUnavailable, match="mutation is in progress"):
            await Q.projection_status()
        (state / "active.json").unlink()
        (state / "epoch").write_text("not-an-epoch")
        with pytest.raises(Q.ProjectionUnavailable, match="epoch is unreadable"):
            await Q.projection_status()

    asyncio.run(scenario())


def test_date_validation_and_envelope():
    assert Q.as_date("2026-08-10", "since") == date(2026, 8, 10)
    assert Q.as_date(date(2026, 1, 1), "since") == date(2026, 1, 1)
    with pytest.raises(Q.ProjectionUnavailable, match="since must be"):
        Q.as_date("bad", "since")
    assert Q.as_date("2026-08-10suffix", "since") == date(2026, 8, 10)
    result = Q.envelope({"epoch": 2, "age_seconds": 3}, [{"x": 1}], 2, 1,
                        {"x": "", "y": 1}, ["pages"])
    assert result["truncated"] is True
    assert result["filters_applied"] == {"y": 1}
    exact = Q.envelope({"epoch": 2, "age_seconds": 3}, [{"x": 1}], 1, 1, {}, ["pages"])
    assert exact["truncated"] is False


def test_count_find_meta_and_links_are_covered_and_parameterized():
    async def scenario():
        pool = Pool(Record(epoch=1, finished_at="now", age_seconds=1),
                    [Record(path="entities/a.md")], 3)
        set_pool(pool)
        counted = await Q.count_pages("entities", "company", "active", False, {"region": "EU"},
                                      "2026-01-01", "2026-12-31", "2026-02-01", "2026-11-30")
        assert counted["count"] == 3 and counted["complete"] is True
        found = await Q.find_pages("entities", updated_after="2026-07-01",
                                   order="updated_desc", limit=1)
        assert found["matched"] == 3 and found["truncated"] is True
        meta = await Q.get_page_meta("entities/a")
        assert meta["found"] is True
        meta_call = next(args for sql, args in pool.calls if "WHERE path=$1 OR canonical_id=$2" in sql)
        assert meta_call == ("entities/a.md", "entities/a")
        linked = await Q.find_links(target="entities/a", resolution="slug", limit=1)
        assert linked["object_classes_searched"] == ["links"]
        assert any("fm ->> 'region'" in call[0] for call in pool.calls)
        count_sql = next(sql for sql, _ in pool.calls if "SELECT count(*) FROM pages" in sql)
        assert "published >= $5" in count_sql and "published <= $6" in count_sql
        assert "updated >= $7" in count_sql and "updated <= $8" in count_sql
        count_args = next(args for sql, args in pool.calls
                          if "SELECT count(*) FROM pages" in sql and len(args) == 8)
        assert count_args[-4:] == (date(2026, 1, 1), date(2026, 12, 31),
                                   date(2026, 2, 1), date(2026, 11, 30))
        assert any("ORDER BY updated DESC NULLS LAST,path" in sql for sql, _ in pool.calls)
    asyncio.run(scenario())


def test_meta_not_found_and_ambiguous():
    async def scenario():
        pool = Pool(Record(epoch=1, finished_at="now", age_seconds=1), [], 0)
        set_pool(pool)
        assert (await Q.get_page_meta("missing"))["found"] is False
        pool.rows = [Record(path="a"), Record(path="b")]
        with pytest.raises(Q.ProjectionUnavailable, match="ambiguous"):
            await Q.get_page_meta("same-id")
    asyncio.run(scenario())


def test_frontmatter_filter_rejects_sql_keys():
    with pytest.raises(Q.ProjectionUnavailable, match="invalid frontmatter"):
        Q._page_where("", "", "", False, {"x' OR TRUE": "bad"})
    with pytest.raises(Q.ProjectionUnavailable, match="invalid frontmatter"):
        Q._page_where("", "", "", False, {"`": "bad"})
    assert "fm ->> '_' = $1" in Q._page_where("", "", "", False, {"_": "ok"})[0]
    with pytest.raises(Q.ProjectionUnavailable, match="invalid page order"):
        asyncio.run(Q.find_pages(order="updated; DROP TABLE pages"))


def test_pool_configuration_creation_and_loop_replacement(monkeypatch):
    class Old:
        terminated = False
        def terminate(self): self.terminated = True

    made = Pool(Record(epoch=1, age_seconds=0))
    created = {}
    async def create_pool(*args, **kwargs): created.update(args=args, kwargs=kwargs); return made
    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=create_pool))

    async def scenario():
        Q.READ_DSN = "postgresql://fixture"
        old = Old()
        Q._pool, Q._pool_loop = old, object()
        assert await Q.get_pool() is made
        assert old.terminated is True
    asyncio.run(scenario())
    assert created == {"args": ("postgresql://fixture",),
                       "kwargs": {"min_size": 1, "max_size": 4, "command_timeout": 30}}


def test_link_predicates_normalize_both_paths_and_preserve_all_filters():
    async def scenario():
        pool = Pool(Record(epoch=1, finished_at="now", age_seconds=0), [], 0)
        set_pool(pool)
        result = await Q.find_links("entities/target", "sources/source", "unresolved", 5)
        assert result["filters_applied"] == {
            "target": "entities/target", "source": "sources/source",
            "resolution": "unresolved"}
        sql, args = next((sql, args) for sql, args in pool.calls if sql.startswith("SELECT count"))
        assert "target_path = $1 AND src_path = $2 AND resolution = $3" in sql
        assert args == ("entities/target.md", "sources/source.md", "unresolved")
    asyncio.run(scenario())


def test_pool_unconfigured():
    async def scenario():
        Q.READ_DSN = ""
        with pytest.raises(Q.ProjectionUnavailable, match="not configured"):
            await Q.get_pool()
    asyncio.run(scenario())


def test_include_tombstoned_omits_default_predicate():
    where, args = Q._page_where("", "", "", True)
    assert where == "TRUE" and args == []


def test_query_functions_refuse_stale_projection_and_keep_safe_defaults():
    async def scenario():
        stale = Pool(Record(epoch=1, finished_at="old", age_seconds=99_999), [], 0)
        for call in (Q.count_pages, Q.find_pages, lambda: Q.get_page_meta("x")):
            set_pool(stale)
            with pytest.raises(Q.ProjectionUnavailable, match="withheld"):
                await call()

        pool = Pool(Record(epoch=2, finished_at="now", age_seconds=None),
                    [Record(path="entities/a.md")], 1)
        set_pool(pool)
        status = await Q.projection_status()
        assert status["age_seconds"] is None and status["stale"] is False
        counted = await Q.count_pages()
        found = await Q.find_pages()
        linked = await Q.find_links()
        assert counted["filters_applied"]["include_tombstoned"] is False
        assert found["limit"] == 40 and found["filters_applied"]["frontmatter"] == {}
        assert linked["limit"] == 40
        assert sum("NOT is_tombstoned" in sql for sql, _ in pool.calls) == 3
    asyncio.run(scenario())


def test_page_predicates_and_limit_bounds_are_exact():
    where, args = Q._page_where("entities", "company", "active", False,
                                {"z_key": 2, "a_key": 1}, "2026-01-01", "2026-02-01")
    assert where == ("TRUE AND NOT is_tombstoned AND namespace = $1 AND type = $2 AND "
                     "status = $3 AND fm ->> 'a_key' = $4 AND fm ->> 'z_key' = $5 AND "
                     "published >= $6 AND published <= $7")
    assert args == ["entities", "company", "active", "1", "2",
                    date(2026, 1, 1), date(2026, 2, 1)]

    async def scenario():
        pool = Pool(Record(epoch=1, finished_at="now", age_seconds=0), [], 0)
        set_pool(pool)
        low = await Q.find_pages(limit=0)
        high = await Q.find_pages(limit=Q.MAX_LIMIT + 100)
        assert low["limit"] == 1
        assert high["limit"] == Q.MAX_LIMIT
        assert any(sql.endswith("LIMIT 1") for sql, _ in pool.calls)
        assert any(sql.endswith(f"LIMIT {Q.MAX_LIMIT}") for sql, _ in pool.calls)
    asyncio.run(scenario())


@pytest.mark.parametrize("stats", [{"unreadable": ["private: PermissionError"]},
                                  '{"unreadable":["private: PermissionError"]}'])
def test_legacy_incomplete_epoch_cannot_certify_a_typed_count(stats):
    async def scenario():
        set_pool(Pool(Record(epoch=1, finished_at="now", age_seconds=0, stats=stats)))
        unchecked = await Q.projection_status()
        assert unchecked["stats"] == stats
        with pytest.raises(Q.ProjectionUnavailable, match="unreadable paths"):
            await Q.count_pages()
    asyncio.run(scenario())


def test_deployment_guard_persists_the_verified_build_context():
    deploy = (REPO / "scripts/deploy.sh").read_text()
    assert 'scripts/engine_source_guard.py" "$ENGINE_DIR" --pack "$PACK"' in deploy


# --- exact envelopes (okengine#566 critical floor) -------------------------------------------------
# The read tools report a latency and fixed provenance counters. A scripted clock makes the whole
# envelope exact, so a changed constant or operator in the latency arithmetic is visible. The start
# (0.125 s) is below half the end time on purpose: with a start above half the end, `end % start`
# equals `end - start` and a remainder would pass for a difference.

def _scripted_clock(monkeypatch, *ticks):
    remaining = list(ticks)

    def monotonic():
        return remaining.pop(0)

    monkeypatch.setattr(Q, "time", types.SimpleNamespace(monotonic=monotonic))
    return remaining


HEALTH = dict(epoch=3, finished_at="now", age_seconds=5, corpus_epoch=0)


def test_query_envelope_is_exact_including_latency_and_scan_counters(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    remaining = _scripted_clock(monkeypatch, 0.125, 0.4004567)

    async def scenario():
        set_pool(Pool(Record(**HEALTH), [Record(path="entities/a.md")], 1))
        return await Q.find_links(target="entities/a", limit=5)

    result = asyncio.run(scenario())
    assert remaining == [], "the latency is measured from exactly one start and one end tick"
    assert result == {
        "results": [{"path": "entities/a.md"}], "matched": 1, "returned": 1, "truncated": False,
        "filters_applied": {"target": "entities/a"}, "object_classes_searched": ["links"],
        "projection_epoch": 3, "projection_age_seconds": 5, "limit": 5,
        "query_latency_ms": 275.457, "projection_hits": 1, "pages_scanned": 0,
        "bytes_parsed": 0, "corpus_lag": 0,
    }


def test_count_envelope_is_exact_including_latency_and_the_frontmatter_filter(
        tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    remaining = _scripted_clock(monkeypatch, 0.125, 0.4004567)

    async def scenario():
        set_pool(Pool(Record(**HEALTH), [], 7))
        return await Q.count_pages("entities", frontmatter={"region": "EU"})

    result = asyncio.run(scenario())
    assert remaining == []
    assert result == {
        "count": 7, "complete": True,
        "filters_applied": {"namespace": "entities", "type": "", "status": "",
                            "include_tombstoned": False, "frontmatter": {"region": "EU"},
                            "published_after": "", "published_before": "",
                            "updated_after": "", "updated_before": ""},
        "object_classes_searched": ["pages"], "projection_epoch": 3,
        "projection_age_seconds": 5, "corpus_lag": 0, "projection_hits": 1,
        "pages_scanned": 0, "bytes_parsed": 0, "query_latency_ms": 275.457,
    }


def test_page_meta_envelope_is_exact_including_latency(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    remaining = _scripted_clock(monkeypatch, 0.125, 0.4004567)

    async def scenario():
        set_pool(Pool(Record(**HEALTH), [Record(path="entities/a.md", title="A")], 0))
        return await Q.get_page_meta("entities/a")

    result = asyncio.run(scenario())
    assert remaining == []
    assert result == {
        "path": "entities/a.md", "title": "A", "found": True, "projection_epoch": 3,
        "projection_age_seconds": 5, "corpus_lag": 0, "projection_hits": 1,
        "pages_scanned": 0, "bytes_parsed": 0, "query_latency_ms": 275.457,
    }


def test_a_count_below_the_returned_rows_is_not_reported_as_truncated():
    # The count and the row fetch are separate statements, so a page removed between them can
    # leave `matched` below the rows returned; only MORE matches than rows is a truncation.
    result = Q.envelope({"epoch": 1, "age_seconds": 0}, [{"x": 1}, {"x": 2}], 1, 5, {}, ["pages"])
    assert result["truncated"] is False


def _with_live_epoch(tmp_path, monkeypatch, epoch: str):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    (state / "epoch").write_text(epoch)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))


def test_a_projection_without_a_recorded_epoch_lags_a_live_corpus_at_epoch_one(
        tmp_path, monkeypatch):
    _with_live_epoch(tmp_path, monkeypatch, "1")

    async def scenario():
        set_pool(Pool(Record(epoch=1, finished_at="now", age_seconds=0)))
        assert (await Q.projection_status())["corpus_lag"] == 1
        with pytest.raises(Q.ProjectionUnavailable, match="lags .* by 1 epoch"):
            await Q.projection_status(checked=True)

    asyncio.run(scenario())


def test_a_projection_ahead_of_the_live_epoch_reports_no_lag(tmp_path, monkeypatch):
    _with_live_epoch(tmp_path, monkeypatch, "3")

    async def scenario():
        set_pool(Pool(Record(epoch=1, corpus_epoch=5, finished_at="now", age_seconds=0)))
        status = await Q.projection_status(checked=True)
        assert status["corpus_lag"] == 0 and status["current_corpus_epoch"] == 3

    asyncio.run(scenario())


def test_an_unknown_age_is_zero_seconds_old(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))

    async def scenario():
        Q.MAX_AGE_HOURS = 0
        set_pool(Pool(Record(epoch=1, finished_at="now", age_seconds=None)))
        assert (await Q.projection_status(checked=True))["stale"] is False
        Q.MAX_AGE_HOURS = -1  # every age is stale; the refusal reports the age it measured
        with pytest.raises(Q.ProjectionUnavailable, match=r"projection is 0\.0h old"):
            await Q.projection_status(checked=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(("age_seconds", "limit_hours", "reported"),
                         [(180, 0.01, "0.1h"), (25_019, 6, "6.9h")])
def test_the_refusal_reports_the_age_in_hours_to_one_decimal(
        tmp_path, monkeypatch, age_seconds, limit_hours, reported):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))

    async def scenario():
        Q.MAX_AGE_HOURS = limit_hours
        set_pool(Pool(Record(epoch=1, finished_at="now", age_seconds=age_seconds)))
        with pytest.raises(Q.ProjectionUnavailable) as refused:
            await Q.projection_status(checked=True)
        assert f"projection is {reported} old" in str(refused.value)

    asyncio.run(scenario())
