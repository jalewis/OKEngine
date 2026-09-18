import importlib.util
import json
from pathlib import Path

import yaml
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "framework_deployment", ROOT / "scripts/framework_deployment.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _pack(tmp_path, offset=20):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pack.yaml").write_text(yaml.safe_dump({
        "name": tmp_path.name, "description": "Fixture", "port_offset": offset}))
    return tmp_path


def test_render_is_deterministic_versioned_and_has_required_services(tmp_path):
    pack = _pack(tmp_path)
    first = MODULE.render(pack)
    assert first == MODULE.render(pack)
    value = yaml.safe_load(first)
    assert value["x-okengine-compose-contract"]["version"] == 1
    assert MODULE.REQUIRED_SERVICES <= set(value["services"])
    assert f"{tmp_path.name}-gateway" == value["services"]["gateway"]["container_name"]
    assert "9220:9200" in value["services"]["okengine-reader"]["ports"][0]


def test_small_override_preserves_env_references_and_rejects_literals(tmp_path):
    pack = _pack(tmp_path)
    (pack / "deployment.compose.yaml").write_text(yaml.safe_dump({
        "api": 1, "services": {"gateway": {
            "environment": ["TOKEN=${TOKEN:-}"]}}}))
    rendered = yaml.safe_load(MODULE.render(pack))
    assert rendered["services"]["gateway"]["environment"] == ["TOKEN=${TOKEN:-}"]
    (pack / "deployment.compose.yaml").write_text(
        "api: 1\nservices:\n  gateway:\n    environment:\n      password: exposed\n")
    try:
        MODULE.render(pack)
    except MODULE.DeploymentError as exc:
        assert "literal secrets" in str(exc)
    else:
        raise AssertionError("literal secret was accepted")


def test_diff_and_render_do_not_touch_operator_env(tmp_path, capsys):
    pack = _pack(tmp_path)
    (pack / ".env").write_text("SECRET=owned-by-operator\n")
    assert MODULE.main(["diff", str(pack)]) == 1
    assert "engine-rendered" in capsys.readouterr().out
    assert MODULE.main(["render", str(pack)]) == 0
    assert MODULE.main(["diff", str(pack)]) == 0
    assert (pack / ".env").read_text() == "SECRET=owned-by-operator\n"


def test_migration_extracts_allowed_differences_without_overwriting_compose(tmp_path):
    pack = _pack(tmp_path)
    current = yaml.safe_load(MODULE.render(pack))
    current["services"]["gateway"]["image"] = "registry.example/gateway@sha256:" + "a" * 64
    original = yaml.safe_dump(current, sort_keys=False)
    (pack / "docker-compose.yml").write_text(original)
    assert MODULE.main(["migrate", str(pack)]) == 0
    assert (pack / "docker-compose.yml").read_text() == original
    override = yaml.safe_load((pack / "deployment.compose.yaml").read_text())
    assert override == {"api": 1, "services": {"gateway": {"image": current["services"]["gateway"]["image"]}}}


def test_migration_discards_legacy_engine_owned_build_context(tmp_path):
    pack = _pack(tmp_path)
    current = yaml.safe_load(MODULE.render(pack))
    current["services"]["okengine-reader"]["build"] = (
        "${ENGINE_DIR:-../engine}/okengine-reader")
    current["services"]["okengine-cockpit"]["build"] = (
        "${ENGINE_DIR:-../engine}/okengine-cockpit")
    (pack / "docker-compose.yml").write_text(yaml.safe_dump(current, sort_keys=False))
    assert MODULE.main(["migrate", str(pack)]) == 0
    override = yaml.safe_load((pack / "deployment.compose.yaml").read_text())
    assert override == {"api": 1, "services": {}}
    assert MODULE.main(["render", str(pack)]) == 0
    rendered = yaml.safe_load((pack / "docker-compose.yml").read_text())
    for service, dockerfile in {
        "okengine-reader": "okengine-reader/Dockerfile",
        "okengine-cockpit": "okengine-cockpit/Dockerfile",
    }.items():
        assert rendered["services"][service]["build"] == {
            "context": "${ENGINE_DIR:-../engine}", "dockerfile": dockerfile}


def test_framework_dispatches_deployment_command():
    spec = importlib.util.spec_from_file_location("framework", ROOT / "scripts/framework.py")
    framework = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(framework)
    assert framework._COMMANDS["deployment"] == (
        "framework_deployment", "framework_deployment.py")


def test_effective_schema_and_catalog_parity(tmp_path, capsys):
    pack = _pack(tmp_path / "catalog/good")
    (pack / "deployment.compose.yaml").write_text("api: 1\nservices: {}\n")
    (pack / "docker-compose.yml").write_text(MODULE.render(pack))
    assert MODULE.main(["parity", str(tmp_path / "catalog")]) == 0
    assert "parity: clean" in capsys.readouterr().out

    (pack / "docker-compose.yml").write_text("services: {}\n")
    assert MODULE.main(["parity", str(tmp_path / "catalog")]) == 1
    assert "differs" in capsys.readouterr().out
    (pack / "deployment.compose.yaml").unlink()
    assert MODULE.parity([tmp_path / "catalog"]) == [
        f"{pack.resolve()}: deployment Compose contract v1 not adopted"
    ]
    assert MODULE.parity([tmp_path / "missing"]) == ["no packs discovered"]


def test_effective_schema_rejects_invalid_service_shapes():
    with pytest.raises(MODULE.DeploymentError, match="services object"):
        MODULE.validate_effective([])
    services = {name: {"image": "example"} for name in MODULE.REQUIRED_SERVICES}
    broken = {"services": dict(services)}
    broken["services"]["gateway"] = "bad"
    with pytest.raises(MODULE.DeploymentError, match="gateway must be an object"):
        MODULE.validate_effective(broken)
    broken["services"]["gateway"] = {"environment": []}
    with pytest.raises(MODULE.DeploymentError, match="image or build"):
        MODULE.validate_effective(broken)
    broken["services"]["gateway"] = {"image": "x", "ports": "bad"}
    with pytest.raises(MODULE.DeploymentError, match="ports must be list"):
        MODULE.validate_effective(broken)


def test_contract_failure_paths_are_fail_closed(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(MODULE.DeploymentError, match="cannot load pack"):
        MODULE._pack(missing)
    (missing / "pack.yaml").write_text("[]\n")
    with pytest.raises(MODULE.DeploymentError, match="must declare name"):
        MODULE._pack(missing)

    base = {"services": {"gateway": {"image": "x"}}}
    assert MODULE.validate_override({"api": 1}, base) == {}
    invalid = [
        ({}, "api: 1"),
        ({"api": 1, "extra": True}, "unknown override"),
        ({"api": 1, "services": []}, "services must be an object"),
        ({"api": 1, "services": {"other": {}}}, "unknown service"),
        ({"api": 1, "services": {"gateway": {"command": []}}}, "may contain only"),
    ]
    for value, message in invalid:
        with pytest.raises(MODULE.DeploymentError, match=message):
            MODULE.validate_override(value, base)

    with pytest.raises(MODULE.DeploymentError, match="lacks required"):
        MODULE.validate_effective(base)

    pack = _pack(tmp_path / "pack")
    unresolved = tmp_path / "unresolved.yml"
    unresolved.write_text("services: {}\nx: '{{UNKNOWN}}'\n")
    monkeypatch.setattr(MODULE, "BASE", unresolved)
    with pytest.raises(MODULE.DeploymentError, match="unresolved"):
        MODULE.render(pack)
    monkeypatch.setattr(MODULE, "BASE", ROOT / "templates/pack/skeleton/docker-compose.yml")
    assert MODULE.discover_packs([pack]) == [pack.resolve()]

    (pack / "deployment.compose.yaml").write_text("api: 1\nservices: {}\n")
    assert "docker-compose.yml" in MODULE.parity([pack])[0]
    assert MODULE.main(["render", str(missing)]) == 2
    assert "ERROR:" in capsys.readouterr().err


def test_migration_rejects_extra_services_and_unsupported_differences():
    with pytest.raises(MODULE.DeploymentError, match="unsupported service"):
        MODULE._migration_override(
            {"services": {"extra": {"image": "x"}}}, {"services": {}})
    with pytest.raises(MODULE.DeploymentError, match="unsupported differences"):
        MODULE._migration_override(
            {"services": {"gateway": {"command": ["bad"]}}},
            {"services": {"gateway": {}}},
        )
