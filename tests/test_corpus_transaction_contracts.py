"""Behavioral contracts of the corpus fence, journal, and recovery protocol.

These pin observable on-disk and journal behavior the older suites left unasserted (okengine#612
critical-mutation ratchet): canonical marker/journal encoding, torn-tail repair of the journal,
transaction-identity lookup, recovery status labeling, and tracking-mode selection by value.
Everything runs against a real temporary deployment directory.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
corpus = importlib.import_module("okengine.corpus_transaction")

ISO_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


def _state(root: Path) -> Path:
    return root / ".okengine" / "corpus"


def _journal_lines(root: Path) -> list[str]:
    return (_state(root) / "journal.jsonl").read_text(encoding="utf-8").splitlines()


def _journal(root: Path) -> list[dict]:
    return [json.loads(line) for line in _journal_lines(root)]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _runtime_string(text: str) -> str:
    """Build an equal but distinct str object, as a value read from config or argv would be."""
    built = "".join(list(text))
    assert built == text
    return built


def _dead_active_marker(root: Path, transaction_id: str = "dead-tx") -> Path:
    state = _state(root)
    state.mkdir(parents=True, exist_ok=True)
    corpus._atomic_json(state / "active.json", {
        "transaction_id": transaction_id, "writer": "killed", "operation": "batch",
        "started_at": "2026-01-01T00:00:00+00:00", "before": {},
    })
    return state


# --- timestamps and identities ---------------------------------------------------------------


def test_journal_timestamps_are_whole_second_utc(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    with corpus.mutation(tmp_path, writer="w", operation="op"):
        (wiki / "page.md").write_text("x", encoding="utf-8")
    record = _journal(tmp_path)[-1]
    assert ISO_SECONDS.match(record["finished_at"]), record["finished_at"]
    assert ISO_SECONDS.match(corpus._now())


def test_transaction_id_is_128_bit_hex_and_journaled(tmp_path):
    (tmp_path / "wiki").mkdir()
    with corpus.mutation(tmp_path, writer="w", operation="op") as transaction_id:
        (tmp_path / "wiki" / "page.md").write_text("x", encoding="utf-8")
    assert re.fullmatch(r"[0-9a-f]{32}", transaction_id), transaction_id
    assert _journal(tmp_path)[-1]["transaction_id"] == transaction_id


# --- canonical on-disk encoding --------------------------------------------------------------


def test_atomic_json_writes_canonical_sorted_compact_json_and_creates_parents(tmp_path):
    target = tmp_path / "missing" / "nested" / "marker.json"
    value = {"zeta": 1, "alpha": {"y": 2, "b": 3}}
    corpus._atomic_json(target, value)
    assert target.read_text(encoding="utf-8") == _canonical(value) + "\n"
    assert not list(target.parent.glob(".*.tmp")), "the temporary file must be renamed away"


def test_journal_records_are_canonical_sorted_json(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    with corpus.mutation(tmp_path, writer="w", operation="op"):
        (wiki / "page.md").write_text("x", encoding="utf-8")
    [line] = _journal_lines(tmp_path)
    assert line == _canonical(json.loads(line))


def test_prepared_marker_declares_protocol_version_one(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    original_append = corpus._append

    def crash_before_journal(state, record):
        raise RuntimeError("crash before journal")

    monkeypatch.setattr(corpus, "_append", crash_before_journal)
    with pytest.raises(RuntimeError, match="crash before journal"):
        with corpus.mutation(tmp_path, writer="w", operation="op") as transaction_id:
            (wiki / "page.md").write_text("x", encoding="utf-8")
    prepared = json.loads((_state(tmp_path) / "commit.json").read_text(encoding="utf-8"))
    assert prepared["version"] == 1
    assert prepared["record"]["transaction_id"] == transaction_id

    monkeypatch.setattr(corpus, "_append", original_append)
    with corpus.stable_corpus(tmp_path) as epoch:
        assert epoch == 1
    assert [r["transaction_id"] for r in _journal(tmp_path)] == [transaction_id]


def test_durable_unlink_of_an_already_removed_marker_is_idempotent(tmp_path):
    marker = tmp_path / "commit.json"
    marker.write_text("{}", encoding="utf-8")
    corpus._unlink_durable(marker)
    corpus._unlink_durable(marker)
    assert not marker.exists()


# --- snapshot mode ---------------------------------------------------------------------------


def test_snapshot_keeps_hashing_after_one_unreadable_page(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "a-unreadable.md").write_text("a", encoding="utf-8")
    readable = wiki / "b-readable.md"
    readable.write_text("b", encoding="utf-8")
    real_digest = corpus._digest

    def digest(path):
        if path.name == "a-unreadable.md":
            raise PermissionError(13, "Permission denied", str(path))
        return real_digest(path)

    monkeypatch.setattr(corpus, "_digest", digest)
    assert corpus.snapshot(tmp_path) == {"wiki/b-readable.md": real_digest(readable)}


def test_snapshot_mode_journals_only_changed_pages_with_their_before_digest(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    changed = wiki / "changed.md"
    changed.write_text("before", encoding="utf-8")
    (wiki / "untouched.md").write_text("same", encoding="utf-8")
    before = corpus._digest(changed)
    with corpus.mutation(tmp_path, writer="w", operation="op"):
        changed.write_text("after", encoding="utf-8")
    record = _journal(tmp_path)[-1]
    assert record["status"] == "committed"
    assert record["affected_paths"] == [{
        "path": "wiki/changed.md", "before_sha256": before,
        "after_sha256": corpus._digest(changed),
    }]


# --- tracking-mode selection is by value -----------------------------------------------------


def test_snapshot_mode_selected_by_an_equal_runtime_string(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "page.md"
    page.write_text("before", encoding="utf-8")
    before = corpus._digest(page)
    with corpus.mutation(
        tmp_path, writer="w", operation="op", tracking=_runtime_string("snapshot"),
    ):
        page.write_text("after", encoding="utf-8")
    [change] = _journal(tmp_path)[-1]["affected_paths"]
    assert change["path"] == "wiki/page.md" and change["before_sha256"] == before


def test_touched_mode_selected_by_an_equal_runtime_string(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "page.md"
    page.write_text("before", encoding="utf-8")
    with corpus.mutation(
        tmp_path, writer="w", operation="op", tracking=_runtime_string("touched"),
    ):
        corpus.touch(page)
        page.write_text("after", encoding="utf-8")
    record = _journal(tmp_path)[-1]
    assert record["epoch"] == 1
    assert [c["path"] for c in record["affected_paths"]] == ["wiki/page.md"]


def test_touch_ignores_non_wiki_markdown_and_wiki_non_markdown(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (tmp_path / "raw").mkdir()
    outside_wiki = tmp_path / "raw" / "note.md"
    wiki_data = wiki / "data.json"
    with corpus.mutation(tmp_path, writer="w", operation="op", tracking="touched"):
        corpus.touch(outside_wiki)
        corpus.touch(wiki_data)
        active = json.loads((_state(tmp_path) / "active.json").read_text(encoding="utf-8"))
        outside_wiki.write_text("x", encoding="utf-8")
        wiki_data.write_text("{}", encoding="utf-8")
    assert active["touched"] == {}
    record = _journal(tmp_path)[-1]
    assert record["affected_paths"] == [] and record["epoch"] == 0


# --- recovery labeling and identity ----------------------------------------------------------


def test_next_writer_recovers_a_dead_marker_with_recovered_status(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _dead_active_marker(tmp_path)
    (wiki / "published.md").write_text("orphan", encoding="utf-8")
    with corpus.mutation(tmp_path, writer="next", operation="op"):
        pass
    records = _journal(tmp_path)
    assert [(r["transaction_id"], r["status"]) for r in records] == [
        ("dead-tx", "recovered"), (records[1]["transaction_id"], "committed"),
    ]
    assert corpus.read_epoch(tmp_path) == 1


@pytest.mark.parametrize("prepared_id", ["aaa-lower", "zzz-higher"])
def test_prepared_marker_for_another_transaction_is_refused_in_either_order(tmp_path, prepared_id):
    state = _dead_active_marker(tmp_path, transaction_id="mmm-active")
    corpus._atomic_json(state / "commit.json", {
        "version": 1, "record": {"transaction_id": prepared_id, "epoch": 1},
    })
    with pytest.raises(RuntimeError, match="does not match the active transaction"):
        corpus._finish_active(tmp_path)
    assert not (state / "journal.jsonl").exists()


def test_journal_lookup_matches_exact_transaction_id_only(tmp_path):
    state = _state(tmp_path)
    state.mkdir(parents=True)
    (state / "journal.jsonl").write_text(
        '{"transaction_id":"mmm","epoch":1}\n'
        '[1,2]\n'
        '"a-string-record"\n'
        '{"epoch":2}\n'
        '{"transaction_id":"aaa","epoch":3}\n'
        '{"transaction_id":"zzz","epoch":4}\n',
        encoding="utf-8",
    )
    assert corpus._journal_transaction(state, "mmm") == {"transaction_id": "mmm", "epoch": 1}
    assert corpus._journal_transaction(state, "nnn") is None


# --- journal torn-tail repair ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing", "kept"),
    [
        pytest.param(b"{", b"", id="one-byte-torn-record"),
        pytest.param(b'{"transaction_id":"torn"', b"", id="only-a-torn-record"),
        pytest.param(b'{"transaction_id":"ok"}\nX', b'{"transaction_id":"ok"}\n',
                     id="one-byte-tail-after-a-record"),
        pytest.param(b"\n{\"transaction_id\":\"torn\"", b"\n", id="blank-first-line"),
        pytest.param(b'{"transaction_id":"ok"}\n\x00\x00\x00', b'{"transaction_id":"ok"}\n',
                     id="nul-filled-tail-after-power-loss"),
        pytest.param(b'{"transaction_id":"ok"}\n', b'{"transaction_id":"ok"}\n',
                     id="terminated-journal-is-untouched"),
    ],
)
def test_append_discards_only_the_unterminated_tail(tmp_path, existing, kept):
    state = _state(tmp_path)
    state.mkdir(parents=True)
    journal = state / "journal.jsonl"
    journal.write_bytes(existing)
    record = {"transaction_id": "retry", "epoch": 1}
    corpus._append(state, record)
    assert journal.read_bytes() == kept + _canonical(record).encode("utf-8") + b"\n"
