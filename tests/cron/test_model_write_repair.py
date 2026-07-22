import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts/cron/model_write_repair.py"
RECEIPTS = REPO / "patches/cron-plus/run_receipts.py"


def _receipt_module():
    spec = importlib.util.spec_from_file_location("run_receipts", RECEIPTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_preconditions_batch_and_receipt_checkpoint(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 2\nraw: raw/a.md\n---\nshort\n")
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    item = "sources/a.md|recompile-from-declared-raw"
    plan.write_text(json.dumps({"actions": [{
        "path": "sources/a.md", "expected_sha256": _sha(page), "expected_version": 2,
        "action": "recompile-from-declared-raw", "fabricate_evidence": False}]}) )
    env = dict(os.environ, WIKI_PATH=str(tmp_path), HERMES_HOME=str(tmp_path / ".hermes"),
               OKENGINE_LANE_ID="repair", OKENGINE_CONTRACT_DIGEST="sha256:contract")
    env["OKENGINE_SELECTION_MANIFEST"] = str(
        tmp_path / ".hermes/cron-plus/selections/model-write-repair-drain.json")
    run = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)
    assert run.returncode == 0 and json.loads(run.stdout.strip().splitlines()[-1])["wakeAgent"]
    manifest = json.loads((tmp_path / ".hermes/cron-plus/selections/model-write-repair-drain.json").read_text())
    assert manifest["selected"] == [item]
    batch = json.loads((tmp_path / ".okengine/model-write-repair-batch.json").read_text())
    assert batch["actions"][0]["canonical_path"] == "wiki/sources/a.md"
    assert batch["actions"][0]["declared_raw"] == ["raw/a.md"]
    template = batch["receipt_template"]
    assert template["lane_id"] == "repair"
    assert template["contract_digest"] == "sha256:contract"
    assert template["input_digest"] == manifest["input_digest"]
    assert [entry["key"] for entry in template["items"]] == [item]
    assert template["items"][0]["writes"][0]["path"] == "wiki/<path>"
    assert "```okengine-receipt" in run.stdout

    response_receipt = dict(template, items=[{
        "key": item, "disposition": "deferred", "writes": [],
        "reason": "declared raw unavailable",
    }])
    response = "```okengine-receipt\n" + json.dumps(response_receipt) + "\n```"
    receipts = _receipt_module()
    parsed = receipts.parse_response(response)
    result = receipts.validate(parsed, manifest, {
        "id": "repair", "output_contract_digest": "sha256:contract",
    }, tmp_path / "wiki")
    assert result["valid"]

    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"items": [{"key": item, "disposition": "accepted"}]}))
    recorded = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(tmp_path),
                               "--receipt", str(receipt)], env=env, text=True, capture_output=True)
    assert recorded.returncode == 0
    drained = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)
    assert not json.loads(drained.stdout.strip().splitlines()[-1])["wakeAgent"]


def test_changed_page_is_not_selected(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 3\n---\nchanged\n")
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps({"actions": [{"path": "sources/a.md",
        "expected_sha256": "sha256:stale", "expected_version": 2,
        "action": "quarantine-for-review"}]}))
    run = subprocess.run([sys.executable, str(SCRIPT), "--vault", str(tmp_path)],
                         env=dict(os.environ, HERMES_HOME=str(tmp_path / ".hermes")),
                         text=True, capture_output=True)
    assert run.returncode == 0
    assert not json.loads(run.stdout.strip().splitlines()[-1])["wakeAgent"]


def test_generated_repair_lane_is_receipt_enforced():
    jobs = json.loads((REPO / "config/engine-crons.json").read_text())
    job = next(j for j in jobs if j["name"] == "model-write-repair-drain")
    assert job["receipt_mode"] == "enforce"
    assert job["output_contract"]["completion"] == "per-selected-item"
    assert "NEVER `/opt/vault/wiki/raw" in job["prompt"]
    assert "ONLY one fenced `okengine-receipt` JSON object" in job["prompt"]
    assert "copied from `receipt_template`" in job["prompt"]
    assert job["enabled_toolsets"][0] == "file_read"
    assert "Never create helper files" in job["prompt"]
    assert "Use `read_file`" in job["prompt"]
    assert "NEVER invent a `file://` URI" in job["prompt"]
    assert "returned verbatim by `list_resources`" in job["prompt"]


def test_batch_selects_one_action_per_page(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 1\nraw: raw/a.md\n---\nshort\n")
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    common = {"path": "sources/a.md", "expected_sha256": _sha(page), "expected_version": 1}
    plan.write_text(json.dumps({"actions": [
        {**common, "action": "recompile-from-declared-raw"},
        {**common, "action": "quarantine-for-review"},
    ]}))
    env = dict(os.environ, WIKI_PATH=str(tmp_path), HERMES_HOME=str(tmp_path / ".hermes"),
               OKENGINE_LANE_ID="repair", OKENGINE_CONTRACT_DIGEST="sha256:contract",
               OKENGINE_SELECTION_MANIFEST=str(tmp_path / "selection.json"))
    run = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)
    assert run.returncode == 0
    assert json.loads((tmp_path / "selection.json").read_text())["selected"] == [
        "sources/a.md|recompile-from-declared-raw"]


def test_quarantine_receipt_template_has_no_fake_page_write(tmp_path):
    page = tmp_path / "wiki/cves/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: cve\nversion: 1\n---\nshort\n")
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps({"actions": [{
        "path": "cves/a.md", "expected_sha256": _sha(page), "expected_version": 1,
        "action": "quarantine-for-review",
    }]}))
    env = dict(os.environ, WIKI_PATH=str(tmp_path), HERMES_HOME=str(tmp_path / ".hermes"),
               OKENGINE_LANE_ID="repair", OKENGINE_CONTRACT_DIGEST="sha256:contract",
               OKENGINE_SELECTION_MANIFEST=str(tmp_path / "selection.json"))
    run = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)
    assert run.returncode == 0
    batch = json.loads((tmp_path / ".okengine/model-write-repair-batch.json").read_text())
    assert batch["receipt_template"]["items"][0]["writes"] == []


def test_scheduled_run_imports_verified_receipt_checkpoint(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 1\nraw: raw/a.md\n---\nshort\n")
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    item = "sources/a.md|recompile-from-declared-raw"
    plan.write_text(json.dumps({"actions": [{"path": "sources/a.md",
        "expected_sha256": _sha(page), "expected_version": 1,
        "action": "recompile-from-declared-raw"}]}))
    hermes = tmp_path / ".hermes"
    receipt_dir = hermes / "cron-plus/receipts/repair"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "run.json").write_text(json.dumps({"valid": True, "receipt": {
        "items": [{"key": item, "disposition": "deferred", "reason": "raw unavailable"}]}}))
    env = dict(os.environ, WIKI_PATH=str(tmp_path), HERMES_HOME=str(hermes),
               OKENGINE_LANE_ID="repair", OKENGINE_CONTRACT_DIGEST="sha256:contract",
               OKENGINE_SELECTION_MANIFEST=str(tmp_path / "selection.json"))
    run = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)
    assert run.returncode == 0
    assert "imported 1 receipt disposition" in run.stdout
    assert not json.loads(run.stdout.strip().splitlines()[-1])["wakeAgent"]
    state = json.loads((tmp_path / ".okengine/model-write-repair-state.json").read_text())
    assert state["completed"][item]["status"] == "deferred"
