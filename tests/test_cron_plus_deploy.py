"""Regression guards for the cron-plus pack-deploy path (#17-#21).

These are static/offline guards — the live path needs a Docker host. They lock in
that the deploy targets the PACK RUNTIME (the gateway container / .hermes-data),
not the old host ~/.hermes or engine-repo compose, so a refactor can't silently
revert the fixes.
"""
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
S = REPO / "scripts"


def test_gitignore_ignores_generated_jobs():  # #21
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "config/cron-plus-jobs.json"],
                       capture_output=True, text=True)
    assert r.returncode == 0, "config/cron-plus-jobs.json is not gitignored (inline-comment bug?)"


def test_deploy_cron_scripts_creates_runtime_dir():  # #17
    t = (S / "deploy-cron-scripts.sh").read_text()
    assert "mkdir -p /opt/data/scripts" in t


def test_deploy_jobs_targets_pack_runtime_not_host_hermes():  # #18
    t = (S / "deploy-cron-plus-jobs.sh").read_text()
    assert "/opt/data/cron-plus/jobs.json" in t
    assert ".hermes/cron-plus/jobs.json" not in t        # no host ~/.hermes target
    assert "com.docker.compose.service=gateway" in t     # finds the pack container


def test_cron_plus_helper_uses_pack_container():  # #19
    t = (S / "cron-plus.sh").read_text()
    assert "docker exec" in t and "docker compose exec" not in t
    assert "com.docker.compose.service=gateway" in t


def test_install_cron_plus_targets_runtime_and_reads_pin():  # #20
    assert (S / "install-cron-plus.sh").is_file()
    t = (S / "install-cron-plus.sh").read_text()
    assert ".hermes-data/plugins/cron-plus" in t
    assert "pinned_sha" in t and "dependencies.cron-plus" not in t.split("\n")[0]  # reads the manifest pin
    r = subprocess.run(["bash", "-n", str(S / "install-cron-plus.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_config_template_enables_cron_plus():  # #20
    c = yaml.safe_load((REPO / "config" / "config.yaml.template").read_text())
    assert "cron-plus" in (c.get("plugins") or {}).get("enabled", [])


def test_deploy_installs_cron_plus_before_compose():  # #20 integration
    t = (S / "deploy.sh").read_text()
    assert "install-cron-plus.sh" in t
    # the install call must run before the compose-up STEP (the "[4/" marker —
    # an unambiguous anchor, unlike "docker compose up" which is also in the header;
    # count-agnostic so a step renumber doesn't break it)
    assert t.index("install-cron-plus.sh") < t.index("[4/")
