"""ci/suite_shards.py: the full suite's deterministic split and exactly-once proof (okengine#898).

Negative fixtures (testing.negative-gate) are the point of most of these: the split must reject a
file in no shard or in two, and the combine-side verify must reject missing shard data rather than
judge coverage on a partial union.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci import suite_shards as S


pytestmark = pytest.mark.invariant


def _tree(root: Path, names: list[str]) -> Path:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_x():\n    pass\n", encoding="utf-8")
    return root


FILES = ["tests/test_a.py", "tests/test_b.py", "tests/sub/test_c.py", "tests/sub/d_test.py"]


# --- discovery ------------------------------------------------------------------------------------
def test_discover_matches_pytests_default_file_patterns(tmp_path):
    _tree(tmp_path, FILES + ["tests/helper.py", "tests/conftest.py"])
    (tmp_path / "tests/__pycache__").mkdir()
    (tmp_path / "tests/__pycache__/test_cached.py").write_text("", encoding="utf-8")
    assert S.discover(tmp_path) == sorted(FILES)


def test_discover_refuses_an_empty_suite(tmp_path):
    (tmp_path / "tests").mkdir()
    with pytest.raises(S.ShardError, match="no test files"):
        S.discover(tmp_path)


def test_discover_refuses_paths_that_would_word_split(tmp_path):
    _tree(tmp_path, ["tests/test_ok x.py"])
    with pytest.raises(S.ShardError, match="whitespace"):
        S.discover(tmp_path)


# --- planning -------------------------------------------------------------------------------------
def test_plan_is_a_deterministic_partition_balanced_by_duration():
    durations = {"tests/test_a.py": 10.0, "tests/test_b.py": 6.0, "tests/sub/test_c.py": 4.0}
    first = S.plan(FILES, 2, durations)
    assert first == S.plan(list(reversed(FILES)), 2, durations), "order of discovery must not matter"
    # Heaviest first onto the lightest shard: a=10 -> 1; d (unrecorded: the median, 6) -> 2;
    # b=6 -> 2 (6 < 10); c=4 -> 1 (10 < 12). Loads 14 / 12.
    assert first == [["tests/sub/test_c.py", "tests/test_a.py"],
                     ["tests/sub/d_test.py", "tests/test_b.py"]]


def test_plan_without_any_recorded_duration_still_partitions():
    shards = S.plan(FILES, 3, {})
    assert sorted(f for shard in shards for f in shard) == sorted(FILES)


def test_plan_rejects_a_nonpositive_shard_count():
    with pytest.raises(S.ShardError, match=">= 1"):
        S.plan(FILES, 0, {})


def test_plan_rejects_more_shards_than_files():
    with pytest.raises(S.ShardError, match="shards with no files"):
        S.plan(FILES[:1], 2, {})


def test_split_check_rejects_a_file_assigned_to_no_shard():
    with pytest.raises(S.ShardError, match=r"assigned to no shard: \['tests/test_b.py'\]"):
        S.verify_partition(["tests/test_a.py", "tests/test_b.py"], [["tests/test_a.py"]])


def test_split_check_rejects_a_file_assigned_to_two_shards():
    with pytest.raises(S.ShardError, match=r"more than one shard: \{'tests/test_a.py': \[1, 2\]\}"):
        S.verify_partition(["tests/test_a.py", "tests/test_b.py"],
                           [["tests/test_a.py"], ["tests/test_a.py", "tests/test_b.py"]])


def test_split_check_rejects_a_file_outside_the_suite():
    with pytest.raises(S.ShardError, match="not part of the suite"):
        S.verify_partition(["tests/test_a.py"], [["tests/test_a.py", "tests/test_gone.py"]])


def test_load_durations_rejects_an_empty_mapping(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"files": {}}), encoding="utf-8")
    with pytest.raises(S.ShardError, match="non-empty"):
        S.load_durations(path)
    path.write_text(json.dumps({"files": {"tests/test_a.py": 2}}), encoding="utf-8")
    assert S.load_durations(path) == {"tests/test_a.py": 2.0}


def test_the_committed_plan_partitions_the_real_suite():
    """The repository's own tests/ tree and durations file, as CI's shards will see them."""
    root = Path(__file__).resolve().parents[2]
    files = S.discover(root)
    shards = S.plan(files, 3, S.load_durations(S.DEFAULT_DURATIONS))
    S.verify_partition(files, shards)
    assert sum(len(s) for s in shards) == len(files)


# --- every test file is weighed by a real duration (v0.15.11 qualification) -----------------------
def test_every_test_file_has_a_recorded_duration():
    """A test file with no entry is packed at the MEDIAN weight. 22 such files, plus entries
    recorded far below their CI time (tests/ci/test_mutation_gate_selection.py: 229s recorded,
    911s measured), put 1,343s of test time on shard 1 against 779-1,009s on the others, and
    pipeline 12868's shard 1 hit the 30m job timeout. Record a new file's duration with it
    (`python ci/suite_shards.py record-durations` from a full-suite JUnit, or a measured local
    JUnit sum until the next re-record)."""
    root = Path(__file__).resolve().parents[2]
    files = set(S.discover(root))
    durations = S.load_durations(S.DEFAULT_DURATIONS)
    missing = sorted(files - set(durations))
    assert missing == [], (
        f"test files with no recorded duration: {missing}. Add each to ci/test-durations.json "
        "(`files`, seconds from a measured run: `pytest --durations=0` locally, or the file's JUnit "
        "time in the MR's full-suite), or re-record from CI with "
        "`python ci/suite_shards.py record-durations --junit <full-suite junit> --source <job ids>`.")
    # An entry without a file is harmless (the plan weighs only discovered files) and is expected
    # in the public snapshot, which omits internal test files the private tree measures.


# --- verify: the combine job's exactly-once and no-partial-data proof ------------------------------
def _layers(nodeids: list[str]) -> str:
    return json.dumps({"total": len(nodeids), "layers": {"unit": nodeids}})


def _shards(root: Path, total: int = 2, *, collected: dict[int, list[str]] | None = None,
            files: dict[int, list[str]] | None = None) -> Path:
    """A complete, valid set of shard artifacts over FILES (one test per file)."""
    files = files or {1: FILES[:2], 2: FILES[2:]}
    collected = collected or {i: [f"{f}::test_x" for f in files[i]] for i in files}
    shard_dir = root / "shards"
    for index in range(1, total + 1):
        d = shard_dir / str(index)
        d.mkdir(parents=True)
        (d / "files.txt").write_text("\n".join(files[index]) + "\n", encoding="utf-8")
        (d / "layers.json").write_text(_layers(collected[index]), encoding="utf-8")
        (d / "junit.xml").write_text("<testsuites/>", encoding="utf-8")
        (d / "coverage.data").write_text("data", encoding="utf-8")
        (d / "manifest.json").write_text(json.dumps(
            {"index": index, "total": total, "coverage_version": "7.0"}), encoding="utf-8")
    everything = sorted(n for nodes in collected.values() for n in nodes)
    (shard_dir / "1" / "collection.json").write_text(_layers(everything), encoding="utf-8")
    return shard_dir


def test_verify_accepts_one_complete_exactly_once_execution(tmp_path):
    summary = S.verify(_shards(tmp_path), 2, sorted(FILES))
    assert summary == {"shards": 2, "files": 4, "tests": 4, "coverage_version": "7.0"}


def test_verify_fails_when_a_shards_coverage_data_is_missing(tmp_path):
    shard_dir = _shards(tmp_path)
    (shard_dir / "2" / "coverage.data").unlink()
    with pytest.raises(S.ShardError, match="shard 2: coverage.data is missing or empty"):
        S.verify(shard_dir, 2, sorted(FILES))


def test_verify_fails_when_a_shards_coverage_data_is_empty(tmp_path):
    shard_dir = _shards(tmp_path)
    (shard_dir / "1" / "coverage.data").write_text("", encoding="utf-8")
    with pytest.raises(S.ShardError, match="partial data"):
        S.verify(shard_dir, 2, sorted(FILES))


def test_verify_fails_when_a_whole_shard_is_missing(tmp_path):
    with pytest.raises(S.ShardError, match=r"expected shard directories \['1', '2', '3'\]"):
        S.verify(_shards(tmp_path), 3, sorted(FILES))


def test_verify_fails_without_any_shard_directory(tmp_path):
    with pytest.raises(S.ShardError, match="found \\[\\]"):
        S.verify(tmp_path / "absent", 1, sorted(FILES))


def test_verify_fails_on_a_manifest_from_another_shard(tmp_path):
    shard_dir = _shards(tmp_path)
    (shard_dir / "2" / "manifest.json").write_text(json.dumps(
        {"index": 1, "total": 2, "coverage_version": "7.0"}), encoding="utf-8")
    with pytest.raises(S.ShardError, match="manifest claims shard 1/2, expected 2/2"):
        S.verify(shard_dir, 2, sorted(FILES))


def test_verify_fails_on_mixed_coverage_versions(tmp_path):
    shard_dir = _shards(tmp_path)
    (shard_dir / "2" / "manifest.json").write_text(json.dumps(
        {"index": 2, "total": 2, "coverage_version": "7.1"}), encoding="utf-8")
    with pytest.raises(S.ShardError, match="different coverage.py versions"):
        S.verify(shard_dir, 2, sorted(FILES))


def test_verify_fails_when_shard_files_overlap(tmp_path):
    shard_dir = _shards(tmp_path, files={1: FILES[:3], 2: FILES[2:]},
                        collected={1: ["tests/test_a.py::t"], 2: ["tests/sub/d_test.py::t"]})
    with pytest.raises(S.ShardError, match="more than one shard"):
        S.verify(shard_dir, 2, sorted(FILES))


def test_verify_fails_without_the_full_collection(tmp_path):
    shard_dir = _shards(tmp_path)
    (shard_dir / "1" / "collection.json").unlink()
    with pytest.raises(S.ShardError, match="collection.json"):
        S.verify(shard_dir, 2, sorted(FILES))


def test_verify_fails_when_a_shard_collects_outside_its_files(tmp_path):
    collected = {1: ["tests/test_a.py::test_x", "tests/sub/test_c.py::test_x"],
                 2: ["tests/sub/d_test.py::test_x"]}
    with pytest.raises(S.ShardError, match="shard 1 collected tests outside its files"):
        S.verify(_shards(tmp_path, collected=collected), 2, sorted(FILES))


def test_verify_fails_when_shards_run_a_test_outside_the_full_collection(tmp_path):
    shard_dir = _shards(tmp_path)
    full = [f"{f}::test_x" for f in FILES[1:]]
    (shard_dir / "1" / "collection.json").write_text(_layers(sorted(full)), encoding="utf-8")
    with pytest.raises(S.ShardError, match="1 not in the full collection"):
        S.verify(shard_dir, 2, sorted(FILES))


def test_verify_fails_when_shards_miss_part_of_the_full_collection(tmp_path):
    shard_dir = _shards(tmp_path)
    full = [f"{f}::test_x" for f in FILES] + ["tests/test_a.py::test_never_ran"]
    (shard_dir / "1" / "collection.json").write_text(_layers(sorted(full)), encoding="utf-8")
    with pytest.raises(S.ShardError, match="1 never run"):
        S.verify(shard_dir, 2, sorted(FILES))


def test_verify_rejects_an_inconsistent_or_empty_layer_report(tmp_path):
    shard_dir = _shards(tmp_path)
    (shard_dir / "2" / "layers.json").write_text(
        json.dumps({"total": 5, "layers": {"unit": ["tests/sub/test_c.py::test_x"]}}),
        encoding="utf-8")
    with pytest.raises(S.ShardError, match="does not match"):
        S.verify(shard_dir, 2, sorted(FILES))
    (shard_dir / "2" / "layers.json").write_text(_layers([]), encoding="utf-8")
    with pytest.raises(S.ShardError, match="collected zero tests"):
        S.verify(shard_dir, 2, sorted(FILES))


# --- JUnit -----------------------------------------------------------------------------------------
def _junit(path: Path, tests: int, failures: int = 0, skipped: int = 0) -> Path:
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{tests}" '
        f'failures="{failures}" errors="0" skipped="{skipped}" time="1.5">'
        f'<testcase classname="tests.test_a" name="t" time="1.5"/></testsuite></testsuites>',
        encoding="utf-8")
    return path


def test_merge_junit_keeps_every_suite_and_sums_the_totals(tmp_path):
    out = tmp_path / "out" / "junit-full.xml"
    totals = S.merge_junit([_junit(tmp_path / "1.xml", 3, skipped=1),
                            _junit(tmp_path / "2.xml", 4, failures=1)], out)
    assert totals == {"tests": 7, "failures": 1, "errors": 0, "skipped": 1}
    text = out.read_text(encoding="utf-8")
    assert 'name="pytest shard 1/2"' in text and 'name="pytest shard 2/2"' in text
    assert 'tests="7"' in text and 'time="3.000"' in text


def test_merge_junit_rejects_a_report_without_suites(tmp_path):
    empty = tmp_path / "e.xml"
    empty.write_text("<testsuites/>", encoding="utf-8")
    with pytest.raises(S.ShardError, match="no <testsuite>"):
        S.merge_junit([empty], tmp_path / "o.xml")


def test_durations_from_junit_maps_dotted_classnames_to_files(tmp_path):
    report = tmp_path / "j.xml"
    report.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_a" name="t1" time="1.25"/>'
        '<testcase classname="tests.test_a.TestGroup" name="t2" time="0.75"/>'
        '<testcase classname="tests.sub.test_c" name="t3"/>'
        '<testcase classname="tests.test_removed" name="t4" time="9"/>'
        '</testsuite></testsuites>', encoding="utf-8")
    assert S.durations_from_junit(report, FILES) == {"tests/sub/test_c.py": 0.0,
                                                     "tests/test_a.py": 2.0}


# --- CLI -------------------------------------------------------------------------------------------
def _durations(tmp_path: Path) -> Path:
    path = tmp_path / "durations.json"
    path.write_text(json.dumps({"files": {"tests/test_a.py": 5.0}}), encoding="utf-8")
    return path


def test_cli_select_writes_the_shards_files(tmp_path, capsys):
    _tree(tmp_path, FILES)
    out = tmp_path / "out" / "files.txt"
    argv = ["--root", str(tmp_path), "--durations", str(_durations(tmp_path)),
            "select", "--index", "1", "--total", "2", "--out", str(out)]
    assert S.main(argv) == 0
    # Every unrecorded file weighs the median (5), so the equal weights alternate in path order.
    assert out.read_text(encoding="utf-8") == "tests/sub/d_test.py\ntests/test_a.py\n"
    assert "shard 1/2: 2 of 4 test files" in capsys.readouterr().out


def test_cli_select_rejects_an_index_outside_the_total(tmp_path, capsys):
    argv = ["--root", str(tmp_path), "select", "--index", "3", "--total", "2",
            "--out", str(tmp_path / "f")]
    assert S.main(argv) == 1
    assert "outside 1..2" in capsys.readouterr().err


def test_cli_verify_and_merge_and_manifest(tmp_path, capsys):
    _tree(tmp_path, FILES)
    shard_dir = _shards(tmp_path)
    assert S.main(["--root", str(tmp_path), "verify", "--total", "2",
                   "--shard-dir", str(shard_dir)]) == 0
    assert '"tests": 4' in capsys.readouterr().out
    merged = tmp_path / "merged.xml"
    assert S.main(["merge-junit", "--out", str(merged), str(_junit(tmp_path / "1.xml", 2))]) == 0
    assert "merged 1 JUnit reports" in capsys.readouterr().out
    manifest = tmp_path / "manifest.json"
    assert S.main(["manifest", "--index", "2", "--total", "3", "--out", str(manifest)]) == 0
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["index"] == 2 and data["total"] == 3 and data["coverage_version"]


def test_cli_verify_reports_partial_data_as_a_failure(tmp_path, capsys):
    _tree(tmp_path, FILES)
    shard_dir = _shards(tmp_path)
    (shard_dir / "1" / "junit.xml").unlink()
    assert S.main(["--root", str(tmp_path), "verify", "--total", "2",
                   "--shard-dir", str(shard_dir)]) == 1
    assert "FAIL: shard 1: junit.xml is missing" in capsys.readouterr().err


def test_cli_record_durations_rewrites_the_file(tmp_path, capsys):
    _tree(tmp_path, FILES)
    report = _junit(tmp_path / "j.xml", 1)
    target = tmp_path / "durations.json"
    assert S.main(["--root", str(tmp_path), "--durations", str(target), "record-durations",
                   "--junit", str(report), "--source", "job 1"]) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "source": "job 1", "files": {"tests/test_a.py": 1.5}}
    assert "recorded 1 file durations from job 1" in capsys.readouterr().out
