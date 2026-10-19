"""okengine#994 [27]: staged extension script dirs the stage plan no longer stages are reconciled.

deploy-cron-scripts.sh stages enabled extensions into /opt/data/scripts/<id>/ by tar extraction,
which never removes, so a disabled or removed extension kept its importable code forever and the
validator's "staged scripts but has NO lane" WARN became permanent noise. The deploy now REPORTS
those dirs and removes them only on the explicit --prune-stale-extensions flag.
"""
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "scripts" / "staged_extensions.py"
DEPLOY = REPO / "scripts" / "deploy-cron-scripts.sh"


def _load():
    spec = importlib.util.spec_from_file_location("staged_extensions_994", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the pure comparison ---------------------------------------------------------------------

def test_a_staged_dir_the_plan_no_longer_stages_is_stale():
    se = _load()
    staged = ["okengine.timeline", "okengine.viz", "operator.example"]
    assert se.stale_dirs(staged, ["okengine.timeline", "operator.example"]) == ["okengine.viz"]


def test_every_planned_dir_is_kept():
    se = _load()
    assert se.stale_dirs(["okengine.timeline"], ["okengine.timeline"]) == []


def test_an_empty_plan_makes_every_staged_extension_dir_stale():
    se = _load()
    assert se.stale_dirs(["zz.b", "aa.a"], []) == ["aa.a", "zz.b"]


@pytest.mark.parametrize("name", ["__pycache__", ".hidden", "Upper.Case", "a", "under_score"])
def test_a_name_that_cannot_be_an_extension_id_is_never_reported(name):
    assert _load().stale_dirs([name], []) == []


def test_plan_ids_are_the_first_column_and_blank_lines_are_ignored():
    se = _load()
    plan = "okengine.timeline\t/engine/extensions/okengine.timeline\n\n  \noperator.x\t/p/x\n"
    assert se._plan_ids(plan) == ["okengine.timeline", "operator.x"]


def test_main_reads_the_listing_from_stdin_and_prints_stale_names(tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan"
    plan.write_text("okengine.timeline\t/x\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO("__pycache__\nokengine.timeline\n\nokengine.old\n"))
    assert _load().main(["--staged-listing", "-", "--plan", str(plan)]) == 0
    assert capsys.readouterr().out == "okengine.old\n"


def test_main_reads_the_listing_from_a_file(tmp_path, capsys):
    listing, plan = tmp_path / "listing", tmp_path / "plan"
    listing.write_text("okengine.gone\n")
    plan.write_text("")
    assert _load().main(["--staged-listing", str(listing), "--plan", str(plan)]) == 0
    assert capsys.readouterr().out == "okengine.gone\n"


def test_main_reads_a_listing_file_whose_name_sorts_before_the_stdin_dash(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "+listing").write_text("okengine.gone\n")
    (tmp_path / "plan").write_text("")
    monkeypatch.setattr(sys, "stdin", io.StringIO("okengine.from-stdin\n"))
    assert _load().main(["--staged-listing", "+listing", "--plan", "plan"]) == 0
    assert capsys.readouterr().out == "okengine.gone\n"


@pytest.mark.parametrize("args", [["--plan", "p"], ["--staged-listing", "-"]])
def test_main_requires_both_the_listing_and_the_plan(args, capsys):
    with pytest.raises(SystemExit) as exc:
        _load().main(args)
    assert exc.value.code == 2
    assert "the following arguments are required" in capsys.readouterr().err


def test_help_describes_the_tool_with_the_docstrings_first_line(capsys):
    with pytest.raises(SystemExit):
        _load().main(["--help"])
    assert "Find staged extension script dirs that the current stage plan" in capsys.readouterr().out


def test_the_sibling_extension_manifest_wins_over_one_earlier_on_the_path(tmp_path, monkeypatch):
    """The id grammar must be the engine's own, not a same-named module found first on sys.path."""
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "extension_manifest.py").write_text("import re\nID_RE = re.compile('.*')\n")
    monkeypatch.setattr(sys, "path", [str(decoy), *sys.path])
    monkeypatch.delitem(sys.modules, "extension_manifest", raising=False)
    se = _load()
    assert se.stale_dirs(["__pycache__"], []) == []


# --- the deploy step, run for real with `docker exec` stubbed to the local filesystem ---------

def _deploy_step(tmp_path, plan_ids, prune):
    start_marker = "# --- stale extension dirs (okengine#994 [27]) ---"
    text = DEPLOY.read_text()
    start = text.index(start_marker)
    end = text.index("\n# --- reader extension panels", start)
    scripts = tmp_path / "opt-data-scripts"
    step = text[start:end].replace("/opt/data/scripts", str(scripts))
    plan = "\n".join(f"{i}\t/src/{i}" for i in plan_ids)
    script = (
        'docker() { shift 4; "$@"; }\n'          # docker exec -u UID CONTAINER <cmd...>
        f"REPO_ROOT={REPO}\nHERMES_UID=1003\nCONTAINER=gw\nPACK_DIR=/pack\n"
        f"PRUNE_STALE_EXTENSIONS={1 if prune else 0}\nEXT_PLAN=$'{plan}'\n"
        "set -euo pipefail\n" + step)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                            env=dict(os.environ, PATH=f"{Path(sys.executable).parent}:{os.environ['PATH']}"))
    return result, scripts


def _stage(tmp_path, names):
    scripts = tmp_path / "opt-data-scripts"
    for name in names:
        (scripts / name).mkdir(parents=True)
        (scripts / name / "run.py").write_text("print('lane')\n")
    (scripts / "__pycache__").mkdir(parents=True, exist_ok=True)
    (scripts / "flat_engine_script.py").write_text("")


def test_the_deploy_reports_a_stale_dir_and_keeps_it_without_the_flag(tmp_path):
    _stage(tmp_path, ["okengine.timeline", "okengine.retired"])
    result, scripts = _deploy_step(tmp_path, ["okengine.timeline"], prune=False)
    assert result.returncode == 0, result.stderr
    assert "    - okengine.retired" in result.stderr
    assert "--prune-stale-extensions" in result.stderr
    assert (scripts / "okengine.retired" / "run.py").is_file()
    assert "okengine.timeline" not in result.stderr


def test_the_flag_removes_only_the_stale_dirs(tmp_path):
    _stage(tmp_path, ["okengine.timeline", "okengine.retired", "operator.gone"])
    result, scripts = _deploy_step(tmp_path, ["okengine.timeline"], prune=True)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in scripts.iterdir()) == [
        "__pycache__", "flat_engine_script.py", "okengine.timeline"]
    assert "removed staged extension dir no longer in the stage plan: okengine.retired" in result.stdout
    assert "removed staged extension dir no longer in the stage plan: operator.gone" in result.stdout


def test_a_reconciled_deployment_reports_nothing(tmp_path):
    _stage(tmp_path, ["okengine.timeline"])
    result, scripts = _deploy_step(tmp_path, ["okengine.timeline"], prune=True)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == "", result
    assert (scripts / "okengine.timeline").is_dir()


def test_an_unknown_deploy_argument_is_refused_before_anything_runs(tmp_path):
    result = subprocess.run(["bash", str(DEPLOY), "--prune-stale-extension"], capture_output=True,
                            text=True, timeout=60, env=dict(os.environ, CRON_PACK_DIR=str(tmp_path)))
    assert result.returncode == 2
    assert "unknown argument '--prune-stale-extension'" in result.stderr
