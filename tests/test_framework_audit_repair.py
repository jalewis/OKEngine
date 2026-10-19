"""Audit/guarded-repair family contract (okengine#407)."""
import importlib.util
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("framework_audit_repair", REPO / "scripts/framework_audit_repair.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def dep(tmp_path):
    root = tmp_path / "dep"
    (root / "wiki/entities").mkdir(parents=True)
    (root / "wiki/entities/a.md").write_text("---\ntype: actor\n---\n# A\n")
    return root


def call(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(argv)
    return code, out.getvalue(), err.getvalue()


def test_audit_writes_one_parent_receipt_and_propagates_failure(tmp_path, monkeypatch):
    root = dep(tmp_path)
    monkeypatch.setattr(mod, "_run", lambda script, deployment, extra=None:
                        {"exit_code": 1 if "policy" in script else 0, "stdout": "", "stderr": "bad"})
    code, out, _ = call(mod.audit, [str(root), "--checks", "grounding,policy", "--json"])
    summary = json.loads(out)
    assert code == 1 and summary["status"] == "failed" and summary["failed"] == ["policy"]
    receipt = json.loads((root / ".okengine/operations/runs" / f"{summary['run_id']}.json").read_text())
    assert receipt["checks"] == ["grounding", "policy"] and len(receipt["children"]) == 2


def test_repair_requires_immutable_plan_and_rejects_drift(tmp_path, monkeypatch):
    root = dep(tmp_path)
    audit_id = "audit-fixture"
    mod._atomic_new(root / ".okengine/operations/runs" / f"{audit_id}.json",
                    {"api": 1, "kind": "audit", "run_id": audit_id})
    code, out, _ = call(mod.repair, ["plan", str(root), "--from-audit", audit_id,
                                    "--repairs", "body-integrity", "--json"])
    assert code == 0
    plan = json.loads(out)
    (root / "wiki/entities/a.md").write_text("changed\n")
    monkeypatch.setattr(mod, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    code, _, err = call(mod.repair, ["apply", str(root), "--plan", plan["plan_id"]])
    assert code == 1 and "changed since plan" in err


def test_apply_and_verify_are_separate_receipted_phases(tmp_path, monkeypatch):
    root = dep(tmp_path)
    audit_id = "audit-fixture"
    mod._atomic_new(root / ".okengine/operations/runs" / f"{audit_id}.json",
                    {"api": 1, "kind": "audit", "run_id": audit_id})
    _, out, _ = call(mod.repair, ["plan", str(root), "--from-audit", audit_id,
                                 "--repairs", "malformed-slugs", "--json"])
    plan = json.loads(out)
    calls = []
    monkeypatch.setattr(mod, "_run", lambda script, deployment, extra=None:
                        calls.append(extra) or {"exit_code": 0, "stdout": "", "stderr": ""})
    assert call(mod.repair, ["apply", str(root), "--plan", plan["plan_id"]])[0] == 0
    assert call(mod.repair, ["verify", str(root), "--plan", plan["plan_id"]])[0] == 0
    assert "--apply" in calls[0] and "--apply" not in calls[1]
    code, _, err = call(mod.repair, ["apply", str(root), "--plan", plan["plan_id"]])
    assert code == 1 and "already applied" in err


def test_verify_requires_successful_apply_receipt(tmp_path):
    root = dep(tmp_path)
    audit_id = "audit-fixture"
    mod._atomic_new(root / ".okengine/operations/runs" / f"{audit_id}.json",
                    {"api": 1, "kind": "audit", "run_id": audit_id})
    _, out, _ = call(mod.repair, ["plan", str(root), "--from-audit", audit_id,
                                 "--repairs", "body-integrity", "--json"])
    plan = json.loads(out)
    code, _, err = call(mod.repair, ["verify", str(root), "--plan", plan["plan_id"]])
    assert code == 1 and "no successful apply receipt" in err


def test_helpers_reject_invalid_immutable_and_artifact_inputs(tmp_path, monkeypatch):
    with pytest.raises(mod.AuditRepairError, match="not an OKEngine deployment"):
        mod._deployment(tmp_path / "missing")
    root = dep(tmp_path)
    generated = root / ".okengine/existing.json"
    mod._atomic_new(generated, {"kind": "fixture"})
    with pytest.raises(mod.AuditRepairError, match="immutable artifact"):
        mod._atomic_new(generated, {})

    bad = root / ".okengine/operations/runs/bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{")
    with pytest.raises(mod.AuditRepairError, match="cannot read audit"):
        mod._load_artifact(root, "audit", "bad")
    wrong = root / ".okengine/operations/runs/wrong.json"
    wrong.write_text(json.dumps({"kind": "plan"}))
    with pytest.raises(mod.AuditRepairError, match="not a audit"):
        mod._load_artifact(root, "audit", "wrong")

    # Generated/operational markdown is excluded from the immutable input digest.
    before = mod._digest(root)
    dashboard = root / "wiki/dashboards/x.md"
    dashboard.parent.mkdir()
    dashboard.write_text("generated")
    assert mod._digest(root) == before

    result = type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
    seen = {}
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda command, **kwargs: seen.update(command=command, kwargs=kwargs) or result,
    )
    assert mod._run("audit.py", root)["exit_code"] == 0
    assert seen["kwargs"]["env"]["WIKI_PATH"] == str(root)


def test_audit_plan_and_execute_rejection_edges(tmp_path, monkeypatch):
    root = dep(tmp_path)
    with pytest.raises(SystemExit):
        mod.audit([str(root), "--checks", "unknown"])
    audit_id = "audit-fixture"
    mod._atomic_new(root / ".okengine/operations/runs" / f"{audit_id}.json",
                    {"api": 1, "kind": "audit", "run_id": audit_id})
    code, _, err = call(mod.repair, [
        "plan", str(root), "--from-audit", audit_id, "--repairs", "unknown",
    ])
    assert code == 1 and "unknown repairs" in err

    _, out, _ = call(mod.repair, [
        "plan", str(root), "--from-audit", audit_id, "--repairs", "body-integrity", "--json",
    ])
    plan = json.loads(out)
    plan_path = root / ".okengine/operations/plans" / f"{plan['plan_id']}.json"
    payload = json.loads(plan_path.read_text())
    payload["deployment"] = str(tmp_path / "elsewhere")
    plan_path.write_text(json.dumps(payload))
    code, _, err = call(mod.repair, ["apply", str(root), "--plan", plan["plan_id"]])
    assert code == 1 and "different deployment" in err


def test_execute_skips_malformed_prior_receipt(tmp_path, monkeypatch):
    root = dep(tmp_path)
    audit_id = "audit-fixture"
    mod._atomic_new(root / ".okengine/operations/runs" / f"{audit_id}.json",
                    {"api": 1, "kind": "audit", "run_id": audit_id})
    _, out, _ = call(mod.repair, [
        "plan", str(root), "--from-audit", audit_id, "--repairs", "body-integrity", "--json",
    ])
    plan = json.loads(out)
    malformed = root / ".okengine/operations/runs/repair-malformed.json"
    malformed.write_text("{")
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: {
        "exit_code": 0, "stdout": "", "stderr": "",
    })
    assert call(mod.repair, ["apply", str(root), "--plan", plan["plan_id"]])[0] == 0
    (root / ".okengine/operations/runs/repair-after.json").write_text(
        json.dumps({"kind": "other", "status": "failed"})
    )
    assert call(mod.repair, ["verify", str(root), "--plan", plan["plan_id"]])[0] == 0


# --- receipts, statuses and argument contracts (mutation target registered for okengine#994) -----

def _plan_fixture(root, repairs="body-integrity"):
    mod._atomic_new(root / ".okengine/operations/runs/audit-fixture.json",
                    {"api": 1, "kind": "audit", "run_id": "audit-fixture"})
    _, out, _ = call(mod.repair, ["plan", str(root), "--from-audit", "audit-fixture",
                                  "--repairs", repairs, "--json"])
    return json.loads(out)


def test_a_directory_without_a_wiki_is_not_a_deployment(tmp_path):
    (tmp_path / "bare").mkdir()
    with pytest.raises(mod.AuditRepairError, match="not an OKEngine deployment"):
        mod._deployment(tmp_path / "bare")


def test_artifacts_are_indented_sorted_json(tmp_path):
    target = tmp_path / "a.json"
    mod._atomic_new(target, {"b": 1, "a": 2})
    assert target.read_text() == '{\n  "a": 2,\n  "b": 1\n}\n'


def test_audit_requires_a_selection_and_passes_check_only_to_schema_drift(tmp_path, monkeypatch):
    root = dep(tmp_path)
    with pytest.raises(SystemExit):
        mod.audit([str(root)])
    seen = {}
    monkeypatch.setattr(mod, "_run", lambda script, deployment, extra=None:
                        seen.update({script: extra}) or {"exit_code": 0, "stdout": "", "stderr": ""})
    code, out, _ = call(mod.audit, [str(root), "--checks", "schema-drift,policy", "--json"])
    summary = json.loads(out)
    assert code == 0 and summary["status"] == "succeeded" and summary["failed"] == []
    assert seen == {"wiki_schema_audit.py": ["--check"], "policy_audit.py": None}
    import re
    assert re.fullmatch(r"audit-\d{4}-\d{2}-\d{2}T\d{6}Z-[0-9a-f]{8}", summary["run_id"])
    receipt = json.loads((root / ".okengine/operations/runs" / f"{summary['run_id']}.json").read_text())
    assert receipt["api"] == 1 and receipt["status"] == "succeeded"
    assert list(json.loads(out)) == sorted(json.loads(out))


def test_a_child_killed_by_a_signal_fails_the_audit_and_the_repair(tmp_path, monkeypatch):
    root = dep(tmp_path)
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: {"exit_code": -9, "stdout": "", "stderr": ""})
    code, out, _ = call(mod.audit, [str(root), "--checks", "policy", "--json"])
    assert code == 1 and json.loads(out)["status"] == "failed"
    monkeypatch.undo()
    plan = _plan_fixture(root)
    assert plan["api"] == 1 and list(plan) == sorted(plan)
    monkeypatch.setattr(mod, "_run", lambda *_a, **_k: {"exit_code": -9, "stdout": "", "stderr": ""})
    code, out, _ = call(mod.repair, ["apply", str(root), "--plan", plan["plan_id"], "--json"])
    receipt = json.loads(out)
    assert code == 1 and receipt["status"] == "failed" and receipt["api"] == 1
    assert list(receipt) == sorted(receipt)


def test_only_a_successful_apply_of_this_plan_counts_as_applied(tmp_path, monkeypatch):
    root = dep(tmp_path)
    plan = _plan_fixture(root)
    runs = root / ".okengine/operations/runs"
    for name, receipt in {
        "repair-failed": {"kind": "apply", "plan_id": plan["plan_id"], "status": "failed"},
        "repair-other": {"kind": "apply", "plan_id": "repair-other", "status": "succeeded"},
        "repair-verify": {"kind": "verify", "plan_id": plan["plan_id"], "status": "succeeded"},
    }.items():
        (runs / f"{name}.json").write_text(json.dumps(receipt))
    code, _, err = call(mod.repair, ["verify", str(root), "--plan", plan["plan_id"]])
    assert code == 1 and "no successful apply receipt" in err


def test_repair_subcommands_require_their_arguments(tmp_path):
    root = dep(tmp_path)
    for argv in (["plan", str(root), "--repairs", "body-integrity"],
                 ["plan", str(root), "--from-audit", "x"],
                 ["apply", str(root)], ["verify", str(root)]):
        with pytest.raises(SystemExit):
            call(mod.repair, argv)
