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
STAGE = REPO / "scripts" / "deploy-cron-scripts.sh"


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
    """#67 scope: reader healthz, MCP read+write, auth, cron-plus, qmd index.

    CANNOT DETECT: whether ANY of these checks runs, is reached, or can fail. Every token here is
    satisfied by a comment. This is a deletion alarm, not evidence the surface is verified — and
    it is why the cockpit literal below once failed a refactor that improved the check it
    guarded.
    """
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
        "cockpit service (B7.5)": "$COCKPIT",
        "cockpit checked only if compose defines it (B7.5)": 'has_service "$COCKPIT"',
        "api_server exposure (#120)": "api_server is LAN-exposed",
        "read-MCP baked-lib drift (M-B4.1)": "read-MCP $lib is STALE",
        "read-MCP schema-lib drift": "kb_search.py tier_lib.py schema_lib.py",
        "read-MCP base-schema drift": "read-MCP base-schema.yaml is STALE",
        "policy digest verification (#283)": "runtime policy digest matches composed source",
        "least-privilege capability probe (#283)": "source-quality capability probe rejects",
        "hardened posture host-side peer (#326 [1])": "hardened_posture_violations",
        "shared deployment-checks section (#405)": "import deployment_checks as C",
        "scheduler-independent full cron contract": '"crons", "timezone"',
        "scheduler-independent Agent Chat toolset check": '"operations", "auth"',
        "scheduler-independent base-schema drift": 'config/base-schema.yaml:config/base-schema.yaml',
        "scheduler-independent schema-validator drift": 'tools/schema_validator.py:config/schema_validator.py',
        "invalid timezone fails closed": 'not a valid IANA timezone',
        "effective gateway health contract (#650)": "gateway effective health contract covers",
        "stale corpus holder health (#650)": "lock-owner.json",
        "stale running receipt health (#650)": "-mmin +60",
        "D-state child health (#650)": "/proc/[0-9]*/stat",
        # NB: the iwe-dep check (#168) was intentionally removed by #179 — backlinks-refresh now
        # builds the graph with an in-process link-scanner and needs no gateway iwe binary, so the
        # verifier no longer probes for it. (Stale required-surface entry dropped.)
    }
    missing = [name for name, token in required.items() if token not in body]
    assert not missing, f"verifier no longer checks: {missing}"


def test_standalone_cron_stage_fails_read_mcp_split_version():
    """
    CANNOT DETECT: whether the split-version refusal fires, or whether the two paths are actually
    compared — only that both path spellings and the refusal message exist in the file.
    """
    body = STAGE.read_text()
    for lib in ("kb_search.py", "tier_lib.py", "schema_lib.py"):
        assert lib in body
    assert "/app/scripts/$lib" in body
    assert "/opt/data/scripts/$lib" in body
    assert "refusing a split-version deploy" in body
    assert "/app/config/base-schema.yaml" in body
    assert "/opt/data/config/base-schema.yaml" in body


def test_hardened_posture_check_reuses_pure_evaluator_and_fails_on_violation():  # okengine#326 [1]
    """check_auth's hardened-posture rules only ran in the daily cron lane (dies with the scheduler).
    The verifier must have a host-side peer that reuses the SAME pure evaluator and FAILs a violation
    — not re-implement the rules (which would drift).

    CANNOT DETECT: whether the evaluator is CALLED with real posture facts, or whether a
    violation reaches bad(). It pins reuse-not-reimplementation, which is a code-shape claim by
    design.
    """
    body = VERIFY.read_text()
    assert "hardened_posture_violations" in body, "verifier does not call the shared posture evaluator"
    assert "is_hardened" in body, "verifier must skip the checks when the hardened profile is off"
    # a flagged violation must FAIL (not merely warn), and the profile-off path must not FAIL
    assert 'bad "hardened posture:' in body, "a hardened-posture violation must FAIL the verifier"
    assert "hardened profile off" in body, "profile-off must be a non-FAIL (N/A) branch"
    # reuse, not re-implementation: it imports from hardening_lib, doesn't hardcode the rule strings
    assert "from hardening_lib import" in body


def test_tick_lock_check_tests_freshness_not_presence():  # invariant-audit HIGH
    """The .tick.lock is never unlinked (bind-mounted, survives every recreate), so a presence test
    passes forever off a fossil and can never FAIL on a dead scheduler. The gate must compare the
    lock's AGE (mtime) and FAIL when stale.

    CANNOT DETECT: whether the mtime comparison runs, or whether a stale lock actually FAILs. It
    rules out the old presence-only test by construction, not the new one by execution.
    """
    body = VERIFY.read_text()
    assert "stat -c %Y" in body and "lock_mtime" in body, "tick-lock check no longer measures mtime"
    assert "STALE" in body, "tick-lock check has no stale-scheduler FAIL branch"
    # zero-wait fossil discriminator: a lock predating container start = dead-on-arrival scheduler
    assert "StartedAt" in body and "FOSSIL" in body, "tick-lock check missing the container-start fossil discriminator"


def test_reports_pass_warn_fail_and_exit_code():
    """
    CANNOT DETECT: whether the exit is reached or which branch it takes.
    tests/test_post_deploy_verify_behaviour.py asserts the executed contract (status follows the
    FAIL tally, tally counts the verdicts printed); this only checks the vocabulary survives.
    """
    body = VERIFY.read_text()
    # has the three verdict helpers and a non-zero exit on FAIL
    for token in ("PASS", "WARN", "FAIL", "exit 1"):
        assert token in body, f"verifier lost its {token!r} reporting"


def test_deploy_wires_in_the_verifier():
    """
    CANNOT DETECT: whether deploy.sh RUNS the verifier or merely mentions it.
    tests/test_deploy_exit_contract.py covers the executed wiring, including its exit status.
    """
    body = DEPLOY.read_text()
    assert "post_deploy_verify.sh" in body, "deploy.sh no longer runs the verifier"
    assert "[6/6]" in body, "deploy.sh step labels not updated for the verify step"


def test_checks_config_at_runtime_mount_not_vault():
    """okengine#106: the runtime config is the pack's .hermes-data mounted at /opt/data;
    checking /opt/vault/.hermes-data/config.yaml (absent in the gateway) produced false
    write-path + cron-plus FAILs.

    CANNOT DETECT: whether CFG is used for the config reads, or whether the stale vault path was
    replaced everywhere rather than deleted from the one line this asserts on.
    """
    body = VERIFY.read_text()
    assert "CFG=/opt/data/config.yaml" in body, "verifier should read the runtime config at /opt/data"
    assert "/opt/vault/.hermes-data/config.yaml" not in body, "stale vault config path still present"


def test_verifier_engine_path_cannot_be_overwritten_by_pack_env():
    """#283: deployments legitimately pin ENGINE_DIR in .env. The verifier's
    own merged source path must use a private name so sourcing that file cannot
    redirect the expected-policy calculation to an old or missing checkout.

    CANNOT DETECT: whether sourcing .env can still clobber the resolved path at runtime — an .env
    exporting VERIFY_ENGINE_DIR would defeat the guard while leaving all three literals intact.
    """
    body = VERIFY.read_text()
    assert 'VERIFY_ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"' in body
    assert 'OKENGINE_POLICY_CATALOG="$VERIFY_ENGINE_DIR/config/policy/catalog.yaml"' in body
    assert 'python3 "$VERIFY_ENGINE_DIR/tools/policy_plane.py" digest' in body


def test_host_selfchecks_can_import_the_source_layout_package():
    """CANNOT DETECT: whether the live host interpreter can import every package dependency."""
    body = VERIFY.read_text()
    assert 'os.path.join(sys.argv[1], "src")' in body


def test_runtime_policy_probe_uses_the_wheel_from_the_vault_workdir():
    """CANNOT DETECT: whether the probe succeeds in a built gateway; live deploy verification owns that."""
    body = VERIFY.read_text()
    assert "cd /opt/vault" in body
    assert "from tools import policy_plane" in body
    assert "engine_catalog_path().is_file()" in body


def test_runtime_policy_probe_checks_each_stdio_child_environment():
    """The gateway shell env is not inherited by Hermes stdio MCP children (#544).

    CANNOT DETECT: whether the probe runs against a live container or the child actually inherits
    the catalog path; this source check only pins the offline verification contract.
    """
    body = VERIFY.read_text()
    assert 'env.get("OKENGINE_POLICY_CATALOG")' in body
    assert 'every stdio write server receives the baked policy catalog path' in body


# --- okengine#596: a command that could not be asked must not answer for what it was asked ------

def _stub_docker(tmp_path: Path, exit_code: int) -> dict:
    """A PATH whose `docker` always exits with `exit_code`, so compose cannot be consulted."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "docker"
    stub.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    stub.chmod(0o755)
    import os
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")
def test_an_unreadable_compose_config_is_undetectable_not_a_missing_service(tmp_path):
    """Observed live: five verifiers run back to back, one `docker compose config` failed under
    the contention, and the run reported "projection services are absent from effective Compose
    configuration" on a deployment whose projection was up and healthy — then passed three times
    in a row immediately after.

    `config --services 2>/dev/null | grep -Fxq` conflates "compose replied and the service is not
    defined" with "compose could not be asked". The first is a finding; the second is a
    measurement that did not happen.
    """
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    result = subprocess.run(["bash", str(VERIFY)], cwd=tmp_path, capture_output=True, text=True,
                            env=_stub_docker(tmp_path, 1))
    out = result.stdout + result.stderr
    assert "undetectable, not a pass" in out, out[-2000:]
    assert "absent from effective Compose configuration" not in out, (
        "an unaskable compose was reported as a missing service"
    )


def test_the_verifier_resolves_the_service_list_once_rather_than_per_probe():
    """Three separate `config --services` calls meant three chances to hit the same transient
    failure, each with its own inconsistent verdict.

    CANNOT DETECT: whether has_service is what the probes actually call. Counting one `config
    --services` call rules out the three-call shape; it does not prove the single call is used.
    """
    body = VERIFY.read_text(encoding="utf-8")
    assert body.count("$(docker compose config --services") == 1, (
        "resolve the service list once into COMPOSE_SERVICES and probe it with has_service"
    )
    assert "COMPOSE_SERVICES_RC" in body and "has_service" in body


def test_verifier_inspects_running_gateway_health_not_only_source_override():
    """The verifier must inspect the effective health contract on the running container.

    CANNOT DETECT: source text cannot prove that docker inspect runs successfully against a live
    deployment or that Docker enforces the inspected healthcheck.
    """
    body = VERIFY.read_text(encoding="utf-8")
    section = body[body.index("# The engine-generated override"):body.index(
        "# PostgreSQL projection")]
    assert "docker compose ps -q" in section
    assert "docker inspect" in section and ".Config.Healthcheck.Test" in section
    assert 'bad "gateway effective health contract is absent or unreadable"' in section
    assert 'bad "gateway effective health contract is incomplete' in section
