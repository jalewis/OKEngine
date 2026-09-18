"""okengine#747 thread: promoting a target to `critical` must be verified when the claim is made.

Classifying a module `critical: true` asserts its surviving mutants are triaged. Nothing checked
that at authoring time — the critical campaign is scheduled, so the first proof arrived on the
next nightly. okengine-mcp/output_contract_enforce.py was promoted on 2026-09-10 with 14
untriaged survivors, and main failed nightly from 09-11 until someone looked.

`newly_critical_targets` is the selector that makes an authoring-time check affordable: it
returns [] for almost every merge request, so the guard costs one `git show` unless someone
actually raises the bar on a module.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "ci" / "mutation_gate.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("mutation_gate_under_test", GATE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path: Path, before: dict, after: dict) -> Path:
    """A throwaway git repo whose HEAD changes the manifest relative to its first commit."""
    run = lambda *a: subprocess.run(a, cwd=tmp_path, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    (tmp_path / "mutation").mkdir()
    manifest = tmp_path / "mutation" / "targets.json"
    manifest.write_text(json.dumps(before, indent=2))
    run("git", "add", "-A"); run("git", "commit", "-qm", "base")
    run("git", "branch", "-q", "base-ref")
    manifest.write_text(json.dumps(after, indent=2))
    # --allow-empty: the unchanged-manifest cases are the POINT of this guard, and git refuses a
    # no-op commit. Without it those fixtures fail to build and the test lies about the code.
    run("git", "add", "-A"); run("git", "commit", "-q", "--allow-empty", "-m", "change")
    return tmp_path


def _m(*targets):
    return {"targets": [{"path": p, "critical": c} for p, c in targets]}


def test_an_untouched_manifest_selects_nothing(gate, tmp_path):
    """The common case, and the reason this can be automatic: no manifest change, no work."""
    m = _m(("a.py", False), ("b.py", True))
    repo = _repo(tmp_path, m, m)
    assert gate.newly_critical_targets(repo, "base-ref", m) == []


def test_a_target_flipped_to_critical_is_selected(gate, tmp_path):
    """THE REGRESSION: exactly what happened to output_contract_enforce.py."""
    before, after = _m(("a.py", False), ("b.py", True)), _m(("a.py", True), ("b.py", True))
    repo = _repo(tmp_path, before, after)
    assert gate.newly_critical_targets(repo, "base-ref", after) == ["a.py"]


def test_a_newly_added_critical_target_is_selected(gate, tmp_path):
    """Registered and classified critical in one go — the same claim, made at once."""
    before, after = _m(("b.py", True)), _m(("b.py", True), ("new.py", True))
    repo = _repo(tmp_path, before, after)
    assert gate.newly_critical_targets(repo, "base-ref", after) == ["new.py"]


def test_an_already_critical_target_is_not_reselected(gate, tmp_path):
    """NEGATIVE: editing a critical module must not drag it back through this guard. That is the
    changed-scope campaign's job, and its schedule is pinned by its own measurement."""
    before, after = _m(("b.py", True)), _m(("b.py", True), ("c.py", False))
    repo = _repo(tmp_path, before, after)
    assert gate.newly_critical_targets(repo, "base-ref", after) == []


def test_demoting_a_target_selects_nothing(gate, tmp_path):
    """NEGATIVE: lowering the bar is not a claim that needs proving here."""
    before, after = _m(("b.py", True)), _m(("b.py", False))
    repo = _repo(tmp_path, before, after)
    assert gate.newly_critical_targets(repo, "base-ref", after) == []


def test_an_unreadable_baseline_checks_everything_rather_than_nothing(gate, tmp_path):
    """A missing baseline — shallow clone, manifest just added — must not read as 'nothing
    changed'. No data is not data saying no; the guard falls back to every critical target."""
    m = _m(("a.py", True), ("b.py", False), ("c.py", True))
    repo = _repo(tmp_path, m, m)
    assert gate.newly_critical_targets(repo, "no-such-ref", m) == ["a.py", "c.py"]


def test_a_corrupt_baseline_manifest_checks_everything_rather_than_nothing(gate, tmp_path):
    """The other half of "no data is not data saying no".

    A baseline that RESOLVES but does not parse is the more dangerous shape: `git show` exits 0,
    so the missing-ref fallback above never fires, and a naive reader would see an empty target
    set and check nothing. Truncated JSON is the realistic cause -- a conflicted or partially
    written manifest on the base ref.
    """
    run = lambda *a: subprocess.run(a, cwd=tmp_path, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    (tmp_path / "mutation").mkdir()
    manifest = tmp_path / "mutation" / "targets.json"
    manifest.write_text('{"targets": [{"path": "a.py", "critic')      # truncated, parses nowhere
    run("git", "add", "-A"); run("git", "commit", "-qm", "corrupt baseline")
    run("git", "branch", "-q", "base-ref")
    after = _m(("a.py", True), ("b.py", False), ("c.py", True))
    manifest.write_text(json.dumps(after, indent=2))
    run("git", "add", "-A"); run("git", "commit", "-qm", "repair")

    assert gate.newly_critical_targets(tmp_path, "base-ref", after) == ["a.py", "c.py"]


# ── the CLI wiring, which is what CI actually invokes ────────────────────────────────────────

def _targets_file(tmp_path: Path, *targets) -> Path:
    # An empty dispositions file: the gate loads it unconditionally and reports a hard error if
    # it is absent, which would mask the selection behaviour these two tests are about.
    (tmp_path / "mutation").mkdir(exist_ok=True)
    (tmp_path / "mutation" / "survivors.json").write_text(json.dumps({"survivors": {}}))
    # The gate refuses targets whose files do not exist, and it measures HEAD in a detached
    # worktree -- so these must be committed, not merely written, or the campaign cannot see them.
    for name, _ in targets:
        (tmp_path / name).write_text("x = 1\n")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({
        "targets": [
            {"path": p, "critical": c, "category": "test", "owner": "t",
             "timeout_seconds": 5, "session_deadline_seconds": 60,
             "test_command": "{python} -c pass", "min_mutants": 1}
            for p, c in targets
        ]}, indent=2))
    subprocess.run(("git", "add", "-A"), cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(("git", "commit", "-qm", "fixture"), cwd=tmp_path, check=True, capture_output=True)
    return path


def test_no_promotion_exits_clean_before_any_session_work(gate, tmp_path, capsys):
    """The reason this gate can run on every merge request: the common case must cost one
    `git show` and stop. If it fell through to session setup it would not be affordable and
    would go back to being scheduled -- which is the hole okengine#747 is about."""
    m = _m(("a.py", False), ("b.py", True))
    repo = _repo(tmp_path, m, m)
    manifest = _targets_file(repo, ("a.py", False), ("b.py", True))

    rc = gate.main(["--mode", "changed", "--newly-critical-only",
                    "--base", "base-ref", "--manifest", str(manifest), "--repo", str(repo)])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["newly_critical"] == [], "nothing was promoted, so nothing is claimed"
    assert report["checked"] == [], "a clean exit must report no campaign, not an absent key"


def test_a_promotion_absent_from_the_selectable_set_is_an_error_not_a_silent_pass(gate, tmp_path, capsys):
    """NEGATIVE: a promoted target that the selection has filtered away must raise, not exit 0.

    The selectable set is narrowed before this check -- by `--target` here, and by diff scope in
    changed mode. If a promotion survived that narrowing unnoticed, the target list would filter
    down to nothing and the gate would report success for a claim it never checked: exactly the
    failure okengine#747 exists to prevent, wearing a green tick.

    The baseline is read from `mutation/targets.json` at the base ref while the live manifest is
    `--manifest`, so promoting a module in one without the other is what makes `ghost.py` newly
    critical here.
    """
    before = _m(("b.py", True))
    repo = _repo(tmp_path, before, before)
    manifest = _targets_file(repo, ("b.py", True), ("ghost.py", True))

    rc = gate.main(["--mode", "full", "--newly-critical-only", "--target", "b.py",
                    "--base", "base-ref", "--manifest", str(manifest), "--repo", str(repo)])
    assert rc != 0, "the gate reported success for a promotion it never checked"
    # A non-zero exit alone would pass on ANY error. Pin the reason, and name the module, so this
    # cannot go green on an unrelated failure that happens to be fatal.
    report = json.loads(capsys.readouterr().out)
    assert any("promoted to critical but absent" in e and "ghost.py" in e
               for e in report["errors"]), report["errors"]


def test_a_selectable_promotion_is_carried_into_the_campaign(gate, tmp_path, capsys):
    """The positive half: when a promotion IS selectable, the run must narrow to it and proceed.

    Without this, the two tests above are satisfied by a gate that never checks anything -- one
    asserts it exits early with no promotions, the other that it errors when a promotion is
    unreachable. Neither would notice a gate that also did nothing when the promotion was real.

    It stops at the deadline check rather than running a real cosmic-ray campaign: that check
    sits immediately downstream of the promotion filter, so reaching it proves the promoted
    target was selected and planned. A unit test must not spend minutes mutating code.
    """
    before = _m(("b.py", True))
    repo = _repo(tmp_path, before, before)
    manifest = _targets_file(repo, ("b.py", True), ("ghost.py", True))

    rc = gate.main(["--mode", "full", "--newly-critical-only",
                    "--base", "base-ref", "--manifest", str(manifest), "--repo", str(repo),
                    "--cosmic-ray", "true", "--deadline-seconds", "1",
                    "--artifacts", str(tmp_path / "artifacts")])

    report = json.loads(capsys.readouterr().out)
    assert rc != 0
    # The planned-work figure is the precise witness that the filter narrowed to ONE target:
    # each fixture target declares session_deadline_seconds=60, so the promoted module alone
    # plans 60s while an unfiltered run over both would plan 120s. Asserting only that SOME
    # deadline error occurred would pass either way.
    assert any("declares 60s" in e and "campaign deadline" in e for e in report["errors"]), (
        f"expected planning for the promoted target alone, got {report['errors']}")
