"""`deploy.sh` end to end against stubs — its decision branches, not its source text (#597/#602).

Every other test of this script greps its source. That catches a deleted line and nothing else: a
string assertion cannot tell "the check is gone" from "the check moved", and it cannot see whether
a branch RUNS, what status it leaves behind, or whether a check does anything at all. #602 lists
three live failures of exactly that kind, none of which a grep could have caught — a masked
`tail` exit status that reported a fleet-wide roll as successful having staged nothing among them.

So this runs the real script. `deploy.sh` resolves its own engine directory from `BASH_SOURCE`, so
the harness copies it into a scratch engine tree whose other scripts are stubs, and puts stub
`docker` and `git` on PATH. Nothing is mocked inside the script; every branch it takes is its own.

The contract under test is #597's: exit status is the only thing a scripted caller sees, and
`deploy.sh` returned 0 whether or not verification passed. A fleet roll, a CI job, or an agent
chaining `deploy.sh && <next>` could not distinguish a verified deployment from one that came up
with failing checks — both print `==> done` and differ only in the following sentence.

    0  up and verified
    3  up, but post-deploy checks reported issues
    other non-zero  bring-up itself failed
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "scripts" / "deploy.sh"

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")

# Engine scripts deploy.sh shells out to. Each becomes a stub that succeeds silently, so the only
# behaviour under test is deploy.sh's own orchestration.
STUBBED_SCRIPTS = (
    "ensure-runtime.sh",
    "install-cron-plus.sh",
    "build-engine-image.sh",
    "deploy-cron-scripts.sh",
    "deploy-cron-plus-jobs.sh",
    "kickstart.sh",
    "post_deploy_verify.sh",
)

# `docker image inspect -f '{{ ... git_sha }}'` must echo the sha the stub `git` reports, or
# deploy.sh decides the image is stale and takes the rebuild branch. Same value in both stubs.
SHA = "abc1234"

DOCKER_STUB = f"""#!/bin/sh
# Answer only what deploy.sh asks; anything else succeeds silently.
case "$1 $2" in
  "image inspect")
      # `-f <fmt> hermes-agent:latest` (provenance probe) vs a bare existence check.
      case "$*" in *org.okengine.git_sha*) echo "{SHA}" ;; esac
      exit 0 ;;
  "compose ps") echo "container-id" ; exit 0 ;;
  "inspect --format") echo "2026-08-17T00:00:00.000000000Z" ; exit 0 ;;
esac
exit 0
"""

GIT_STUB = f"""#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    rev-parse) echo "{SHA}" ; exit 0 ;;
    status)    exit 0 ;;          # no output = clean tree = no forced rebuild
  esac
done
exit 0
"""

# deploy.sh calls $PYTHON for framework validate/upgrade, the composed-schema recompose, the policy
# plane, cron_pack_split and the projection config. All succeed; none is what this file tests.
PYTHON_STUB = "#!/bin/sh\nexit 0\n"


def write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def deployment(tmp_path):
    """A scratch engine tree + pack + stub PATH. Returns a runner for the real deploy.sh."""
    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    shutil.copy(DEPLOY, engine / "scripts" / "deploy.sh")
    (engine / "engine-manifest.yaml").write_text(
        "engine_release: v9.8.7\n", encoding="utf-8")
    for name in STUBBED_SCRIPTS:
        write_exec(engine / "scripts" / name, "#!/bin/sh\nexit 0\n")

    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_exec(bindir / "docker", DOCKER_STUB)
    write_exec(bindir / "git", GIT_STUB)
    write_exec(bindir / "python-stub", PYTHON_STUB)

    def run(*args, verify_exit=0, **env):
        write_exec(engine / "scripts" / "post_deploy_verify.sh",
                   f"#!/bin/sh\necho 'stub verifier'\nexit {verify_exit}\n")
        environment = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "PYTHON": str(bindir / "python-stub"),
            "HERMES_UID": "1003",
            "HERMES_GID": "1003",
            # The real 5s settle before probing is for a gateway that is still binding; there is
            # no gateway here, and paying it twelve times dominated this file's runtime.
            "OKENGINE_VERIFY_DELAY": "0",
            **env,
        }
        return subprocess.run(
            ["bash", str(engine / "scripts" / "deploy.sh"), *args],
            cwd=pack, capture_output=True, text=True, timeout=180, env=environment,
        )

    run.pack = pack
    run.engine = engine
    return run


# --- the exit contract (okengine#597) ------------------------------------------------------------

def test_a_verified_deployment_exits_zero(deployment):
    result = deployment(verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deployment verified healthy" in result.stdout


def test_a_deployment_whose_checks_failed_exits_three_not_zero(deployment):
    """The defect: identical exit status for "it works" and "it came up broken". A scripted caller
    sees nothing else — `deploy.sh && <next step>` ran the next step either way."""
    result = deployment(verify_exit=1)
    assert result.returncode == 3, (
        f"expected 3 (up, checks reported issues), got {result.returncode}\n"
        f"{result.stdout}{result.stderr}"
    )
    assert "post-deploy checks reported issues" in result.stdout


def test_the_stack_is_still_brought_up_when_verification_fails(deployment):
    """3 must not become "abort the deploy". The stack IS up, the gateway may only be binding, and
    the remediation above is what the operator needs — the change is to the status, not the flow."""
    result = deployment(verify_exit=1)
    assert "[4/6] docker compose up -d --build" in result.stdout
    assert "[5/6] deploy crons" in result.stdout
    assert "==> done" in result.stdout


def test_the_verdict_survives_the_steps_that_run_after_it(deployment):
    """The status was lost because nothing set it: the script ended on whatever ran last, an echo
    or the kickstart block. --kickstart runs AFTER verification and must not launder a 3 into a 0."""
    result = deployment("--kickstart", verify_exit=1)
    assert result.returncode == 3, result.stdout + result.stderr


def test_kickstart_after_a_healthy_verify_still_exits_zero(deployment):
    result = deployment("--kickstart", verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr


# --- bring-up failure stays distinguishable from verification failure ----------------------------

def test_a_pack_without_a_compose_file_fails_before_anything_runs(tmp_path):
    engine = tmp_path / "engine"
    (engine / "scripts").mkdir(parents=True)
    shutil.copy(DEPLOY, engine / "scripts" / "deploy.sh")
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(["bash", str(engine / "scripts" / "deploy.sh")],
                            cwd=empty, capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "no docker-compose.yml" in result.stderr
    assert "[1/6]" not in result.stdout, "validation ran against a directory that is not a pack"


def test_an_unknown_flag_is_refused_rather_than_treated_as_a_pack_path(deployment):
    """`*)` assigns any non-flag argument to PACK, so an unrecognised flag that fell through would
    become the deployment directory."""
    result = deployment("--not-a-flag")
    assert result.returncode == 2
    assert "unknown flag" in result.stderr


def test_the_three_states_use_three_different_codes(deployment):
    """The point of #597: 0, 3 and the bring-up failures are mutually distinguishable. A scripted
    caller that cannot tell them apart is why this issue existed."""
    healthy = deployment(verify_exit=0).returncode
    unverified = deployment(verify_exit=1).returncode
    unusable = deployment("--not-a-flag").returncode
    assert len({healthy, unverified, unusable}) == 3, (
        f"states collapsed: verified={healthy}, checks-failed={unverified}, refused={unusable}"
    )


# --- flags that change the flow ------------------------------------------------------------------

def test_no_crons_skips_cron_deployment_but_still_verifies(deployment):
    result = deployment("--no-crons", verify_exit=0)
    assert result.returncode == 0
    assert "crons skipped (--no-crons)" in result.stdout
    assert "[6/6] verify deployment" in result.stdout


def test_kickstart_is_refused_when_no_crons_were_deployed(deployment):
    """kickstart drives the cron lanes; running it with none deployed would do nothing and look
    like it had."""
    result = deployment("--no-crons", "--kickstart", verify_exit=0)
    assert "--kickstart skipped: no crons were deployed" in result.stderr


def test_the_resolved_uid_is_pinned_into_the_pack_env(deployment):
    """A later bare `docker compose up` reads .env, not this process's environment. Without the
    pin compose falls back to the image default and the scheduler dies on a .tick.lock permission
    error — the trap that once forced a full rebuild of a review instance."""
    deployment(verify_exit=0)
    env_text = (deployment.pack / ".env").read_text(encoding="utf-8")
    assert "HERMES_UID=1003" in env_text and "HERMES_GID=1003" in env_text


def test_an_existing_uid_pin_is_never_overwritten(deployment):
    """Re-running as a different user must not silently retag a tree it does not own."""
    (deployment.pack / ".env").write_text("HERMES_UID=4242\nHERMES_GID=4242\n", encoding="utf-8")
    deployment(verify_exit=0, HERMES_UID="1003", HERMES_GID="1003")
    env_text = (deployment.pack / ".env").read_text(encoding="utf-8")
    assert "HERMES_UID=4242" in env_text
    assert env_text.count("HERMES_UID=") == 1, "deploy appended a second, conflicting pin"


def test_deploy_persists_an_immutable_gateway_image_and_compose_override(deployment):
    result = deployment(verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    env_text = (deployment.pack / ".env").read_text(encoding="utf-8")
    expected = f"OKENGINE_GATEWAY_IMAGE=hermes-agent:okengine-v9.8.7-{SHA}"
    assert expected in env_text
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.okengine-image.yml" in env_text
    override = (deployment.pack / "docker-compose.okengine-image.yml").read_text(
        encoding="utf-8")
    assert "${OKENGINE_GATEWAY_IMAGE:?" in override
    assert "latest" not in override
    assert "lock-owner.json" in override and "now-mtime" in override
    assert "-mmin +60" in override and "/proc/[0-9]*/stat" in override
    assert "interval: 60s" in override and "retries: 3" in override
    health = yaml.safe_load(override)["services"]["gateway"]["healthcheck"]
    assert health["test"][0] == "CMD-SHELL"
    assert "$$(date +%s)" in health["test"][1], "Compose interpolation was expanded too early"


def test_redeploy_updates_image_pin_once_without_duplicate_compose_entries(deployment):
    first = deployment(verify_exit=0)
    second = deployment(verify_exit=0)
    assert first.returncode == second.returncode == 0
    env_text = (deployment.pack / ".env").read_text(encoding="utf-8")
    assert env_text.count("OKENGINE_GATEWAY_IMAGE=") == 1
    assert env_text.count("COMPOSE_FILE=") == 1
    compose_file = next(line for line in env_text.splitlines()
                        if line.startswith("COMPOSE_FILE="))
    assert compose_file.count("docker-compose.okengine-image.yml") == 1


def test_deploy_never_attaches_generated_sidecar_to_default_compose_project(deployment):
    generated = deployment.pack / ".okengine/generated/sidecars.compose.yml"
    generated.parent.mkdir(parents=True)
    generated.write_text("services: {}\n", encoding="utf-8")

    first = deployment(verify_exit=0)
    second = deployment(verify_exit=0)

    assert first.returncode == second.returncode == 0
    compose_line = next(
        line for line in (deployment.pack / ".env").read_text().splitlines()
        if line.startswith("COMPOSE_FILE=")
    )
    assert ".okengine/generated/sidecars.compose.yml" not in compose_line


def test_deploy_removes_absolute_legacy_sidecar_override(deployment):
    generated = deployment.pack / ".okengine/generated/sidecars.compose.yml"
    generated.parent.mkdir(parents=True)
    generated.write_text("services: {demo-sidecar: {image: example.invalid/demo@sha256:abc}}\n")
    (deployment.pack / ".env").write_text(
        "COMPOSE_FILE=docker-compose.yml:" + str(generated.resolve()) + "\n",
        encoding="utf-8",
    )
    result = deployment(verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    compose_line = next(
        line for line in (deployment.pack / ".env").read_text().splitlines()
        if line.startswith("COMPOSE_FILE=")
    )
    assert str(generated.resolve()) not in compose_line
