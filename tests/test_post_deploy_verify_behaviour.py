"""`post_deploy_verify.sh` run for real against stubs — its verdicts, not its source text (#602).

`tests/test_post_deploy_verify.py` pins this script's SHAPE: that required strings are present and
that deploy.sh wires it in. That is worth keeping, and it is also the thing #602 was filed about —
a string assertion cannot tell "the check is gone" from "the check moved", which is how a literal
`'grep -Fxq "$COCKPIT"'` assertion failed a refactor that strictly improved the surface it guarded.

This file asserts what the script DOES. The verifier needs a live stack, so it gets a stub `docker`
on PATH and a scratch compose directory; every branch it then takes is its own.

The contract is small and load-bearing: **the exit status is a function of the FAIL tally, and the
FAIL tally is a function of the verdicts actually printed.** An operator reads the verdicts, a
script reads the status, and if those two ever disagree the whole verifier is decorative. Nothing
checked that they agree.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "scripts" / "post_deploy_verify.sh"

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")

# The verifier colours each verdict; matching the escape sequence rather than the bare word keeps
# remediation text that merely mentions "FAIL" out of the count.
VERDICT = {"PASS": "32", "WARN": "33", "FAIL": "31"}
SUMMARY = re.compile(r"^(\d+) pass, (\d+) warn, (\d+) fail$", re.M)


def verdict_count(output: str, name: str) -> int:
    return len(re.findall(f"\x1b\\[{VERDICT[name]}m{name}\x1b\\[0m", output))


def run_verifier(tmp_path: Path, docker_exit: int):
    """The real verifier, in a scratch compose dir, with a `docker` that always exits `docker_exit`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "docker"
    stub.write_text(f"#!/bin/sh\nexit {docker_exit}\n", encoding="utf-8")
    stub.chmod(0o755)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return subprocess.run(
        ["bash", str(VERIFY)], cwd=tmp_path, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )


@pytest.fixture(params=[0, 1], ids=["docker-succeeds-emptily", "docker-cannot-be-asked"])
def verified(request, tmp_path):
    """Two stub worlds that reach the summary by different routes."""
    result = run_verifier(tmp_path, request.param)
    output = result.stdout + result.stderr
    match = SUMMARY.search(output)
    assert match, f"the verifier printed no summary line:\n{output[-3000:]}"
    passed, warned, failed = (int(value) for value in match.groups())
    return result, output, passed, warned, failed


def test_the_tally_counts_exactly_the_verdicts_it_printed(verified):
    """The number a script acts on and the lines an operator reads must be the same evidence. A
    counter that drifts from the printed verdicts — a `bad` that forgets to increment, a check
    that prints FAIL through some other path — makes the exit status quietly wrong while the
    output still looks right."""
    _, output, passed, warned, failed = verified
    assert (verdict_count(output, "PASS"), verdict_count(output, "WARN"),
            verdict_count(output, "FAIL")) == (passed, warned, failed)


def test_the_exit_status_follows_the_fail_tally(verified):
    """`exit 0` means every required check passed. Both stub worlds produce FAILs, so both must
    exit 1 — and the assertion is written against the tally, not against the constant, so it stays
    correct if a future stub world produces none."""
    result, _, _, _, failed = verified
    assert result.returncode == (1 if failed else 0), (
        f"{failed} FAIL(s) reported but exited {result.returncode}"
    )


def test_warnings_are_reported_and_are_not_failures(verified):
    """WARN exists to say "look at this" without failing a deploy. If warnings leaked into the
    fail tally, every deployment with an unpublished MCP port would report as broken."""
    _, output, _, warned, failed = verified
    assert warned > 0, "neither stub world produced a WARN, so this proves nothing"
    assert verdict_count(output, "WARN") == warned
    # The verdict sentence names FAILs only. A WARN counted as a failure would show up as a fail
    # tally larger than the FAIL lines printed, which the tally test above would catch.
    assert "deployment has FAILs" in output
    assert failed == verdict_count(output, "FAIL"), "a WARN leaked into the fail tally"


def test_a_stack_that_cannot_be_reached_fails_rather_than_passing_vacuously(verified):
    """The whole point of the verifier. A stub docker answers nothing useful, so a verifier that
    treated silence as success would sail through with zero FAILs — which is precisely the
    "silence is never success" rule the deployment gates are built on."""
    result, _, _, _, failed = verified
    assert failed > 0 and result.returncode == 1


def test_running_outside_a_deployment_directory_is_a_distinct_status(tmp_path):
    """2 is not 1: "you ran this in the wrong place" is not "your deployment is broken", and a
    scripted caller has to be able to tell them apart."""
    result = subprocess.run(["bash", str(VERIFY)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 2
    assert SUMMARY.search(result.stdout) is None, "it summarised a run it never made"


# --- what this file still cannot reach, stated rather than implied -------------------------------

def test_the_healthy_branch_is_pinned_at_the_source_because_no_stub_reaches_it():
    """ANNOTATION, per okengine#602's third acceptance item — a source assertion labelled with what
    it cannot detect.

    The `fail == 0` branch needs a stub stack convincing enough to satisfy every check (reader
    healthz, MCP read and write, policy digest, cron-plus ticking, qmd index, image provenance).
    Both stub worlds above produce FAILs, so the tests here only ever observe the failing side, and
    the passing side is asserted from the source.

    CANNOT DETECT: that the branch is reachable, that `exit 0` is what runs, or that the success
    message is printed. It detects only that the comparison is still against zero and still decides
    between the two exits. A real fail-free run — the fixture #602 option (1) describes — is what
    would close the gap; live evidence today comes from actual deployments (okcti-test reported
    "27 pass, 3 warn, 0 fail" and exited 0).
    """
    tail = VERIFY.read_text(encoding="utf-8").splitlines()[-1]
    assert '[ "$fail" -eq 0 ]' in tail, "the verdict no longer keys off the fail tally"
    assert "exit 0" in tail and "exit 1" in tail, "the two exits are no longer both present"


# ---- host-side checks must not inherit CONTAINER-scoped .env values ----------------------------

def _stub_docker(bindir: Path, body: str) -> None:
    bindir.mkdir(exist_ok=True)
    stub = bindir / "docker"
    stub.write_text("#!/bin/sh\n" + body + "\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)


def _policy_vault(tmp_path: Path, extensions_yaml: str) -> Path:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "extensions.yaml").write_text(extensions_yaml, encoding="utf-8")
    return tmp_path


def _run(tmp_path: Path, extra_env: dict | None = None) -> str:
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", **(extra_env or {})}
    result = subprocess.run(["bash", str(VERIFY)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=300, env=env)
    return result.stdout + result.stderr


def test_the_policy_digest_is_computed_without_the_containers_engine_root(tmp_path):
    """.env carries OKENGINE_ENGINE_ROOT=/opt/data for the GATEWAY. Inherited by the host-side
    composer it hid every tier-1 extension, the digest came back empty, and an identical policy
    was reported as drift -- on every roll of the deployment that sets it."""
    vault = _policy_vault(tmp_path, "enabled:\n  okengine.dedupe: {}\n")
    digest = subprocess.run(
        ["python3", str(REPO / "tools" / "policy_plane.py"), "digest", "--vault", str(vault)],
        capture_output=True, text=True, timeout=120, check=True,
        env={**os.environ, "OKENGINE_POLICY_CATALOG": str(REPO / "config/policy/catalog.yaml")},
    ).stdout.strip()
    assert digest, "fixture: the composer must produce a digest for this vault"
    (vault / ".okengine" / "effective-policy.json").write_text(f'{{"digest": "{digest}"}}\n',
                                                               encoding="utf-8")
    (vault / ".env").write_text("OKENGINE_ENGINE_ROOT=/opt/data\n", encoding="utf-8")
    _stub_docker(vault / "bin", "")
    out = _run(vault)
    assert f"runtime policy digest matches composed source ({digest})" in out, out[-3000:]
    assert "digest drift" not in out


def test_a_digest_that_cannot_be_computed_is_not_reported_as_drift(tmp_path):
    vault = _policy_vault(tmp_path, "enabled:\n  - not-a-mapping\n")
    (vault / ".okengine" / "effective-policy.json").write_text('{"digest": "abc"}\n', encoding="utf-8")
    _stub_docker(vault / "bin", "")
    out = _run(vault)
    assert "cannot compute the composed policy digest to compare (runtime=abc)" in out, out[-3000:]
    assert "must be a mapping" in out, "the composer's own reason is shown, not swallowed"
    assert "digest drift" not in out


def test_a_projection_health_check_that_times_out_says_so(tmp_path):
    """The health scan walks the whole corpus (158s on a 72k-file vault). A fixed 120s budget
    killed a healthy check and printed an empty reason under 'conformance failed'."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    _stub_docker(tmp_path / "bin", (
        'case "$*" in\n'
        '  *"config --services"*|*"--services"*) printf "okengine-projection\\npostgres\\n" ;;\n'
        '  *"exec -T okengine-projection"*) sleep 5 ;;\n'
        'esac'))
    out = _run(tmp_path, {"OKENGINE_VERIFY_PROJECTION_TIMEOUT": "1"})
    assert "projection health check timed out after 1s (no verdict)" in out, out[-3000:]
    assert "projection health/conformance failed" not in out
