"""post_deploy_verify step [9]: its imports, its completion, and its remediation hints.

okengine#1043: on the v0.15.12 roll the deployer's ``python3`` carried a venv whose site-packages
held a stale ``tools/schema_validator.py`` and an editable ``okengine`` pointing at an old
worktree. Step [9] judged the deployment with that code: a false ``reserved-basenames crashed``
FAIL (deploy exit 3) and a silently weakened schema check. The release e2e lane verifies in a
clean interpreter and cannot see it; the decoy tests below are the earlier detector
(testing.real-invocation + testing.local-env-shadowing): they run the real verifier with decoy
engine packages planted ahead of the checkout, both on the path and pre-imported by a ``.pth``.

okengine#1042 [15]: a crashed or killed step [9] printed nothing, and nothing read as a PASS.
okengine#1042 [21]: the remediation hint named the finding's AREA (``tool search``,
``validator``), which ``framework doctor --checks`` rejects; it now names the check.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "scripts" / "post_deploy_verify.sh"
DRIVER = REPO / "scripts" / "post_deploy_selfchecks.py"

needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
FAIL = "\x1b[31mFAIL\x1b[0m"


def _driver():
    spec = importlib.util.spec_from_file_location("post_deploy_selfchecks_t", DRIVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deployment(root: Path) -> Path:
    """A deployment dir step [9] can judge: a vault with a reserved-basename page to scan, a staged
    schema_lib (as deploy-cron-scripts stages it) and no cron-plus jobs (a FAIL whose message
    carries an em dash)."""
    dep = root / "dep"
    (dep / "wiki" / "concepts").mkdir(parents=True)
    (dep / ".hermes-data" / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "cron" / "schema_lib.py", dep / ".hermes-data" / "scripts")
    (dep / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (dep / "pack.yaml").write_text("name: toy\n", encoding="utf-8")
    (dep / "schema.yaml").write_text("types: {}\n", encoding="utf-8")
    (dep / "wiki" / "concepts" / "a.md").write_text("---\ntitle: a\n---\n", encoding="utf-8")
    bindir = dep / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    return dep


def _decoy_python(root: Path) -> Path:
    """A `python3` for PATH whose interpreter carries decoy engine packages, the way a venv with a
    stale okengine does: a site dir whose ``.pth`` puts the decoys FIRST on sys.path and imports
    them at interpreter start, before the verifier's script runs."""
    decoys = root / "decoy-packages"
    (decoys / "tools").mkdir(parents=True)
    (decoys / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (decoys / "tools" / "schema_validator.py").write_text("DECOY = True\n", encoding="utf-8")
    (decoys / "okengine").mkdir()
    (decoys / "okengine" / "__init__.py").write_text("DECOY = True\n", encoding="utf-8")
    (decoys / "schema_lib.py").write_text("DECOY = True\n", encoding="utf-8")
    site_dir = root / "decoy-site"
    site_dir.mkdir()
    (site_dir / "zz-decoy.pth").write_text(
        f"import sys; sys.path.insert(0, {str(decoys)!r}); import tools.schema_validator, okengine\n",
        encoding="utf-8")
    hook = root / "decoy-hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        f"import site\nsite.addsitedir({str(site_dir)!r})\n", encoding="utf-8")
    bindir = root / "decoy-bin"
    bindir.mkdir()
    wrapper = bindir / "python3"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}{hook}" exec {sys.executable} "$@"\n',
        encoding="utf-8")
    wrapper.chmod(0o755)
    return bindir


def _verify(dep: Path, extra_path: Path | None = None, **env) -> subprocess.CompletedProcess:
    path = f"{dep / 'bin'}:" + (f"{extra_path}:" if extra_path else "") + os.environ["PATH"]
    clean = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(["bash", str(VERIFY)], cwd=dep, capture_output=True, text=True,
                          timeout=300, env={**clean, "PATH": path, **env})


def _step9(output: str) -> str:
    return output[output.index("[9] deployment self-checks"):]


# ---- okengine#1043: imports come from the checkout being deployed ------------------------------

@pytest.fixture
def decoy_world(tmp_path):
    dep = _deployment(tmp_path)
    bindir = _decoy_python(tmp_path)
    probe = subprocess.run([str(bindir / "python3"), "-c",
                            "import tools.schema_validator as v, okengine; print(v.__file__)"],
                           capture_output=True, text=True, timeout=60, check=True)
    assert "decoy-packages" in probe.stdout, "fixture: the decoy interpreter must shadow the engine"
    return dep, bindir


@needs_bash
def test_step9_judges_with_the_engine_checkout_not_a_decoy_interpreter(decoy_world):
    """The v0.15.12 symptoms, reproduced with a decoy and absent with the fix."""
    dep, bindir = decoy_world
    out = _step9(_verify(dep, bindir).stdout)
    assert "predates okengine#997" not in out, out
    assert "reserved-basenames crashed" not in out, out
    assert "staged schema_lib compose unavailable" not in out, out
    assert "source-identity crashed" not in out, out
    assert "decoy-packages" not in out, out
    assert "[crons] deployed jobs.json missing" in out, "the checks did run"


def test_a_module_still_foreign_after_the_pin_is_a_provenance_fail(tmp_path):
    """When something a check imports resolves outside the checkout anyway, say so, naming both
    paths, instead of letting it look like a stale image."""
    engine = tmp_path / "engine"
    (engine / "scripts" / "cron").mkdir(parents=True)
    shutil.copy(DRIVER, engine / "scripts" / "post_deploy_selfchecks.py")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "okengine").mkdir(parents=True)
    (elsewhere / "okengine" / "__init__.py").write_text("", encoding="utf-8")
    (engine / "scripts" / "cron" / "deployment_checks.py").write_text(
        "import sys\n"
        "CHECKS = {'pins': None}\nPOST_DEPLOY_HOST_EXCLUDED = {}\n"
        "def configure(*a, **k):\n    pass\n"
        "def host_env(dep):\n    return {}\n"
        "def run(names):\n"
        f"    sys.path.insert(0, {str(elsewhere)!r})\n"
        "    sys.modules.pop('okengine', None)\n"
        "    import okengine\n"
        "    return [('PASS', 'pins', 'fine')]\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(engine / "scripts" / "post_deploy_selfchecks.py"),
                           str(tmp_path)], capture_output=True, text=True, timeout=60, check=True)
    rows = [line.split("\t") for line in proc.stdout.splitlines()]
    provenance = [row for row in rows if row[2] == "engine-import"]
    assert len(provenance) == 1, proc.stdout
    level, check, _area, msg = provenance[0]
    assert (level, check) == ("FAIL", "-")
    assert str(elsewhere / "okengine" / "__init__.py") in msg and str(engine) in msg
    assert rows[-1] == ["END", "-", "-", "2"]


# ---- okengine#1042 [15]: a run that did not finish is a FAIL ------------------------------------

@needs_bash
def test_a_non_utf8_locale_still_reports_the_findings(tmp_path):
    """The finder's repro: PYTHONIOENCODING=ascii crashed the step on its first em dash, printed
    nothing, and the verifier reported PASS for a deployment with no cron-plus jobs."""
    dep = _deployment(tmp_path)
    result = _verify(dep, PYTHONIOENCODING="ascii", LC_ALL="C", LANG="C")
    out = _step9(result.stdout)
    assert "[crons] deployed jobs.json missing" in out, out
    assert "shared checks produced no findings" not in out
    assert result.returncode == 1


def _stub_engine_verifier(tmp_path: Path, driver_body: str) -> Path:
    """A copy of the verifier inside an engine whose step-[9] driver is `driver_body`."""
    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    shutil.copy(VERIFY, engine / "scripts" / "post_deploy_verify.sh")
    (engine / "scripts" / "post_deploy_selfchecks.py").write_text(driver_body, encoding="utf-8")
    return engine / "scripts" / "post_deploy_verify.sh"


@needs_bash
@pytest.mark.parametrize("ending,rc", [
    ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)\n", "137"),
    ("raise RuntimeError('mid-scan crash')\n", "1"),
    ("", "0"),     # exited cleanly but never reached END: truncated output is not a verdict
])
def test_a_step9_run_that_does_not_finish_fails(tmp_path, ending, rc):
    dep = _deployment(tmp_path)
    script = _stub_engine_verifier(
        tmp_path, "print('WARN\\tpins\\tpins\\tfirst finding', flush=True)\n" + ending)
    path = f"{dep / 'bin'}:{os.environ['PATH']}"
    result = subprocess.run(["bash", str(script)], cwd=dep, capture_output=True, text=True,
                            timeout=300, env={**os.environ, "PATH": path})
    out = _step9(result.stdout)
    assert f"{FAIL}  shared checks did not finish (rc={rc})" in out, out
    assert "[pins] first finding" in out, "what it found before it died is still shown"
    assert result.returncode == 1
    if ending.startswith("raise"):
        assert "mid-scan crash" in out, "the crash's own reason is shown"


@needs_bash
def test_a_step9_run_that_finishes_is_not_reported_as_unfinished(tmp_path):
    dep = _deployment(tmp_path)
    script = _stub_engine_verifier(
        tmp_path, "print('PASS\\tpins\\tpins\\tok')\nprint('END\\t-\\t-\\t1')\n")
    path = f"{dep / 'bin'}:{os.environ['PATH']}"
    result = subprocess.run(["bash", str(script)], cwd=dep, capture_output=True, text=True,
                            timeout=300, env={**os.environ, "PATH": path})
    out = _step9(result.stdout)
    assert "[pins] ok" in out and "did not finish" not in out, out


@needs_bash
def test_a_step9_driver_failure_is_a_fail_not_a_warn(tmp_path):
    """v0.15.13 audit: the driver reports its own crash as an ERR row and still ends with END, so
    the completion proof passed and ERR graded as a WARN. None of the shared checks ran, and the
    verifier exited 0: the [15] FAIL never fired for the one crash it can see. (The stub deployment
    FAILs other steps, so the exit code cannot tell the two gradings apart; the row can.)"""
    dep = _deployment(tmp_path)
    script = _stub_engine_verifier(
        tmp_path, "print('ERR\\t-\\t-\\tRuntimeError: broken checkout')\n"
                  "print('END\\t-\\t-\\t1')\n")
    path = f"{dep / 'bin'}:{os.environ['PATH']}"
    result = subprocess.run(["bash", str(script)], cwd=dep, capture_output=True, text=True,
                            timeout=300, env={**os.environ, "PATH": path})
    out = _step9(result.stdout)
    assert f"{FAIL}  shared checks did not run (RuntimeError: broken checkout)" in out, out
    assert "shared checks unavailable" not in out, "the old WARN wording is gone"


# ---- okengine#1042 [21]: the hint names a check `framework doctor` accepts ---------------------

@needs_bash
def test_every_step9_hint_names_a_registered_check(tmp_path):
    """tool-search reports under the area 'tool search'. The hint used to be
    `--checks tool search`, which argparse rejects; it must name the check."""
    dep = _deployment(tmp_path)
    (dep / ".hermes-data" / "config.yaml").write_text("tools:\n  tool_search: auto\n",
                                                      encoding="utf-8")
    out = _step9(_verify(dep).stdout)
    assert "[tool search] tools.tool_search.enabled is not" in out, out
    hints = re.findall(r"framework doctor \S+ --checks (\S+)", out)
    assert "tool-search" in hints, out
    checks = _load_checks()
    unknown = sorted(set(hints) - set(checks.CHECKS))
    assert not unknown, f"hints name checks framework doctor does not register: {unknown}"


def _load_checks():
    spec = importlib.util.spec_from_file_location(
        "deployment_checks_hint", REPO / "scripts" / "cron" / "deployment_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- the driver's pure parts, in-process (coverage of each branch) -----------------------------

def test_owned_names_cover_packages_and_script_stems_but_not_stdlib(tmp_path):
    driver = _driver()
    (tmp_path / "scripts" / "cron").mkdir(parents=True)
    for stem in ("schema_lib", "types"):
        (tmp_path / "scripts" / "cron" / f"{stem}.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts" / "framework.py").write_text("", encoding="utf-8")
    assert driver.owned_names(tmp_path) == {"okengine", "tools", "schema_lib", "framework"}


def test_module_origin_prefers_file_then_namespace_portion():
    driver = _driver()
    with_file = types.SimpleNamespace(__file__="/x/mod.py")
    namespace = types.SimpleNamespace(__file__=None, __path__=["/y/pkg", "/z/pkg"])
    assert driver.module_origin(with_file) == "/x/mod.py"
    assert driver.module_origin(namespace) == "/y/pkg"
    assert driver.module_origin(types.SimpleNamespace()) is None


def test_foreign_modules_judges_only_owned_loaded_modules(tmp_path):
    driver = _driver()
    inside = tmp_path / "engine"
    (inside / "src").mkdir(parents=True)
    sibling = tmp_path / "engine-old"     # a prefix of the root, not under it
    modules = {
        "okengine": types.SimpleNamespace(__file__=str(inside / "src" / "okengine.py")),
        "okengine.old": types.SimpleNamespace(__file__=str(sibling / "old.py")),
        "tools": None,
        "yaml": types.SimpleNamespace(__file__="/elsewhere/yaml.py"),
        "schema_lib": types.SimpleNamespace(),
    }
    found = driver.foreign_modules({"okengine", "tools", "schema_lib"}, [str(inside)], modules)
    assert found == [("okengine.old", str(sibling / "old.py"))]


def test_pin_orders_the_checkout_first_drops_foreign_modules_and_binds_tools(tmp_path):
    driver = _driver()
    engine = tmp_path / "engine"
    (engine / "tools").mkdir(parents=True)
    (engine / "scripts" / "cron").mkdir(parents=True)
    path = ["/site", str(engine / "src")]
    modules = {"okengine": types.SimpleNamespace(__file__="/site/okengine/__init__.py"),
               "keep": types.SimpleNamespace(__file__="/site/keep.py")}
    driver.pin(engine, [str(engine)], path, modules)
    assert path == [*driver.engine_paths(engine), "/site"]
    assert "okengine" not in modules and "keep" in modules
    assert list(modules["tools"].__path__) == [str(engine / "tools")]


def test_pin_leaves_tools_alone_without_a_tools_dir(tmp_path):
    driver = _driver()
    modules = {}
    driver.pin(tmp_path, [str(tmp_path)], [], modules)
    assert "tools" not in modules


def test_rows_never_carry_an_empty_field():
    """bash `read` with a tab IFS collapses consecutive tabs, shifting every later field."""
    driver = _driver()
    assert driver._row("ERR", "", "", "") == "ERR\t-\t-\t-"
    assert driver._row("WARN", "pins", "pins", "a\tb\n c") == "WARN\tpins\tpins\ta b c"


@pytest.fixture
def isolated_imports(monkeypatch):
    """main() pins the real import state; restore it so later tests see the modules they loaded."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    saved = dict(sys.modules)
    sys.modules.pop("deployment_checks", None)
    yield
    sys.modules.clear()
    sys.modules.update(saved)


def test_main_reports_a_driver_crash_and_still_ends(tmp_path, isolated_imports):
    """A failure outside any check (the import of the checks itself) is an ERR row, then END."""
    driver = _driver()
    engine = tmp_path / "engine"
    (engine / "scripts" / "cron").mkdir(parents=True)
    (engine / "scripts" / "cron" / "deployment_checks.py").write_text(
        "raise RuntimeError('broken checkout')\n", encoding="utf-8")
    rows = []
    assert driver.main(["x", str(tmp_path)], engine=engine, emit=rows.append) == 0
    assert rows[0].startswith("ERR\t-\t-\tRuntimeError: broken checkout")
    assert rows[-1] == f"END\t-\t-\t{len(rows) - 1}"


def test_main_defaults_the_deployment_to_the_working_directory(tmp_path, monkeypatch,
                                                               isolated_imports):
    driver = _driver()
    engine = tmp_path / "engine"
    (engine / "scripts" / "cron").mkdir(parents=True)
    (engine / "scripts" / "cron" / "deployment_checks.py").write_text(
        "CHECKS = {'pins': None}\nPOST_DEPLOY_HOST_EXCLUDED = {}\nSEEN = []\n"
        "def configure(dep, **k):\n    SEEN.append(dep)\n"
        "def host_env(dep):\n    return {}\n"
        "def run(names):\n    return [('PASS', 'pins', SEEN[0])]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rows = []
    driver.main(["x"], engine=engine, emit=rows.append)
    assert rows == [f"PASS\tpins\tpins\t{tmp_path}", "END\t-\t-\t1"]


def test_run_checks_skips_exclusions_and_keeps_each_findings_check():
    driver = _driver()
    calls = []

    def run(names):
        calls.append(names)
        return [("WARN", "tool search", "auto")] if names == ["tool-search"] else []

    checks = types.SimpleNamespace(
        CHECKS={"write-path": None, "tool-search": None, "pins": None},
        POST_DEPLOY_HOST_EXCLUDED={"write-path": "in the gateway"},
        configure=lambda *a, **k: None, host_env=lambda dep: {}, run=run)
    assert list(driver.run_checks(checks, "/dep")) == ["WARN\ttool-search\ttool search\tauto"]
    assert calls == [["tool-search"], ["pins"]]


def test_emit_prints_a_row(capsys):
    _driver()._emit("PASS\tpins\tpins\tok")
    assert capsys.readouterr().out == "PASS\tpins\tpins\tok\n"


def test_main_flags_a_module_a_check_loaded_from_outside_the_checkout(tmp_path, monkeypatch,
                                                                      isolated_imports):
    """In-process twin of the subprocess provenance test, with a stdout that cannot be
    reconfigured (the driver must not depend on it)."""
    import io

    driver = _driver()
    engine = tmp_path / "engine"
    (engine / "scripts" / "cron").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "id_lib.py").write_text("", encoding="utf-8")
    (engine / "scripts" / "cron" / "id_lib.py").write_text("", encoding="utf-8")
    (engine / "scripts" / "cron" / "deployment_checks.py").write_text(
        "import sys\nCHECKS = {'pins': None}\nPOST_DEPLOY_HOST_EXCLUDED = {}\n"
        "def configure(*a, **k):\n    pass\n"
        "def host_env(dep):\n    return {}\n"
        "def run(names):\n"
        f"    sys.path.insert(0, {str(elsewhere)!r})\n"
        "    sys.modules.pop('id_lib', None)\n"
        "    import id_lib\n"
        "    return []\n", encoding="utf-8")
    sys.modules.pop("id_lib", None)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    rows = []
    driver.main(["x", str(tmp_path)], engine=engine, emit=rows.append)
    assert len(rows) == 2 and rows[-1] == "END\t-\t-\t1", rows
    level, check, area, msg = rows[0].split("\t")
    assert (level, check, area) == ("FAIL", "-", "engine-import")
    assert f"id_lib was imported from {elsewhere / 'id_lib.py'}" in msg and str(engine) in msg


def test_a_module_inside_the_checkout_but_off_its_import_paths_is_foreign(tmp_path):
    """The mutation lanes put TMPDIR inside the checkout, so a decoy can sit BELOW the engine dir.
    "Under the checkout" is not provenance; under one of its import paths is."""
    driver = _driver()
    engine = tmp_path / "engine"
    roots = driver.import_roots(engine, str(tmp_path / "dep"))
    assert roots == [str(engine / "scripts" / "cron"), str(engine / "scripts"),
                     str(engine / "src"), str(engine / "tools"),
                     str(tmp_path / "dep" / ".hermes-data" / "scripts")]
    decoy = engine / "mutation-tmp" / "pytest-0" / "okengine" / "__init__.py"
    modules = {"okengine": types.SimpleNamespace(__file__=str(decoy)),
               "tools": types.SimpleNamespace(__path__=[str(engine / "tools")]),
               "schema_lib": types.SimpleNamespace(
                   __file__=str(tmp_path / "dep" / ".hermes-data" / "scripts" / "schema_lib.py"))}
    found = driver.foreign_modules({"okengine", "tools", "schema_lib"}, roots, modules)
    assert found == [("okengine", str(decoy))]


def test_main_judges_the_deployment_it_is_given_not_the_working_directory(tmp_path, monkeypatch,
                                                                          isolated_imports):
    driver = _driver()
    engine = tmp_path / "engine"
    (engine / "scripts" / "cron").mkdir(parents=True)
    (engine / "scripts" / "cron" / "deployment_checks.py").write_text(
        "CHECKS = {'pins': None}\nPOST_DEPLOY_HOST_EXCLUDED = {}\nSEEN = []\n"
        "def configure(dep, **k):\n    SEEN.append(dep)\n"
        "def host_env(dep):\n    return {}\n"
        "def run(names):\n    return [('PASS', 'pins', SEEN[-1])]\n", encoding="utf-8")
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rows = []
    driver.main(["x", str(tmp_path / "dep")], engine=engine, emit=rows.append)
    assert rows[0] == f"PASS\tpins\tpins\t{tmp_path / 'dep'}"


def test_findings_printed_before_a_kill_survive_it(tmp_path):
    """Rows are flushed as they are produced: a run killed part-way (OOM, timeout) still shows the
    verifier what it found, and the missing END says the rest is lost."""
    engine = tmp_path / "engine"
    (engine / "scripts" / "cron").mkdir(parents=True)
    shutil.copy(DRIVER, engine / "scripts" / "post_deploy_selfchecks.py")
    (engine / "scripts" / "cron" / "deployment_checks.py").write_text(
        "import os, signal\nCHECKS = {'crons': None, 'partition-dups': None}\n"
        "POST_DEPLOY_HOST_EXCLUDED = {}\n"
        "def configure(*a, **k):\n    pass\n"
        "def host_env(dep):\n    return {}\n"
        "def run(names):\n"
        "    if names == ['partition-dups']:\n"
        "        os.kill(os.getpid(), signal.SIGKILL)\n"
        "    return [('FAIL', 'crons', 'deployed jobs.json missing')]\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(engine / "scripts" / "post_deploy_selfchecks.py"),
                           str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == -9
    assert proc.stdout == "FAIL\tcrons\tcrons\tdeployed jobs.json missing\n"
