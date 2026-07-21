import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "scripts/deploy-cron-scripts.sh"


def test_connector_staging_script_is_valid_bash():
    result = subprocess.run(["bash", "-n", str(DEPLOY)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_deploy_stages_only_valid_manifest_extensions_into_connector_namespace():
    script = DEPLOY.read_text()
    assert 'PACK_CONNECTORS="$PACK_DIR/connectors"' in script
    assert "-name '*.yaml' -o -name '*.yml'" in script
    assert "/opt/data/config/connectors/" in script
    assert "source connector manifest(s) deployed" in script
