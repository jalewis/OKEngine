from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ci"))
SPEC = importlib.util.spec_from_file_location("mutation_aggregate", REPO / "ci/mutation_aggregate.py")
assert SPEC and SPEC.loader
aggregate_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aggregate_module)
gate = aggregate_module.gate


def manifest() -> dict:
    return {
        "floors": {"overall": 80, "critical": 90},
        "targets": [
            {"path": "a.py", "test_command": "pytest a", "critical": True,
             "min_mutants": 1},
            {"path": "b.py", "test_command": "pytest b", "critical": False,
             "min_mutants": 1},
            {"path": "c.py", "test_command": "pytest c", "critical": True,
             "min_mutants": 1},
        ],
    }


def result(path: str, critical: bool) -> dict:
    return {
        "path": path, "critical": critical, "category": "general", "owner": "test",
        "total": 10, "killed": 10, "survived": 0, "equivalent": 0,
        "out_of_scope": 0, "scoped_lines": None, "infrastructure_errors": [],
        "survivors": [], "score": 100.0, "min_mutants": None, "floor_waiver": None,
    }


def fragments(data: dict, *, critical_only: bool = False) -> list[dict]:
    targets, _ = gate.select_targets(data, "full", set())
    if critical_only:
        targets = [target for target in targets if target.get("critical")]
    identity = gate.manifest_identity(data)
    output = []
    for index in range(2):
        selected = gate.select_shard(targets, 2, index)
        output.append({
            "mode": "full", "manifest_sha256": identity,
            "shard": {"count": 2, "index": index,
                      "manifest_target_count": len(targets),
                      "selected_paths": [item["path"] for item in selected]},
            "targets": [result(item["path"], bool(item.get("critical"))) for item in selected],
            "not_measured": [], "campaign_errors": [], "elapsed_seconds": 12.5,
        })
    return output


def test_combine_proves_full_coverage_and_recomputes_scores():
    data = manifest()
    report = aggregate_module.combine(data, fragments(data))
    assert [item["path"] for item in report["targets"]] == ["a.py", "b.py", "c.py"]
    assert report["overall"]["score"] == 100.0
    assert report["critical"]["score"] == 100.0
    assert report["errors"] == []
    assert report["elapsed_seconds_sum"] == 25.0


def test_combine_proves_critical_only_coverage():
    data = manifest()
    report = aggregate_module.combine(
        data, fragments(data, critical_only=True), critical_only=True
    )
    assert [item["path"] for item in report["targets"]] == ["a.py", "c.py"]
    assert report["overall"]["score"] == 100.0
    assert report["critical"]["score"] == 100.0
    assert report["errors"] == []


def test_combine_rejects_duplicate_or_missing_shards():
    data = manifest()
    shards = fragments(data)
    with pytest.raises(ValueError, match="expected 2 mutation shards"):
        aggregate_module.combine(data, shards[:1])
    with pytest.raises(ValueError, match="duplicate mutation shard index"):
        aggregate_module.combine(data, [shards[0], shards[0]])


def test_combine_rejects_invalid_runtime_cost_mapping():
    data = manifest()
    shards = fragments(data)
    data["runtime_costs"] = {"worker_seconds": []}
    with pytest.raises(ValueError, match="worker_seconds must be a mapping"):
        aggregate_module.combine(data, shards)


def test_combine_rejects_manifest_or_selected_path_drift():
    data = manifest()
    shards = fragments(data)
    shards[0]["manifest_sha256"] = "stale"
    with pytest.raises(ValueError, match="manifest identity"):
        aggregate_module.combine(data, shards)
    shards = fragments(data)
    shards[0]["shard"]["selected_paths"].reverse()
    with pytest.raises(ValueError, match="selected paths"):
        aggregate_module.combine(data, shards)
    shards = fragments(data)
    shards[0]["shard"]["manifest_target_count"] = 999
    with pytest.raises(ValueError, match="manifest target count"):
        aggregate_module.combine(data, shards)


def test_combine_rejects_an_incomplete_fragment():
    data = manifest()
    shards = fragments(data)
    shards[0]["targets"].pop()
    with pytest.raises(ValueError, match="did not measure every selected target"):
        aggregate_module.combine(data, shards)


def test_combine_rejects_missing_disagreeing_or_invalid_shard_metadata():
    data = manifest()
    with pytest.raises(ValueError, match="no mutation shard summaries"):
        aggregate_module.combine(data, [])

    shards = fragments(data)
    shards[0]["shard"]["count"] = 3
    with pytest.raises(ValueError, match="disagree on shard count"):
        aggregate_module.combine(data, shards)

    for value in (0, "2"):
        shards = fragments(data)
        for shard in shards:
            shard["shard"]["count"] = value
        with pytest.raises(ValueError, match="shard count is invalid"):
            aggregate_module.combine(data, shards)


@pytest.mark.parametrize("index", [-1, 2, "0"])
def test_combine_rejects_invalid_shard_indices(index):
    data = manifest()
    shards = fragments(data)
    shards[0]["shard"]["index"] = index
    with pytest.raises(ValueError, match="shard index is invalid"):
        aggregate_module.combine(data, shards)


def test_combine_rejects_a_fragment_that_reports_unmeasured_targets():
    data = manifest()
    shards = fragments(data)
    shards[0]["not_measured"] = [shards[0]["targets"][0]["path"]]
    with pytest.raises(ValueError, match="reports unmeasured targets"):
        aggregate_module.combine(data, shards)


def test_combine_preserves_campaign_errors_for_the_authoritative_gate():
    data = manifest()
    shards = fragments(data)
    shards[0]["campaign_errors"] = ["worker setup failed"]
    report = aggregate_module.combine(data, shards)
    assert "worker setup failed" in report["errors"]


def write_inputs(tmp_path: Path) -> tuple[Path, list[Path]]:
    data = manifest()
    manifest_path = tmp_path / "targets.json"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    summaries = []
    for index, fragment in enumerate(fragments(data)):
        path = tmp_path / f"shard-{index}.json"
        path.write_text(json.dumps(fragment), encoding="utf-8")
        summaries.append(path)
    return manifest_path, summaries


def test_main_writes_successful_summary_and_junit(tmp_path, capsys):
    manifest_path, summaries = write_inputs(tmp_path)
    artifacts = tmp_path / "artifacts"
    argv = [str(path) for path in summaries]
    argv += ["--manifest", str(manifest_path), "--artifacts", str(artifacts)]
    assert aggregate_module.main(argv) == 0
    report = json.loads((artifacts / "summary.json").read_text(encoding="utf-8"))
    assert report["overall"]["score"] == 100.0
    assert (artifacts / "junit.xml").is_file()
    assert '"errors": []' in capsys.readouterr().out


def test_main_accepts_critical_only_shards(tmp_path, capsys):
    data = manifest()
    manifest_path = tmp_path / "targets.json"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    summaries = []
    for index, fragment in enumerate(fragments(data, critical_only=True)):
        path = tmp_path / f"critical-shard-{index}.json"
        path.write_text(json.dumps(fragment), encoding="utf-8")
        summaries.append(path)
    artifacts = tmp_path / "artifacts"
    argv = ["--critical-only", *(str(path) for path in summaries)]
    argv += ["--manifest", str(manifest_path), "--artifacts", str(artifacts)]
    assert aggregate_module.main(argv) == 0
    report = json.loads((artifacts / "summary.json").read_text(encoding="utf-8"))
    assert [item["path"] for item in report["targets"]] == ["a.py", "c.py"]
    assert '"errors": []' in capsys.readouterr().out


def test_main_publishes_a_failed_report_when_an_input_is_invalid(tmp_path, capsys):
    manifest_path, summaries = write_inputs(tmp_path)
    summaries[0].write_text("not json", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    argv = [str(path) for path in summaries]
    argv += ["--manifest", str(manifest_path), "--artifacts", str(artifacts)]
    assert aggregate_module.main(argv) == 1
    report = json.loads((artifacts / "summary.json").read_text(encoding="utf-8"))
    assert report["errors"] and "cannot load" in report["errors"][0]
    assert (artifacts / "junit.xml").is_file()
    assert "cannot load" in capsys.readouterr().out
