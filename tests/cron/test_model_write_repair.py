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


def _module():
    sys.path.insert(0,str(REPO/"scripts/cron"))
    spec=importlib.util.spec_from_file_location("model_write_repair_edges",SCRIPT)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
    spec.loader.exec_module(module);return module


def _receipt_module():
    spec = importlib.util.spec_from_file_location("run_receipts", RECEIPTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_helper_parsing_path_and_raw_resolution_edges(tmp_path):
    m=_module()
    plain=tmp_path/"plain.md";plain.write_text("body");assert m._metadata(plain)=={}
    bad=tmp_path/"bad.md";bad.write_text("---\n[bad\n---");assert m._metadata(bad)=={}
    scalar=tmp_path/"scalar.md";scalar.write_text("---\n- x\n---");assert m._metadata(scalar)=={}
    assert m._load(tmp_path/"missing.json",{"x":1})=={"x":1}
    broken=tmp_path/"bad.json";broken.write_text("{");assert m._load(broken,[])==[]
    assert m._canonical(tmp_path,"../escape") is None
    assert m._declared_raw({"raw":"raw/a.md, -raw/b.md"})==["raw/a.md","raw/b.md"]
    assert m._declared_raw({"raw":["raw/a.md",None,""]})==["raw/a.md","None"]
    assert m._declared_raw({"raw":1})==[]
    cases={
      "wiki/x.md":"declared-raw-outside-raw",
      "raw/../outside.md":"declared-raw-escapes-vault",
      "raw/demo/x.md":"declared-raw-generated-artifact",
      "raw/x.pdf":"declared-raw-binary-or-unsupported",
      "raw/missing.md":"declared-raw-missing",
    }
    for path,reason in cases.items():
        assert m._resolve_raw(tmp_path,path)==(None,reason)
    raw=tmp_path/"raw/x.txt";raw.parent.mkdir();raw.write_text("x")
    assert m._resolve_raw(tmp_path,"raw/x.txt")== (raw,None)


def test_receipt_shapes_invalid_items_and_import_disabled(tmp_path,monkeypatch):
    m=_module();state=tmp_path/"state.json"
    receipt=tmp_path/"receipt.json";receipt.write_text(json.dumps({"items":[None,{},{
      "key":"x","disposition":"rejected"},{"key":"unknown","disposition":"future"}]}))
    assert m.record_receipt(state,receipt)==4
    saved=json.loads(state.read_text())
    assert saved["completed"]=={} and len(saved["imported_receipts"])==1
    # importing the same receipt is idempotent in the receipt registry
    m.record_receipt(state,receipt)
    assert len(json.loads(state.read_text())["imported_receipts"])==1
    bad=tmp_path/"bad-receipt.json";bad.write_text(json.dumps({"items":"scalar"}))
    import pytest
    with pytest.raises(ValueError,match="must be a list"):
        m.record_receipt(state,bad)
    monkeypatch.delenv("OKENGINE_LANE_ID",raising=False)
    assert m.import_receipts(state,tmp_path)==0


def test_import_receipts_walks_multiple_new_receipts(tmp_path,monkeypatch):
    m=_module();state=tmp_path/"state.json"
    monkeypatch.setenv("OKENGINE_LANE_ID","lane")
    monkeypatch.setenv("HERMES_HOME",str(tmp_path/"home"))
    receipt_dir=tmp_path/"home/cron-plus/receipts/lane";receipt_dir.mkdir(parents=True)
    for i in range(2):
      (receipt_dir/f"{i}.json").write_text(json.dumps({"items":[{
        "key":f"k{i}","disposition":"skipped","reason":"done"}]}))
    assert m.import_receipts(state,tmp_path)==2
    assert len(json.loads(state.read_text())["imported_receipts"])==2
    assert m.import_receipts(state,tmp_path)==0


def test_plan_skips_malformed_missing_version_and_empty_raw_actions(tmp_path):
    page=tmp_path/"wiki/sources/a.md";page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 2\n---\nbody\n")
    plan=tmp_path/".okengine/model-write-repair-plan.json";plan.parent.mkdir()
    plan.write_text(json.dumps({"actions":[
      None,
      {"path":"missing.md","action":"quarantine-for-review"},
      {"path":"sources/a.md","action":"quarantine-for-review","expected_version":1},
      {"path":"sources/a.md","action":"recompile-from-declared-raw","expected_version":2},
    ]}))
    env=dict(os.environ,WIKI_PATH=str(tmp_path),HERMES_HOME=str(tmp_path/".hermes"),
             OKENGINE_LANE_ID="repair",OKENGINE_CONTRACT_DIGEST="sha256:contract")
    run=subprocess.run([sys.executable,str(SCRIPT)],env=env,text=True,capture_output=True)
    assert run.returncode==0
    assert not json.loads(run.stdout.strip().splitlines()[-1])["wakeAgent"]
    state=json.loads((tmp_path/".okengine/model-write-repair-state.json").read_text())
    assert state["input_deferrals"]["sources/a.md|recompile-from-declared-raw"]["inputs"]==[
      {"path":None,"reason":"declared-raw-empty"}]


def test_multiple_declared_raw_inputs_are_all_validated(tmp_path):
    page=tmp_path/"wiki/sources/a.md";page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 1\nraw: [raw/a.md, raw/b.md]\n---\n")
    for name in ("a","b"):
      raw=tmp_path/f"raw/{name}.md";raw.parent.mkdir(exist_ok=True);raw.write_text(name)
    plan=tmp_path/".okengine/model-write-repair-plan.json";plan.parent.mkdir()
    plan.write_text(json.dumps({"actions":[{"path":"sources/a.md",
      "action":"recompile-from-declared-raw","expected_version":1}]}))
    env=dict(os.environ,WIKI_PATH=str(tmp_path),HERMES_HOME=str(tmp_path/".hermes"),
             OKENGINE_LANE_ID="repair",OKENGINE_CONTRACT_DIGEST="sha256:contract")
    run=subprocess.run([sys.executable,str(SCRIPT)],env=env,text=True,capture_output=True)
    assert run.returncode==0
    batch=json.loads((tmp_path/".okengine/model-write-repair-batch.json").read_text())
    assert batch["actions"][0]["declared_raw"]==["raw/a.md","raw/b.md"]


def test_no_work_writes_empty_manifest(tmp_path):
    env = dict(
        os.environ,
        WIKI_PATH=str(tmp_path),
        HERMES_HOME=str(tmp_path / ".hermes"),
        OKENGINE_LANE_ID="repair",
        OKENGINE_CONTRACT_DIGEST="sha256:contract",
    )

    run = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)

    assert run.returncode == 0, run.stderr
    manifest = json.loads(
        (tmp_path / ".hermes/cron-plus/selections/model-write-repair-drain.json").read_text()
    )
    assert manifest["selected"] == []
    assert not json.loads(run.stdout.strip().splitlines()[-1])["wakeAgent"]


def test_preconditions_batch_and_receipt_checkpoint(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 2\nraw: raw/a.md\n---\nshort\n")
    raw = tmp_path / "raw/a.md"
    raw.parent.mkdir()
    raw.write_text("grounded raw input")
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
    assert batch["actions"][0]["selected_sha256"] == _sha(page)
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


def test_missing_raw_is_deferred_before_model_and_recovers_when_input_arrives(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 1\nraw: raw/missing.md\n---\nshort\n")
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps({"actions": [{
        "path": "sources/a.md", "expected_sha256": _sha(page), "expected_version": 1,
        "action": "recompile-from-declared-raw",
    }]}))
    env = dict(
        os.environ,
        WIKI_PATH=str(tmp_path),
        HERMES_HOME=str(tmp_path / ".hermes"),
        OKENGINE_LANE_ID="repair",
        OKENGINE_CONTRACT_DIGEST="sha256:contract",
        OKENGINE_SELECTION_MANIFEST=str(tmp_path / "selection.json"),
    )
    first = subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True
    )
    assert first.returncode == 0
    assert not json.loads(first.stdout.strip().splitlines()[-1])["wakeAgent"]
    state = json.loads(
        (tmp_path / ".okengine/model-write-repair-state.json").read_text()
    )
    key = "sources/a.md|recompile-from-declared-raw"
    assert state["input_deferrals"][key]["inputs"] == [{
        "path": "raw/missing.md", "reason": "declared-raw-missing",
    }]

    raw = tmp_path / "raw/missing.md"
    raw.parent.mkdir()
    raw.write_text("input arrived")
    second = subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True
    )
    assert second.returncode == 0
    assert json.loads(second.stdout.strip().splitlines()[-1])["wakeAgent"]
    state = json.loads(
        (tmp_path / ".okengine/model-write-repair-state.json").read_text()
    )
    assert key not in state["input_deferrals"]


def test_binary_and_demo_artifacts_are_never_selected_as_repair_evidence(tmp_path):
    actions = []
    for index, raw_path in enumerate(("raw/demo/walkthrough.mp4", "raw/report.pdf")):
        page = tmp_path / f"wiki/sources/{index}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"---\ntype: source\nversion: 1\nraw: {raw_path}\n---\nshort\n"
        )
        artifact = tmp_path / raw_path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"binary")
        actions.append({
            "path": f"sources/{index}.md",
            "expected_sha256": _sha(page),
            "expected_version": 1,
            "action": "recompile-from-declared-raw",
        })
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps({"actions": actions}))
    env = dict(
        os.environ,
        WIKI_PATH=str(tmp_path),
        HERMES_HOME=str(tmp_path / ".hermes"),
        OKENGINE_LANE_ID="repair",
        OKENGINE_CONTRACT_DIGEST="sha256:contract",
        OKENGINE_SELECTION_MANIFEST=str(tmp_path / "selection.json"),
    )
    run = subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True
    )
    assert run.returncode == 0
    assert not json.loads(run.stdout.strip().splitlines()[-1])["wakeAgent"]
    state = json.loads(
        (tmp_path / ".okengine/model-write-repair-state.json").read_text()
    )
    reasons = {
        entry["inputs"][0]["reason"]
        for entry in state["input_deferrals"].values()
    }
    assert reasons == {
        "declared-raw-generated-artifact",
        "declared-raw-binary-or-unsupported",
    }


def test_default_batch_selects_ten_actions(tmp_path):
    actions = []
    for index in range(12):
        page = tmp_path / f"wiki/sources/{index}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"---\ntype: source\nversion: 1\nraw: raw/{index}.md\n---\nshort\n")
        raw = tmp_path / f"raw/{index}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"grounded raw {index}")
        actions.append({"path": f"sources/{index}.md", "expected_sha256": _sha(page),
                        "expected_version": 1, "action": "recompile-from-declared-raw"})
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps({"actions": actions}))
    env = dict(os.environ, WIKI_PATH=str(tmp_path), HERMES_HOME=str(tmp_path / ".hermes"),
               OKENGINE_LANE_ID="repair", OKENGINE_CONTRACT_DIGEST="sha256:contract",
               OKENGINE_SELECTION_MANIFEST=str(tmp_path / "selection.json"))
    run = subprocess.run([sys.executable, str(SCRIPT)], env=env, text=True, capture_output=True)
    assert run.returncode == 0
    assert len(json.loads((tmp_path / "selection.json").read_text())["selected"]) == 10


def test_generated_repair_lane_is_receipt_enforced():
    jobs = json.loads((REPO / "config/engine-crons.json").read_text())
    job = next(j for j in jobs if j["name"] == "model-write-repair-drain")
    prompt = (REPO / job["prompt_file"]).read_text()
    assert job["receipt_mode"] == "enforce"
    assert job["output_contract"]["completion"] == "per-selected-item"
    assert "NEVER `/opt/vault/wiki/raw" in prompt
    assert "ONLY one fenced `okengine-receipt` JSON object" in prompt
    assert "copied from `receipt_template`" in prompt
    assert job["enabled_toolsets"][0] == "file_read"
    assert "Never create helper files" in prompt
    assert "Use `read_file`" in prompt
    assert "NEVER invent a `file://` URI" in prompt
    assert "returned verbatim by `list_resources`" in prompt
    assert job["receipt_hash_mode"] == "readback"
    assert "re-read `canonical_path`" in prompt
    assert "pass `selected_sha256` as `expected_sha256`" in prompt


def test_batch_selects_one_action_per_page(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 1\nraw: raw/a.md\n---\nshort\n")
    raw = tmp_path / "raw/a.md"
    raw.parent.mkdir()
    raw.write_text("grounded raw input")
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


def test_scheduled_run_keeps_deferred_receipt_retryable(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nversion: 1\nraw: raw/a.md\n---\nshort\n")
    raw = tmp_path / "raw/a.md"
    raw.parent.mkdir()
    raw.write_text("grounded raw input")
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
    assert json.loads(run.stdout.strip().splitlines()[-1])["wakeAgent"]
    state = json.loads((tmp_path / ".okengine/model-write-repair-state.json").read_text())
    assert item not in state["completed"]


def test_invalid_mixed_receipt_reconciles_writes_and_preserves_retryability(tmp_path):
    actions = []
    items = []
    reconciliation = []
    for index in range(10):
        page = tmp_path / f"wiki/sources/{index}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"---\ntype: source\nversion: 2\n---\nrepaired {index}\n")
        key = f"sources/{index}.md|recompile-from-declared-raw"
        actions.append({"path": f"sources/{index}.md", "expected_sha256": "sha256:before",
                        "expected_version": 1, "action": "recompile-from-declared-raw"})
        if index < 4:
            items.append({"key": key, "disposition": "accepted", "writes": [{
                "path": f"wiki/sources/{index}.md", "sha256": "sha256:before"}]})
            reconciliation.append({
                "key": key, "path": f"wiki/sources/{index}.md",
                "claimed_sha256": "sha256:before", "observed_sha256": _sha(page),
                "classification": "receipt-hash-mismatch",
            })
        elif index < 8:
            items.append({"key": key, "disposition": "accepted", "writes": [{
                "path": f"wiki/sources/{index}.md", "sha256": _sha(page)}]})
        else:
            items.append({"key": key, "disposition": "deferred", "writes": [],
                          "reason": "capacity"})
    plan = tmp_path / ".okengine/model-write-repair-plan.json"
    plan.parent.mkdir()
    plan.write_text(json.dumps({"actions": actions}))
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "valid": False, "receipt": {"items": items},
        "verified": [item["key"] for item in items[4:8]],
        "reconciliation": reconciliation,
    }))
    env = dict(os.environ, WIKI_PATH=str(tmp_path), HERMES_HOME=str(tmp_path / ".hermes"),
               OKENGINE_LANE_ID="repair", OKENGINE_CONTRACT_DIGEST="sha256:contract",
               OKENGINE_SELECTION_MANIFEST=str(tmp_path / "selection.json"))
    recorded = subprocess.run(
        [sys.executable, str(SCRIPT), "--vault", str(tmp_path), "--receipt", str(receipt)],
        env=env, text=True, capture_output=True)
    assert recorded.returncode == 0
    state = json.loads((tmp_path / ".okengine/model-write-repair-state.json").read_text())
    assert set(state["completed"]) == {
        f"sources/{index}.md|recompile-from-declared-raw" for index in range(8)}
    assert all(state["completed"][
        f"sources/{index}.md|recompile-from-declared-raw"]["reason"]
        == "post-write-reconciled" for index in range(4))
    assert not state["reconciliation"]


def test_reconciliation_distinguishes_concurrent_mutation(tmp_path):
    page = tmp_path / "wiki/sources/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("changed after validation")
    key = "sources/a.md|recompile-from-declared-raw"
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "valid": False,
        "receipt": {"items": [{"key": key, "disposition": "accepted"}]},
        "reconciliation": [{
            "key": key, "path": "wiki/sources/a.md",
            "claimed_sha256": "sha256:old", "observed_sha256": "sha256:writer-result",
            "classification": "receipt-hash-mismatch",
        }],
    }))
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--vault", str(tmp_path), "--receipt", str(receipt)],
        text=True, capture_output=True)
    assert run.returncode == 0
    state = json.loads((tmp_path / ".okengine/model-write-repair-state.json").read_text())
    assert key not in state["completed"]
    assert state["reconciliation"][key]["status"] == "concurrent-mutation"
