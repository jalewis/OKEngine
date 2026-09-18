import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration


ROOT = Path(__file__).resolve().parents[1]


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class FakeFastAPI:
    def __init__(self, *args, **kwargs):
        pass

    def add_middleware(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda fn: fn

    def post(self, *args, **kwargs):
        return lambda fn: fn


class FakeRequest:
    def __init__(self, value):
        self.value = value

    async def json(self):
        return self.value


fastapi = types.ModuleType("fastapi")
fastapi.FastAPI, fastapi.HTTPException, fastapi.Request = FakeFastAPI, HTTPException, FakeRequest
prior_fastapi = sys.modules.get("fastapi")
sys.modules["fastapi"] = fastapi
spec = importlib.util.spec_from_file_location("operation_api_app", ROOT / "okengine-operations/app.py")
operation_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operation_api)
if prior_fastapi is None:
    del sys.modules["fastapi"]
else:
    sys.modules["fastapi"] = prior_fastapi


class OperationAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.deployment = Path(self.temp.name)
        (self.deployment / "wiki").mkdir()
        (self.deployment / "crons/scripts").mkdir(parents=True)
        op = self.deployment / "operations/fixture"
        op.mkdir(parents=True)
        (op / "operation.yaml").write_text(yaml.safe_dump({
            "operation_api": 1, "name": "fixture-review", "owner": "fixture",
            "title": "Fixture review", "entrypoint": "crons/scripts/fixture.py",
            "mutates": True, "supports": {"plan": True, "resume": False, "cancel": False},
        }), encoding="utf-8")
        # Worker contract (okengine#402/#404): reads the ENGINE run id, prints a result, and does NOT
        # write the receipt or choose the digest — the engine owns both, on the CLI and the API alike.
        (self.deployment / "crons/scripts/fixture.py").write_text(
            "import argparse,json,os,time\n"
            "p=argparse.ArgumentParser();p.add_argument('--target-vault');"
            "p.add_argument('--dry-run',action='store_true');p.add_argument('--all',action='store_true');"
            "a=p.parse_args()\n"
            "rid=os.environ.get('OKENGINE_OPERATION_RUN_ID') or 'plan'\n"
            "time.sleep(0 if a.dry_run else .05)\n"
            "print(json.dumps({'operation':'fixture-review','run_id':rid,"
            "'status':'planned' if a.dry_run else 'succeeded','actor_inventory':[{'ref':'a'}],"
            "'dimensions':['one'],'counts':{'actor_questions':1}}))\n", encoding="utf-8")
        operation_api.DEPLOYMENT = self.deployment
        operation_api.REQUESTS = self.deployment / ".okengine/operations/requests"
        operation_api.ALLOWED = {"fixture-review"}

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_and_async_run_share_manifest_and_receipt(self):
        plan = asyncio.run(operation_api.plan_operation(
            "fixture-review", FakeRequest({"arguments": ["--all"]})))
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["counts"]["actor_questions"], 1)
        digest = plan["snapshot_digest"]            # ENGINE-computed, not the pack's self-report
        started = asyncio.run(operation_api.run_operation(
            "fixture-review", FakeRequest({"arguments": ["--all"], "plan_digest": digest})))
        self.assertEqual(started["status"], "running")
        run_id = started["run_id"]                  # ENGINE-allocated
        self.assertTrue(run_id.startswith("fixture-review-"))
        process = operation_api._PROCESSES.get(started["request_id"])
        self.assertIsNotNone(process)
        # The API reaper is the sole owner of process.wait() and terminal
        # finalization. Waiting on the same Popen here races that background
        # thread under full-suite load and defeats the lifecycle being tested.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            path, value = operation_api.load_request(started["request_id"])
            value = operation_api.refresh(path, value)
            if value["status"] in operation_api.TERMINAL:
                break
            time.sleep(.02)
        stderr = Path(value["stderr"]).read_text(errors="replace")
        self.assertEqual(
            value["status"], "succeeded",
            f"the reaper's finalize is authoritative; worker stderr: {stderr}",
        )
        self.assertEqual(value["run_id"], run_id)
        self.assertTrue(Path(value["receipt"]).as_posix().endswith(f"{run_id}.json"))
        # the ENGINE wrote the terminal receipt (the worker never touches it)
        self.assertTrue((self.deployment / value["receipt"]).is_file())
        # The reaper projects the terminal receipt durably before dropping its
        # in-memory ownership, so a later read cannot misclassify the dead pid
        # as a service-restart failure.
        while (started["request_id"] in operation_api._PROCESSES
               and time.monotonic() < deadline):
            time.sleep(.01)
        persisted = json.loads(operation_api.request_path(started["request_id"]).read_text())
        self.assertEqual(persisted["status"], "succeeded")
        self.assertNotIn(started["request_id"], operation_api._PROCESSES)

    def test_allowlist_fails_closed(self):
        operation_api.ALLOWED = set()
        with self.assertRaises(HTTPException) as raised:
            operation_api.manifest("fixture-review")
        self.assertEqual(raised.exception.status_code, 403)

    def test_run_requires_matching_current_plan_digest(self):
        with self.assertRaises(HTTPException) as missing:
            asyncio.run(operation_api.run_operation(
                "fixture-review", FakeRequest({"arguments": ["--all"]})))
        self.assertEqual(missing.exception.status_code, 409)
        with self.assertRaises(HTTPException) as stale:
            asyncio.run(operation_api.run_operation(
                "fixture-review", FakeRequest({"arguments": ["--all"],
                                               "plan_digest": "stale"})))
        self.assertEqual(stale.exception.status_code, 409)

    def test_bearer_auth_rejects_missing_token(self):
        operation_api.TOKEN = "internal-token"
        called, sent = [], []

        async def inner(scope, receive, send):
            called.append(scope)

        async def receive():
            return {}

        async def send(value):
            sent.append(value)

        auth = operation_api.BearerAuth(inner)
        asyncio.run(auth({"type": "http", "path": "/operations", "headers": []}, receive, send))
        self.assertFalse(called)
        self.assertEqual(sent[0]["status"], 401)

    def test_post_spawn_receipt_failure_aborts_worker_and_releases_lock(self):
        plan = asyncio.run(operation_api.plan_operation(
            "fixture-review", FakeRequest({"arguments": ["--all"]})))
        spawned = []
        real_popen = operation_api.subprocess.Popen

        class Lock:
            released = False
            def release(self):
                self.released = True

        lock = Lock()

        def capture(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        original_lock = operation_api.operation_run.acquire_lockset
        original_receipt = operation_api.operation_run.initial_receipt
        original_popen = operation_api.subprocess.Popen
        operation_api.operation_run.acquire_lockset = lambda *_a, **_k: lock
        operation_api.operation_run.initial_receipt = lambda *_a, **_k: (_ for _ in ()).throw(
            OSError("disk full"))
        operation_api.subprocess.Popen = capture
        try:
            with self.assertRaisesRegex(OSError, "disk full"):
                asyncio.run(operation_api.run_operation(
                    "fixture-review", FakeRequest({"arguments": ["--all"],
                                                   "plan_digest": plan["snapshot_digest"]})))
        finally:
            operation_api.operation_run.acquire_lockset = original_lock
            operation_api.operation_run.initial_receipt = original_receipt
            operation_api.subprocess.Popen = original_popen
        self.assertTrue(lock.released)
        self.assertGreaterEqual(len(spawned), 1)  # plan subprocess plus the asynchronous worker
        self.assertTrue(all(process.poll() is not None for process in spawned))

    def test_abort_start_fallback_and_forced_kill_paths(self):
        calls = []
        class Process:
            pid = 123
            waits = 0
            def poll(self): return None
            def terminate(self): calls.append("terminate")
            def kill(self): calls.append("kill")
            def wait(self, timeout):
                self.waits += 1
                if self.waits == 1:
                    raise operation_api.subprocess.TimeoutExpired("worker", timeout)
                calls.append("waited")
        class Lock:
            def release(self): calls.append("released")
        original = operation_api.os.killpg
        operation_api.os.killpg = lambda *_a: (_ for _ in ()).throw(OSError("gone"))
        try:
            operation_api.abort_start(Process(), Lock())
        finally:
            operation_api.os.killpg = original
        self.assertEqual(calls, ["terminate", "kill", "waited", "released"])

        calls.clear()
        ended = Process()
        ended.poll = lambda: 0
        operation_api.abort_start(ended, Lock())
        self.assertEqual(calls, ["released"])


if __name__ == "__main__":
    unittest.main()
