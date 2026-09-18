"""ci/mutation_history.py — the streak a single run cannot see (okengine#612).

Each of the seventeen nightlies that failed between 2026-08-05 and 2026-08-22 reported, correctly
and exactly once, that `okengine-mcp/write_server.py` had not run. None of them could say it had
been that way for seventeen runs, and that duration is the entire signal: "unmeasured today" reads
as bad luck, "unmeasured since the summer" reads as the write path having no mutation coverage.

The contracts worth pinning are the ones that stop this becoming another vacuous pass: no history
FAILS rather than reporting a clean sweep, a hole in the window FAILS rather than being skipped
over, and a target that dropped out of the manifest entirely still counts as unmeasured.
"""
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "ci" / "mutation_history.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="mutation_history absent")


def _mod():
    spec = importlib.util.spec_from_file_location("mutation_history", MOD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(**scores):
    """One published summary: path -> score, where None means the campaign produced nothing."""
    return {"targets": [{"path": p, "score": s} for p, s in scores.items()]}


def _manifest(tmp_path, *paths, critical=()):
    payload = {"targets": [{"path": p, "critical": p in critical} for p in paths]}
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _summaries(tmp_path, runs):
    """Write runs OLDEST first; filenames sort chronologically like `summary-<pipeline id>.json`."""
    directory = tmp_path / "summaries"
    directory.mkdir(exist_ok=True)
    for index, run in enumerate(runs):
        (directory / f"summary-{1000 + index}.json").write_text(json.dumps(run), encoding="utf-8")
    return directory


# --- the streak itself --------------------------------------------------------------------------

def test_a_target_measured_in_the_newest_run_has_no_streak():
    m = _mod()
    runs = [_run(a=91.0), _run(a=None), _run(a=None)]          # newest first: it came back
    assert m.dropout_streaks(runs, ["a"]) == {}


def test_the_streak_counts_consecutive_runs_from_the_newest():
    """The write-path case. Seventeen runs each said 'did not run'; only the streak says how long."""
    m = _mod()
    runs = [_run(w=None)] * 17 + [_run(w=93.72)]
    assert m.dropout_streaks(runs, ["w"]) == {"w": 17}


def test_a_target_absent_from_a_run_entirely_counts_as_unmeasured():
    """Worse than failing in the manifest, not better: nothing in that run mentions it at all, so
    a target quietly dropped from the target list would otherwise vanish from this report too."""
    m = _mod()
    runs = [_run(other=90.0), _run(other=90.0), _run(gone=88.0, other=90.0)]
    assert m.dropout_streaks(runs, ["gone"]) == {"gone": 2}


def test_a_score_of_zero_is_a_measurement():
    """0.0 is falsy. A target scoring zero is measured and catastrophically bad -- a very different
    thing from one that never ran, and conflating them hides whichever is worse."""
    m = _mod()
    assert m.dropout_streaks([_run(a=0.0)], ["a"]) == {}


def test_a_target_not_in_the_manifest_is_not_reported():
    """A deliberately retired target must not haunt the report forever, or the signal decays into
    a list of things nobody intends to measure."""
    m = _mod()
    assert m.dropout_streaks([_run(kept=90.0)], ["kept"]) == {}


# --- the verdict --------------------------------------------------------------------------------

def test_a_streak_at_the_threshold_fails_and_names_the_target():
    m = _mod()
    lines, code = m.report({"okengine-mcp/write_server.py": 17}, runs=20, threshold=3, expected=14)
    assert code == 1
    assert any("okengine-mcp/write_server.py" in line and "17 runs" in line for line in lines)
    assert any("cannot fail" in line for line in lines), (
        "the report must say WHY an unmeasured target matters, not just that it is unmeasured")


def test_a_streak_below_the_threshold_warns_without_failing():
    """One bad night is not a dropout. Failing on it would train everyone to ignore the report,
    which is the same way seventeen red nightlies became invisible."""
    m = _mod()
    lines, code = m.report({"a.py": 1}, runs=20, threshold=3, expected=14)
    assert code == 0
    assert any("⚠" in line and "a.py" in line for line in lines)


def test_a_clean_window_says_so_explicitly():
    m = _mod()
    lines, code = m.report({}, runs=20, threshold=3, expected=14)
    assert code == 0
    assert any("every expected target produced a score" in line for line in lines)


# --- fail closed --------------------------------------------------------------------------------

def test_no_history_fails_rather_than_reporting_a_clean_sweep(tmp_path):
    """The whole family of bugs this repo keeps hitting: an empty measurement read as a good one."""
    m = _mod()
    empty = tmp_path / "summaries"
    empty.mkdir()
    with pytest.raises(SystemExit) as exc:
        m.main(["--summaries", str(empty), "--manifest", str(_manifest(tmp_path, "a"))])
    assert "UNDETECTABLE" in str(exc.value)


def test_a_missing_summary_directory_fails(tmp_path):
    m = _mod()
    with pytest.raises(SystemExit) as exc:
        m.main(["--summaries", str(tmp_path / "nope"),
                "--manifest", str(_manifest(tmp_path, "a"))])
    assert "UNDETECTABLE" in str(exc.value)


def test_a_hole_in_the_window_fails_rather_than_being_skipped(tmp_path):
    """Silently skipping a corrupt summary would shorten the window without saying so, and a streak
    measured over a window with a hole in it is not the streak."""
    m = _mod()
    directory = _summaries(tmp_path, [_run(a=90.0)])
    (directory / "summary-9999.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        m.main(["--summaries", str(directory), "--manifest", str(_manifest(tmp_path, "a"))])
    assert "unreadable" in str(exc.value)


def test_non_object_summary_is_ignored_and_bad_manifest_fails(tmp_path):
    m = _mod()
    directory = _summaries(tmp_path, [["not", "a", "summary"], _run(a=90.0)])
    assert m.load_summaries(directory) == [_run(a=90.0)]
    malformed = tmp_path / "malformed.json"
    malformed.write_text("not json")
    with pytest.raises(SystemExit, match="expected set is UNDETECTABLE"):
        m.expected_targets(malformed, False)


def test_threshold_must_be_positive(tmp_path):
    m = _mod()
    directory = _summaries(tmp_path, [_run(a=90.0)])
    with pytest.raises(SystemExit, match="at least 1"):
        m.main(["--summaries", str(directory), "--threshold", "0"])


def test_an_empty_manifest_refuses_to_report_a_clean_sweep(tmp_path):
    m = _mod()
    directory = _summaries(tmp_path, [_run(a=90.0)])
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"targets": []}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        m.main(["--summaries", str(directory), "--manifest", str(empty)])
    assert "measured nothing" in str(exc.value)


def test_end_to_end_on_the_write_path_scenario(tmp_path, capsys):
    """The real shape: one critical target unmeasured for the whole window, others fine."""
    m = _mod()
    runs = [_run(**{"okengine-mcp/write_server.py": None, "tools/policy_plane.py": 91.0})
            for _ in range(5)]
    directory = _summaries(tmp_path, runs)
    manifest = _manifest(tmp_path, "okengine-mcp/write_server.py", "tools/policy_plane.py",
                         critical=("okengine-mcp/write_server.py", "tools/policy_plane.py"))
    code = m.main(["--summaries", str(directory), "--manifest", str(manifest),
                   "--threshold", "3", "--critical-only"])
    out = capsys.readouterr().out
    assert code == 1
    assert "okengine-mcp/write_server.py: no score in the last 5 runs" in out
    assert "tools/policy_plane.py" not in out, "a healthy target must not be listed"
