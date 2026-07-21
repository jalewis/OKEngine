import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import yaml


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
        (self.deployment / "crons/scripts/fixture.py").write_text(
            "import argparse,json,os,time\n"
            "from pathlib import Path\n"
            "p=argparse.ArgumentParser();p.add_argument('--target-vault',type=Path);"
            "p.add_argument('--dry-run',action='store_true');p.add_argument('--all',action='store_true');"
            "a=p.parse_args()\n"
            "r={'operation':'fixture-review','run_id':'fixture-run','pid':os.getpid(),"
            "'status':'planned' if a.dry_run else 'succeeded','actor_inventory':[{'ref':'a'}],"
            "'dimensions':['one'],'counts':{'actor_questions':1},'snapshot_digest':'fixture-digest'}\n"
            "if not a.dry_run:\n"
            " time.sleep(.1);d=a.target_vault/'.okengine/operations/runs/fixture-review';"
            "d.mkdir(parents=True,exist_ok=True);(d/'fixture-run.json').write_text(json.dumps(r))\n"
            "print(json.dumps(r))\n", encoding="utf-8")
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
        started = asyncio.run(operation_api.run_operation(
            "fixture-review", FakeRequest({"arguments": ["--all"],
                                           "plan_digest": "fixture-digest"})))
        self.assertEqual(started["status"], "running")
        process = operation_api._PROCESSES.get(started["request_id"])
        self.assertIsNotNone(process)
        process.wait(timeout=10)
        for _ in range(50):
            path, value = operation_api.load_request(started["request_id"])
            value = operation_api.refresh(path, value)
            if value["status"] == "succeeded":
                break
            time.sleep(.02)
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(value["run_id"], "fixture-run")
        self.assertTrue(Path(value["receipt"]).as_posix().endswith("fixture-run.json"))

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


if __name__ == "__main__":
    unittest.main()
