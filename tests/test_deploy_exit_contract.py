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
import sys
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
# engine_images.py (okengine#1033) is the exception: it derives the image pins deploy.sh persists,
# which several tests below assert, so it runs for real from the scratch engine tree.
PYTHON_STUB = f"""#!/bin/sh
case "$1" in
  */scripts/engine_images.py) exec "{sys.executable}" "$@" ;;
esac
exit 0
"""


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
    (engine / "scripts" / "cron").mkdir()
    shutil.copy(REPO / "scripts" / "engine_images.py", engine / "scripts" / "engine_images.py")
    shutil.copy(REPO / "scripts" / "cron" / "dotenv_lib.py",
                engine / "scripts" / "cron" / "dotenv_lib.py")
    (engine / "scripts" / "lib").mkdir()
    shutil.copy(REPO / "scripts" / "lib" / "hermes_uid.sh",
                engine / "scripts" / "lib" / "hermes_uid.sh")

    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_exec(bindir / "docker", DOCKER_STUB)
    write_exec(bindir / "git", GIT_STUB)
    write_exec(bindir / "python-stub", PYTHON_STUB)

    def run(*args, verify_exit=0, cwd=None, **env):
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
        # A None value unsets the variable, so a test can exercise the .env pin alone.
        environment = {key: value for key, value in environment.items() if value is not None}
        return subprocess.run(
            ["bash", str(engine / "scripts" / "deploy.sh"), *args],
            cwd=cwd or pack, capture_output=True, text=True, timeout=180, env=environment,
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


# --- okengine#1044 [1]: deploy.sh reads the .env uid pin the way compose does -------------------
# deploy.sh used a private first-wins `grep '^HERMES_UID=[0-9]+'` that disagreed with compose and
# scripts/lib/hermes_uid.sh (dotenv_lib: last assignment wins, quotes and `export` handled). The
# deploy exported its own reading, so the deploy itself looked consistent while every later bare
# `docker compose up` ran the gateway as the uid compose reads.

_UNPINNED = {"HERMES_UID": None, "HERMES_GID": None}


def _resolved_uid(result) -> str:
    line = next((l for l in result.stdout.splitlines() if "uid:gid " in l), "")
    return line.rsplit("uid:gid ", 1)[-1].strip()


@pytest.mark.parametrize("env_text", [
    "HERMES_UID=10000\nHERMES_GID=10000\nHERMES_UID=4242\nHERMES_GID=4242\n",  # last one wins
    'HERMES_UID="4242"\nHERMES_GID="4242"\n',                                  # quoted
    "export HERMES_UID=4242\nexport HERMES_GID=4242\n",                          # export form
    "HERMES_UID=4242 # pinned\nHERMES_GID=4242 # pinned\n",                      # inline comment
], ids=["duplicate-key", "quoted", "export", "inline-comment"])
def test_the_env_uid_pin_is_read_as_compose_reads_it(deployment, env_text):
    (deployment.pack / ".env").write_text(env_text, encoding="utf-8")
    result = deployment(verify_exit=0, **_UNPINNED)
    assert _resolved_uid(result) == "4242:4242", result.stdout + result.stderr


def test_an_export_form_pin_is_not_shadowed_by_an_appended_one(deployment):
    """`grep -qE '^HERMES_UID='` missed `export HERMES_UID=...` and appended a second pin, which
    compose then took as the last assignment: the operator's pin silently lost."""
    (deployment.pack / ".env").write_text("export HERMES_UID=1003\nexport HERMES_GID=1003\n",
                                          encoding="utf-8")
    deployment(verify_exit=0, HERMES_UID="4242", HERMES_GID="4242")
    env_text = (deployment.pack / ".env").read_text(encoding="utf-8")
    assert env_text.count("HERMES_UID=") == 1, env_text


def test_an_empty_env_pin_is_replaced_by_the_resolved_uid(deployment):
    """An empty `HERMES_UID=` is no pin: compose's `${HERMES_UID:-10000}` gives 10000. deploy.sh
    must append the uid it resolved, so a later bare `compose up` runs as the tree owner."""
    (deployment.pack / ".env").write_text("HERMES_UID=\nHERMES_GID=\n", encoding="utf-8")
    deployment(verify_exit=0, HERMES_UID="1003", HERMES_GID="1003")
    lines = (deployment.pack / ".env").read_text(encoding="utf-8").splitlines()
    assert [l for l in lines if l.startswith("HERMES_UID=")][-1] == "HERMES_UID=1003", lines


def test_with_no_pin_and_no_export_the_invoking_uid_is_used(deployment):
    """okengine#102: a clone-as-yourself pack defaults to your uid, not the tree-owner fallback."""
    result = deployment(verify_exit=0, **_UNPINNED)
    assert _resolved_uid(result) == f"{os.getuid()}:{os.getgid()}", result.stdout


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


# --- okengine#1033: the compose-built engine images are pinned too ----------------------------

LEGACY_COMPOSE = """services:
  gateway:
    image: hermes-agent
  okengine-reader:
    build: {context: "${ENGINE_DIR:-../engine}", dockerfile: okengine-reader/Dockerfile}
    image: okengine-reader
  okengine-cockpit:
    build: {context: "${ENGINE_DIR:-../engine}", dockerfile: okengine-cockpit/Dockerfile}
    image: ${OKENGINE_COCKPIT_IMAGE:-okengine-cockpit}
"""


def _override_images(deployment) -> dict:
    override = yaml.safe_load(
        (deployment.pack / "docker-compose.okengine-image.yml").read_text(encoding="utf-8"))
    return {name: spec.get("image") for name, spec in override["services"].items()}


def test_deploy_pins_a_legacy_packs_engine_images_to_its_scoped_tag(deployment):
    """Migration: an existing pack keeps `image: okengine-reader` in its own docker-compose.yml.
    Its next deploy.sh must move it onto the scoped tag without editing that file."""
    (deployment.pack / "docker-compose.yml").write_text(LEGACY_COMPOSE, encoding="utf-8")
    result = deployment(verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    tag = f"okengine-v9.8.7-{SHA}.pack"
    env_text = (deployment.pack / ".env").read_text(encoding="utf-8")
    assert f"OKENGINE_ENGINE_IMAGE_TAG={tag}" in env_text.splitlines()
    images = _override_images(deployment)
    pin = "${OKENGINE_ENGINE_IMAGE_TAG:?deploy.sh must pin an immutable engine image tag}"
    assert images["okengine-reader"] == f"okengine-reader:{pin}"
    assert images["okengine-cockpit"] == f"okengine-cockpit:{pin}", \
        "a pack-local cockpit pin must not keep the cockpit on a hand-maintained tag"
    assert "okengine-mcp" not in images, \
        "an override entry for a service the pack does not define would CREATE that service"
    assert (deployment.pack / "docker-compose.yml").read_text() == LEGACY_COMPOSE


def test_redeploy_keeps_one_engine_tag_pin_and_one_override_entry(deployment):
    (deployment.pack / "docker-compose.yml").write_text(LEGACY_COMPOSE, encoding="utf-8")
    assert deployment(verify_exit=0).returncode == 0
    assert deployment(verify_exit=0).returncode == 0
    env_text = (deployment.pack / ".env").read_text(encoding="utf-8")
    assert env_text.count("OKENGINE_ENGINE_IMAGE_TAG=") == 1
    text = (deployment.pack / "docker-compose.okengine-image.yml").read_text(encoding="utf-8")
    assert text.count("  okengine-reader:") == 1


def test_the_engine_tag_is_scoped_to_the_compose_project(deployment):
    (deployment.pack / ".env").write_text("COMPOSE_PROJECT_NAME=Okcti_Test\n", encoding="utf-8")
    assert deployment(verify_exit=0).returncode == 0
    env_text = (deployment.pack / ".env").read_text(encoding="utf-8")
    assert f"OKENGINE_ENGINE_IMAGE_TAG=okengine-v9.8.7-{SHA}.okcti_test" in env_text


def test_an_unreadable_compose_file_stops_the_deploy_before_bring_up(deployment):
    (deployment.pack / "docker-compose.yml").write_text("services: [unterminated\n",
                                                         encoding="utf-8")
    result = deployment(verify_exit=0)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot read this deployment's compose files" in result.stderr
    assert "[4/6]" not in result.stdout


def test_an_unresolvable_engine_revision_stops_the_deploy(deployment):
    """QA on !1317: `git rev-parse` failing used to yield `unknown`, so every unattributable engine
    tree got the same `okengine-<rel>-unknown` gateway and engine tags. Fail closed instead."""
    write_exec(deployment.pack.parent / "bin" / "git", GIT_STUB.replace(
        'rev-parse) echo "abc1234" ; exit 0 ;;', "rev-parse) exit 128 ;;"))
    result = deployment(verify_exit=0)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot resolve the engine revision" in result.stderr
    assert "[3/6]" not in result.stdout and "[4/6]" not in result.stdout
    env = deployment.pack / ".env"
    assert "unknown" not in (env.read_text() if env.exists() else "")


def test_a_missing_compose_file_entry_stops_the_deploy(deployment):
    (deployment.pack / ".env").write_text(
        "COMPOSE_FILE=docker-compose.yml:.hermes-data/projection-compose.yml\n", encoding="utf-8")
    result = deployment(verify_exit=0)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "projection-compose.yml" in result.stderr and "[4/6]" not in result.stdout


def test_a_tag_that_cannot_be_derived_stops_the_deploy(deployment):
    manifest = deployment.engine / "engine-manifest.yaml"
    manifest.write_text("engine_release: v9.8.7/../x\n", encoding="utf-8")
    result = deployment(verify_exit=0)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot derive the immutable engine image tag" in result.stderr


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


# --- the pre-verify projection reconcile (okengine#853) -------------------------------------------

RACING_PROJECTION_DOCKER = DOCKER_STUB.replace(
    'case "$1 $2" in',
    '''case "$*" in
  *"config --services"*) echo okengine-projection ; exit 0 ;;
  *"exec -T okengine-projection python /app/service.py --reconcile"*)
      echo "projection operation failed: RuntimeError: canonical corpus changed during projection reconciliation; retry" >&2
      exit 1 ;;
esac
case "$1 $2" in''', 1)


def test_a_reconcile_that_loses_the_race_with_live_writes_does_not_fail_the_bring_up(deployment):
    """The live finding: every rollout step had completed and the gateway was healthy, but the
    forced full scan lost the race with lanes writing to the vault and `set -e` turned that into
    exit 1 -- "bring-up failed". The verdict belongs to post_deploy_verify."""
    write_exec(deployment.pack.parent / "bin" / "docker", RACING_PROJECTION_DOCKER)
    result = deployment(verify_exit=0)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "WARN: projection reconcile did not complete" in out
    assert "deployment verified healthy" in out


def test_a_reconcile_failure_with_a_failing_verify_is_reported_as_three(deployment):
    """A genuinely broken projector is not hidden: verification still fails it, and the deploy
    reports the stack-up-with-issues state rather than success."""
    write_exec(deployment.pack.parent / "bin" / "docker", RACING_PROJECTION_DOCKER)
    assert deployment(verify_exit=1).returncode == 3


# --- step [0/6]: a failed engine-pin reconcile stops the deploy (okengine#991) -------------------

def _upgrade_exits(deployment, rc: int) -> None:
    """`framework upgrade` exits `rc`; every other $PYTHON call succeeds."""
    (deployment.pack / "engine.version").write_text("version: v9.8.6\n", encoding="utf-8")
    stub = deployment.pack.parent / "bin" / "python-stub"
    write_exec(stub, PYTHON_STUB.replace(
        "esac\nexit 0", f'esac\ncase " $* " in *" upgrade "*) exit {rc} ;; esac\nexit 0'))


def test_a_rolled_back_engine_upgrade_stops_the_deploy(deployment):
    """framework upgrade exits 1 when a migration failed and rolled back. validate passes a
    same-series pin, so swallowing that status deployed an un-migrated pack and reported success."""
    _upgrade_exits(deployment, 1)
    result = deployment()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[1/6]" not in result.stdout, "the deploy continued past a failed migration"
    assert "engine pin reconcile FAILED" in result.stderr


def test_an_ahead_or_unknown_pin_still_falls_through_to_validate(deployment):
    """okengine#359: exit 2 (nothing attempted -- ahead or unparseable pin) must not dead-end the
    deploy; the validate gate reports the actionable mismatch."""
    _upgrade_exits(deployment, 2)
    result = deployment()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[1/6] validate" in result.stdout
    assert "pin not reconciled" in result.stderr


# --- okengine#1042 [28]: a validate FAIL in step [0/6] is named as one, and --skip-validate reaches it

def _record_upgrade_args(deployment, rc: int) -> Path:
    (deployment.pack / "engine.version").write_text("version: v9.8.6\n", encoding="utf-8")
    log = deployment.pack.parent / "upgrade-args.log"
    stub = deployment.pack.parent / "bin" / "python-stub"
    write_exec(stub, PYTHON_STUB.replace(
        "esac\nexit 0",
        f'esac\ncase " $* " in *" upgrade "*) echo "$*" >> "{log}"; exit {rc} ;; esac\nexit 0'))
    return log


def test_an_upgrade_validate_fail_is_not_reported_as_a_migration_failure(deployment):
    _upgrade_exits(deployment, 3)
    result = deployment()
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[1/6]" not in result.stdout
    assert "framework validate FAILED" in result.stderr
    assert "a migration" not in result.stderr
    assert "--skip-validate" in result.stderr


def test_skip_validate_also_skips_the_upgrades_roll_forward_validate(deployment):
    """The documented escape for 'other FAILs' did not reach step [0/6], so a pre-existing FAIL
    rolled the pin back and aborted before --skip-validate was ever honored."""
    log = _record_upgrade_args(deployment, 0)
    result = deployment("--skip-validate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--no-validate" in log.read_text()


def test_without_skip_validate_the_upgrade_keeps_its_validate_gate(deployment):
    log = _record_upgrade_args(deployment, 0)
    assert deployment().returncode == 0
    assert "--no-validate" not in log.read_text()


# --- okengine#1042 [8]/[14]: project ownership and build provenance, as deploy.sh runs them -----

def _logging_docker(deployment, owners: str = "") -> Path:
    """The default stub, plus: `ps -a` reports `owners` as the project's working dirs, and every
    `compose up` records the build-tag variable it saw."""
    log = deployment.pack.parent / "docker.log"
    body = DOCKER_STUB.replace(
        'case "$1 $2" in',
        f'echo "$* BUILD_TAG=${{OKENGINE_ENGINE_BUILD_TAG:-}}" >> "{log}"\n'
        f'case "$1 $2" in\n  "ps -a") printf "{owners}" ; exit 0 ;;', 1)
    write_exec(deployment.pack.parent / "bin" / "docker", body)
    return log


def test_a_compose_project_another_directory_owns_stops_the_deploy(deployment):
    """[8]: two deployments named `pack` in different parents derive one compose project and one
    engine tag. The second deploy must not recreate the first one's containers."""
    log = _logging_docker(deployment, owners="/srv/elsewhere/pack\\n")
    result = deployment(verify_exit=0)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "already belongs to '/srv/elsewhere/pack'" in result.stderr
    assert "[4/6]" not in result.stdout
    assert "compose up" not in log.read_text(encoding="utf-8")


def test_this_directorys_own_project_deploys_and_labels_its_build(deployment):
    """[14]: deploy.sh exports the build tag for its own `compose up --build` only, so the image
    it builds carries the tag it runs under; .env never persists it for a later bare rebuild."""
    log = _logging_docker(deployment, owners=f"{deployment.pack}\\n")
    (deployment.pack / "docker-compose.yml").write_text(LEGACY_COMPOSE, encoding="utf-8")
    result = deployment(verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    up = [line for line in log.read_text(encoding="utf-8").splitlines()
          if line.startswith("compose up")]
    assert up == [f"compose up -d --build BUILD_TAG=okengine-v9.8.7-{SHA}.pack"]
    assert "OKENGINE_ENGINE_BUILD_TAG" not in (deployment.pack / ".env").read_text(encoding="utf-8")
    override = yaml.safe_load((deployment.pack / "docker-compose.okengine-image.yml").read_text())
    assert override["services"]["okengine-reader"]["build"]["labels"] == {
        "org.okengine.engine_image_tag": "${OKENGINE_ENGINE_BUILD_TAG:-unattributed}"}


def test_an_unanswerable_ownership_check_warns_and_continues(deployment):
    write_exec(deployment.pack.parent / "bin" / "docker", DOCKER_STUB.replace(
        'case "$1 $2" in', 'case "$1 $2" in\n  "ps -a") exit 1 ;;', 1))
    result = deployment(verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "undetectable, not a pass" in result.stderr


# --- okengine#1044 [25]: a host port another deployment publishes stops the deploy pre-compose --

def _port_docker(deployment, ps_line: str) -> Path:
    """A docker stub whose compose model publishes reader 9200 and whose `ps` shows `ps_line`;
    every invocation is logged so the test can tell whether `compose up` ran."""
    log = deployment.pack.parent / "docker.log"
    config = ('{"services": {"okengine-reader": {"ports": [{"host_ip": "127.0.0.1", "target": 9200, '
              '"published": "9200", "protocol": "tcp"}]}}}')
    write_exec(deployment.pack.parent / "bin" / "docker", f"""#!/bin/sh
echo "$*" >> "{log}"
case "$*" in
  *"config --format json"*)
    # Compose finds compose files in its cwd; --project-directory does not move that search.
    [ -f docker-compose.yml ] || {{ echo "no configuration file provided: not found" >&2; exit 1; }}
    echo '{config}' ; exit 0 ;;
  "ps --format"*) printf '%s\\n' "{ps_line}" ; exit 0 ;;
esac
case "$1 $2" in
  "image inspect") case "$*" in *org.okengine.git_sha*) echo "{SHA}" ;; esac; exit 0 ;;
  "compose ps") echo "container-id" ; exit 0 ;;
  "inspect --format") echo "2026-08-17T00:00:00.000000000Z" ; exit 0 ;;
esac
exit 0
""")
    return log


def test_a_host_port_another_deployment_holds_stops_the_deploy_before_compose_up(deployment):
    log = _port_docker(deployment, "other-stack\tother-reader\t0.0.0.0:9200->9200/tcp")
    result = deployment(verify_exit=0)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "host port conflict: okengine-reader wants 127.0.0.1:9200/tcp, held by other-reader" \
        in result.stderr
    assert "compose up" not in log.read_text(encoding="utf-8"), "compose up ran into the conflict"
    assert "[4/6]" not in result.stdout


def test_a_host_port_held_by_this_deployment_itself_deploys(deployment):
    _port_docker(deployment, "pack\tpack-reader\t127.0.0.1:9200->9200/tcp")
    result = deployment(verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "host port conflict" not in result.stderr


def test_an_unanswerable_port_check_warns_and_continues(deployment):
    """The default stub answers `compose config` with nothing: undetectable, never a pass."""
    result = deployment(verify_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "could not check this deployment's host ports" in result.stderr


def test_a_deploy_run_from_another_cwd_still_checks_the_packs_ports(deployment, tmp_path):
    """v0.15.14 audit: the documented `bash $ENGINE_DIR/scripts/deploy.sh /path/to/pack` runs from
    the deployer's cwd. `ports` ran compose there, found no compose file, and deploy.sh warned
    "undetectable" and brought the stack up into the conflict."""
    log = _port_docker(deployment, "other-stack\tother-reader\t0.0.0.0:9200->9200/tcp")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = deployment(str(deployment.pack), verify_exit=0, cwd=elsewhere)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "host port conflict: okengine-reader wants 127.0.0.1:9200/tcp, held by other-reader" \
        in result.stderr
    assert "could not check this deployment's host ports" not in result.stderr
    assert "compose up" not in log.read_text(encoding="utf-8"), "compose up ran into the conflict"
