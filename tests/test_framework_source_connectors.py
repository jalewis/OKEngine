import importlib.util
import shutil
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
VALIDATOR = REPO / "scripts/framework_validate.py"
FIXTURES = REPO / "tests/fixtures/source_connectors"


def _load():
    spec = importlib.util.spec_from_file_location("framework_validate_connectors", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_framework_validates_all_pack_connector_manifests(tmp_path):
    module = _load()
    pack = tmp_path / "pack"
    connectors = pack / "connectors"
    connectors.mkdir(parents=True)
    for mode in ("bundle", "query", "enrichment", "stream", "poll"):
        shutil.copy(FIXTURES / f"{mode}.yaml", connectors)
    report = module.Report()
    module.check_source_connectors(pack, report)
    assert report.n_fail == 0
    assert len([row for row in report.rows if row[0] == "OK"]) == 5


def test_framework_fails_invalid_connector_before_deploy(tmp_path):
    module = _load()
    pack = tmp_path / "pack"
    connectors = pack / "connectors"
    connectors.mkdir(parents=True)
    manifest = (FIXTURES / "bundle.yaml").read_text().replace(
        "https://sources.example/catalog", "https://undeclared.example/catalog")
    (connectors / "bad.yaml").write_text(manifest)
    report = module.Report()
    module.check_source_connectors(pack, report)
    assert report.n_fail == 1
    assert "allowed_hosts" in report.rows[0][2]


def test_empty_connector_directory_warns_instead_of_silent_pass(tmp_path):
    module = _load()
    pack = tmp_path / "pack"
    (pack / "connectors").mkdir(parents=True)
    report = module.Report()
    module.check_source_connectors(pack, report)
    assert report.n_fail == 0
    assert report.rows == [("WARN", "connectors/", "directory exists but contains no *.yaml or *.yml manifests")]
