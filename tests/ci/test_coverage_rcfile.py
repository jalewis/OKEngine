"""ci/coverage_rcfile.py and the public workflow's coverage configuration (okengine#1067).

The public test job ran ``pytest --cov --cov-config=pyproject.toml``, whose ``source`` entries are
relative. Tests that copy the engine tree into a temporary directory and run it in a subprocess
were then measured against the copy, and the installed ``tools`` distribution in site-packages
matched ``tools``: 191,799 statements at 33.64%, where the checkout holds 62,676. Nothing ran that
command before publication, so these tests read the workflow itself and run its configuration step
exactly as the workflow does.
"""
import importlib.util
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from coverage.config import read_coverage_config

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
SNAPSHOT_SCRIPT = REPO / "scripts" / "publish-snapshot.sh"
HELPER = REPO / "ci" / "coverage_rcfile.py"
SPEC = importlib.util.spec_from_file_location("coverage_rcfile_under_test", HELPER)
rcfile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rcfile)

# The coverage step exactly as it stood when the public job measured 33.64% (okengine#1067).
RELATIVE_WORKFLOW = """
jobs:
  test:
    steps:
      - run: pip install -e .
      - name: pytest (authoritative correctness + coverage)
        if: matrix.python-version == '3.12'
        run: >-
          python -m pytest tests/ -q --import-mode=importlib
          --junitxml=artifacts/junit-3.12.xml --cov
          --cov-config=pyproject.toml --cov-report=term-missing
          --cov-fail-under=100
"""


def _expand(text: str, env: dict[str, str]) -> str:
    text = text.replace("${{ github.workspace }}", env["GITHUB_WORKSPACE"])
    for name, value in env.items():
        text = text.replace("${" + name + "}", value).replace("$" + name, value)
    return text


def coverage_config_problems(workflow: str, root: Path, scratch: Path) -> list[str]:
    """Why the workflow's 3.12 coverage run would measure anything but the checkout ([] = sound).

    Every step of the test job that precedes the coverage run and invokes ``ci/coverage_rcfile.py``
    is executed the way the runner executes it (a shell, ``cwd`` at the checkout,
    ``GITHUB_WORKSPACE`` and ``RUNNER_TEMP`` set). The configuration the coverage run names is
    then read with coverage.py's own reader, and every ``source`` must be an absolute path inside
    the checkout.
    """
    steps = yaml.safe_load(workflow)["jobs"]["test"]["steps"]
    measured = [index for index, step in enumerate(steps)
                if "--cov" in str(step.get("run", "")) and "3.12" in str(step.get("if", ""))]
    if len(measured) != 1:
        return [f"expected one 3.12 coverage step, found {len(measured)}"]
    env = {"GITHUB_WORKSPACE": str(root), "RUNNER_TEMP": str(scratch)}
    shell_env = {**os.environ, **env,
                 "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    shell_env = {key: value for key, value in shell_env.items() if not key.startswith("COV_CORE")}
    for step in steps[:measured[0]]:
        command = str(step.get("run", ""))
        if "ci/coverage_rcfile.py" in command:
            done = subprocess.run(["bash", "-ec", command], cwd=root, env=shell_env,
                                  capture_output=True, text=True, check=False)
            if done.returncode:
                return [f"configuration step failed: {done.stderr.strip()}"]
    arguments = shlex.split(_expand(str(steps[measured[0]]["run"]), env))
    configs = [arg.split("=", 1)[1] for arg in arguments if arg.startswith("--cov-config=")]
    if len(configs) != 1:
        return ["the coverage run names no single --cov-config"]
    config = Path(configs[0]) if Path(configs[0]).is_absolute() else root / configs[0]
    if not config.is_file():
        return [f"--cov-config {configs[0]} does not exist before the coverage run"]
    sources = read_coverage_config(str(config), warn=lambda _message: None).source or []
    if not sources:
        return [f"{configs[0]} declares no coverage source"]
    problems = []
    for source in sources:
        if not Path(source).is_absolute():
            problems.append(f"{configs[0]}: source {source!r} is relative, so a process in a "
                            "temporary copy or an installed package of that name is measured")
        elif not Path(source).resolve().is_relative_to(root.resolve()):
            problems.append(f"{configs[0]}: source {source!r} lies outside the checkout")
    return problems


def publish_excludes(script: str) -> list[str]:
    block = re.search(r"^EXCLUDE=\(\n(.*?)^\)", script, re.M | re.S)
    assert block, "publish-snapshot.sh no longer declares an EXCLUDE=( ... ) block"
    return re.findall(r'^\s*"([^"]+)"', block.group(1), re.M)


def unshipped_workflow_scripts(workflow: str, excludes: list[str], root: Path) -> list[str]:
    """Repository scripts a public workflow step runs that the public snapshot would not carry."""
    missing = []
    for job in yaml.safe_load(workflow)["jobs"].values():
        for step in job.get("steps", []):
            for script in re.findall(r"\bpython3?\s+((?:ci|scripts|tools)/[\w./-]+\.py)",
                                     str(step.get("run", ""))):
                excluded = any(script == ex or script.startswith(ex.rstrip("/") + "/")
                               for ex in excludes)
                if excluded or not (root / script).is_file():
                    missing.append(script)
    return sorted(set(missing))


# --- the public workflow -------------------------------------------------------------------------

def test_the_public_coverage_run_measures_only_the_checkout_with_absolute_sources(tmp_path):
    problems = coverage_config_problems(WORKFLOW.read_text(encoding="utf-8"), REPO, tmp_path)
    assert problems == [], (
        "the public 3.12 coverage run would measure temporary copies or installed packages; "
        f"generate its --cov-config with ci/coverage_rcfile.py: {problems}")


def test_a_relative_source_configuration_is_refused(tmp_path):
    """Negative fixture: the workflow exactly as it measured 33.64% must fail the check."""
    problems = coverage_config_problems(RELATIVE_WORKFLOW, REPO, tmp_path)
    assert problems and all("is relative" in problem for problem in problems), problems
    assert any("'tools'" in problem for problem in problems), problems


def test_a_configuration_generated_after_the_coverage_run_is_refused(tmp_path):
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["test"]["steps"]
    rc_steps = [step for step in steps if "ci/coverage_rcfile.py" in str(step.get("run", ""))]
    assert rc_steps, "the public workflow no longer generates the absolute coverage configuration"
    reordered = [step for step in steps if step not in rc_steps] + rc_steps
    problems = coverage_config_problems(yaml.safe_dump({"jobs": {"test": {"steps": reordered}}}),
                                        REPO, tmp_path)
    assert problems and "does not exist before the coverage run" in problems[0], problems


@pytest.mark.parametrize("run, expected", [
    ("python -m pytest --cov", "names no single --cov-config"),
    ("python -m pytest --cov --cov-config=absent.rc", "does not exist before the coverage run"),
])
def test_a_missing_configuration_is_refused(tmp_path, run, expected):
    workflow = yaml.safe_dump({"jobs": {"test": {"steps": [
        {"if": "matrix.python-version == '3.12'", "run": run}]}}})
    problems = coverage_config_problems(workflow, REPO, tmp_path)
    assert problems and expected in problems[0], problems


def test_a_failing_configuration_step_or_a_missing_coverage_step_is_refused(tmp_path):
    failing = yaml.safe_dump({"jobs": {"test": {"steps": [
        {"run": "python ci/coverage_rcfile.py --root /nonexistent --out x.rc"},
        {"if": "matrix.python-version == '3.12'", "run": "python -m pytest --cov"}]}}})
    assert "configuration step failed" in coverage_config_problems(failing, REPO, tmp_path)[0]
    none = yaml.safe_dump({"jobs": {"test": {"steps": [{"run": "python -m pytest"}]}}})
    assert coverage_config_problems(none, REPO, tmp_path) == [
        "expected one 3.12 coverage step, found 0"]


@pytest.mark.parametrize("rc, expected", [
    ("[run]\nbranch = true\n", "declares no coverage source"),
    ("[run]\nsource =\n    /elsewhere/src\n", "lies outside the checkout"),
])
def test_an_empty_or_foreign_source_is_refused(tmp_path, rc, expected):
    (tmp_path / "given.rc").write_text(rc, encoding="utf-8")
    workflow = yaml.safe_dump({"jobs": {"test": {"steps": [
        {"if": "matrix.python-version == '3.12'",
         "run": "python -m pytest --cov --cov-config=$RUNNER_TEMP/given.rc"}]}}})
    problems = coverage_config_problems(workflow, REPO, tmp_path)
    assert problems and expected in problems[0], problems


def test_every_script_the_public_workflow_runs_ships_in_the_public_snapshot():
    if not SNAPSHOT_SCRIPT.is_file():
        pytest.skip("publish-snapshot.sh is itself publish-excluded; the source tree always "
                    "carries it, so this check can never be skipped there")
    excludes = publish_excludes(SNAPSHOT_SCRIPT.read_text(encoding="utf-8"))
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "ci/coverage_rcfile.py" in workflow
    assert unshipped_workflow_scripts(workflow, excludes, REPO) == [], (
        "the public workflow runs a script the public snapshot drops")


def test_an_excluded_or_absent_workflow_script_is_reported():
    """Negative fixture: the internal rcfile builder is publish-excluded and cannot be used."""
    workflow = yaml.safe_dump({"jobs": {"test": {"steps": [
        {"run": "python ci/coverage_map.py rcfile --out x.rc"},
        {"run": "python scripts/absent_helper.py && python ci/coverage_rcfile.py --out y.rc"}]}}})
    assert unshipped_workflow_scripts(workflow, ["ci/coverage_map.py"], REPO) == [
        "ci/coverage_map.py", "scripts/absent_helper.py"]


# --- the helper ----------------------------------------------------------------------------------

def _pyproject(tmp_path: Path, body: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_sources_and_omits_become_absolute_and_the_report_table_is_kept(tmp_path):
    root = _pyproject(tmp_path, """
[tool.coverage.run]
branch = true
source = ["src", "tools", "/already/absolute"]
omit = ["*/tests/*", "?x", "ci/skip.py"]

[tool.coverage.report]
skip_covered = false
exclude_also = ["if __name__ == ['\\"]__main__['\\"]:"]
fail_under = 100
""")
    text = rcfile.absolute_rcfile(root)
    (tmp_path / "out.rc").write_text(text, encoding="utf-8")
    config = read_coverage_config(str(tmp_path / "out.rc"), warn=lambda _message: None)
    assert config.source == [str(root / "src"), str(root / "tools"), "/already/absolute"]
    assert config.run_omit == ["*/tests/*", "?x", str(root / "ci/skip.py")]
    assert config.branch is True and config.skip_covered is False
    assert config.fail_under == 100
    assert "if __name__ == ['\"]__main__['\"]:" in config.exclude_list


def test_a_relative_root_is_resolved_and_the_report_table_is_optional(tmp_path, monkeypatch):
    _pyproject(tmp_path, '[tool.coverage.run]\nsource = ["src"]\n')
    monkeypatch.chdir(tmp_path)
    text = rcfile.absolute_rcfile(Path("."))
    assert text == f"[run]\nsource =\n    {tmp_path.resolve() / 'src'}\nomit =\n"
    assert "[report]" not in text


def test_the_repository_configuration_round_trips(tmp_path):
    out = tmp_path / "abs.rc"
    out.write_text(rcfile.absolute_rcfile(REPO), encoding="utf-8")
    config = read_coverage_config(str(out), warn=lambda _message: None)
    assert config.source and all(Path(source).is_absolute() for source in config.source)
    assert str(REPO.resolve() / "tools") in config.source
    assert config.fail_under == 100


@pytest.mark.parametrize("body, expected", [
    (None, "no readable [tool.coverage.run] table"),
    ("[tool.coverage.run\n", "no readable [tool.coverage.run] table"),
    ("[tool.other]\n", "no readable [tool.coverage.run] table"),
    ("[tool.coverage.run]\nbranch = true\n", "declares no source"),
])
def test_an_unusable_configuration_is_an_error(tmp_path, body, expected):
    if body is not None:
        _pyproject(tmp_path, body)
    with pytest.raises(rcfile.RcfileError, match=re.escape(expected)):
        rcfile.absolute_rcfile(tmp_path)


def test_main_writes_the_rcfile_and_reports_a_failure(tmp_path, capsys):
    root = _pyproject(tmp_path, '[tool.coverage.run]\nsource = ["src"]\n')
    out = tmp_path / "nested" / "abs.rc"
    assert rcfile.main(["--root", str(root), "--out", str(out)]) == 0
    assert str(root / "src") in out.read_text(encoding="utf-8")
    assert "absolute paths" in capsys.readouterr().out
    assert rcfile.main(["--root", str(tmp_path / "missing"), "--out", str(out)]) == 1
    assert "FAIL:" in capsys.readouterr().err
