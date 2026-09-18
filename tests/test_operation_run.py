"""okengine#402 keystone: the engine owns the operation run lifecycle — run-id, locks, snapshot
digest, and the terminal receipt (a partial/failed worker cannot report a complete success)."""
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import operation_run  # noqa: E402


def _worker(body: str) -> str:
    return ("import argparse,json,os,sys\n"
            "p=argparse.ArgumentParser();p.add_argument('--target-vault');"
            "p.add_argument('--dry-run',action='store_true');p.add_argument('--resume');"
            "a,_=p.parse_known_args()\n" + body)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dep = Path(self.temp.name)
        (self.dep / "wiki").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_conflicting_lock_is_refused_and_released_after(self):
        with operation_run.acquire_locks(self.dep, ["res-a"], "run-1"):
            with self.assertRaises(operation_run.OperationRunError):
                with operation_run.acquire_locks(self.dep, ["res-a"], "run-2"):
                    pass
        # once the outer holder exits, the resource is acquirable again
        with operation_run.acquire_locks(self.dep, ["res-a"], "run-3"):
            pass

    def test_stale_lock_file_from_dead_holder_does_not_block(self):
        ld = self.dep / ".okengine/operations/locks"
        ld.mkdir(parents=True)
        (ld / "res-b.lock").write_text(json.dumps(
            {"run_id": "dead", "pid": 999999, "resource": "res-b"}), encoding="utf-8")
        with operation_run.acquire_locks(self.dep, ["res-b"], "run-new"):
            pass   # the flock was never held by a live process — recovered, not blocked


class DigestAndRedactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dep = Path(self.temp.name)
        (self.dep / "wiki").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_digest_is_stable_and_input_sensitive(self):
        (self.dep / "wiki" / "a.md").write_text("x", encoding="utf-8")
        d1 = operation_run.snapshot_digest(self.dep, ["wiki/*.md"])
        self.assertEqual(d1, operation_run.snapshot_digest(self.dep, ["wiki/*.md"]))
        os.utime(self.dep / "wiki" / "a.md", (2_000_000_000, 2_000_000_000))
        self.assertNotEqual(d1, operation_run.snapshot_digest(self.dep, ["wiki/*.md"]))

    def test_redact_hides_secret_argument_values(self):
        self.assertEqual(
            operation_run._redact(["--all", "--api-key", "sekret", "--token=abc", "--actor", "x"]),
            ["--all", "--api-key", "***", "--token=***", "--actor", "x"])


class RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dep = Path(self.temp.name)
        (self.dep / "wiki").mkdir()
        (self.dep / "crons/scripts").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _manifest(self, entry: str, **extra):
        (self.dep / "crons/scripts" / entry).write_text(_worker(extra.pop("_body")), encoding="utf-8")
        return {"operation_api": 1, "name": "op", "owner": "p",
                "entrypoint": f"crons/scripts/{entry}", **extra}

    def test_engine_allocates_run_id_and_writes_terminal_receipt(self):
        m = self._manifest("ok.py", _body="print(json.dumps({'status':'succeeded'}))\n")
        code, receipt = operation_run.run(self.dep, m, [], source="schedule")
        self.assertEqual(code, 0)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertTrue(receipt["run_id"].startswith("op-"))
        self.assertEqual(receipt["source"], "schedule")
        self.assertTrue(operation_run.receipt_path(self.dep, "op", receipt["run_id"]).is_file())
        journal = self.dep / ".okengine/corpus/journal.jsonl"
        self.assertTrue(journal.is_file())
        self.assertEqual(json.loads(journal.read_text())["writer"], "operation:op")

    def test_worker_writes_share_one_corpus_epoch(self):
        m = self._manifest(
            "writes.py", outputs=["wiki/*.md"],
            _body=("from pathlib import Path\n"
                   "root=Path(a.target_vault)/'wiki'\n"
                   "(root/'a.md').write_text('a')\n"
                   "(root/'b.md').write_text('b')\n"
                   "print(json.dumps({'status':'succeeded'}))\n"))
        code, _ = operation_run.run(self.dep, m, [])
        self.assertEqual(code, 0)
        self.assertEqual((self.dep / ".okengine/corpus/epoch").read_text().strip(), "1")
        record = json.loads((self.dep / ".okengine/corpus/journal.jsonl").read_text())
        self.assertEqual(len(record["affected_paths"]), 2)

    def test_missing_declared_output_downgrades_success_to_degraded(self):
        m = self._manifest("claim.py", outputs=["wiki/out/**"],
                           _body="print(json.dumps({'status':'succeeded'}))\n")
        code, receipt = operation_run.run(self.dep, m, [], source="cli")
        self.assertEqual(receipt["status"], "degraded")               # AC #7: partial != complete
        self.assertEqual(receipt["output_validation"]["missing"], ["wiki/out/**"])
        self.assertEqual(code, 0)

    def test_present_declared_output_allows_success(self):
        (self.dep / "wiki" / "out").mkdir()
        (self.dep / "wiki" / "out" / "r.md").write_text("done", encoding="utf-8")
        m = self._manifest("claim2.py", outputs=["wiki/out/**"],
                           _body="print(json.dumps({'status':'succeeded'}))\n")
        code, receipt = operation_run.run(self.dep, m, [], source="cli")
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(code, 0)

    def test_worker_nonzero_exit_is_failed(self):
        m = self._manifest("boom.py", _body="sys.exit(3)\n")
        code, receipt = operation_run.run(self.dep, m, [], source="cli")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(code, 1)

    def test_plan_writes_no_receipt_and_reports_engine_digest(self):
        (self.dep / "wiki" / "a.md").write_text("x", encoding="utf-8")
        m = self._manifest("planner.py", inputs=["wiki/*.md"], supports={"plan": True},
                           _body="print(json.dumps({'status':'planned'}))\n")
        code, result = operation_run.run(self.dep, m, [], source="cli", dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["snapshot_digest"], operation_run.snapshot_digest(self.dep, ["wiki/*.md"]))
        self.assertFalse((self.dep / ".okengine").exists())


if __name__ == "__main__":
    unittest.main()


def test_digest_pid_and_lock_release_error_edges(tmp_path, monkeypatch):
    deployment = tmp_path
    directory = deployment / "wiki/dir"
    directory.mkdir(parents=True)
    raced = deployment / "wiki/raced.md"
    raced.write_text("x")
    original_stat = Path.stat
    raced_stats = {"n": 0}
    def stat_with_race(self, *args, **kwargs):
        if self == raced:
            raced_stats["n"] += 1
            if raced_stats["n"] > 1:
                raise OSError("race")
        return original_stat(self, *args, **kwargs)
    monkeypatch.setattr(
        Path, "stat", stat_with_race,
    )
    operation_run.snapshot_digest(deployment, ["wiki/*"])

    monkeypatch.setattr(
        operation_run.os, "kill",
        lambda *_a: (_ for _ in ()).throw(ProcessLookupError()),
    )


def test_snapshot_digest_tolerates_stat_race(tmp_path, monkeypatch):
    target = tmp_path / "page.md"
    target.write_text("x")
    original_is_file = Path.is_file
    original_stat = Path.stat
    monkeypatch.setattr(Path, "is_file", lambda p: True if p == target else original_is_file(p))
    monkeypatch.setattr(Path, "stat", lambda p, *a, **k: (
        (_ for _ in ()).throw(OSError("race")) if p == target else original_stat(p, *a, **k)))
    assert operation_run.snapshot_digest(tmp_path, ["*.md"])
    monkeypatch.setattr(
        operation_run.os, "kill",
        lambda *_a: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert not operation_run._pid_alive(123)
    monkeypatch.setattr(
        operation_run.os, "kill",
        lambda *_a: (_ for _ in ()).throw(PermissionError()),
    )
    assert operation_run._pid_alive(123)

    # Invalid fd and absent path independently exercise best-effort release catches.
    fd = os.open(tmp_path / "closed-fd", os.O_RDWR | os.O_CREAT)
    os.close(fd)
    operation_run.LockSet([(fd, tmp_path / "absent.lock")]).release()


def test_lock_recovery_from_malformed_and_dead_holders(tmp_path, monkeypatch):
    deployment = tmp_path
    real_flock = operation_run.fcntl.flock
    calls = {"n": 0}

    def contend_once(fd, flags):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("contended")
        return real_flock(fd, flags)

    monkeypatch.setattr(operation_run.fcntl, "flock", contend_once)
    monkeypatch.setattr(operation_run.os, "pread", lambda *_a: b"not-json")
    lockset = operation_run.acquire_lockset(deployment, ["resource"], "run")
    lockset.release()

    calls["n"] = 0
    monkeypatch.setattr(
        operation_run.os, "pread",
        lambda *_a: json.dumps({"pid": 999999, "run_id": "dead"}).encode(),
    )
    monkeypatch.setattr(operation_run, "_pid_alive", lambda _pid: False)
    lockset = operation_run.acquire_lockset(deployment, ["resource-2"], "run")
    lockset.release()


def test_plan_and_run_stderr_and_lock_failure_receipt(tmp_path, monkeypatch, capsys):
    deployment = tmp_path
    (deployment / "wiki").mkdir()
    manifest = {"name": "op", "owner": "pack", "inputs": [], "outputs": [], "locks": ["x"]}
    import framework_operations
    monkeypatch.setattr(
        framework_operations, "operation_command",
        lambda *_a, **_k: (["worker"], {}),
    )
    monkeypatch.setattr(framework_operations, "result_from_output", lambda _out: {"status": "planned"})
    monkeypatch.setattr(
        operation_run.subprocess, "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="{}", stderr="plan warning"),
    )
    assert operation_run.plan(deployment, manifest, ["--x"])[0] == 0
    assert "plan warning" in capsys.readouterr().err

    @contextmanager
    def blocked(*_a, **_k):
        raise operation_run.OperationRunError("held")
        yield

    monkeypatch.setattr(operation_run, "acquire_locks", blocked)
    code, receipt = operation_run.run(deployment, manifest, [], run_id="fixed")
    assert code == 1 and receipt["status"] == "failed" and receipt["error"] == "held"

    @contextmanager
    def allowed(*_a, **_k):
        yield

    monkeypatch.setattr(operation_run, "acquire_locks", allowed)
    def completed_process(*_args, **kwargs):
        assert kwargs["check"] is False
        assert kwargs["text"] is True
        return SimpleNamespace(returncode=0, stdout="{}", stderr="run warning\n")

    monkeypatch.setattr(operation_run.subprocess, "run", completed_process)
    monkeypatch.setattr(framework_operations, "result_from_output", lambda _out: {"status": "succeeded"})
    code, receipt = operation_run.run(deployment, manifest, [], run_id="fixed-2")
    assert code == 0 and receipt["status"] == "succeeded"
    assert "run warning" in capsys.readouterr().err
