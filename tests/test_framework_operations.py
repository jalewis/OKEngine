import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import framework_operations as operations


class FrameworkOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.deployment = Path(self.temp.name)
        (self.deployment / "wiki").mkdir()
        (self.deployment / "crons/scripts").mkdir(parents=True)
        operation_dir = self.deployment / "operations/fixture"
        operation_dir.mkdir(parents=True)
        manifest = {
            "operation_api": 1, "name": "fixture-review", "owner": "fixture-pack",
            "title": "Fixture review", "entrypoint": "crons/scripts/fixture_operation.py",
            "execution": "deterministic", "mutates": True,
            "supports": {"plan": True, "resume": True, "cancel": True},
        }
        (operation_dir / "operation.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        (self.deployment / "crons/scripts/fixture_operation.py").write_text(
            "import argparse,json\n"
            "from pathlib import Path\n"
            "p=argparse.ArgumentParser();p.add_argument('--target-vault',type=Path);"
            "p.add_argument('--dry-run',action='store_true');p.add_argument('--resume');"
            "p.add_argument('--all',action='store_true');a=p.parse_args()\n"
            "r={'operation':'fixture-review','run_id':a.resume or 'run-1',"
            "'status':'planned' if a.dry_run else 'succeeded'}\n"
            "if not a.dry_run:\n"
            " d=a.target_vault/'.okengine/operations/runs/fixture-review';d.mkdir(parents=True,exist_ok=True);"
            "(d/f\"{r['run_id']}.json\").write_text(json.dumps(r))\n"
            "print(json.dumps(r))\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = operations.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_list_and_inspect_discover_pack_operation(self):
        code, output, _ = self.call("list", str(self.deployment), "--json")
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertEqual(result["operations"][0]["name"], "fixture-review")
        code, output, _ = self.call("inspect", str(self.deployment), "fixture-review", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["owner"], "fixture-pack")

    def test_plan_is_non_mutating_and_run_produces_status_receipt(self):
        code, output, _ = self.call("plan", str(self.deployment), "fixture-review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "planned")
        self.assertFalse((self.deployment / ".okengine").exists())
        code, output, _ = self.call("run", str(self.deployment), "fixture-review", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "succeeded")
        code, output, _ = self.call("status", str(self.deployment), "run-1", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["operation"], "fixture-review")

    def test_resume_uses_existing_operation_and_cancel_is_durable(self):
        self.call("run", str(self.deployment), "fixture-review", "--all")
        code, output, _ = self.call("resume", str(self.deployment), "run-1", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["run_id"], "run-1")
        code, output, _ = self.call("cancel", str(self.deployment), "run-1",
                                    "--reason", "operator requested stop")
        self.assertEqual(code, 0)
        request = json.loads(output)
        self.assertEqual(request["reason"], "operator requested stop")
        self.assertTrue((self.deployment / ".okengine/operations/cancel/run-1.json").is_file())

    def test_conflicting_duplicate_manifest_fails_closed(self):
        other = self.deployment / ".okengine/operations/fixture"
        other.mkdir(parents=True)
        manifest = yaml.safe_load((self.deployment / "operations/fixture/operation.yaml").read_text())
        manifest["owner"] = "different-owner"
        (other / "operation.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        with self.assertRaisesRegex(operations.OperationsError, "collision"):
            operations.discover(self.deployment)

    def test_shared_command_builder_records_invocation_source(self):
        manifest = operations._operation(self.deployment, "fixture-review")
        command, env = operations.operation_command(
            self.deployment, manifest, ["--all"], source="cockpit")
        self.assertEqual(command[-1], "--all")
        self.assertEqual(env["OKENGINE_OPERATION_SOURCE"], "cockpit")
        self.assertEqual(env["OKENGINE_OPERATION_NAME"], "fixture-review")

    def test_result_parser_uses_last_structured_record(self):
        self.assertEqual(
            operations.result_from_output('progress\n{"status":"running"}\n{"status":"succeeded"}\n'),
            {"status": "succeeded"})


if __name__ == "__main__":
    unittest.main()
