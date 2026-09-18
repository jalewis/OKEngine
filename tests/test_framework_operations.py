import contextlib
import io
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

import yaml

import pytest

pytestmark = pytest.mark.integration


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
        # Worker contract (okengine#402): the entrypoint reads the ENGINE-allocated run id and prints
        # its result — it no longer chooses the run id or writes the receipt (the engine owns both).
        (self.deployment / "crons/scripts/fixture_operation.py").write_text(
            "import argparse,json,os\n"
            "p=argparse.ArgumentParser();p.add_argument('--target-vault');"
            "p.add_argument('--dry-run',action='store_true');p.add_argument('--resume');"
            "p.add_argument('--all',action='store_true');a=p.parse_args()\n"
            "rid=os.environ.get('OKENGINE_OPERATION_RUN_ID') or a.resume or 'plan'\n"
            "print(json.dumps({'operation':'fixture-review','run_id':rid,"
            "'status':'planned' if a.dry_run else 'succeeded'}))\n", encoding="utf-8")

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
        self.assertFalse((self.deployment / ".okengine").exists())      # plan writes nothing
        code, output, _ = self.call("run", str(self.deployment), "fixture-review", "--all")
        self.assertEqual(code, 0)
        receipt = json.loads(output)
        self.assertEqual(receipt["status"], "succeeded")
        run_id = receipt["run_id"]
        self.assertTrue(run_id.startswith("fixture-review-"))           # ENGINE-allocated, not the pack's id
        # the ENGINE (not the entrypoint) wrote the authoritative receipt
        self.assertTrue((self.deployment / ".okengine/operations/runs/fixture-review"
                         / f"{run_id}.json").is_file())
        code, output, _ = self.call("status", str(self.deployment), run_id, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["operation"], "fixture-review")

    def test_resume_uses_existing_operation_and_cancel_is_durable(self):
        _, output, _ = self.call("run", str(self.deployment), "fixture-review", "--all")
        run_id = json.loads(output)["run_id"]                           # engine-allocated
        code, output, _ = self.call("resume", str(self.deployment), run_id, "--all")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["run_id"], run_id)          # resume re-uses the same run id
        code, output, _ = self.call("cancel", str(self.deployment), run_id,
                                    "--reason", "operator requested stop")
        self.assertEqual(code, 0)
        request = json.loads(output)
        self.assertEqual(request["reason"], "operator requested stop")
        self.assertTrue((self.deployment / ".okengine/operations/cancel" / f"{run_id}.json").is_file())

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
        self.assertIsNone(operations.result_from_output("noise\n[]\n{bad}"))

    def test_text_list_inspect_history_logs_and_failed_status(self):
        code, output, _ = self.call("list", str(self.deployment))
        self.assertEqual(code, 0)
        self.assertIn("fixture-review", output)
        code, output, _ = self.call("inspect", str(self.deployment), "fixture-review")
        self.assertEqual(code, 0)
        self.assertIn("owner: fixture-pack", output)

        runs = self.deployment / ".okengine/operations/runs/fixture-review"
        runs.mkdir(parents=True)
        (runs / "bad.json").write_text("{")
        (runs / "other.json").write_text(json.dumps({
            "run_id": "other", "operation": "other", "status": "succeeded",
        }))
        (runs / "failed.json").write_text(json.dumps({
            "run_id": "failed", "operation": "fixture-review", "status": "failed",
            "started_at": "2026-01-01",
        }))
        (runs / "failed.jsonl").write_text('{"event":"failed"}\n')
        code, output, _ = self.call(
            "history", str(self.deployment), "--operation", "fixture-review", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["runs"][0]["run_id"], "failed")
        code, output, _ = self.call("history", str(self.deployment))
        self.assertEqual(code, 0)
        self.assertIn("failed", output)
        code, output, _ = self.call("logs", str(self.deployment), "failed")
        self.assertEqual(code, 0)
        self.assertIn('"event":"failed"', output)
        code, _, _ = self.call("status", str(self.deployment), "failed")
        self.assertEqual(code, 1)

    def test_empty_list_unknown_operation_and_receipt_failures(self):
        (self.deployment / "operations/fixture/operation.yaml").unlink()
        code, output, _ = self.call("list", str(self.deployment))
        self.assertEqual(code, 0)
        self.assertIn("no operations discovered", output)
        code, _, error = self.call("inspect", str(self.deployment), "missing")
        self.assertEqual(code, 1)
        self.assertIn("operation not found", error)
        for run_id in ("../bad", "missing"):
            code, _, error = self.call("status", str(self.deployment), run_id)
            self.assertEqual(code, 1)
        base = self.deployment / ".okengine/operations/runs"
        for op in ("a", "b"):
            (base / op).mkdir(parents=True, exist_ok=True)
            (base / op / "same.json").write_text("{}")
        code, _, error = self.call("status", str(self.deployment), "same")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", error)

    def test_resume_default_actor_all_and_unsupported_controls(self):
        # Exercise the actor-review convenience insertion using a renamed manifest.
        manifest_path = self.deployment / "operations/fixture/operation.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["name"] = "actor-review"
        manifest_path.write_text(yaml.safe_dump(manifest))
        runs = self.deployment / ".okengine/operations/runs/actor-review"
        runs.mkdir(parents=True)
        (runs / "run.json").write_text(json.dumps({
            "run_id": "run", "operation": "actor-review", "status": "failed",
        }))
        import operation_run
        original = operation_run.run
        seen = {}
        operation_run.run = lambda dep, man, args, **kwargs: (
            seen.setdefault("args", args) is None, {"status": "succeeded"})
        # The boolean expression above returns False as an exit code (==0).
        code, _, _ = self.call("resume", str(self.deployment), "run")
        operation_run.run = original
        self.assertEqual(code, 0)
        self.assertEqual(seen["args"][:2], ["--all", "--resume"])

        manifest["supports"]["resume"] = False
        manifest["supports"]["cancel"] = False
        manifest_path.write_text(yaml.safe_dump(manifest))
        code, _, error = self.call("resume", str(self.deployment), "run")
        self.assertEqual(code, 1)
        self.assertIn("does not support resume", error)
        code, _, error = self.call(
            "cancel", str(self.deployment), "run", "--reason", "stop")
        self.assertEqual(code, 1)
        self.assertIn("does not support cancel", error)

    def test_cancel_idempotency_conflict(self):
        _, output, _ = self.call("run", str(self.deployment), "fixture-review")
        run_id = json.loads(output)["run_id"]
        code, _, _ = self.call(
            "cancel", str(self.deployment), run_id, "--reason", "same")
        self.assertEqual(code, 0)
        code, _, error = self.call(
            "cancel", str(self.deployment), run_id, "--reason", "different")
        self.assertEqual(code, 1)
        self.assertIn("different cancel request", error)

    def test_invalid_receipt_watch_poll_idempotent_cancel_and_unknown_dispatch(self):
        runs = self.deployment / ".okengine/operations/runs/fixture-review"
        runs.mkdir(parents=True)
        (runs / "broken.json").write_text("{")
        code, _, error = self.call("status", str(self.deployment), "broken")
        self.assertEqual(code, 1)
        self.assertIn("invalid operation receipt", error)

        receipts = iter([
            (runs / "watch.json", {"run_id": "watch", "status": "running"}),
            (runs / "watch.json", {"run_id": "watch", "status": "succeeded"}),
        ])
        with mock.patch.object(
                operations, "_find_receipt", autospec=True,
                side_effect=lambda *_: next(receipts)), \
                mock.patch.object(operations.time, "sleep", autospec=True) as sleep:
            code, _, _ = self.call("status", str(self.deployment), "watch", "--watch")
        self.assertEqual(code, 0)
        sleep.assert_called_once_with(2)

        _, output, _ = self.call("run", str(self.deployment), "fixture-review")
        run_id = json.loads(output)["run_id"]
        fixed = operations.dt.datetime(2026, 1, 1, tzinfo=operations.dt.timezone.utc)
        clock = SimpleNamespace(
            datetime=SimpleNamespace(now=lambda _tz: fixed), timezone=operations.dt.timezone)
        with mock.patch.object(operations, "dt", clock):
            self.assertEqual(self.call("cancel", str(self.deployment), run_id,
                                       "--reason", "same")[0], 0)
            self.assertEqual(self.call("cancel", str(self.deployment), run_id,
                                       "--reason", "same")[0], 0)

        parser = SimpleNamespace(parse_args=lambda _argv: SimpleNamespace(
            command="unknown", deployment=str(self.deployment)))
        with mock.patch.object(
                operations, "_parser", autospec=True, return_value=parser):
            self.assertEqual(operations.main([]), 2)


class OperationManifestValidationTests(unittest.TestCase):
    """okengine#401: the manifest contract is validated fail-closed — every declared field the runner
    depends on (locks/inputs/outputs/capability/receipt_schema/timeout/mutates/execution/arguments) is
    shape-checked before an operation can be discovered, not just the original five keys."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dep = Path(self.temp.name)
        (self.dep / "wiki").mkdir()
        (self.dep / "crons/scripts").mkdir(parents=True)
        (self.dep / "crons/scripts/op.py").write_text("print('x')\n", encoding="utf-8")
        self.src = self.dep / "operations/x/operation.yaml"
        self.src.parent.mkdir(parents=True)
        self.base = {"operation_api": 1, "name": "x-op", "owner": "p",
                     "entrypoint": "crons/scripts/op.py"}

    def tearDown(self):
        self.temp.cleanup()

    def _v(self, **over):
        return operations._validate({**self.base, **over}, self.src, self.dep)

    def test_full_valid_manifest_is_accepted_and_normalized(self):
        m = self._v(execution="deterministic", mutates=True,
                    supports={"plan": True, "resume": False, "cancel": True},
                    arguments={"all": {"type": "boolean"},
                               "actor": {"type": "page-ref", "repeatable": True}},
                    locks=["assessments/threat-actors"],
                    inputs=["wiki/entities/**", "wiki/sources/**"],
                    outputs=["wiki/assessments/**"],
                    permissions={"capability": "assessments.run"},
                    receipt_schema="schemas/r.yaml", timeout=600)
        self.assertEqual(m["locks"], ["assessments/threat-actors"])
        self.assertEqual(m["inputs"], ["wiki/entities/**", "wiki/sources/**"])
        self.assertEqual(m["outputs"], ["wiki/assessments/**"])

    def test_invalid_fields_fail_closed(self):
        for bad in (
            {"execution": "wishful"},                       # unknown execution class
            {"mutates": "yes"},                             # non-bool
            {"supports": {"plan": "true"}},                 # non-bool support flag
            {"arguments": {"all": {"type": "colour"}}},     # unknown arg type
            {"arguments": {"all": {"repeatable": "no"}}},   # non-bool repeatable
            {"arguments": {"all": "boolean"}},              # arg spec not a mapping
            {"locks": ["Bad Lock!"]},                       # invalid lock resource id
            {"locks": "notalist"},                          # locks not a list
            {"inputs": ["/etc/passwd"]},                    # absolute input path
            {"outputs": ["../escape/**"]},                  # traversal in output path
            {"permissions": {"capability": ""}},            # empty capability
            {"permissions": "nope"},                        # permissions not a mapping
            {"receipt_schema": "/abs/r.yaml"},              # unsafe receipt_schema path
            {"timeout": 0}, {"timeout": -5}, {"timeout": True},   # non-positive / bool timeout
        ):
            with self.assertRaises(operations.OperationsError, msg=f"should reject {bad}"):
                self._v(**bad)

    def test_basic_manifest_and_safe_path_failures(self):
        invalid = (
            None,
            {"operation_api": 2, "name": "x-op", "owner": "p",
             "entrypoint": "crons/scripts/op.py"},
            {**self.base, "name": "X"},
            {**self.base, "owner": ""},
            {**self.base, "entrypoint": ""},
            {**self.base, "entrypoint": "missing.py"},
            {**self.base, "supports": ["x"]},
            {**self.base, "arguments": ["x"]},
        )
        for raw in invalid:
            with self.assertRaises(operations.OperationsError):
                operations._validate(raw, self.src, self.dep)
        with self.assertRaises(operations.OperationsError):
            operations._safe_glob("not-list", "inputs", self.src)
        self.assertEqual(operations._safe_glob(None, "inputs", self.src), [])
        with self.assertRaises(operations.OperationsError):
            operations._deployment(self.dep / "missing")
        outside = self.dep.parent / "outside-operation.py"
        outside.write_text("print('outside')\n")
        link = self.dep / "crons/scripts/escape.py"
        link.symlink_to(outside)
        with self.assertRaisesRegex(operations.OperationsError, "escapes deployment"):
            self._v(entrypoint="crons/scripts/escape.py")

    def test_discover_read_errors_identical_override_and_command_plan(self):
        self.src.write_text("[")
        with self.assertRaisesRegex(operations.OperationsError, "cannot read"):
            operations.discover(self.dep)
        self.src.write_text(yaml.safe_dump(self.base))
        effective = self.dep / ".okengine/operations/x/operation.yaml"
        effective.parent.mkdir(parents=True)
        effective.write_text(yaml.safe_dump(self.base))
        found = operations.discover(self.dep)
        self.assertTrue(found["x-op"]["manifest_path"].startswith(".okengine/"))
        manifest = found["x-op"]
        manifest["supports"] = {}
        with self.assertRaisesRegex(operations.OperationsError, "does not support planning"):
            operations.operation_command(self.dep, manifest, [], plan=True)
        manifest["supports"] = {"plan": True}
        command, _ = operations.operation_command(self.dep, manifest, [], plan=True)
        self.assertEqual(command[-1], "--dry-run")

        # Exercise replacement when the effective manifest is encountered second.
        real_sorted = sorted
        with mock.patch("builtins.sorted", autospec=True,
                        side_effect=lambda values, *a, **k:
                        list(reversed(real_sorted(values, *a, **k)))):
            found = operations.discover(self.dep)
        self.assertTrue(found["x-op"]["manifest_path"].startswith(".okengine/"))


if __name__ == "__main__":
    unittest.main()
