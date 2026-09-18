import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import corpus_transaction as corpus
import scheduler_watchdog
from scripts import framework_validate_report


def test_review_write_image_contains_transaction_runtime_dependency():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "okengine-mcp/Dockerfile.review").read_text(encoding="utf-8")
    assert "/wheel/*.whl" in dockerfile


def test_multi_file_mutation_advances_one_epoch_and_journals_hashes_only(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    first = wiki / "a.md"
    first.write_text("secret-before", encoding="utf-8")
    with corpus.mutation(tmp_path, writer="test-writer", operation="rewrite"):
        first.write_text("secret-after", encoding="utf-8")
        (wiki / "b.md").write_text("another-secret", encoding="utf-8")

    assert corpus.read_epoch(tmp_path) == 1
    record = json.loads((tmp_path / ".okengine/corpus/journal.jsonl").read_text())
    assert record["epoch"] == 1
    assert record["writer"] == "test-writer"
    assert [item["path"] for item in record["affected_paths"]] == ["wiki/a.md", "wiki/b.md"]
    rendered = json.dumps(record)
    assert "secret-before" not in rendered
    assert "secret-after" not in rendered
    assert "another-secret" not in rendered


def test_noop_transaction_does_not_advance_epoch(tmp_path):
    (tmp_path / "wiki").mkdir()
    with corpus.mutation(tmp_path, writer="noop", operation="scan"):
        pass
    assert corpus.read_epoch(tmp_path) == 0


def test_snapshot_handles_absent_corpus_md_directories_and_disappearing_pages(
        tmp_path, monkeypatch):
    assert corpus.snapshot(tmp_path) == {}
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "directory.md").mkdir()
    page = wiki / "gone.md"
    page.write_text("transient", encoding="utf-8")
    monkeypatch.setattr(corpus, "_digest", lambda _path: (_ for _ in ()).throw(
        OSError("page disappeared")))
    assert corpus.snapshot(tmp_path) == {}


def test_failed_writer_records_published_changes_and_releases_fence(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    with pytest.raises(RuntimeError):
        with corpus.mutation(tmp_path, writer="broken", operation="partial"):
            (wiki / "partial.md").write_text("published", encoding="utf-8")
            raise RuntimeError("boom")
    assert corpus.read_epoch(tmp_path) == 1
    assert json.loads((tmp_path / ".okengine/corpus/journal.jsonl").read_text())["status"] == "failed"
    with corpus.stable_corpus(tmp_path) as epoch:
        assert epoch == 1


def test_restart_recovers_active_writer_before_stable_read(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    corpus._atomic_json(state / "active.json", {
        "transaction_id": "dead-tx", "writer": "killed", "operation": "batch",
        "started_at": "2026-01-01T00:00:00+00:00", "before": {},
    })
    (wiki / "survivor.md").write_text("complete me", encoding="utf-8")

    with corpus.stable_corpus(tmp_path) as epoch:
        assert epoch == 1
    record = json.loads((state / "journal.jsonl").read_text())
    assert record["status"] == "recovered"
    assert not (state / "active.json").exists()


@pytest.mark.parametrize("boundary", ["prepared", "epoch", "journal", "active-removed"])
def test_commit_recovery_is_idempotent_at_every_durable_boundary(tmp_path, monkeypatch, boundary):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "page.md"
    page.write_text("before", encoding="utf-8")
    original_atomic = corpus._atomic_json
    original_append = corpus._append
    original_unlink = corpus._unlink_durable
    tripped = False

    def atomic(path, value):
        nonlocal tripped
        original_atomic(path, value)
        target = "prepared" if path.name == "commit.json" else "epoch" if path.name == "epoch" else ""
        if not tripped and boundary == target:
            tripped = True
            raise RuntimeError(f"crash after {boundary}")

    def append(state, record):
        nonlocal tripped
        original_append(state, record)
        if not tripped and boundary == "journal":
            tripped = True
            raise RuntimeError("crash after journal")

    def unlink(path):
        nonlocal tripped
        original_unlink(path)
        if not tripped and boundary == "active-removed" and path.name == "active.json":
            tripped = True
            raise RuntimeError("crash after active removal")

    monkeypatch.setattr(corpus, "_atomic_json", atomic)
    monkeypatch.setattr(corpus, "_append", append)
    monkeypatch.setattr(corpus, "_unlink_durable", unlink)
    with pytest.raises(RuntimeError, match="crash after"):
        with corpus.mutation(tmp_path, writer="fault", operation=boundary) as transaction_id:
            page.write_text("after", encoding="utf-8")
    assert tripped

    monkeypatch.setattr(corpus, "_atomic_json", original_atomic)
    monkeypatch.setattr(corpus, "_append", original_append)
    monkeypatch.setattr(corpus, "_unlink_durable", original_unlink)
    with corpus.stable_corpus(tmp_path) as epoch:
        assert epoch == 1
    with corpus.stable_corpus(tmp_path) as epoch:
        assert epoch == 1
    records = [json.loads(line) for line in (
        tmp_path / ".okengine/corpus/journal.jsonl"
    ).read_text().splitlines()]
    matching = [record for record in records if record["transaction_id"] == transaction_id]
    assert len(matching) == 1
    assert matching[0]["epoch"] == 1
    assert not (tmp_path / ".okengine/corpus/active.json").exists()
    assert not (tmp_path / ".okengine/corpus/commit.json").exists()


def test_killed_subprocess_is_recovered_once(tmp_path):
    (tmp_path / "wiki").mkdir()
    script = """
import os, pathlib
from okengine.corpus_transaction import mutation
root = pathlib.Path(os.environ['CORPUS_TEST_ROOT'])
with mutation(root, writer='killed', operation='subprocess'):
    (root / 'wiki/killed.md').write_text('published', encoding='utf-8')
    os._exit(9)
"""
    environment = dict(os.environ, CORPUS_TEST_ROOT=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", script], env=environment, check=False)
    assert result.returncode == 9
    with corpus.stable_corpus(tmp_path) as epoch:
        assert epoch == 1
    with corpus.stable_corpus(tmp_path) as epoch:
        assert epoch == 1
    records = [json.loads(line) for line in (
        tmp_path / ".okengine/corpus/journal.jsonl"
    ).read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["status"] == "recovered"


def test_partial_journal_tail_is_trimmed_before_idempotent_retry(tmp_path):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    journal = state / "journal.jsonl"
    journal.write_bytes(b'{"transaction_id":"complete"}\n{"transaction_id":"torn"')
    corpus._append(state, {"transaction_id": "retry", "epoch": 1})
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["transaction_id"] for line in lines] == ["complete", "retry"]


def test_journal_lookup_skips_malformed_records(tmp_path):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    (state / "journal.jsonl").write_text(
        '{"transaction_id":"wanted"}\nnot-json\n', encoding="utf-8"
    )
    assert corpus._journal_transaction(state, "wanted")["transaction_id"] == "wanted"


def test_prepared_commit_recovery_rejects_invalid_and_divergent_state(tmp_path):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    active = state / "active.json"
    prepared = state / "commit.json"
    active.write_text('{"transaction_id":"tx"}', encoding="utf-8")
    prepared.write_text('{"record":{"transaction_id":"tx"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid prepared corpus commit"):
        corpus._finish_active(tmp_path)

    prepared.write_text(
        '{"record":{"transaction_id":"tx","epoch":1}}', encoding="utf-8"
    )
    (state / "epoch").write_text("2\n", encoding="ascii")
    with pytest.raises(RuntimeError, match="advanced beyond prepared epoch"):
        corpus._finish_active(tmp_path)

    prepared.write_text(
        '{"record":{"transaction_id":"different","epoch":2}}', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="does not match the active transaction"):
        corpus._finish_active(tmp_path)


def test_orphaned_prepared_commit_is_retained_until_journaled(tmp_path):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    prepared = state / "commit.json"
    prepared.write_text(
        '{"record":{"transaction_id":"not-journaled","epoch":1}}', encoding="utf-8"
    )

    assert corpus._finish_active(tmp_path) == 0
    assert prepared.exists()


def test_overlapping_writer_waits_for_current_epoch(tmp_path):
    (tmp_path / "wiki").mkdir()
    entered = threading.Event()

    def second_writer():
        with corpus.mutation(tmp_path, writer="second", operation="overlap"):
            entered.set()

    with corpus.mutation(tmp_path, writer="first", operation="hold"):
        thread = threading.Thread(target=second_writer)
        thread.start()
        time.sleep(0.05)
        assert not entered.is_set()
    thread.join(timeout=2)
    assert entered.is_set()


def test_waiting_writer_times_out_with_holder_diagnostics(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    monkeypatch.setattr(corpus.sys, "argv", ["/opt/hermes/write_server.py"])
    error = []

    def second_writer():
        try:
            with corpus.mutation(
                tmp_path, writer="second", operation="overlap", lock_timeout_seconds=0.02,
            ):
                pass
        except corpus.CorpusLockTimeout as exc:
            error.append(str(exc))

    with corpus.mutation(tmp_path, writer="first", operation="hold"):
        thread = threading.Thread(target=second_writer)
        thread.start()
        thread.join(timeout=1)

    assert error
    assert "timed out after 0.02s" in error[0]
    assert "writer='first'" in error[0]
    assert "operation='hold'" in error[0]
    assert "command='write_server.py'" in error[0]
    assert "age_seconds=" in error[0]
    assert "do not delete the lock file" in error[0]
    assert not (tmp_path / ".okengine/corpus/lock-owner.json").exists()


def test_stable_reader_times_out_instead_of_waiting_forever(tmp_path):
    (tmp_path / "wiki").mkdir()
    with corpus.mutation(tmp_path, writer="wedged-writer", operation="long-write"):
        with pytest.raises(corpus.CorpusLockTimeout, match="wedged-writer"):
            with corpus.stable_corpus(tmp_path, lock_timeout_seconds=0.01):
                pytest.fail("contended stable reader must not enter")


def test_held_lock_makes_validation_bounded_and_runtime_health_red(tmp_path, monkeypatch):
    """Integration: one real kernel fence drives both the deploy reader and health evidence."""
    (tmp_path / "wiki").mkdir()
    monkeypatch.setattr(corpus, "_now", lambda: "2026-01-01T00:00:00+00:00")
    with corpus.mutation(tmp_path, writer="wedged", operation="integration"):
        with pytest.raises(corpus.CorpusLockTimeout, match="wedged"):
            with corpus.stable_corpus(tmp_path, lock_timeout_seconds=0.01):
                pytest.fail("validation must not enter while the fence is held")
        check = scheduler_watchdog.inspect(
            tmp_path, time.time(), 180, max_corpus_lock_age=60,
        )
        assert check["healthy"] is False
        assert check["corpus_lock_owner"]["writer"] == "wedged"
        assert any("corpus lock owner is stale" in reason for reason in check["reasons"])


@pytest.mark.parametrize("timeout", [0, -1])
def test_corpus_lock_timeout_must_be_positive(tmp_path, timeout):
    with pytest.raises(ValueError, match="must be positive"):
        with corpus.stable_corpus(tmp_path, lock_timeout_seconds=timeout):
            pass


def test_corpus_lock_timeout_rejects_malformed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(corpus.LOCK_TIMEOUT_ENV, "eventually")
    with pytest.raises(ValueError, match=corpus.LOCK_TIMEOUT_ENV):
        with corpus.stable_corpus(tmp_path):
            pass


def test_default_lock_timeout_is_exact_and_not_silently_changed(monkeypatch):
    monkeypatch.delenv(corpus.LOCK_TIMEOUT_ENV, raising=False)
    assert corpus.DEFAULT_LOCK_TIMEOUT_SECONDS == 30.0
    assert corpus._lock_timeout(None) == 30.0


def test_acquire_uses_exact_nonblocking_flags_deadline_and_poll(monkeypatch, tmp_path):
    class Lock:
        @staticmethod
        def fileno():
            return 7

    monotonic = iter([10.0, 11.0, 12.0])
    flags = []
    sleeps = []

    def blocked(fd, value):
        flags.append((fd, value))
        raise BlockingIOError

    monkeypatch.setattr(corpus.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(corpus.time, "sleep", sleeps.append)
    monkeypatch.setattr(corpus.fcntl, "flock", blocked)
    with pytest.raises(corpus.CorpusLockTimeout, match="timed out after 2s"):
        corpus._acquire(
            lock=Lock(), state=tmp_path, mode=corpus.fcntl.LOCK_EX, timeout=2.0,
        )
    expected_flags = corpus.fcntl.LOCK_EX | corpus.fcntl.LOCK_NB
    assert flags == [(7, expected_flags), (7, expected_flags)]
    assert sleeps == [0.05]


def test_owner_record_uses_exact_token_size_command_and_keywords(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(corpus.secrets, "token_hex", lambda size: calls.append(size) or "owned")
    monkeypatch.setattr(corpus.sys, "argv", ["/opt/hermes/write_server.py", "ignored"])
    token = corpus._record_lock_owner(
        state=tmp_path, writer="writer", operation="operation", mode="exclusive",
    )
    owner = json.loads((tmp_path / "lock-owner.json").read_text())
    assert token == "owned" and calls == [16]
    assert owner["command"] == "write_server.py"


def test_owner_age_is_exact_and_future_acquisition_clamps_to_zero(tmp_path, monkeypatch):
    class FixedDateTime(corpus.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 0, 0, 5, tzinfo=corpus.dt.timezone.utc)

    monkeypatch.setattr(corpus.dt, "datetime", FixedDateTime)
    owner_path = tmp_path / "lock-owner.json"
    owner_path.write_text(
        '{"writer":"old","acquired_at":"2026-01-01T00:00:00+00:00"}', encoding="utf-8",
    )
    assert "age_seconds=5" in corpus._owner_detail(tmp_path)
    owner_path.write_text(
        '{"writer":"future","acquired_at":"2026-01-01T00:00:06+00:00"}', encoding="utf-8",
    )
    assert "age_seconds=0" in corpus._owner_detail(tmp_path)


def test_lock_owner_diagnostics_and_cleanup_tolerate_stale_metadata(tmp_path):
    state = tmp_path / ".okengine/corpus"
    state.mkdir(parents=True)
    assert corpus._owner_detail(state) == "owner metadata unavailable"

    owner_path = state / "lock-owner.json"
    owner_path.write_text('{"token":"zzz"}\n', encoding="utf-8")
    assert corpus._owner_detail(state) == "owner metadata unavailable"
    corpus._clear_lock_owner(state, "aaa")
    assert owner_path.exists()

    owner_path.write_text("not-json\n", encoding="utf-8")
    corpus._clear_lock_owner(state, "current")
    assert owner_path.exists()

    owner_path.write_text(
        '{"token":"current","writer":"old","acquired_at":"not-a-date"}\n', encoding="utf-8",
    )
    assert "age_seconds=unknown" in corpus._owner_detail(state)

    owner_path.write_text(
        '{"token":"aaa","writer":"typed","acquired_at":123}\n', encoding="utf-8",
    )
    assert corpus._owner_detail(state) == "writer='typed', acquired_at=123"
    corpus._clear_lock_owner(state, "zzz")
    assert owner_path.exists(), "a lexically lower nonmatching token must not clear another owner"


def test_context_managers_accept_deployment_as_keyword(tmp_path):
    (tmp_path / "wiki").mkdir()
    with corpus.mutation(
        deployment=tmp_path, writer="keyword", operation="write",
    ):
        pass
    with corpus.stable_corpus(deployment=tmp_path) as epoch:
        assert epoch == 0


def test_validator_report_holds_stable_epoch(tmp_path, capsys):
    class Result:
        rows = []
        n_fail = 0
        n_warn = 0

    assert framework_validate_report.main(
        [str(tmp_path)],
        lambda pack, probe: Result(),
        stable_corpus=corpus.stable_corpus,
    ) == 0
    assert f"framework validate — {tmp_path} (corpus epoch 0)" in capsys.readouterr().out
