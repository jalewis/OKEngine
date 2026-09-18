"""okengine — ordered gateway bring-up (scripts/staggered-start.sh).

`restart: unless-stopped` is mandatory and it starts every gateway on the same tick at
boot; each then wakes a scheduler holding hours of overdue lanes. On 2026-08-14 that put
a 12-core host at loadavg 63, the kernel OOM killer ran, the host rebooted, and two
containers that had already been killed were never restored — one vault went 43 hours
without an update and nothing reported it.

These tests pin the properties that make the ordering safe, in the offline text-asserting
style of test_deploy_ownership_hardening.py: no live stack required.
"""
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "staggered-start.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_the_script_is_executable_and_parses():
    assert SCRIPT.is_file()
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_it_refuses_to_run_with_no_deployments():
    """A no-arg run must not silently 'succeed' having ordered nothing."""
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "PRIORITY ORDER" in proc.stderr


def test_order_comes_from_the_caller_not_the_engine():
    """The engine ships no deployment-specific paths (engine/pack boundary)."""
    assert "/home/" not in TEXT
    assert "okpack-" not in TEXT
    assert "okcti" not in TEXT
    assert 'for dir in "$@"' in TEXT


def test_a_gateway_is_resolved_by_compose_LABEL_not_by_name():
    """Container names collide across hosts and stacks; the label ties it to THIS dir."""
    body = re.search(r"gateway_of\(\)\s*\{(.+?)\n\}", TEXT, re.S)
    assert body, "gateway_of() not found"
    assert "com.docker.compose.project.working_dir" in body.group(1)
    assert "com.docker.compose.service=gateway" in body.group(1)
    assert "--filter name=" not in body.group(1)


def test_a_deployment_reached_by_two_path_spellings_still_resolves():
    """This host reaches one tree as ~/Source and ~/MEGA/Source. Compose records whichever
    spelling the caller used, so an exact label-string match on the other one finds
    nothing and skips the deployment SILENTLY — unordered and unreported."""
    body = re.search(r"gateway_of\(\)\s*\{(.+?)\n\}", TEXT, re.S).group(1)
    assert body.count("readlink -f") >= 2, \
        "both the argument and the recorded label must be resolved to a physical path"
    assert 'label=com.docker.compose.project.working_dir=' not in body, \
        "an exact label-string filter cannot match the other spelling of the same tree"


def test_an_unresolvable_deployment_is_reported_not_silently_dropped():
    assert "SKIP   no gateway container for:" in TEXT
    assert "SKIP   invalid deployment" in TEXT
    assert "no gateways resolved" in TEXT


def test_ready_means_the_scheduler_is_ticking_not_merely_running():
    """'Up' proves a process exists. The next gateway competes with a SCHEDULER."""
    body = re.search(r"\nready\(\)\s*\{(.+?)\n\}", TEXT, re.S)
    assert body, "ready() not found"
    assert "tick_mtime" in body.group(1)
    assert "{{.State.Running}}" in body.group(1)


def test_readiness_requires_a_NEW_tick_not_the_one_that_survived_the_stop():
    """The first live run passed every gate in the same second it started each gateway.

    .tick.lock is a file on the host and it outlives the container. A restarted gateway
    still has one stamped from before the stop, so an age-based check ('ticked within
    180s') reports ready having verified nothing. Readiness must be a tick STRICTLY
    NEWER than the one observed before stopping — only the new process can produce that.
    """
    body = re.search(r"\nready\(\)\s*\{(.+?)\n\}", TEXT, re.S).group(1)
    assert '-gt "$baseline"' in body, "readiness must compare against a pre-stop baseline"
    assert "TICK_MAX_AGE" not in body, "an age window passes on the surviving tick file"
    # the baseline has to be captured before anything is stopped, or it is already lost
    assert TEXT.index("baselines+=(") < TEXT.index('docker stop "${cids[$i]}"')


def test_a_deployment_with_no_scheduler_says_so_rather_than_implying_it_checked_one():
    assert "no scheduler to check" in TEXT


def test_a_readiness_timeout_is_reported_and_never_counted_as_ready():
    body = re.search(r"wait_ready\(\)\s*\{(.+?)\n\}", TEXT, re.S)
    assert body, "wait_ready() not found"
    assert "TIMEOUT" in body.group(1)
    assert "return 1" in body.group(1)


def test_every_stopped_gateway_is_restarted_on_any_exit_path():
    """`unless-stopped` does NOT undo a deliberate stop: dying mid-run would leave the
    held gateways down indefinitely — the exact 43-hour outage this script prevents."""
    assert re.search(r"^restore\(\)\s*\{", TEXT, re.M), "restore() not found"
    trap = re.search(r"^trap restore (.+)$", TEXT, re.M)
    assert trap, "no trap installed"
    for signal in ("EXIT", "INT", "TERM"):
        assert signal in trap.group(1), f"trap does not cover {signal}"
    assert TEXT.index("trap restore") < TEXT.index('docker stop "${cids[$i]}"'), \
        "the trap must be installed BEFORE anything is stopped"


def test_it_serializes_against_itself():
    """A @reboot run and a manual run must never interleave stops and starts."""
    assert "flock -n 9" in TEXT
    assert "okengine-staggered-start.lock" in TEXT


def test_it_waits_for_the_docker_daemon():
    """At @reboot this script can beat dockerd; a failed probe is not 'no gateways'."""
    assert "docker info" in TEXT
    assert "docker unavailable" in TEXT


def test_the_first_deployment_is_never_stopped():
    """The priority deployment keeps the host to itself; it is not stopped and restarted."""
    loop = re.search(r"# Hold everything after the first.*?\ndone", TEXT, re.S)
    assert loop, "hold loop not found"
    assert '[ "$i" -eq 0 ] && continue' in loop.group(0)
