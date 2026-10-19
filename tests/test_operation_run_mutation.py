"""Behavioral contracts for src/okengine/operations/run.py that its critical mutation campaign
showed were unpinned (okengine#612): keyword-only APIs, run-id shape, receipt format, the
engine-computed digest, worker-status and signal handling, real lock semantics (including
recovery and orphan-held locks), and stderr relay."""
from __future__ import annotations

import errno
import fcntl
import inspect
import json
import os
import re
import signal
import stat
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from okengine.operations import run as operation_run

pytestmark = pytest.mark.integration


def _worker(body: str) -> str:
    return ("import argparse,json,os,sys\n"
            "p=argparse.ArgumentParser();p.add_argument('--target-vault');"
            "p.add_argument('--dry-run',action='store_true');a,_=p.parse_known_args()\n" + body)


@pytest.fixture()
def dep(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    (tmp_path / "crons/scripts").mkdir(parents=True)
    return tmp_path


def _manifest(dep: Path, body: str, **extra) -> dict:
    entry = dep / "crons/scripts/worker.py"
    entry.write_text(_worker(body), encoding="utf-8")
    return {"operation_api": 1, "name": "op", "owner": "pack",
            "entrypoint": "crons/scripts/worker.py", "supports": {"plan": True}, **extra}


def _held_elsewhere(path: Path) -> bool:
    """True when another open file description cannot take the flock on `path`."""
    fd = os.open(str(path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


# --- public signatures -------------------------------------------------------------------------

@pytest.mark.parametrize(("function", "keyword_only"), [
    (operation_run.new_run_id, {"stamp", "token"}),
    (operation_run.plan, {"source"}),
    (operation_run.initial_receipt, {"source", "pid"}),
    (operation_run.finalize, {"returncode", "result"}),
    (operation_run.run, {"source", "dry_run", "run_id"}),
])
def test_options_are_keyword_only_and_leading_arguments_accept_keywords(function, keyword_only):
    parameters = inspect.signature(function).parameters.values()
    assert {p.name for p in parameters if p.kind is p.KEYWORD_ONLY} == keyword_only
    assert all(p.kind is p.POSITIONAL_OR_KEYWORD for p in parameters
               if p.name not in keyword_only)


# --- run ids, timestamps, digests --------------------------------------------------------------

def test_explicit_stamp_and_token_compose_the_run_id_verbatim():
    assert operation_run.new_run_id("op", stamp="20260101T000000", token="abc") == \
        "op-20260101T000000-abc"


def test_generated_run_id_has_a_utc_stamp_and_six_hex_digits():
    assert re.fullmatch(r"op-\d{8}T\d{6}-[0-9a-f]{6}", operation_run.new_run_id("op"))
    assert re.fullmatch(r"op-S-[0-9a-f]{6}", operation_run.new_run_id("op", stamp="S"))


def test_receipt_timestamps_carry_whole_seconds_only():
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", operation_run._now())


def test_snapshot_digest_is_32_hex_characters(dep):
    assert re.fullmatch(r"[0-9a-f]{32}", operation_run.snapshot_digest(dep, ["wiki/*"]))


def test_digest_covers_files_listed_after_a_directory(dep):
    (dep / "wiki/a-dir").mkdir()
    page = dep / "wiki/b.md"
    page.write_text("x")
    empty = operation_run.snapshot_digest(dep, [])
    first = operation_run.snapshot_digest(dep, ["wiki/*"])
    assert first != empty
    os.utime(page, (2_000_000_000, 2_000_000_000))
    assert operation_run.snapshot_digest(dep, ["wiki/*"]) != first


def test_digest_covers_files_listed_after_one_that_vanished(dep, monkeypatch):
    vanished = dep / "wiki/a.md"
    vanished.write_text("gone")
    page = dep / "wiki/b.md"
    page.write_text("x")
    real_stat = Path.stat

    def racing_stat(self, *, follow_symlinks=True):
        if self == vanished:
            raise FileNotFoundError(errno.ENOENT, "raced", str(self))
        return real_stat(self, follow_symlinks=follow_symlinks)

    real_is_file = Path.is_file

    def listed_as_a_file(self, *args, **kwargs):
        # The file vanishes BETWEEN the is_file probe and the stat. Python 3.12's is_file calls
        # self.stat() itself, so without pinning the probe the patched stat makes is_file report
        # False and the test exercises the not-a-file branch instead of the OSError one.
        return True if self == vanished else real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", create_autospec(Path.stat, side_effect=racing_stat))
    monkeypatch.setattr(Path, "is_file", create_autospec(Path.is_file,
                                                         side_effect=listed_as_a_file))
    first = operation_run.snapshot_digest(dep, ["wiki/*.md"])
    os.utime(page, (2_000_000_000, 2_000_000_000))
    assert operation_run.snapshot_digest(dep, ["wiki/*.md"]) != first


def test_redaction_judges_the_flag_name_not_the_value():
    assert operation_run._redact(["--label=my-token-name"]) == ["--label=my-token-name"]
    assert operation_run._redact(["--api-key=x=y"]) == ["--api-key=***"]


# --- receipts ----------------------------------------------------------------------------------

def test_initial_receipt_is_sorted_two_space_utf8_json_with_engine_digest(dep):
    (dep / "wiki/a.md").write_text("x")
    manifest = {"name": "op", "owner": "pack", "inputs": ["wiki/*.md"], "locks": ["b", "a", "b"]}
    receipt = operation_run.initial_receipt(dep, manifest, ["--label", "café"], "run-1",
                                            source="cli", pid=7)
    assert receipt["snapshot_digest"] == operation_run.snapshot_digest(dep, ["wiki/*.md"])
    assert receipt["snapshot_digest"] != operation_run.snapshot_digest(dep, [])
    assert receipt["locks"] == ["a", "b"]
    text = operation_run.receipt_path(dep, "op", "run-1").read_text(encoding="utf-8")
    assert '"café"' in text
    assert text.startswith('{\n  "arguments": [\n    "--label",')
    keys = re.findall(r'^  "([a-z_]+)":', text, flags=re.M)
    assert keys == sorted(keys) and len(keys) == len(receipt)


@pytest.mark.parametrize("worker_status", ["failed", "degraded"])
def test_a_worker_reporting_failure_with_exit_zero_keeps_its_status(dep, worker_status):
    manifest = _manifest(dep, f"print(json.dumps({{'status':'{worker_status}'}}))\n")
    code, receipt = operation_run.run(dep, manifest, [])
    assert receipt["status"] == worker_status
    assert code == (0 if worker_status == "degraded" else 1)


def test_a_signal_killed_worker_is_failed(dep):
    manifest = _manifest(dep, "import signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    code, receipt = operation_run.run(dep, manifest, [])
    assert (code, receipt["status"]) == (1, "failed")


def test_output_validation_records_the_declared_outputs(dep):
    (dep / "wiki/out.md").write_text("done")
    manifest = _manifest(dep, "print(json.dumps({'status':'succeeded'}))\n",
                         outputs=["wiki/out.md", "wiki/missing/**"])
    _code, receipt = operation_run.run(dep, manifest, [])
    assert receipt["output_validation"] == {
        "declared": ["wiki/out.md", "wiki/missing/**"], "missing": ["wiki/missing/**"]}
    assert receipt["status"] == "degraded"


def test_run_refuses_while_a_declared_lock_is_held(dep):
    manifest = _manifest(dep, "print(json.dumps({'status':'succeeded'}))\n", locks=["res"])
    with operation_run.acquire_locks(dep, ["res"], "other-run"):
        code, receipt = operation_run.run(dep, manifest, [])
    assert (code, receipt["status"]) == (1, "failed")
    assert "other-run" in receipt["error"]


@pytest.mark.parametrize(("stderr", "relayed"), [("warn", "warn\n"), ("warn\n", "warn\n")])
def test_worker_stderr_is_relayed_with_exactly_one_trailing_newline(dep, capsys, stderr,
                                                                     relayed):
    manifest = _manifest(dep, f"sys.stderr.write({stderr!r})\n"
                              "print(json.dumps({'status':'planned'}))\n")
    operation_run.plan(dep, manifest, [])
    assert capsys.readouterr().err == relayed
    operation_run.run(dep, manifest, [])
    assert capsys.readouterr().err == relayed


# --- plan --------------------------------------------------------------------------------------

def test_plan_returns_the_workers_result_fields_and_status(dep):
    manifest = _manifest(dep, "print(json.dumps({'status':'blocked','items':3}))\n")
    code, result = operation_run.plan(dep, manifest, [])
    assert code == 0
    assert result["status"] == "blocked" and result["items"] == 3


def test_plan_defaults_status_to_planned_and_returns_a_nonzero_exit(dep):
    manifest = _manifest(dep, "print(json.dumps({'items':1}))\nsys.exit(2)\n")
    code, result = operation_run.plan(dep, manifest, [])
    assert code == 2
    assert result["status"] == "planned" and result["items"] == 1


# --- locks -------------------------------------------------------------------------------------

def test_lock_files_are_created_owner_writable_and_world_readable(dep):
    previous = os.umask(0o022)
    try:
        with operation_run.acquire_locks(dep, ["res"], "run"):
            mode = stat.S_IMODE((dep / ".okengine/operations/locks/res.lock").stat().st_mode)
    finally:
        os.umask(previous)
    assert mode == 0o644


def test_an_unlocked_lock_file_naming_a_live_pid_is_taken_over(dep):
    locks = dep / ".okengine/operations/locks"
    locks.mkdir(parents=True)
    path = locks / "res.lock"
    path.write_text(json.dumps({"run_id": "crashed", "pid": os.getpid()}))
    lockset = operation_run.acquire_lockset(dep, ["res"], "new-run")
    try:
        assert _held_elsewhere(path)
        assert json.loads(path.read_text())["run_id"] == "new-run"
    finally:
        lockset.release()


def test_recovering_a_stale_lock_actually_holds_it(dep, monkeypatch):
    real_flock = fcntl.flock
    attempts = []

    def contended_once(fd, operation):
        attempts.append(operation)
        if len(attempts) == 1:
            raise BlockingIOError(errno.EWOULDBLOCK, "held by a dead run")
        return real_flock(fd, operation)

    monkeypatch.setattr(operation_run.fcntl, "flock",
                        create_autospec(fcntl.flock, side_effect=contended_once))
    lockset = operation_run.acquire_lockset(dep, ["res"], "recovering")
    monkeypatch.undo()
    try:
        assert _held_elsewhere(dep / ".okengine/operations/locks/res.lock")
    finally:
        lockset.release()


class _Hung(Exception):
    pass


def test_a_lock_still_held_by_an_orphan_of_a_dead_run_fails_fast(dep):
    locks = dep / ".okengine/operations/locks"
    locks.mkdir(parents=True)
    path = locks / "res.lock"
    path.write_text(json.dumps({"run_id": "dead", "pid": 999_999_999}))
    orphan = os.open(str(path), os.O_RDWR)
    fcntl.flock(orphan, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def hung(_signum, _frame):
        raise _Hung("lock recovery blocked instead of failing")

    previous = signal.signal(signal.SIGALRM, hung)
    signal.alarm(5)
    try:
        with pytest.raises(BlockingIOError):
            operation_run.acquire_lockset(dep, ["res"], "new-run")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        os.close(orphan)


def test_a_live_holder_record_filling_the_read_window_is_honored(dep):
    locks = dep / ".okengine/operations/locks"
    locks.mkdir(parents=True)
    path = locks / "res.lock"
    record = {"pid": os.getpid(), "run_id": "live", "pad": ""}
    record["pad"] = "x" * (4096 - len(json.dumps(record)))
    path.write_text(json.dumps(record))
    assert path.stat().st_size == 4096
    holder = os.open(str(path), os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(operation_run.OperationRunError, match="held by run live"):
            operation_run.acquire_lockset(dep, ["res"], "new-run")
    finally:
        os.close(holder)
