"""okengine#67 — guard the post-deploy verifier's shape.

The verifier (`scripts/post_deploy_verify.sh`) exercises a LIVE docker stack, so it can't run in
the offline suite. These tests instead pin its contract: it parses as valid bash, it actually
covers every surface #67 requires, and deploy.sh wires it in as the final step. They fail loudly
if a future edit drops a check or unhooks it from deploy."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "scripts" / "post_deploy_verify.sh"
DEPLOY = REPO / "scripts" / "deploy.sh"


def test_verifier_exists_and_executable():
    assert VERIFY.is_file(), "scripts/post_deploy_verify.sh is missing"
    assert VERIFY.stat().st_mode & 0o111, "post_deploy_verify.sh should be executable"


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_verifier_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(VERIFY)], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_verifier_errors_when_not_in_a_deployment_dir(tmp_path):
    """Run outside a compose dir -> exit 2 with guidance, not a crash."""
    r = subprocess.run(["bash", str(VERIFY)], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 2
    assert "deployment dir" in (r.stderr + r.stdout)


def test_covers_every_required_surface():
    """#67 scope: reader healthz, MCP read+write, auth, cron-plus, qmd index."""
    body = VERIFY.read_text()
    required = {
        "reader /healthz": "/healthz",
        "MCP read endpoint": "/mcp",
        "MCP write path": "okengine-write",
        "write_server file": "write_server.py",
        "cron-plus plugin": "cron-plus",
        "cron-plus jobs": "jobs.json",
        "qmd index": "qmd status",
        "reader auth": "OKENGINE_READER_PASSWORD",
        "MCP token": "OKENGINE_MCP_TOKEN",
        "api_server exposure (#120)": "api_server is LAN-exposed",
        # NB: the iwe-dep check (#168) was intentionally removed by #179 — backlinks-refresh now
        # builds the graph with an in-process link-scanner and needs no gateway iwe binary, so the
        # verifier no longer probes for it. (Stale required-surface entry dropped.)
    }
    missing = [name for name, token in required.items() if token not in body]
    assert not missing, f"verifier no longer checks: {missing}"


def test_reports_pass_warn_fail_and_exit_code():
    body = VERIFY.read_text()
    # has the three verdict helpers and a non-zero exit on FAIL
    for token in ("PASS", "WARN", "FAIL", "exit 1"):
        assert token in body, f"verifier lost its {token!r} reporting"


def test_deploy_wires_in_the_verifier():
    body = DEPLOY.read_text()
    assert "post_deploy_verify.sh" in body, "deploy.sh no longer runs the verifier"
    assert "[6/6]" in body, "deploy.sh step labels not updated for the verify step"


def test_checks_config_at_runtime_mount_not_vault():
    """okengine#106: the runtime config is the pack's .hermes-data mounted at /opt/data;
    checking /opt/vault/.hermes-data/config.yaml (absent in the gateway) produced false
    write-path + cron-plus FAILs."""
    body = VERIFY.read_text()
    assert "CFG=/opt/data/config.yaml" in body, "verifier should read the runtime config at /opt/data"
    assert "/opt/vault/.hermes-data/config.yaml" not in body, "stale vault config path still present"
