import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/composed_pack_state.py"


def _load():
    spec = importlib.util.spec_from_file_location("composed_pack_state_direct", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_write_safe_names_and_empty_source_shape(tmp_path):
    m = _load()
    assert m.manifest_path(tmp_path, " !!! ").name == "pack.json"
    assert m.load(tmp_path, "missing") == {}
    path = m.manifest_path(tmp_path, "demo")
    path.parent.mkdir(parents=True)
    path.write_text("[]")
    assert m.load(tmp_path, "demo") == {}
    path.write_text("{broken")
    assert m.load(tmp_path, "demo") == {}

    pack = tmp_path / "pack"; pack.mkdir()
    (pack / "pack.yaml").write_text("name: demo\n")
    manifest = m.source_manifest(pack, "domain")
    assert manifest["lane_scripts"] == {} and manifest["cron_jobs"] == {}
    assert m.write(tmp_path, manifest) is True
    assert m.write(tmp_path, manifest) is False


def test_source_manifest_import_walk_handles_syntax_missing_and_transitive_support(tmp_path):
    m = _load(); pack = tmp_path / "pack"
    scripts = pack / "crons/scripts"; scripts.mkdir(parents=True)
    (pack / "pack.yaml").write_text("name: demo\nversion: 1\n")
    (scripts / "lane.py").write_text("import helper\nimport external\n")
    (scripts / "helper.py").write_text("from nested import value\n")
    (scripts / "nested.py").write_text("value = 1\n")
    (scripts / "broken.py").write_text("[syntax")
    cron = pack / "crons/domain-crons.json"
    cron.write_text(json.dumps({"jobs": [
        {"name": "demo-lane", "script": "lane.py"},
        {"name": "demo-broken", "script": "broken.py"},
        {"name": "other-lane", "script": "helper.py"},
        "scalar",
    ]}))
    manifest = m.source_manifest(pack, "domain")
    assert set(manifest["lane_scripts"]) == {"lane.py", "broken.py"}
    assert set(manifest["shared_support_scripts"]) == {"helper.py", "nested.py"}


def test_installed_drift_covers_missing_modified_and_invalid_state(tmp_path):
    m = _load(); host = tmp_path / "host"
    scripts = host / "crons/scripts"; scripts.mkdir(parents=True)
    modified = scripts / "modified.py"; modified.write_text("changed")
    cron = host / "crons/domain-crons.json"; cron.write_text("{broken")
    manifest = {
        "pack": "demo",
        "lane_scripts": {"missing.py": "x", "modified.py": "x"},
        "cron_jobs": {"missing-job": "x"},
    }
    drift = m.installed_drift(host, manifest)
    assert any("missing crons/scripts/missing.py" in item for item in drift)
    assert any("modified crons/scripts/modified.py" in item for item in drift)
    assert any("missing cron job missing-job" in item for item in drift)

    cron.write_text(json.dumps([{"name": "job", "enabled": False, "schedule": "old"}, None]))
    manifest["cron_jobs"] = {"job": "wrong"}
    assert any("modified cron job job" in item for item in m.installed_drift(host, manifest))

    base = host / ".okengine/installed-domains"; base.mkdir(parents=True)
    (base / "bad.json").write_text("{broken")
    (base / "good.json").write_text(json.dumps({"pack": "good"}))
    assert any("invalid installed-domain manifest" in item for item in m.all_installed_drift(host))
