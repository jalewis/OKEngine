import importlib.util
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from okengine import __version__
from okengine import cli, compat


def test_package_version_matches_project():
    assert __version__ == "0.14.5"
    assert 'okengine-framework = "okengine.cli:framework"' in (
        ROOT / "pyproject.toml").read_text()


def test_compat_root_is_explicit_and_script_dispatch_is_bounded(tmp_path, monkeypatch):
    (tmp_path / "engine-manifest.yaml").write_text("version: test")
    script = tmp_path / "scripts/example.py"
    script.parent.mkdir()
    script.write_text("def main(argv): return len(argv)\n")
    monkeypatch.setenv("OKENGINE_SOURCE_ROOT", str(tmp_path))
    assert compat.run_script("scripts/example.py", ["one", "two"]) == 2
    script.write_text("def main(): return 7\n")
    assert compat.run_script("scripts/example.py", ["ignored"]) == 7
    with pytest.raises(RuntimeError, match="unavailable"):
        compat.run_script("scripts/missing.py", [])


def test_cron_rejects_path_traversal(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["okengine-cron", "../secret"])
    with pytest.raises(SystemExit):
        cli.cron()


def test_console_entry_points_dispatch_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_script", lambda path, args: calls.append((path, args)) or 4)
    monkeypatch.setattr(sys, "argv", ["okengine-framework", "status", "vault"])
    assert cli.framework() == 4
    monkeypatch.setattr(sys, "argv", ["okengine-cron", "kb-health", "--strict"])
    assert cli.cron() == 4
    assert calls == [
        ("scripts/framework.py", ["status", "vault"]),
        ("scripts/cron/kb_health.py", ["--strict"]),
    ]


def test_source_framework_runs_from_clean_checkout_without_pythonpath():
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts/framework.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "framework" in result.stdout.lower()


def test_compat_root_fallback_failure_and_noncommand(tmp_path, monkeypatch):
    monkeypatch.delenv("OKENGINE_SOURCE_ROOT", raising=False)
    monkeypatch.chdir(ROOT)
    assert compat.engine_root() == ROOT

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setattr(compat, "__file__", str(empty / "installed/okengine/compat.py"))
    with pytest.raises(RuntimeError, match="source tree not found"):
        compat.engine_root()

    (empty / "engine-manifest.yaml").write_text("version: test")
    script = empty / "scripts/no_main.py"
    script.parent.mkdir()
    script.write_text("VALUE = 1\n")
    monkeypatch.setenv("OKENGINE_SOURCE_ROOT", str(empty))
    assert compat.run_script("scripts/no_main.py", []) == 0


def test_runtime_import_boundary_has_no_unreviewed_dynamic_loading():
    path = ROOT / "scripts/audit/import_boundary.py"
    if not path.is_file():
        pytest.skip("internal audit tooling is not part of the public snapshot")
    spec = importlib.util.spec_from_file_location("import_boundary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    errors = module.violations()
    assert errors == [], "\n".join(errors)


def test_import_boundary_reports_injection_and_cli_status(tmp_path, monkeypatch, capsys):
    path = ROOT / "scripts/audit/import_boundary.py"
    if not path.is_file():
        pytest.skip("internal audit tooling is not part of the public snapshot")
    spec = importlib.util.spec_from_file_location("import_boundary_edges", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for base in ("src", "okengine-mcp", "okengine-reader", "okengine-cockpit",
                 "okengine-operations", "okengine-projection"):
        (tmp_path / base).mkdir(parents=True)
    offender = tmp_path / "src/bad.py"
    offender.write_text("import sys\nsys.path.insert(0, 'bad')\n", encoding="utf-8")

    scan = module.violations
    assert scan(tmp_path) == ["src/bad.py:2: dynamic import/path injection"]
    monkeypatch.setattr(module, "violations", lambda: scan(tmp_path))
    assert module.main() == 1
    assert "src/bad.py:2: dynamic import/path injection" in capsys.readouterr().out
    monkeypatch.setattr(module, "violations", lambda: [])
    assert module.main() == 0


def test_gateway_assembly_installs_the_revision_wheel_immutably():
    build = (ROOT / "scripts/build-engine-image.sh").read_text()
    assert 'scripts/build_engine_wheel.py" --out "$WORK/.okengine-build"' in build
    assert "uv pip install --no-cache-dir --no-deps /opt/hermes/.okengine-build/" in build
    assert 'engine-manifest.yaml" "$WORK/engine-manifest.yaml' in build
    assert "ENV OKENGINE_POLICY_CATALOG=/opt/hermes/config/policy/catalog.yaml" in build


def test_ci_installs_the_revision_wheel_before_every_gate():
    pipeline_path = ROOT / ".gitlab-ci.yml"
    if not pipeline_path.is_file():
        pytest.skip("private GitLab pipeline is not part of the public snapshot")
    pipeline = pipeline_path.read_text()
    assert "python scripts/build_engine_wheel.py --out artifacts/wheel" in pipeline
    assert "pip install -q --no-deps artifacts/wheel/*.whl" in pipeline


def test_coverage_runs_against_canonical_sources_after_wheel_correctness_gate():
    pipeline_path = ROOT / ".gitlab-ci.yml"
    if not pipeline_path.is_file():
        pytest.skip("private GitLab pipeline is not part of the public snapshot")
    pipeline = pipeline_path.read_text()
    assert 'PYTHONPATH="$SUITE_SOURCE_ROOT"' in pipeline
    assert 'SUITE_SOURCE_ROOT: "$CI_PROJECT_DIR/src"' in pipeline


def test_legacy_projection_facade_aliases_the_canonical_module():
    path = ROOT / "okengine-mcp/projection.py"
    name = "legacy_projection_package_boundary"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    from okengine.mcp import projection as canonical
    assert sys.modules[name] is canonical


def test_every_wheel_builder_stages_the_packaged_base_schema():
    dockerfiles = (
        "okengine-cockpit/Dockerfile", "okengine-reader/Dockerfile",
        "okengine-mcp/Dockerfile", "okengine-mcp/Dockerfile.review",
        "okengine-projection/Dockerfile", "okengine-operations/Dockerfile",
    )
    for relative in dockerfiles:
        content = (ROOT / relative).read_text()
        assert "COPY config/base-schema.yaml ./config/base-schema.yaml" in content, relative


def test_qualification_uses_projection_image_entrypoint():
    plan = json.loads((ROOT / "config/qualification-100k.json").read_text())
    projection_commands = [
        phase["command"] for phase in [*plan["commands"], *plan["overlap"]]
        if phase.get("name", "").startswith("projection")
    ]
    assert projection_commands
    for command in projection_commands:
        assert command[-3:] == ["python", "/app/service.py", "--once"]


def test_release_smoke_gives_wheel_images_repository_root_context():
    compose = yaml.safe_load((ROOT / "tests/e2e/smoke/docker-compose.smoke.yml").read_text())
    expected = {
        "reader": "okengine-reader/Dockerfile",
        "okengine-mcp": "okengine-mcp/Dockerfile",
        "cockpit": "okengine-cockpit/Dockerfile",
        "review-write": "okengine-mcp/Dockerfile.review",
    }
    expected_base = (
        "${OKENGINE_PYTHON_BASE_IMAGE:-python:3.13-slim-trixie@sha256:"
        "c33f0bc4364a6881bed1ec0cc2665e6c53c87a43e774aaeab88e6f17af105e4f}"
    )
    # okengine-mcp and okengine-cockpit also take Node from a pinned base image rather than
    # Debian's npm, so they carry exactly one more override. Kept EXACT, not loosened to a subset:
    # the equality is what catches a stray or misspelled build arg that compose would silently pass.
    expected_node = (
        "${OKENGINE_NODE_BASE_IMAGE:-node@sha256:"
        "4d676821dff059fd00d277ee4261ef34ea712317fed0737c03941481b5760c96}"
    )
    node_services = {"okengine-mcp", "cockpit"}
    for service, dockerfile in expected.items():
        build = compose["services"][service]["build"]
        assert build["context"] == "../../../"
        assert build["dockerfile"] == dockerfile
        args = {"PYTHON_BASE_IMAGE": expected_base}
        if service in node_services:
            args["NODE_BASE_IMAGE"] = expected_node
        assert build["args"] == args, f"{service}: unexpected build args"


def test_wheel_builder_is_reproducible(tmp_path):
    path = ROOT / "scripts/build_engine_wheel.py"
    spec = importlib.util.spec_from_file_location("engine_wheel", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = module.build(tmp_path / "one")
    second = module.build(tmp_path / "two")
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    assert module.main(["--out", str(tmp_path / "cli")]) == 0
