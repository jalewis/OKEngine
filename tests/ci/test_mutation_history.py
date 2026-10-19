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


# ── okengine#943: a carried-forward score is fresh only within the re-baseline window ─────────

def _carried(run_at: str, measured_at, score: float = 95.0) -> dict:
    reused = {"pipeline_id": 1, "measured_at": measured_at} if measured_at != "absent" else True
    return {"generated_at": run_at,
            "targets": [{"path": "w.py", "score": score, "reused": reused}]}


@pytest.mark.parametrize(("run_at", "measured_at", "counted"), [
    ("2026-10-08T04:00:00Z", "2026-10-01T04:00:00Z", True),     # exactly the window
    ("2026-10-08T04:00:01Z", "2026-10-01T04:00:00Z", False),    # one second past it
    ("2026-10-02T04:00:00Z", "2026-10-01T04:00:00Z", True),
    ("2026-10-01T04:00:00Z", "2026-10-02T04:00:00Z", False),    # measured after the run: bogus
    ("2026-10-02T04:00:00Z", "absent", False),                  # undatable carry
    ("2026-10-02T04:00:00Z", "not a date", False),
    ("2026-10-02T04:00:00Z", "2026-10-01T04:00:00", False),     # naive timestamp
    ("2026-10-02T04:00:00Z", 17, False),
    (None, "2026-10-01T04:00:00Z", False),                      # the run itself is undated
])
def test_a_carried_score_counts_as_measured_only_within_the_window(run_at, measured_at, counted):
    mod = _mod()
    assert (mod.measured_paths(_carried(run_at, measured_at)) == {"w.py"}) is counted


def test_a_target_carried_past_the_window_accrues_a_dropout_streak(tmp_path, capsys):
    """A lane that reused one old measurement forever would otherwise read as measured every
    night. Three runs carrying a 10-day-old score are a three-run streak."""
    mod = _mod()
    runs = tmp_path / "runs"
    runs.mkdir()
    for index, day in enumerate(("2026-10-11", "2026-10-12", "2026-10-13"), 1):
        (runs / f"summary-{index}.json").write_text(json.dumps(
            _carried(f"{day}T04:00:00Z", "2026-10-01T04:00:00Z")), encoding="utf-8")
    code = mod.main(["--summaries", str(runs), "--manifest",
                     str(_manifest(tmp_path, "w.py", critical=("w.py",))), "--critical-only"])
    assert code == 1
    assert "w.py: no score in the last 3 runs" in capsys.readouterr().out
    assert mod.main(["--summaries", str(runs), "--manifest",
                     str(_manifest(tmp_path, "w.py", critical=("w.py",))), "--critical-only",
                     "--rebaseline-days", "14"]) == 0


def test_the_watcher_window_defaults_to_seven_days_and_rejects_nonsense(tmp_path):
    mod = _mod()
    assert mod.DEFAULT_REBASELINE_DAYS == 7
    runs = tmp_path / "runs"
    runs.mkdir()
    with pytest.raises(SystemExit, match="--rebaseline-days must be at least 1"):
        mod.main(["--summaries", str(runs), "--rebaseline-days", "0"])


# ── okengine#1047: newest-first means by pipeline id, not by string ───────────────────────────

def _named(tmp_path, named_runs):
    directory = tmp_path / "summaries"
    directory.mkdir(exist_ok=True)
    for pipeline_id, run in named_runs:
        (directory / f"summary-{pipeline_id}.json").write_text(json.dumps(run), encoding="utf-8")
    return directory


def test_newest_first_orders_pipeline_ids_numerically_across_a_digit_boundary(tmp_path):
    """`summary-9947` sorts after `summary-13159` as a string. Read that way, every four-digit
    pipeline counted as newer than every five-digit one, and the watcher reported six critical
    targets as unmeasured for 16 runs -- the 16 four-digit summaries, which predate those targets --
    while the actual newest run (13159) had scored all six."""
    m = _mod()
    directory = _named(tmp_path, [(9947, _run(a=None)), (10196, _run(a=None)),
                                  (13159, _run(a=100.0))])
    runs = m.load_summaries(directory)
    assert runs == [_run(a=100.0), _run(a=None), _run(a=None)], \
        "summaries must come back newest pipeline id first"


def test_a_target_scored_in_the_newest_five_digit_run_has_no_streak(tmp_path, capsys):
    """The okengine#1047 window, reduced: 16 four-digit runs from before the target existed, then
    five-digit runs ending in one that scored it. The newest run measured it, so there is no
    streak to report."""
    m = _mod()
    old = [(7000 + i, _run(**{"tools/policy_plane.py": 90.0})) for i in range(16)]
    new = [(12608, _run(**{"src/okengine/attestation.py": 100.0, "tools/policy_plane.py": 90.0})),
           (13011, {"targets": []}),
           (13159, _run(**{"src/okengine/attestation.py": 100.0, "tools/policy_plane.py": 90.0}))]
    directory = _named(tmp_path, old + new)
    manifest = _manifest(tmp_path, "src/okengine/attestation.py", "tools/policy_plane.py",
                         critical=("src/okengine/attestation.py", "tools/policy_plane.py"))
    code = m.main(["--summaries", str(directory), "--manifest", str(manifest),
                   "--threshold", "3", "--critical-only"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "attestation.py" not in out


def test_a_real_streak_still_fails_across_a_digit_boundary(tmp_path, capsys):
    """Negative fixture: the ordering fix must not hide a target that genuinely stopped being
    measured. Scored in the four-digit runs, unscored in the three newest five-digit ones."""
    m = _mod()
    directory = _named(tmp_path, [(9998, _run(a=90.0)), (9999, _run(a=90.0)),
                                  (10000, _run(a=None)), (10001, _run(a=None)),
                                  (10002, _run(a=None))])
    code = m.main(["--summaries", str(directory), "--manifest", str(_manifest(tmp_path, "a")),
                   "--threshold", "3"])
    out = capsys.readouterr().out
    assert code == 1
    assert "a: no score in the last 3 runs" in out


def test_names_without_a_pipeline_id_still_load_in_a_stable_order(tmp_path):
    """A local archive need not use pipeline ids; such names still sort deterministically."""
    m = _mod()
    directory = tmp_path / "summaries"
    directory.mkdir()
    (directory / "local-b.json").write_text(json.dumps(_run(b=1.0)), encoding="utf-8")
    (directory / "local-a.json").write_text(json.dumps(_run(a=1.0)), encoding="utf-8")
    (directory / "summary-5.json").write_text(json.dumps(_run(c=1.0)), encoding="utf-8")
    assert m.load_summaries(directory) == [_run(c=1.0), _run(b=1.0), _run(a=1.0)]
