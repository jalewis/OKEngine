"""review_reconcile — prune wiki/_review-queue.md back to one outstanding row per live page.

The contract under test: a row is dropped ONLY when its death is provable from the vault
(page gone everywhere, or page present and demonstrably no longer open), the preamble and
newest-first order survive untouched, and an absent/unmounted vault refuses rather than
grading every row dead.
"""
import importlib.util
from datetime import date
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "review_reconcile.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="review_reconcile absent")

PREAMBLE = ("---\ntitle: Review Queue\n---\n\n"
            "# Review Queue\n\nAgent-flagged pages awaiting human review "
            "(highlight, not a gate — the writes already landed).\n\n")


def _load(vault: Path):
    spec = importlib.util.spec_from_file_location("review_reconcile", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.VAULT = vault
    m.WIKI = vault / "wiki"
    return m


def _page(vault: Path, rel: str, *, needs_review=False, extra="", body="body\n"):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = "---\ntype: concept\nname: X\n"
    if needs_review:
        fm += "needs_review: true\n"
    fm += extra + "---\n\n"
    p.write_text(fm + body, encoding="utf-8")
    return p


def _record(vault: Path, subject: str, state: str, name: str):
    store = vault / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{name}.yaml").write_text(
        yaml.safe_dump({"version": 1, "review_id": f"review:{subject}:1:a:b",
                        "subject": subject, "state": state}), encoding="utf-8")


def _queue(vault: Path, rows: str):
    q = vault / "wiki" / "_review-queue.md"
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(PREAMBLE + rows, encoding="utf-8")
    return q


def _run(m, *argv):
    return m.main(list(argv))


def _rows(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("- 2")]


# --- dispositions ---------------------------------------------------------

def test_phantom_row_is_dropped(tmp_path, capsys):
    """A `slug id collision on create` queues the path the REJECTED create wanted. No page was
    ever written there, the basename resolves nowhere, and the row can never be actioned."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/other.md")
    q = _queue(tmp_path, "- 2026-08-01 **concepts/never-created.md** — slug id collision on create\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == []
    assert "phantom=1" in capsys.readouterr().out


def test_live_flagged_page_is_kept_verbatim(tmp_path):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/live.md", needs_review=True)
    row = "- 2026-07-02 **concepts/live.md** — needs_review flag set\n"
    q = _queue(tmp_path, row)
    assert _run(m) == 0
    assert _rows(q.read_text()) == [row.rstrip("\n")]      # original DATE preserved, not restamped


def test_cleared_page_is_dropped(tmp_path, capsys):
    """Page still exists, flag gone, no open record — e.g. review-autoverify cleared it."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/done.md", needs_review=False)
    q = _queue(tmp_path, "- 2026-07-02 **concepts/done.md** — needs_review flag set\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == []
    assert "cleared=1" in capsys.readouterr().out


def test_open_review_record_keeps_an_unflagged_page(tmp_path):
    """`_flag` rows carry no page state, so the record store is the second liveness signal.
    Dropping a row whose record is still open would hide it from the human queue entirely."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/recorded.md", needs_review=False)
    _record(tmp_path, "concepts/recorded", "open", "r1")
    q = _queue(tmp_path, "- 2026-07-02 **concepts/recorded.md** — field `type` caller attempted 'x'\n")
    assert _run(m) == 0
    assert len(_rows(q.read_text())) == 1


def test_closed_review_record_does_not_keep_the_row(tmp_path):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/settled.md", needs_review=False)
    _record(tmp_path, "concepts/settled", "approved", "r1")
    q = _queue(tmp_path, "- 2026-07-02 **concepts/settled.md** — field `type` caller attempted 'x'\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == []


def test_relocated_page_has_its_path_rewritten(tmp_path, capsys):
    """A reshard moves the page; the row's recorded path goes stale (okengine#336/#54).
    The row is still live, so repair the path rather than dropping a real item."""
    m = _load(tmp_path)
    _page(tmp_path, "entities/a/c/acme.md", needs_review=True)
    q = _queue(tmp_path, "- 2026-07-02 **entities/acme.md** — needs_review flag set\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == ["- 2026-07-02 **entities/a/c/acme.md** — needs_review flag set"]
    assert "relocated=1" in capsys.readouterr().out


def test_relocated_page_that_is_cleared_is_still_dropped(tmp_path):
    m = _load(tmp_path)
    _page(tmp_path, "entities/a/c/acme.md", needs_review=False)
    q = _queue(tmp_path, "- 2026-07-02 **entities/acme.md** — needs_review flag set\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == []


def test_ambiguous_basename_is_kept_not_guessed(tmp_path, capsys):
    """Two candidates is not proof of death OR of location — keep the row and count it."""
    m = _load(tmp_path)
    _page(tmp_path, "entities/a/c/acme.md")
    _page(tmp_path, "concepts/a/c/acme.md")
    q = _queue(tmp_path, "- 2026-07-02 **entities/acme.md** — needs_review flag set\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == ["- 2026-07-02 **entities/acme.md** — needs_review flag set"]
    assert "ambiguous=1" in capsys.readouterr().out


def test_duplicate_rows_collapse_keeping_the_newest(tmp_path, capsys):
    """The queue is newest-first, so the FIRST occurrence is the newest. Folds in
    scripts/dedupe_review_queue.py, which was never scheduled."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/dup.md", needs_review=True)
    q = _queue(tmp_path,
               "- 2026-08-01 **concepts/dup.md** — newest reason\n"
               "- 2026-07-01 **concepts/dup.md** — older reason\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == ["- 2026-08-01 **concepts/dup.md** — newest reason"]
    assert "duplicate=1" in capsys.readouterr().out


def test_relocation_onto_an_already_queued_page_collapses(tmp_path):
    """A stale path and its post-reshard path are the same page; keeping both would
    reintroduce the duplicate the rewrite was supposed to resolve."""
    m = _load(tmp_path)
    _page(tmp_path, "entities/a/c/acme.md", needs_review=True)
    q = _queue(tmp_path,
               "- 2026-08-01 **entities/a/c/acme.md** — current path\n"
               "- 2026-07-01 **entities/acme.md** — stale path\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == ["- 2026-08-01 **entities/a/c/acme.md** — current path"]


def test_unparseable_frontmatter_is_kept(tmp_path):
    """An unreadable page proves nothing; dropping its row would silently discard the one
    signal that something is wrong with it."""
    m = _load(tmp_path)
    p = tmp_path / "wiki" / "concepts" / "broken.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: [unclosed\n---\n\nbody\n", encoding="utf-8")
    q = _queue(tmp_path, "- 2026-07-02 **concepts/broken.md** — degenerate content\n")
    assert _run(m) == 0
    assert len(_rows(q.read_text())) == 1


# --- file integrity -------------------------------------------------------

def test_preamble_and_order_survive(tmp_path):
    m = _load(tmp_path)
    for n in ("a", "b", "c"):
        _page(tmp_path, f"concepts/{n}.md", needs_review=True)
    q = _queue(tmp_path,
               "- 2026-08-03 **concepts/a.md** — a\n"
               "- 2026-08-02 **concepts/gone.md** — slug id collision on create\n"
               "- 2026-08-01 **concepts/b.md** — b\n"
               "- 2026-07-30 **concepts/c.md** — c\n")
    assert _run(m) == 0
    text = q.read_text()
    assert text.startswith(PREAMBLE)                      # frontmatter + heading + prose intact
    assert _rows(text) == ["- 2026-08-03 **concepts/a.md** — a",
                           "- 2026-08-01 **concepts/b.md** — b",
                           "- 2026-07-30 **concepts/c.md** — c"]


def test_undo_copy_and_log_entry_are_written(tmp_path):
    """Rows are only safe to delete because every reason is durable in log.md — so record the
    prune there too, and keep one generation of the file itself."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/keep.md", needs_review=True)
    (tmp_path / "wiki" / "log.md").write_text("- existing\n", encoding="utf-8")
    q = _queue(tmp_path,
               "- 2026-08-01 **concepts/keep.md** — keep\n"
               "- 2026-08-01 **concepts/gone.md** — slug id collision on create\n")
    before = q.read_text()
    assert _run(m) == 0
    assert q.with_suffix(".md.prev").read_text() == before
    log = (tmp_path / "wiki" / "log.md").read_text()
    assert "review-reconcile pruned 1 row(s)" in log and log.startswith("- existing\n")
    # LOCAL date, matching write_server._today() — a UTC stamp put 2026-08-04 on a line logged at
    # 22:55 on the 3rd in the fleet's America/New_York containers, one day off every row near it.
    assert f"- {date.today().isoformat()} review-reconcile" in log


def test_ledger_date_is_local_and_honours_the_write_path_override(tmp_path, monkeypatch):
    """log.md is one ledger with two writers. A stamp that ignores the pin the write path honours
    leaves it half-pinned, which is worse than either convention alone."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/keep.md", needs_review=True)
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2019-01-02")
    _queue(tmp_path,
           "- 2026-08-01 **concepts/keep.md** — keep\n"
           "- 2026-08-01 **concepts/gone.md** — slug id collision on create\n")
    assert _run(m) == 0
    assert "- 2019-01-02 review-reconcile pruned" in (tmp_path / "wiki" / "log.md").read_text()
    monkeypatch.delenv("OKENGINE_MCP_WRITE_DATE")
    assert m._ledger_date() == date.today().isoformat()


def test_dry_run_writes_nothing(tmp_path):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/other.md")
    q = _queue(tmp_path, "- 2026-08-01 **concepts/gone.md** — slug id collision on create\n")
    before = q.read_text()
    assert _run(m, "--dry-run") == 0
    assert q.read_text() == before
    assert not q.with_suffix(".md.prev").exists()


def test_idempotent_second_pass_is_a_no_op(tmp_path):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/keep.md", needs_review=True)
    q = _queue(tmp_path,
               "- 2026-08-01 **concepts/keep.md** — keep\n"
               "- 2026-08-01 **concepts/gone.md** — slug id collision on create\n")
    assert _run(m) == 0
    once = q.read_text()
    assert _run(m) == 0
    assert q.read_text() == once


# --- refusals -------------------------------------------------------------

def test_empty_wiki_refuses_instead_of_deleting_everything(tmp_path, capsys):
    """An unmounted volume or a mid-move reshard presents as an empty wiki. Grading every row
    `phantom` there would delete the entire queue on a vault that is merely absent."""
    m = _load(tmp_path)
    q = _queue(tmp_path, "- 2026-08-01 **concepts/x.md** — needs_review flag set\n")
    before = q.read_text()
    assert _run(m) == 1
    assert q.read_text() == before
    assert "REFUSING" in capsys.readouterr().err


def test_missing_wiki_is_an_error(tmp_path, capsys):
    m = _load(tmp_path / "absent")
    assert _run(m) == 1
    assert "wiki not found" in capsys.readouterr().err


def test_absent_queue_is_a_clean_no_op(tmp_path, capsys):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/a.md")
    assert _run(m) == 0
    assert "nothing to reconcile" in capsys.readouterr().out


def test_structural_files_never_resolve_a_relocation(tmp_path):
    """INDEX/underscore files are not knowledge pages; letting one satisfy a basename lookup
    would rewrite a row onto a structural file."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/real.md")
    (tmp_path / "wiki" / "concepts" / "_scratch.md").write_text("---\ntype: x\n---\n", encoding="utf-8")
    q = _queue(tmp_path, "- 2026-08-01 **entities/_scratch.md** — flagged\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == []                      # phantom, not relocated onto _scratch


# --- degraded inputs ------------------------------------------------------

def test_unreadable_review_record_does_not_close_a_subject(tmp_path):
    """A record that will not parse is not evidence the subject is settled — the row must
    survive on the page's own state rather than on a corrupt record's silence."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/x.md", needs_review=True)
    store = tmp_path / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True)
    (store / "corrupt.yaml").write_text("state: [unclosed\n", encoding="utf-8")
    q = _queue(tmp_path, "- 2026-08-01 **concepts/x.md** — flagged\n")
    assert _run(m) == 0
    assert len(_rows(q.read_text())) == 1


def test_non_mapping_review_record_is_ignored(tmp_path):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/x.md", needs_review=False)
    store = tmp_path / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True)
    (store / "listy.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
    q = _queue(tmp_path, "- 2026-08-01 **concepts/x.md** — flagged\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == []       # ignored, so the page's cleared state decides


def test_open_record_without_a_subject_is_ignored(tmp_path):
    """An open record naming no subject cannot keep any row alive — it would otherwise add
    an empty string to the open set and silently match a page whose rel is ''."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/x.md", needs_review=False)
    store = tmp_path / "wiki" / "operational" / "reviews"
    store.mkdir(parents=True)
    (store / "nosubject.yaml").write_text("state: open\nsubject: ''\n", encoding="utf-8")
    q = _queue(tmp_path, "- 2026-08-01 **concepts/x.md** — flagged\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == []


def test_page_with_no_frontmatter_at_all_keeps_the_row(tmp_path):
    """A page with no frontmatter block cannot carry `needs_review`, so reading its absence as
    'cleared' would drop the row for every hand-written or truncated page in the vault."""
    m = _load(tmp_path)
    p = tmp_path / "wiki" / "concepts" / "bare.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Just a heading\n\nno frontmatter here\n", encoding="utf-8")
    q = _queue(tmp_path, "- 2026-08-01 **concepts/bare.md** — flagged\n")
    assert _run(m) == 0
    assert len(_rows(q.read_text())) == 1


def test_page_unreadable_mid_scan_keeps_the_row(tmp_path, monkeypatch):
    """A reshelve/curation race can remove the page between the index walk and the read.
    Losing the race must not be read as 'this row is dead'."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/racy.md", needs_review=False)
    real = Path.read_text

    def boom(self, *a, **kw):
        if self.name == "racy.md":
            raise OSError("vanished mid-scan")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", boom)
    q = _queue(tmp_path, "- 2026-08-01 **concepts/racy.md** — flagged\n")
    assert _run(m) == 0
    assert len(_rows(q.read_text())) == 1


def test_row_with_an_impossible_date_is_kept_and_not_counted_stale(tmp_path, capsys):
    """The row regex accepts any \\d{4}-\\d{2}-\\d{2}, so an impossible date reaches strptime.
    It must not crash the lane or masquerade as an ancient row."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/x.md", needs_review=True)
    q = _queue(tmp_path, "- 2026-13-45 **concepts/x.md** — flagged\n")
    assert _run(m) == 0
    assert len(_rows(q.read_text())) == 1
    assert "older than" not in capsys.readouterr().out


def test_stale_outstanding_rows_are_reported(tmp_path, capsys):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/x.md", needs_review=True)
    _queue(tmp_path, "- 2020-01-01 **concepts/x.md** — flagged\n")
    assert _run(m) == 0
    assert "older than 30d" in capsys.readouterr().out


def test_unwritable_log_does_not_fail_the_prune(tmp_path, capsys):
    """log.md is the durable copy, but a host-owned or read-only log must not abort a prune
    that has already been decided — the queue write is the point of the lane."""
    m = _load(tmp_path)
    _page(tmp_path, "concepts/keep.md", needs_review=True)
    (tmp_path / "wiki" / "log.md").mkdir()          # a directory: append raises IsADirectoryError
    q = _queue(tmp_path,
               "- 2026-08-01 **concepts/keep.md** — keep\n"
               "- 2026-08-01 **concepts/gone.md** — slug id collision on create\n")
    assert _run(m) == 0
    assert _rows(q.read_text()) == ["- 2026-08-01 **concepts/keep.md** — keep"]
    assert "could not append to wiki/log.md" in capsys.readouterr().err


def test_verbose_lists_every_disposition(tmp_path, capsys):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/keep.md", needs_review=True)
    _queue(tmp_path,
           "- 2026-08-01 **concepts/keep.md** — keep\n"
           "- 2026-08-01 **concepts/gone.md** — slug id collision on create\n")
    assert _run(m, "--verbose", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "phantom" in out and "concepts/gone.md" in out


def test_emits_wake_agent_false(tmp_path, capsys):
    m = _load(tmp_path)
    _page(tmp_path, "concepts/a.md")
    _queue(tmp_path, "- 2026-08-01 **concepts/a.md** — x\n")
    assert _run(m) == 0
    assert '{"wakeAgent": false}' in capsys.readouterr().out
