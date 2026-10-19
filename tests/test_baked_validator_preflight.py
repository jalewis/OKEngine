"""okengine#997/#1017/#1016 roll safety: deploy-cron-scripts.sh refuses to stage onto a gateway whose
baked engine (the okengine wheel and /opt/hermes/tools) cannot satisfy an import the staged scripts
make. The probe is derived from the scripts themselves (scripts/lib/baked_imports.py): v0.15.11's
hand-listed probe forgot `okengine.actor_types`, so a stage onto a v0.15.10 image passed the gate
and every importer, index and audit cron then raised ImportError."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "baked_validator_preflight.sh"
SCAN = REPO / "scripts" / "lib" / "baked_imports.py"
DEPLOY = REPO / "scripts" / "deploy-cron-scripts.sh"

spec = importlib.util.spec_from_file_location("baked_imports", SCAN)
baked_imports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(baked_imports)


def _run(docker_stub: str, *paths: Path):
    args = " ".join(f'"{p}"' for p in paths)
    script = (f'{docker_stub}\n. "{LIB}"\n'
              f'require_baked_validator gw-1 1003 {args}; echo "rc=$?"\n')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False,
                          env={"PATH": "/usr/bin:/bin", "PYTHON": sys.executable})


def _baked_tree(root: Path, *, reserved: bool = True, modules=("file_fingerprint",)) -> Path:
    """A stand-in /opt/hermes: the validator in the tree, the okengine wheel in the venv's path."""
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "__init__.py").write_text("")
    (root / "tools" / "schema_validator.py").write_text(
        "def is_reserved_basename(name):\n    return False\n" if reserved else "X = 1\n")
    pkg = root / "site" / "okengine"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for name in modules:
        (pkg / f"{name}.py").write_text("def fingerprint(path):\n    return ()\n")
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text(f'#!/bin/sh\nPYTHONPATH="{root}:{root}/site" exec "{sys.executable}" -s "$@"\n')
    python.chmod(0o755)
    return root


def _against(tree: Path) -> str:
    """docker exec, simulated: run the probe's own sh -c string with /opt/hermes at the fixture
    (stdin, which carries the probe, passes through)."""
    return f'docker() {{ local cmd="${{@: -1}}"; bash -c "${{cmd//\\/opt\\/hermes/{tree}}}"; }}'


def _staged(tmp_path: Path, source: str) -> Path:
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "lane.py").write_text(source)
    return staged


@pytest.mark.parametrize(("reserved", "modules", "rc"), [
    (True, ("file_fingerprint", "actor_types"), 0),   # current image
    (True, ("file_fingerprint",), 1),                  # v0.15.10 image: no okengine.actor_types
    (True, ("actor_types",), 1),                       # v0.15.6-v0.15.9: no file_fingerprint (#1017)
    (False, ("file_fingerprint", "actor_types"), 1),   # pre-#997 validator
])
def test_the_probe_refuses_a_baked_engine_missing_any_import_the_staged_scripts_make(
        tmp_path, reserved, modules, rc):
    staged = _staged(tmp_path, "from okengine import actor_types\n"
                               "from okengine.file_fingerprint import fingerprint\n")
    tree = _baked_tree(tmp_path / "hermes", reserved=reserved, modules=modules)
    out = _run(_against(tree), staged)
    assert f"rc={rc}" in out.stdout, out.stdout + out.stderr
    if rc:
        assert "Nothing was staged" in out.stderr and "Rebuild the engine image" in out.stderr


def test_the_refusal_names_every_import_the_image_cannot_satisfy(tmp_path):
    staged = _staged(tmp_path, "from okengine import actor_types\nimport okengine.brand_new\n")
    out = _run(_against(_baked_tree(tmp_path / "hermes")), staged)
    assert "rc=1" in out.stdout
    assert "from okengine import actor_types (ImportError)" in out.stderr
    assert "import okengine.brand_new (ModuleNotFoundError)" in out.stderr


def test_a_new_baked_module_needs_no_probe_edit(tmp_path):
    """The point of #1016: a script importing a module the probe never heard of is still probed."""
    staged = _staged(tmp_path, "from okengine.brand_new import thing\n")
    out = _run(_against(_baked_tree(tmp_path / "hermes")), staged)
    assert "rc=1" in out.stdout and "from okengine.brand_new import thing" in out.stderr


def test_a_gateway_where_the_probe_cannot_run_refuses(tmp_path):
    out = _run("docker() { return 1; }", _staged(tmp_path, "import okengine\n"))
    assert "rc=1" in out.stdout and "could not run in the gateway" in out.stderr


def test_an_underivable_probe_refuses(tmp_path):
    out = subprocess.run(["bash", "-c", f'. "{LIB}"\nrequire_baked_validator gw-1 1003 x; '
                          'echo "rc=$?"'], capture_output=True, text=True, check=False,
                         env={"PATH": "/usr/bin:/bin", "PYTHON": "false"})
    assert "rc=1" in out.stdout and "could not derive" in out.stderr


def test_without_paths_it_probes_the_engines_own_staged_sources(tmp_path):
    """The real tree: the derived probe covers v0.15.11's actor_types and #1017's fingerprint."""
    seen = tmp_path / "probe.txt"
    stub = f'docker() {{ cat > "{seen}"; return 0; }}'
    out = subprocess.run(["bash", "-c", f'{stub}\n. "{LIB}"\nrequire_baked_validator gw-1 1003; '
                          'echo "rc=$?"'], capture_output=True, text=True, check=False,
                         env={"PATH": "/usr/bin:/bin", "PYTHON": sys.executable})
    assert "rc=0" in out.stdout, out.stderr
    probe = seen.read_text().splitlines()
    for statement in ("from okengine import actor_types",
                      "from okengine.file_fingerprint import fingerprint",
                      "from tools.schema_validator import is_reserved_basename"):
        assert statement in probe, probe


def test_the_stage_script_probes_everything_it_stages_before_its_first_write():
    text = DEPLOY.read_text(encoding="utf-8")
    call = text.index('require_baked_validator "$CONTAINER" "$HERMES_UID" "$SRC_DIR"')
    call_text = text[call:text.index("|| exit 1", call)]
    for staged in ('"$SRC_DIR"', "RUNTIME_COMPOSE_HELPERS", '"$REPO_ROOT/extensions"',
                   '"$PACK_SCRIPTS"', '"$PACK_CONNECTORS"', '"${EXT_DIRS[@]}"'):
        assert staged in call_text, staged
    first_write = text.index('docker exec -u "$HERMES_UID" "$CONTAINER" mkdir -p /opt/data/scripts')
    assert call < first_write
    assert '. "$REPO_ROOT/scripts/lib/baked_validator_preflight.sh"' in text


# --- the scan --------------------------------------------------------------------------------


def test_the_scan_reads_baked_imports_exactly_as_written():
    source = ("import os\nimport okengine.alpha\nimport tools.schema_validator as sv\n"
              "from okengine import beta, gamma\nfrom okengine.delta import thing\n"
              "from tools import schema_validator\nfrom other import okengine\n"
              "from . import okengine_local\nfrom okengine.star import *\n"
              "def lazy():\n    from okengine import deferred\n")
    assert baked_imports.statements(source) == {
        "import okengine.alpha", "import tools.schema_validator", "from okengine import beta",
        "from okengine import gamma", "from okengine.delta import thing",
        "from tools import schema_validator", "from okengine import deferred"}


@pytest.mark.parametrize("handler", ["ImportError", "ModuleNotFoundError", "Exception",
                                     "BaseException", "(ValueError, ImportError)", ""])
def test_an_import_the_script_tolerates_losing_is_optional(handler):
    source = (f"try:\n    from okengine.optional import x\nexcept {handler}:\n    x = None\n"
              "from okengine import required\n")
    assert baked_imports.statements(source) == {"from okengine import required"}


def test_a_try_that_does_not_tolerate_import_failure_still_requires_it():
    source = "try:\n    from okengine import needed\nexcept (KeyError, ValueError):\n    pass\n"
    assert baked_imports.statements(source) == {"from okengine import needed"}


def test_unparseable_sources_and_missing_paths_contribute_nothing(tmp_path):
    (tmp_path / "broken.py").write_text("def (:\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.py").write_text("import okengine.cached\n")
    (tmp_path / "ok.py").write_text("import okengine.real\n")
    (tmp_path / "notes.txt").write_text("import okengine.text\n")
    assert baked_imports.scan([tmp_path, tmp_path / "absent.py"]) == ["import okengine.real"]
    assert baked_imports.scan([tmp_path / "ok.py"]) == ["import okengine.real"]


def test_a_relative_import_is_never_a_baked_one():
    assert baked_imports.statements("from .okengine import local\nfrom ..tools import x\n") == set()


def test_an_optional_import_met_first_does_not_end_the_scan():
    source = ("try:\n    from okengine.optional import x\nexcept ImportError:\n    x = None\n"
              "def lane():\n    from okengine import required\n")
    assert baked_imports.statements(source) == {"from okengine import required"}



def test_the_enabled_extensions_stage_plan_feeds_the_probe_before_staging():
    """!1305 QA N2: pack- and operator-tier extensions are staged from the composer's plan, so
    the probe reads the same plan before anything is written."""
    text = DEPLOY.read_text(encoding="utf-8")
    plan = text.index('EXT_PLAN="$(python3 "$REPO_ROOT/scripts/framework.py" extensions stage-plan')
    dirs = text.index('EXT_DIRS+=("$ext_dir")')
    call = text.index('require_baked_validator "$CONTAINER" "$HERMES_UID"')
    staging = text.index('done <<< "$EXT_PLAN"\n    echo "  $estaged enabled extension(s) staged"')
    assert plan < dirs < call < staging
    assert text.count("extensions stage-plan") == 1
