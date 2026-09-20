"""The coverage floor is declared in TWO places and they must AGREE (okengine#461).

`pyproject.toml` sets `[tool.coverage.report] fail_under`, but the CI full-suite job
also passes `--cov-fail-under=<n>` on the command line — and the command-line flag
WINS. Raising the floor in pyproject alone is therefore inert for CI: the ratchet
looks applied while the real bar stays where it was, and a regression below the
intended floor merges green.

This asserts the INVARIANT (the two numbers match) rather than pinning a literal, so
a ratchet is a two-file edit rather than three, and the guard keeps working at every
future floor instead of needing to be rewritten each time.

`.gitlab-ci.yml` is publish-EXCLUDED — it names internal infrastructure and never
ships in the public snapshot — so the CI-side assertions SKIP when the file is absent
rather than erroring there.
"""
from pathlib import Path
import re

import pytest

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".gitlab-ci.yml"
PYPROJECT = REPO / "pyproject.toml"
BRANCH_CHECK = REPO / "scripts" / "check_branch_coverage.py"
MAKEFILE = REPO / "Makefile"
DEV_REQUIREMENTS = REPO / "requirements-dev.txt"


def declared_floor() -> int:
    match = re.search(r"^fail_under\s*=\s*(\d+)",
                      PYPROJECT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "pyproject.toml must declare [tool.coverage.report] fail_under"
    return int(match.group(1))


def declared_branch_floor() -> int:
    match = re.search(r"^branch_fail_under\s*=\s*(\d+)",
                      PYPROJECT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "pyproject.toml must declare the branch coverage floor"
    return int(match.group(1))


def test_coverage_floor_and_ratchet_policy_are_documented():
    config = PYPROJECT.read_text(encoding="utf-8")
    assert declared_floor() > 0
    assert "branch = true" in config
    assert "Never lower this floor" in config
    assert declared_floor() == 100
    assert declared_branch_floor() == 100


def test_gitlab_full_suite_publishes_and_enforces_branch_aware_coverage():
    if not CI.is_file():
        pytest.skip(".gitlab-ci.yml is publish-excluded and absent from this snapshot")
    ci = CI.read_text(encoding="utf-8")
    assert "pip install -q -r requirements-dev.txt" in ci
    assert "pytest-cov" in DEV_REQUIREMENTS.read_text(encoding="utf-8")
    # UNQUOTED on purpose. SUITE_COV is a CI variable expanded as $SUITE_COV, unquoted, so the
    # shell word-splits it but does NOT strip quotes that came from a variable VALUE rather than
    # from script source. With quotes, coverage received a path containing literal " characters
    # and every coverage-floor run on main died with:
    #   ConfigError: Couldn't read '"/builds/jlew/okengine/pyproject.toml"'
    # The old assertion pinned the broken form, so the gate that should have caught this asserted
    # it instead.
    assert "--cov-config=$CI_PROJECT_DIR/pyproject.toml" in ci
    assert '--cov-config="' not in ci, (
        "a quoted --cov-config path reaches coverage with the quotes attached")
    assert "--cov-report=term" in ci
    assert "--cov-report=json:coverage.json" in ci
    assert "--cov-fail-under=" in ci
    assert "scripts/check_branch_coverage.py coverage.json" in ci
    assert "--line-min 100 --per-file" in ci
    assert "coverage: '/TOTAL.*?([0-9]{1,3}%)$/'" in ci


def test_local_coverage_target_enforces_every_file_and_category():
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert "TEST_PYTHON ?=" in makefile
    assert 'PREFLIGHT_PYTHON="$(TEST_PYTHON)" bash scripts/preflight.sh' in makefile
    assert '"$(TEST_PYTHON)" -c \'import pytest_cov\'' in makefile
    assert "OKENGINE_REQUIRE_FULL_DEPS=1" in makefile
    assert "tests/ -q --import-mode=importlib --cov" in makefile
    assert "--cov-report=json:coverage.json" in makefile
    assert "--cov-fail-under=100" in makefile
    assert "scripts/check_branch_coverage.py coverage.json" in makefile
    assert "--line-min 100 --per-file" in makefile


def test_release_and_coverage_targets_share_the_canonical_interpreter():
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert ('OKENGINE_REQUIRE_FULL_DEPS=1 $(TEST_TIMEOUT) $(TEST_FULL_DEADLINE) '
            '"$(TEST_PYTHON)" scripts/check-test-skips.py') in makefile
    assert '"$(TEST_PYTHON)" -m pytest tests/' in makefile
    assert makefile.count('PREFLIGHT_PYTHON="$(TEST_PYTHON)" bash scripts/preflight.sh') >= 3


def test_ci_flag_matches_the_declared_floor():
    """The half-applied-ratchet guard: the CLI flag wins, so a mismatch means the
    lower of the two is silently the real bar."""
    if not CI.is_file():
        pytest.skip(".gitlab-ci.yml is publish-excluded and absent from this snapshot")

    flags = re.findall(r"--cov-fail-under=(\d+)", CI.read_text(encoding="utf-8"))
    assert flags, "the CI full-suite job must pass an explicit --cov-fail-under"

    declared = declared_floor()
    for flag in flags:
        assert int(flag) == declared, (
            f"CI passes --cov-fail-under={flag} but pyproject declares "
            f"fail_under={declared}. The command-line flag WINS, so the lower of the "
            f"two is the real bar — raise both together or the ratchet is inert."
        )


def test_branch_checker_accepts_and_rejects_exact_ratchet(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_branch_coverage", BRANCH_CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = tmp_path / "coverage.json"
    report.write_text('{"totals":{"covered_branches":91,"num_branches":100}}')
    assert module.main([str(report), "--min", "91"]) == 0
    report.write_text('{"totals":{"covered_branches":90,"num_branches":100}}')
    assert module.main([str(report), "--min", "91"]) == 1
    report.write_text("{bad")
    assert module.main([str(report), "--min", "91"]) == 2

    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.okengine.coverage]\nbranch_fail_under = 91\n')
    report.write_text('{"totals":{"covered_branches":91,"num_branches":100}}')
    assert module.main([str(report), "--config", str(config)]) == 0


def test_branch_checker_handles_a_branchless_report(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_branchless_coverage", BRANCH_CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = tmp_path / "coverage.json"
    report.write_text('{"totals":{"covered_branches":0,"num_branches":0}}')
    assert module.main([str(report), "--min", "100"]) == 0


def test_coverage_checker_can_enforce_every_category_per_file(tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_all_coverage", BRANCH_CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = tmp_path / "coverage.json"
    report.write_text(
        '{"totals":{"covered_lines":2,"num_statements":2,'
        '"covered_branches":2,"num_branches":2},"files":{'
        '"perfect.py":{"summary":{"covered_lines":1,"num_statements":1,'
        '"covered_branches":0,"num_branches":0}},'
        '"gap.py":{"summary":{"covered_lines":1,"num_statements":2,'
        '"covered_branches":1,"num_branches":2}}}}'
    )
    assert module.main([str(report), "--min", "100", "--line-min", "100",
                        "--per-file"]) == 1
    output = capsys.readouterr().out
    assert "gap.py: statements 50.00%" in output
    assert "gap.py: branches 50.00%" in output
    assert "perfect.py" not in output

    report.write_text(
        '{"totals":{"covered_lines":1,"num_statements":1,'
        '"covered_branches":0,"num_branches":0},"files":{'
        '"perfect.py":{"summary":{"covered_lines":1,"num_statements":1,'
        '"covered_branches":0,"num_branches":0}}}}'
    )
    assert module.main([str(report), "--min", "100", "--line-min", "100",
                        "--per-file"]) == 0
    assert "all requested categories" in capsys.readouterr().out

    # Aggregate-only mode exercises the same two-category gate without walking files.
    assert module.main([str(report), "--min", "100", "--line-min", "100"]) == 0


def test_coverage_is_part_of_the_automatic_full_suite():
    """Coverage must gate the authoritative automatic suite, never a blocking duplicate job."""
    if not CI.is_file():
        pytest.skip(".gitlab-ci.yml is publish-excluded and absent from this snapshot")
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert "coverage-floor" not in doc
    job = doc["full-suite"]
    extends = job.get("extends") or []
    extends = [extends] if isinstance(extends, str) else list(extends)
    assert ".code-gates" in extends
    assert "--cov-fail-under=100" in job["variables"]["SUITE_COV"]
