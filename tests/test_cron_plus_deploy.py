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
    assert "ps -q gateway" in t                          # finds the pack container (compose-scoped, #108)


def test_deploy_jobs_expands_jitter_sentinels():  # #107
    """Engine crons ship @jitter:* sentinels; the deploy must expand them to concrete schedules
    (cron-plus can't parse a raw sentinel — it errors every tick) before streaming to the container."""
    t = (S / "deploy-cron-plus-jobs.sh").read_text()
    assert "cron_jitter" in t and "expand_jobs" in t, "deploy no longer expands @jitter sentinels"
    assert 'DEPLOY_JOBS' in t and '< "$DEPLOY_JOBS"' in t, "deploy must stream the expanded copy, not raw $SRC"


def test_deploy_scripts_scope_gateway_to_pack_project():  # #108
    """On a multi-pack host the deploy must target THIS pack's gateway (its compose project),
    not the first 'gateway'-labeled container globally."""
    for name in ("deploy-cron-plus-jobs.sh", "deploy-cron-scripts.sh"):
        t = (S / name).read_text()
        assert 'docker compose -f "$PACK_DIR/docker-compose.yml" ps -q gateway' in t, f"{name} not project-scoped"
    c = (S / "cron-plus.sh").read_text()   # scope via CRON_PACK_DIR; refuse ambiguity vs pick head -1
    assert "CRON_PACK_DIR" in c and "set CRON_PACK_DIR" in c, "cron-plus.sh lacks the disambiguation guard"


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
