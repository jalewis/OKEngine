from __future__ import annotations

import importlib.util
import json
import asyncio
import sys
import types
from datetime import date, datetime
from pathlib import Path
from unittest.mock import create_autospec

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "postgres_projection", REPO / "src/okengine/projection/projector.py")
P = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = P
SPEC.loader.exec_module(P)


def vault(tmp_path: Path) -> Path:
    (tmp_path / "wiki/entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {}\n    sources: {}\n")
    return tmp_path


def test_parse_page_preserves_frontmatter_body_links_and_generic_fields():
    raw = b"""---
id: entity-1
name: Example
type: company
status: tombstoned
published: 2026-08-01
ingested: bad-date
last_updated: 2026-08-02T12:00:00
aliases: [EX, Example Inc]
sources: ['[[sources/one]]']
---
## Evidence
See [[entities/two|Two]] and [[entity-3#History]].
"""
    row, links = P.parse_page("entities/e/example.md", raw)
    assert row["canonical_id"] == "entity-1"
    assert row["title"] == "Example"
    assert row["is_tombstoned"] is True
    assert row["published"] == date(2026, 8, 1)
    assert row["ingested"] is None
    assert row["updated"] == date(2026, 8, 2)
    assert row["aliases"] == ["EX", "Example Inc"]
    assert row["namespace"] == "entities" and row["slug"] == "example"
    assert row["body_chars"] == len(
        "## Evidence\nSee [[entities/two|Two]] and [[entity-3#History]].\n")
    assert [(x.target_ref, x.section) for x in links] == [
        ("sources/one", "(frontmatter)"),
        ("entities/two", "Evidence"),
        ("entity-3", "Evidence"),
    ]


@pytest.mark.parametrize("raw,error", [
    (b"plain body", "no frontmatter block"),
    (b"---\n- list\n---\n", "frontmatter is list, not a mapping"),
    (b"---\na: [\n---\n", "while parsing"),
])
def test_parse_page_keeps_bad_frontmatter(raw, error):
    row, _ = P.parse_page("entities/x.md", raw)
    assert error in row["fm_error"]
    assert json.loads(row["fm"]) == {}


def test_alias_coercion_and_empty_normalization():
    row, _ = P.parse_page("entities/x.md", b"---\naliases: A, B\ntitle: ''\n---\n")
    assert row["aliases"] == ["A", "B"]
    assert row["title"] is None
    row, _ = P.parse_page("entities/x.md", b"---\naliases: 4\n---\n")
    assert row["aliases"] == []
    row, _ = P.parse_page("entities/x.md", b"---\n\n---\n")
    assert row["fm_error"] is None
    assert P._to_date(datetime(2026, 1, 2)) == date(2026, 1, 2)
    assert P._to_date(date(2026, 1, 3)) == date(2026, 1, 3)
    assert P._to_date("") is None
    assert P._to_date("2026-01-04suffix") == date(2026, 1, 4)


def test_resolution_records_method_and_refuses_ambiguity():
    pages = [
        {"path": "entities/a/acme.md", "canonical_id": "entity-acme", "slug": "acme",
         "aliases": ["old-acme"]},
        {"path": "sources/a/acme.md", "canonical_id": None, "slug": "acme", "aliases": []},
        {"path": "entities/b/beta.md", "canonical_id": "entity-beta", "slug": "beta",
         "aliases": []},
    ]
    indexes = P.identity_indexes(pages)
    assert P.resolve_link("entities/a/acme", indexes) == ("entities/a/acme.md", "exact")
    assert P.resolve_link("entity-beta", indexes) == ("entities/b/beta.md", "id")
    assert P.resolve_link("old-acme", indexes) == ("entities/a/acme.md", "alias")
    assert P.resolve_link("entities/acme", indexes) == ("entities/a/acme.md", "slug")
    assert P.resolve_link("acme", indexes) == (None, "ambiguous")
    assert P.resolve_link("missing", indexes) == (None, "unresolved")
    assert P.resolve_link("entities/a/acme.md", indexes) == ("entities/a/acme.md", "exact")


def test_scan_is_sorted_schema_scoped_and_accounts_for_exclusions(tmp_path):
    root = vault(tmp_path)
    (root / "wiki/sources").mkdir()
    (root / "wiki/operational").mkdir()
    (root / "wiki/entities/b.md").write_text("---\nid: b\n---\n")
    (root / "wiki/entities/a.md").write_text("---\nid: a\n---\n")
    (root / "wiki/entities/INDEX.md").write_text("derived")
    (root / "wiki/operational/no.md").write_text("excluded")
    scan = P.scan_vault(root)
    assert [row["path"] for row in scan.pages] == ["entities/a.md", "entities/b.md"]
    assert scan.excluded == 2
    assert scan.skipped_dirs == [] and scan.unreadable == []
    assert all(len(row["content_digest"]) == 64 for row in scan.pages)


def test_warm_scan_skips_yaml_parse_and_reuses_links(tmp_path, monkeypatch):
    root = vault(tmp_path)
    path = root / "wiki/entities/a.md"
    path.write_text("---\nid: a\naliases: [old-a]\n---\n[[entities/b]]")
    cold = P.scan_vault(root)
    row = cold.pages[0]
    known = {row["path"]: dict(row)}
    prior = {row["path"]: [P.Link("preserved", "old section")]}
    monkeypatch.setattr(P, "parse_page", lambda *_: (_ for _ in ()).throw(AssertionError()))
    warm = P.scan_vault(root, known=known, prior_links=prior)
    assert warm.pages[0]["_unchanged"] is True
    assert warm.pages[0]["aliases"] == ["old-a"]
    assert warm.links[row["path"]] == prior[row["path"]]
    known[row["path"]]["fm"] = json.loads(known[row["path"]]["fm"])
    assert P.scan_vault(root, known=known).pages[0]["_unchanged"] is True


def test_missing_vault_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="refusing to project an empty corpus"):
        P.scan_vault(tmp_path)


def test_namespace_fallback_filters_hidden_and_excluded(tmp_path):
    (tmp_path / "wiki/good").mkdir(parents=True)
    (tmp_path / "wiki/.hidden").mkdir()
    (tmp_path / "wiki/operational").mkdir()
    (tmp_path / "schema.yaml").write_text("partitioning: {}\n")
    assert P.selected_namespaces(tmp_path) == {"good"}

    (tmp_path / "schema.yaml").unlink()
    assert P.selected_namespaces(tmp_path) == {"good"}


def test_schema_namespaces_still_remove_reserved_namespaces(tmp_path):
    (tmp_path / "wiki/good").mkdir(parents=True)
    (tmp_path / "wiki/operational").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    good: {}\n    operational: {}\n")
    assert P.selected_namespaces(tmp_path) == {"good"}


def test_wiki_schema_and_explicit_exclusions_take_precedence(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki/schema.yaml").write_text(
        "exclude:\n  - wiki/custom/archive\n  - ''\n"
        "partitioning:\n  namespaces:\n    good: {}\n    custom: {}\n"
    )
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    stale: {}\n"
    )
    assert P.selected_namespaces(tmp_path) == {"good"}


def test_scan_accounts_for_md_directory_read_and_stat_failures(tmp_path, monkeypatch):
    root = vault(tmp_path)
    directory = root / "wiki/entities/folder.md"
    directory.mkdir()
    unreadable = root / "wiki/entities/unreadable.md"
    unreadable.write_text("x")
    statless = root / "wiki/entities/statless.md"
    statless.write_text("x")
    original_read = Path.read_bytes
    original_stat = Path.stat
    stat_calls = 0

    def read(path):
        if path == unreadable:
            raise PermissionError("fixture")
        return original_read(path)

    def stat(path, *args, **kwargs):
        nonlocal stat_calls
        if path == statless:
            # scan_vault explicitly stats the file type first, then its mtime.
            # Fail only the latter; no dependency on interpreter is_file internals.
            stat_calls += 1
            if stat_calls > 1:
                raise OSError("fixture")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(Path, "stat", stat)
    scan = P.scan_vault(root)
    assert scan.skipped_dirs == ["entities/folder.md"]
    assert scan.unreadable == ["entities/unreadable.md: PermissionError"]
    assert next(row for row in scan.pages if row["path"].endswith("statless.md"))["file_mtime"] is None


def test_duplicate_id_is_ambiguous():
    pages = [{"path": f"entities/{x}.md", "canonical_id": "same", "slug": x,
              "aliases": []} for x in ("a", "b")]
    assert P.resolve_link("same", P.identity_indexes(pages)) == (None, "ambiguous")


class Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False


class FakeConn:
    def __init__(self, known=None):
        self.known = known or []
        self.epoch = 7
        self.executed = []
        self.link_rows = []

    async def fetch(self, sql, *args):
        assert "SELECT path, content_digest" in sql
        return self.known

    async def fetchval(self, sql, *args):
        assert "projection_runs" in sql
        return self.epoch

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def executemany(self, sql, rows):
        self.link_rows.extend(rows)

    def transaction(self): return Tx()


def test_epoch_writes_complete_graph_and_dry_run(monkeypatch):
    row, links = P.parse_page("entities/a.md", b"---\nid: a\n---\n[[missing]]")
    row.update(content_digest="d", file_mtime=None)
    scan = P.Scan([row], {row["path"]: links}, [], [])
    conn = FakeConn([{"path": "entities/a.md", "content_digest": "d", "canonical_id": "a"}])
    ticks = iter((10.0, 22.5))
    monkeypatch.setattr(P, "_monotonic", lambda: next(ticks))
    dry = asyncio.run(P.run_epoch(conn, scan, dry_run=True))
    assert dry["unchanged"] == 0
    assert dry["elapsed_seconds"] == 12.5
    assert not conn.executed
    ticks = iter((20.0, 43.25))
    monkeypatch.setattr(P, "_monotonic", lambda: next(ticks))
    result = asyncio.run(P.run_epoch(conn, scan))
    assert result["epoch"] == 7
    assert result["unchanged"] == 1
    assert conn.link_rows == [("entities/a.md", "missing", None, "unresolved", "")]
    assert result["files_seen"] == 1 and result["reparsed"] == 0
    assert result["removed"] == 0 and result["links"] == 1 and result["fm_errors"] == 0
    page_write = next(args for sql, args in conn.executed if sql.startswith("INSERT INTO pages"))
    assert len(page_write) == 19 and page_write[-2] == 7, "epoch, then the snapshot time"
    assert result["elapsed_seconds"] == 23.25


def test_epoch_removed_count_excludes_new_pages():
    row, links = P.parse_page("entities/new.md", b"---\nid: new\n---\n")
    row.update(content_digest="new", file_mtime=None)
    known = [{"path": "entities/old.md", "content_digest": "old", "canonical_id": "old"}]
    result = asyncio.run(P.run_epoch(
        FakeConn(known), P.Scan([row], {row["path"]: links}, [], [])))
    assert result["removed"] == 1


def test_epoch_batches_every_link(monkeypatch):
    row, links = P.parse_page("entities/a.md", b"---\nid: a\n---\n[[one]] [[two]]")
    row.update(content_digest="d", file_mtime=None)
    monkeypatch.setattr(P, "BATCH", 1)
    conn = FakeConn()
    result = asyncio.run(P.run_epoch(conn, P.Scan([row], {row["path"]: links}, [], [])))
    assert result["links"] == 2
    assert [item[1] for item in conn.link_rows] == ["one", "two"]


def test_epoch_dual_deletion_guard(monkeypatch):
    monkeypatch.setattr(P, "MAX_DELETE_ABS", 1)
    monkeypatch.setattr(P, "MAX_DELETE_PCT", 2)
    known = [{"path": f"entities/{n}.md", "content_digest": f"x{n}", "canonical_id": f"e{n}"}
             for n in range(3)]
    with pytest.raises(RuntimeError, match="would remove 3 of 3"):
        asyncio.run(P.run_epoch(FakeConn(known), P.Scan([], {}, [], [])))


def test_cli_paths(monkeypatch, capsys):
    args = type("Args", (), {"dry_run": False})()
    monkeypatch.setattr(P, "DSN", "")
    assert asyncio.run(P._run(args)) == 2
    assert "WRITER_DSN" in capsys.readouterr().err

    class Connection:
        closed = False
        async def close(self): self.closed = True
    connection = Connection()
    async def connect(_dsn): return connection
    async def epoch(_conn, _scan, dry_run=False): return {"dry": dry_run}
    async def state(_conn): return {}, {}
    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(P, "DSN", "postgresql://fixture")
    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_vault", lambda **_kwargs: P.Scan([], {}, [], []))
    monkeypatch.setattr(P, "run_epoch", epoch)
    args.dry_run = True
    assert asyncio.run(P._run(args)) == 0
    assert connection.closed is True
    assert '"dry": true' in capsys.readouterr().out

    async def projected(_conn): return {"mode": "incremental"}
    monkeypatch.setattr(P, "project", projected)
    connection.closed = False
    args.dry_run = False
    assert asyncio.run(P._run(args)) == 0
    assert connection.closed is True
    assert '"mode": "incremental"' in capsys.readouterr().out

    async def ok(_args): return 0
    monkeypatch.setattr(P, "_run", ok)
    assert P.main([]) == 0

    async def bad(_args): raise RuntimeError("boom")
    monkeypatch.setattr(P, "_run", bad)
    assert P.main([]) == 1
    assert "boom" in capsys.readouterr().err


def test_load_projection_state():
    class Connection:
        async def fetch(self, sql):
            if "FROM pages" in sql:
                return [{"path": "entities/a.md", "fm": {}, "content_digest": "d"}]
            return [{"src_path": "entities/a.md", "target_ref": "x", "section": "s"}]
    known, links = asyncio.run(P.load_projection_state(Connection()))
    assert known["entities/a.md"]["content_digest"] == "d"
    assert links == {"entities/a.md": [P.Link("x", "s")]}


# --- okengine#605: a page moved is not a page removed --------------------------------------------

def known_row(path, digest, canonical=None):
    """A row shaped like the real `SELECT path, content_digest, canonical_id FROM pages`."""
    return {"path": path, "content_digest": digest, "canonical_id": canonical}


def scanned_page(path, digest, canonical=None):
    return {"path": path, "content_digest": digest, "canonical_id": canonical}


def test_a_page_is_identified_by_its_id_and_falls_back_to_its_bytes():
    """A page with no `id:` still has to survive a reshelve, and a rename does not touch a byte."""
    assert P.page_identity(known_row("a.md", "d", "entity-a")) == "entity-a"
    assert P.page_identity(known_row("a.md", "d", None)) == "digest:d"


def test_a_reshelved_page_is_moved_not_vanished():
    """The live case: a partition deepening moved 160 of 2,749 pages and the guard read it as a
    5.8% deletion, freezing the projection for hours."""
    known = [known_row("entities/b/acme.md", "d1", "entity-acme")]
    scanned = [scanned_page("entities/b/a/acme.md", "d1", "entity-acme")]
    vanished, moved = P.classify_removals(known, {"entities/b/acme.md"}, scanned)
    assert vanished == [] and moved == ["entities/b/acme.md"]


def test_a_reshelved_page_without_an_id_is_tracked_by_content():
    known = [known_row("notes/x.md", "same-bytes", None)]
    scanned = [scanned_page("notes/n/x.md", "same-bytes", None)]
    vanished, moved = P.classify_removals(known, {"notes/x.md"}, scanned)
    assert vanished == [] and moved == ["notes/x.md"]


def test_a_genuinely_deleted_page_still_counts_as_vanished():
    """The guard must keep its teeth: identity absent from the vault entirely."""
    known = [known_row("entities/b/gone.md", "d1", "entity-gone")]
    vanished, moved = P.classify_removals(known, {"entities/b/gone.md"}, [])
    assert vanished == ["entities/b/gone.md"] and moved == []


def test_a_row_whose_path_is_unchanged_is_neither_moved_nor_vanished():
    known = [known_row("entities/a.md", "d", "entity-a")]
    assert P.classify_removals(known, set(), [scanned_page("entities/a.md", "d", "entity-a")]) \
        == ([], [])


def test_a_reshard_no_longer_trips_the_deletion_guard(monkeypatch):
    """End to end through run_epoch: every stale path is a move, so the epoch completes."""
    monkeypatch.setattr(P, "MAX_DELETE_ABS", 1)
    monkeypatch.setattr(P, "MAX_DELETE_PCT", 2)
    known = [known_row(f"entities/{n}.md", f"d{n}", f"e{n}") for n in range(3)]
    pages = []
    for n in range(3):
        row, _ = P.parse_page(f"entities/{n}/x{n}.md", f"---\nid: e{n}\n---\n".encode())
        row.update(content_digest=f"d{n}", file_mtime=None, canonical_id=f"e{n}")
        pages.append(row)
    result = asyncio.run(P.run_epoch(FakeConn(known), P.Scan(pages, {}, [], [])))
    assert result["moved"] == 3 and result["vanished"] == 0
    assert result["removed"] == 3, "the stale rows are still deleted; only the VERDICT changed"


def test_an_unmounted_vault_still_trips_the_guard_and_says_what_moved(monkeypatch):
    """The failure this guard exists for. A vault that did not mount loses every identity, not
    just every path, so it must still refuse — and the message must separate the two counts."""
    monkeypatch.setattr(P, "MAX_DELETE_ABS", 1)
    monkeypatch.setattr(P, "MAX_DELETE_PCT", 2)
    known = [known_row(f"entities/{n}.md", f"d{n}", f"e{n}") for n in range(3)]
    with pytest.raises(RuntimeError, match=r"would remove 3 of 3 pages \(100.0%\).*0 other row"):
        asyncio.run(P.run_epoch(FakeConn(known), P.Scan([], {}, [], [])))


def test_a_mixed_epoch_counts_only_the_truly_absent_against_the_budget(monkeypatch):
    """Two moved, one deleted, with a budget of one: the deletion alone is under it, so the
    reshelve must not push the epoch over."""
    monkeypatch.setattr(P, "MAX_DELETE_ABS", 1)
    monkeypatch.setattr(P, "MAX_DELETE_PCT", 2)
    known = [known_row("entities/a.md", "da", "ea"), known_row("entities/b.md", "db", "eb"),
             known_row("entities/c.md", "dc", "ec")]
    pages = []
    for name, ident in (("a", "ea"), ("b", "eb")):
        row, _ = P.parse_page(f"entities/{name}/x.md", f"---\nid: {ident}\n---\n".encode())
        row.update(content_digest=f"d{name}", file_mtime=None, canonical_id=ident)
        pages.append(row)
    result = asyncio.run(P.run_epoch(FakeConn(known), P.Scan(pages, {}, [], [])))
    assert result["moved"] == 2 and result["vanished"] == 1 and result["removed"] == 3


# --- indexed_at is the snapshot's as-of time, not the row's write time ---------------------------

def test_the_scan_records_when_it_started_reading(tmp_path):
    """The walk's start is the only honest as-of time for the rows it produces."""
    (tmp_path / "wiki/entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("partitioning:\n  namespaces:\n    entities: {}\n")
    (tmp_path / "wiki/entities/a.md").write_text("---\nid: a\n---\n", encoding="utf-8")
    before = P.datetime.now(P.timezone.utc)
    scan = P.scan_vault(tmp_path)
    assert scan.scanned_at is not None and scan.scanned_at >= before


def test_indexed_at_is_stamped_from_the_scan_start_not_the_write():
    """The defect, measured on a live 50,000-page vault: the scan READ a page at 04:18, a lane
    rewrote it at 04:20, and the epoch committed at 04:28 stamping indexed_at=04:28. The digest
    check skips a page whose mtime is newer than indexed_at — 04:20 < 04:28, so it did not skip,
    and reported a page it had never actually compared as `digest drift`.

    Those two times are minutes apart on a large vault. Stamping the moment the walk began makes
    "changed after we looked at it" answerable, which is the question the guard is asking."""
    row, links = P.parse_page("entities/a.md", b"---\nid: a\n---\n")
    row.update(content_digest="d", file_mtime=None)
    scanned = P.datetime(2026, 8, 19, 4, 18, tzinfo=P.timezone.utc)
    scan = P.Scan([row], {row["path"]: links}, [], [], scanned_at=scanned)
    conn = FakeConn()
    asyncio.run(P.run_epoch(conn, scan))
    page_write = next(a for sql, a in conn.executed if sql.startswith("INSERT INTO pages"))
    assert page_write[-1] == scanned, "indexed_at must be the scan's as-of time"
    sql = next(s for s, _ in conn.executed if s.startswith("INSERT INTO pages"))
    assert "indexed_at=$19" in sql and "indexed_at=now()" not in sql


def test_a_scan_without_a_recorded_start_still_stamps_something(monkeypatch):
    """A hand-assembled Scan (a caller, or an older pickle) must not write a NULL as-of time —
    that would make every page look infinitely stale to the freshness guard."""
    row, links = P.parse_page("entities/a.md", b"---\nid: a\n---\n")
    row.update(content_digest="d", file_mtime=None)
    conn = FakeConn()
    asyncio.run(P.run_epoch(conn, P.Scan([row], {row["path"]: links}, [], [])))
    page_write = next(a for sql, a in conn.executed if sql.startswith("INSERT INTO pages"))
    assert isinstance(page_write[-1], P.datetime) and page_write[-1].tzinfo is not None


def test_journal_delta_requires_contiguous_epochs_and_returns_canonical_paths(tmp_path):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    (state / "epoch").write_text("3\n")
    rows = [
        {"epoch": 2, "affected_paths": [{"path": "wiki/entities/a.md"}]},
        {"epoch": 3, "affected_paths": [{"path": "wiki/entities/b.md"},
                                          {"path": ".okengine/private.json"}]},
    ]
    (state / "journal.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert P.journal_delta(tmp_path, 1) == (
        3, {"wiki/entities/a.md", "wiki/entities/b.md"})
    assert P.journal_delta(tmp_path, 0) is None


def test_journal_delta_empty_and_unreadable_paths(tmp_path):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    (state / "epoch").write_text("2\n")
    assert P.journal_delta(tmp_path, 2) == (2, set())
    assert P.journal_delta(tmp_path, 1) is None
    (state / "journal.jsonl").write_text("{broken\n")
    assert P.journal_delta(tmp_path, 1) is None


def _restored_vault(tmp_path: Path, epoch: int) -> Path:
    """A vault restored from a backup taken at `epoch`: its epoch file and journal stop there."""
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    (state / "epoch").write_text(f"{epoch}\n")
    (state / "journal.jsonl").write_text("".join(
        json.dumps({"epoch": n, "affected_paths": [{"path": f"wiki/entities/p{n}.md"}]}) + "\n"
        for n in range(1, epoch + 1)))
    return tmp_path


def test_journal_delta_refuses_a_corpus_epoch_that_went_backwards_after_a_restore(tmp_path):
    """okengine#1032: the projection checkpoint is at 10, the vault was restored to 5. The empty
    contiguity range for 11..5 used to pass and return (5, set()) -- keep every pre-restore row."""
    vault_path = _restored_vault(tmp_path, 5)
    assert P.journal_delta(vault_path, 10) is None, (
        "an epoch regression must force a full reconcile, not an empty incremental delta")
    assert P.journal_delta(vault_path, 4) == (5, {"wiki/entities/p5.md"})


def test_project_reconciles_a_restored_vault_instead_of_keeping_pre_restore_rows(
    tmp_path, monkeypatch
):
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, _sql): pass
        async def fetchval(self, _sql):
            return 10                     # last successful projection, before the restore

    async def state(_conn):
        return {"entities/after-backup.md": {"path": "entities/after-backup.md", "fm": {}}}, {}

    async def capture(_conn, scan, **kwargs):
        return {"paths": [row["path"] for row in scan.pages], **kwargs}

    restored = P.Scan([{"path": "entities/p1.md"}], {}, [], [])
    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_vault", lambda *_args, **_kwargs: restored)
    monkeypatch.setattr(P, "scan_changed", lambda *_args: P.Scan([], {}, [], []))
    monkeypatch.setattr(P, "run_epoch", capture)
    result = asyncio.run(P.project(Conn(), _restored_vault(tmp_path, 5)))
    assert result == {"paths": ["entities/p1.md"], "source_epoch": 5}, (
        f"a restored vault must be reconciled from disk, not projected incrementally: {result}")


def test_scan_changed_parses_only_live_journal_pages(tmp_path):
    (tmp_path / "wiki/entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {}\n")
    live = tmp_path / "wiki/entities/live.md"
    live.write_text("---\nid: live\ntype: entity\n---\nbody\n")
    scan = P.scan_changed(tmp_path, {"wiki/entities/live.md", "wiki/entities/deleted.md"})
    assert [row["path"] for row in scan.pages] == ["entities/live.md"]
    assert scan.bytes_parsed == live.stat().st_size


def test_scan_changed_counts_excluded_live_pages(tmp_path):
    (tmp_path / "wiki/other").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {}\n")
    hidden = tmp_path / "wiki/other/_hidden.md"
    hidden.write_text("body")
    scan = P.scan_changed(tmp_path, {"wiki/other/_hidden.md"})
    assert scan.pages == [] and scan.excluded == 1


def test_project_incremental_reuses_database_rows_and_parses_only_delta(tmp_path, monkeypatch):
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, _sql): pass
        async def fetchval(self, _sql):
            return 4

    prior = {"entities/keep.md": {
        "path": "entities/keep.md", "namespace": "entities", "canonical_id": "keep",
        "slug": "keep", "type": "entity", "title": "Keep", "status": None,
        "is_tombstoned": False, "superseded_by": None, "published": None,
        "ingested": None, "updated": None, "fm": {"id": "keep"}, "body_chars": 1,
        "content_digest": "old", "fm_error": None, "file_mtime": None,
    }}
    changed_row = dict(prior["entities/keep.md"], path="entities/new.md", slug="new",
                       canonical_id="new", content_digest="new")

    async def state(_conn):
        return prior, {"entities/keep.md": [P.Link("entities/x", "")]}

    async def capture(_conn, scan, **kwargs):
        return {"paths": [row["path"] for row in scan.pages], **kwargs}

    monkeypatch.setattr(P, "corpus_epoch", lambda _vault: 5)
    monkeypatch.setattr(P, "journal_delta", lambda _vault, _after: (
        5, {"wiki/entities/new.md"}))
    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_changed", lambda _vault, _paths: P.Scan(
        [changed_row], {"entities/new.md": []}, [], [], bytes_parsed=12))
    monkeypatch.setattr(P, "run_epoch", capture)
    result = asyncio.run(P.project(Conn(), tmp_path))
    assert result == {"paths": ["entities/keep.md", "entities/new.md"],
                      "source_epoch": 5, "mode": "incremental"}


def test_project_incremental_skips_replaced_rows_and_decodes_stored_fm(tmp_path, monkeypatch):
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, _sql): pass
        async def fetchval(self, _sql):
            return 4

    prior = {
        "entities/replaced.md": {"path": "entities/replaced.md", "fm": {}},
        "entities/keep.md": {"path": "entities/keep.md", "fm": '{"aliases":["k"]}'},
    }

    async def state(_conn):
        return prior, {}

    async def capture(_conn, scan, **kwargs):
        assert [row["path"] for row in scan.pages] == ["entities/keep.md"]
        assert scan.pages[0]["aliases"] == ["k"]
        return kwargs

    monkeypatch.setattr(P, "corpus_epoch", lambda _vault: 5)
    monkeypatch.setattr(P, "journal_delta", lambda *_args: (
        5, {"wiki/entities/replaced.md"}))
    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_changed", lambda *_args: P.Scan([], {}, [], []))
    monkeypatch.setattr(P, "run_epoch", capture)
    assert asyncio.run(P.project(Conn(), tmp_path))["mode"] == "incremental"


@pytest.mark.parametrize("checkpoint,delta", [(None, "unused"), (4, None)])
def test_project_reconciles_when_checkpoint_or_journal_is_unusable(
    tmp_path, monkeypatch, checkpoint, delta
):
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, _sql): pass
        async def fetchval(self, _sql):
            return checkpoint

    async def state(_conn):
        return {}, {}

    async def capture(_conn, _scan, **kwargs):
        return kwargs

    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_vault", lambda *_args, **_kwargs: P.Scan([], {}, [], []))
    monkeypatch.setattr(P, "run_epoch", capture)
    monkeypatch.setattr(P, "corpus_epoch", lambda _vault: 5)
    monkeypatch.setattr(P, "journal_delta", lambda *_args: delta)
    assert asyncio.run(P.project(Conn(), tmp_path))["source_epoch"] == 5


def test_project_force_reconcile_bypasses_a_usable_incremental_checkpoint(
    tmp_path, monkeypatch
):
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, _sql): pass
        async def fetchval(self, _sql):
            raise AssertionError("forced reconciliation must not read the checkpoint")

    async def state(_conn):
        return {}, {}

    async def capture(_conn, _scan, **kwargs):
        return kwargs

    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_vault", lambda *_args, **_kwargs: P.Scan([], {}, [], []))
    monkeypatch.setattr(P, "run_epoch", capture)
    monkeypatch.setattr(P, "corpus_epoch", lambda _vault: 5)
    result = asyncio.run(P.project(Conn(), tmp_path, force_reconcile=True))
    assert result["source_epoch"] == 5


def test_project_reconciliation_refuses_moving_or_active_corpus(tmp_path, monkeypatch):
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, _sql): pass
        async def fetchval(self, _sql):
            return None

    async def state(_conn):
        return {}, {}

    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_vault", lambda *_args, **_kwargs: P.Scan([], {}, [], []))
    values = iter(range(7))
    monkeypatch.setattr(P, "corpus_epoch", lambda _vault: next(values))
    with pytest.raises(RuntimeError, match="changed during projection"):
        asyncio.run(P.project(Conn(), tmp_path))


def test_project_reconciliation_retries_an_active_corpus(tmp_path, monkeypatch):
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, _sql): pass
        async def fetchval(self, _sql):
            return None

    async def state(_conn):
        return {}, {}

    async def capture(_conn, _scan, **kwargs):
        return kwargs

    state_dir = tmp_path / ".okengine/corpus"
    state_dir.mkdir(parents=True)
    active = state_dir / "active.json"
    active.write_text("{}")
    scans = 0

    def scan(*_args, **_kwargs):
        nonlocal scans
        scans += 1
        return P.Scan([], {}, [], [])

    calls = 0

    def epoch(_vault):
        nonlocal calls
        calls += 1
        if calls == 3:
            active.unlink(missing_ok=True)
        return 2

    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_vault", scan)
    monkeypatch.setattr(P, "run_epoch", capture)
    monkeypatch.setattr(P, "corpus_epoch", epoch)
    result = asyncio.run(P.project(Conn(), tmp_path))
    assert result["source_epoch"] == 2 and scans == 1


# ── paginated directory indexes are structure, not content ──────────────────
#
# build_index_tree writes INDEX-pNN.md beside INDEX.md. Only INDEX.md was reserved, so page-2+
# indexes were projected as content; their writer does not journal, so the incremental projector
# never re-read them and every sampled one reported "digest drift" hours after the nightly rebuild
# (84 rows on one live vault).

@pytest.mark.parametrize("name,structural", [
    ("INDEX.md", True), ("INDEX-p02.md", True), ("index-p10.md", True), ("INDEX-p123.md", True),
    ("HOT.md", True),
    ("INDEX-page.md", False), ("myINDEX-p02.md", False), ("notes-p02.md", False),
    ("INDEX-p.md", False), ("entity.md", False),
])
def test_structural_names_cover_paged_indexes_and_nothing_else(name, structural):
    assert P._structural(name) is structural


def test_full_scan_excludes_paged_indexes(tmp_path):
    root = vault(tmp_path)
    (root / "wiki/entities/a.md").write_text("---\nid: a\n---\n")
    (root / "wiki/entities/INDEX-p02.md").write_text("derived page 2")
    scan = P.scan_vault(root)
    assert [row["path"] for row in scan.pages] == ["entities/a.md"]
    assert scan.excluded == 1


def test_incremental_scan_excludes_paged_indexes(tmp_path):
    root = vault(tmp_path)
    (root / "wiki/entities/INDEX-p03.md").write_text("derived page 3")
    scan = P.scan_changed(root, {"wiki/entities/INDEX-p03.md"})
    assert scan.pages == [] and scan.excluded == 1


def test_incremental_pass_drops_a_paged_index_projected_before_the_fix(tmp_path, monkeypatch):
    """Rows already in the database are carried forward by the incremental pass; a stale paged
    index row must be dropped there, or the drift outlives the fix until a full reconcile."""
    class Conn:
        def transaction(self): return Tx()
        async def execute(self, _sql): pass
        async def fetchval(self, _sql):
            return 4

    # The paged index comes FIRST so a `continue` that became `break` would also lose keep.md.
    prior = {
        "entities/c/v/INDEX-p45.md": {"path": "entities/c/v/INDEX-p45.md", "fm": {}},
        "entities/keep.md": {"path": "entities/keep.md", "fm": {}},
    }

    async def state(_conn):
        return prior, {}

    async def capture(_conn, scan, **kwargs):
        return {"paths": [row["path"] for row in scan.pages], **kwargs}

    monkeypatch.setattr(P, "corpus_epoch", lambda _vault: 5)
    monkeypatch.setattr(P, "journal_delta", lambda *_args: (5, set()))
    monkeypatch.setattr(P, "load_projection_state", state)
    monkeypatch.setattr(P, "scan_changed", lambda *_args: P.Scan([], {}, [], []))
    monkeypatch.setattr(P, "run_epoch", capture)
    assert asyncio.run(P.project(Conn(), tmp_path))["paths"] == ["entities/keep.md"]


def test_a_surrogate_pair_in_frontmatter_is_projected_as_its_character():
    """okengine#894: a JSON surrogate-pair escape in YAML decodes as two halves; the projection
    stores the one character they encode, and it is not an error."""
    row, _ = P.parse_page("sources/a.md",
                          b'---\ntitle: "Providers \\ud83d\\udd25"\naliases: ["F \\ud83d\\ude00"]\n'
                          b'version: 3\npublished: 2026-08-06\n---\n')
    assert row["title"] == "Providers \U0001F525"
    assert row["aliases"] == ["F \U0001F600"]
    assert row["fm_error"] is None, "numbers and dates are not text and never flag a page"


def test_an_unpaired_surrogate_early_in_frontmatter_still_flags_the_page():
    """Every later field is clean; the flag from the first one must survive them."""
    row, _ = P.parse_page("sources/d.md", b'---\ntitle: "Lone \\ud83d"\ntype: source\nversion: 2\n---\n')
    assert "unpaired UTF-16 surrogate" in row["fm_error"]


def test_an_unpaired_surrogate_is_replaced_and_reported_never_dropped():
    """okengine#894: one unpaired half made asyncpg refuse the row and aborted the whole rebuild
    (4,483 of 8,382 pages unprojected). The page is projected with U+FFFD and flagged instead."""
    raw = b'---\ntitle: "Lone \\ud83d"\ntags: ["\\udd25"]\n"k\\udd25": {"n": ["\\ud83d"]}\n---\n# x\n'
    row, _ = P.parse_page("sources/b.md", raw)
    assert row["title"] == "Lone \ufffd"
    assert "unpaired UTF-16 surrogate" in row["fm_error"]
    fm = json.loads(row["fm"])
    assert fm == {"title": "Lone \ufffd", "tags": ["\ufffd"], "k\ufffd": {"n": ["\ufffd"]}}
    for value in (row["title"], row["fm"]):
        value.encode("utf-8")


def test_unreadable_scan_cannot_delete_pages_or_publish_a_successful_epoch():
    scan = P.Scan([], {}, [], ["entities/private.md: PermissionError"])
    conn = FakeConn([known_row("entities/private.md", "digest", "private")])
    with pytest.raises(RuntimeError, match="population is incomplete"):
        asyncio.run(P.run_epoch(conn, scan))
    assert conn.executed == [] and conn.link_rows == []
    with pytest.raises(RuntimeError, match="unreadable"):
        asyncio.run(P.run_epoch(conn, scan, dry_run=True))


@pytest.mark.parametrize("blocked,expected", [("wiki", [".: PermissionError"]),
    ("wiki/entities", ["entities: PermissionError"]), ("wiki/operational", []),
    ("wiki/-excluded", [])])
def test_walk_errors_preserve_unknown_population_but_respect_excluded_namespaces(
        tmp_path, monkeypatch, blocked, expected):
    import os
    root = vault(tmp_path)
    (root / "wiki/operational").mkdir()
    (root / "wiki/-excluded").mkdir()
    original = os.scandir
    def scandir(path):
        if Path(path) == root / blocked:
            raise PermissionError(13, "fixture directory is unreadable", str(path))
        return original(path)
    monkeypatch.setattr(os, "scandir", scandir)
    scan = P.scan_vault(root)
    assert scan.unreadable == expected
    if expected:
        with pytest.raises(RuntimeError, match="population is incomplete"):
            P.require_readable_scan(scan)


@pytest.mark.parametrize("boundary", ["stat", "read_bytes"])
def test_incremental_unknown_path_cannot_become_deletion(tmp_path, monkeypatch, boundary):
    root = vault(tmp_path)
    private = root / "wiki/entities/private.md"
    private.write_text("---\nid: private\n---\n")
    original = getattr(Path, boundary)

    def inaccessible(path, *args, **kwargs):
        if path == private:
            raise PermissionError("owner-only fixture")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, boundary, inaccessible)
    scan = P.scan_changed(root, {"wiki/entities/private.md"})
    assert scan.unreadable == ["wiki/entities/private.md: PermissionError"]
    conn = FakeConn([known_row("entities/private.md", "digest", "private")])
    with pytest.raises(RuntimeError, match="population is incomplete"):
        asyncio.run(P.run_epoch(conn, scan))
    assert conn.executed == []


def test_incremental_non_file_path_is_not_parsed(tmp_path):
    root = vault(tmp_path)
    (root / "wiki/entities/folder.md").mkdir()
    scan = P.scan_changed(root, {"wiki/entities/folder.md"})
    assert scan.pages == [] and scan.unreadable == []


def test_incremental_non_file_does_not_hide_a_later_changed_page(tmp_path):
    root = vault(tmp_path)
    (root / "wiki/entities/a-folder.md").mkdir()
    (root / "wiki/entities/z-page.md").write_text("---\nid: later\n---\n")
    scan = P.scan_changed(root, {"wiki/entities/a-folder.md", "wiki/entities/z-page.md"})
    assert [row["path"] for row in scan.pages] == ["entities/z-page.md"]
    assert scan.unreadable == []


def test_unreadable_full_scan_preserves_later_readable_population(tmp_path, monkeypatch):
    root = vault(tmp_path)
    blocked = root / "wiki/entities/a-private.md"
    blocked.write_text("---\nid: private\n---\n")
    (root / "wiki/entities/z-page.md").write_text("---\nid: later\n---\n")
    real_stat = Path.stat

    def inaccessible(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("unknown file type")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", create_autospec(Path.stat, side_effect=inaccessible))
    scan = P.scan_vault(root)
    assert scan.unreadable == ["entities/a-private.md: PermissionError"]
    assert [row["path"] for row in scan.pages] == ["entities/z-page.md"]
    with pytest.raises(RuntimeError, match="population is incomplete"):
        P.require_readable_scan(scan)


def test_incomplete_population_diagnostic_retains_total_and_three_examples():
    unreadable = [f"entities/private-{i}.md: PermissionError" for i in range(5)]
    scan = P.Scan([], {}, [], unreadable)
    with pytest.raises(RuntimeError) as failure:
        P.require_readable_scan(scan)
    message = str(failure.value)
    assert "5 unreadable path(s)" in message
    assert all(path in message for path in unreadable[:3])
    assert all(path not in message for path in unreadable[3:])
    assert scan.unreadable == unreadable, "the complete evidence must survive diagnostic truncation"


def test_full_scan_records_inaccessible_type_stat_instead_of_skipping(tmp_path, monkeypatch):
    root = vault(tmp_path)
    private = root / "wiki/entities/private.md"
    private.write_text("---\nid: private\n---\n")
    original = Path.stat

    def inaccessible(path, *args, **kwargs):
        if path == private:
            raise PermissionError("unknown file type")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inaccessible)
    scan = P.scan_vault(root)
    assert scan.unreadable == ["entities/private.md: PermissionError"]
    assert scan.skipped_dirs == []
    with pytest.raises(RuntimeError, match="population is incomplete"):
        P.require_readable_scan(scan)
