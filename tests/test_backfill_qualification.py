import importlib.util
import json
import os
import runpy
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "backfill_qualification", REPO / "scripts" / "backfill_qualification.py")
Q = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = Q
SPEC.loader.exec_module(Q)


def _pack(tmp_path: Path):
    pack = tmp_path / "pack"
    root = pack / ".hermes-data"
    (root / "cron-plus" / "receipts" / "lane").mkdir(parents=True)
    (root / "logs" / "cron-plus").mkdir(parents=True)
    (root / "cron-plus" / "jobs.json").write_text(json.dumps([{
        "id": "lane", "name": "entity-backfill", "enabled": True,
        "receipt_mode": "enforce",
    }, {
        "id": "free", "name": "daily-brief", "enabled": True,
    }, {
        "id": "page", "name": "page-quality-enrich", "enabled": True,
    }]))
    return pack, root


def test_inventory_and_qualification_require_qwen_valid_receipt(tmp_path):
    pack, root = _pack(tmp_path)
    (root / "logs" / "cron-plus" / "entity-backfill-20260727-020000.log").write_text(
        "Inference transport selected: api_mode=codex_responses "
        "endpoint=http://qwen/v1/responses model=qwen3-coder:30b provider=custom\n")
    (root / "cron-plus" / "receipts" / "lane" /
     "2026-07-27_02-01-00.json").write_text(json.dumps({
         "valid": True, "counts": {"selected": 2, "accepted": 1, "skipped": 1,
                                    "undisposed": 0}}))
    rows = Q.qualify(pack, datetime(2026, 7, 27, 1, tzinfo=timezone.utc))
    assert len(rows) == 2
    assert rows[0]["verdict"] == "pass"
    assert rows[0]["models"] == ["qwen3-coder:30b"]
    assert rows[1]["lane"] == "page-quality-enrich"
    assert rows[1]["verdict"] == "fail"


def test_cloud_call_invalid_receipt_and_missing_exercise_fail(tmp_path):
    pack, root = _pack(tmp_path)
    since = datetime(2026, 7, 27, 1, tzinfo=timezone.utc)
    assert Q.qualify(pack, since)[0]["errors"] == [
        "not exercised in qualification window",
        "no receipt in qualification window",
    ]
    (root / "logs" / "cron-plus" / "entity-backfill-20260727-020000.log").write_text(
        "Inference transport selected: api_mode=chat_completions "
        "endpoint=https://paid/v1/chat/completions model=deepseek-flash provider=deepseek\n")
    (root / "cron-plus" / "receipts" / "lane" /
     "2026-07-27_02-01-00.json").write_text(json.dumps({
         "valid": False, "counts": {"selected": 1, "undisposed": 1}}))
    errors = Q.qualify(pack, since)[0]["errors"]
    assert "non-Qwen or non-Responses model call observed" in errors
    assert "invalid receipt" in errors
    assert "1 selected item(s) undisposed" in errors


def test_extension_lane_log_uses_filesystem_safe_namespace_separator(tmp_path):
    pack, root = _pack(tmp_path)
    jobs = json.loads((root / "cron-plus" / "jobs.json").read_text())
    jobs.append({
        "id": "extension",
        "name": "okengine.predictions:prediction-structural-backfill",
        "enabled": True,
        "receipt_mode": "enforce",
    })
    (root / "cron-plus" / "jobs.json").write_text(json.dumps(jobs))
    (root / "cron-plus" / "receipts" / "extension").mkdir()
    (root / "logs" / "cron-plus" /
     "okengine.predictions_prediction-structural-backfill-20260727-020000.log").write_text(
        "Inference transport selected: api_mode=codex_responses "
        "endpoint=http://qwen/v1/responses model=qwen3-coder:30b provider=custom\n")
    (root / "cron-plus" / "receipts" / "extension" /
     "2026-07-27_02-01-00.json").write_text(json.dumps({
         "valid": True, "counts": {"selected": 1, "accepted": 1, "undisposed": 0}}))

    row = next(item for item in Q.qualify(
        pack, datetime(2026, 7, 27, 1, tzinfo=timezone.utc)
    ) if item["lane_id"] == "extension")
    assert row["verdict"] == "pass"
    assert row["runs"] == 1


def test_naive_cli_cutoff_is_utc_not_host_local_time():
    assert Q._dt("2026-07-29T13:45:00") == datetime(
        2026, 7, 29, 13, 45, tzinfo=timezone.utc
    )


def test_live_matrix_bypasses_gateway_container_entrypoint():
    script = (REPO / "scripts" / "run_backfill_qualification_matrix.sh").read_text()
    assert "--entrypoint python3" in script
    assert '"$IMAGE" /opt/data/plugins/cron-plus/runner.py' in script


def test_time_inventory_and_receipt_edge_shapes(tmp_path, monkeypatch):
    pack, root = _pack(tmp_path)
    assert Q._dt("2026-01-01T00:00:00Z").tzinfo == timezone.utc
    unstamped = root / "logs" / "cron-plus" / "plain.log"
    unstamped.write_text("x")
    assert Q._log_time(unstamped) is None
    assert Q._receipt_time(root / "bad.json") is None

    jobs = root / "cron-plus" / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [
        "bad",
        {"id": "disabled", "name": "x-backfill", "enabled": False},
        {"id": "agentless", "name": "x-backfill", "no_agent": True},
        {"id": "normal", "name": "daily"},
        {"id": "kept", "name": "x-backfill"},
    ]}))
    assert [x["id"] for x in Q.inventory(pack)] == ["kept"]


def test_qualification_unreadable_receipts_no_identity_and_old_files(
    tmp_path, monkeypatch
):
    pack, root = _pack(tmp_path)
    log = root / "logs" / "cron-plus" / "entity-backfill-20260727-020000.log"
    log.write_text("run without transport")
    receipt = root / "cron-plus" / "receipts" / "lane" / "2026-07-27_02-01-00.json"
    receipt.write_text("{")
    since = datetime(2026, 7, 27, 1, tzinfo=timezone.utc)
    row = Q.qualify(pack, since)[0]
    assert "no model identity observed" in row["errors"]
    assert "invalid receipt" in row["errors"]

    old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(log, (old, old))
    os.utime(receipt, (old, old))
    row = Q.qualify(pack, since)[0]
    assert row["runs"] == 0 and row["receipts"] == 0


def test_main_output_pass_fail_and_entrypoint(tmp_path, monkeypatch, capsys):
    pack, _ = _pack(tmp_path)
    monkeypatch.setattr(Q, "qualify", lambda *_a: [])
    monkeypatch.setattr(Q.review_context, "collect", lambda _root: {"sha": "a" * 40})
    monkeypatch.setattr(Q.review_context, "problems", lambda _context: ["dirty workspace"])
    output = tmp_path / "report.json"
    assert Q.main(["--pack", str(pack), "--since", "2026-01-01"]) == 2
    assert "dirty workspace" in capsys.readouterr().err
    assert Q.main(["--pack", str(pack), "--since", "2026-01-01",
                   "--output", str(output), "--allow-unattributable"]) == 0
    assert json.loads(output.read_text())["summary"]["failed"] == 0
    assert json.loads(capsys.readouterr().out)["api"] == 1

    monkeypatch.setattr(Q, "qualify", lambda *_a: [{"verdict": "fail"}])
    assert Q.main(["--pack", str(pack), "--since", "2026-01-01",
                   "--allow-unattributable"]) == 1

    monkeypatch.setattr(sys, "argv", [
        str(REPO / "scripts" / "backfill_qualification.py"),
        "--pack", str(pack), "--since", "2026-01-01", "--allow-unattributable",
    ])
    with __import__("pytest").raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts" / "backfill_qualification.py"),
                       run_name="__main__")
    assert exc.value.code == 1
