"""ci/prepull_base_images.sh — fetch the release stack's base images before the build.

The job it guards failed four ways at once on main (smoke/e2e/resilience/performance-release), all
at the base-image metadata step, before a line of project code ran. The mechanism (okengine#556):
Docker Hub redirects blob fetches to either production.cloudFLARE.docker.com or
production.cloudFRONT.docker.com, chosen per request, and the second was unreachable here -- so the
same pull passed or failed at random and read as flake.

Why it was unreachable was misdiagnosed for weeks as CloudFront being IPv6-only. It is not: on
2026-09-13 it resolved to IPv4 from the runner host. A DNS filter on this network was withholding
its A record by blocking cloudfront.net (okengine#751). The diagnosis below never depended on that
theory -- it measures whether an A record exists, which is why it stayed correct while the prose
around it was wrong.

Disabling IPv6 from the dind entrypoint was never the fix: measured 2026-08-22, privileged the write
SUCCEEDS and the pull still fails; it only changes the errno.

The contracts worth pinning are the ones that keep this from becoming another silent pass: the
digests come from the Dockerfiles (not a second copy that desyncs), exhausting the retries FAILS,
and finding nothing to pull FAILS rather than reporting success over an empty set.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "ci" / "prepull_base_images.sh"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="prepull script absent")


def _run(tmp_path, docker_body, extra_env=None, cwd=None):
    """Run the script with a fake `docker` on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text("#!/usr/bin/env bash\n" + docker_body, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
               PREPULL_ATTEMPTS="3", PREPULL_SLEEP="0")
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCRIPT)], cwd=str(cwd or REPO), env=env,
                          capture_output=True, text=True, timeout=60)


def test_the_script_is_executable():
    """before_script invokes it as ./ci/prepull_base_images.sh — a non-executable file fails the
    job with 'Permission denied', which looks like a CI config error rather than this."""
    assert SCRIPT.stat().st_mode & stat.S_IEXEC


def test_a_successful_pull_exits_zero_and_names_what_it_pulled(tmp_path):
    result = _run(tmp_path, "exit 0\n")
    assert result.returncode == 0, result.stderr
    assert "prepull: ok" in result.stdout
    assert "python:3.13-slim-trixie@sha256:" in result.stdout, (
        "the digest must be reported, so a silently changed pin is visible in the job log")


def test_exhausting_every_retry_fails_the_job(tmp_path):
    """The whole point. A base image that never arrived must not let the gate run and report."""
    result = _run(tmp_path, "exit 1\n")
    assert result.returncode != 0
    assert "FAILED after 3 attempts" in result.stderr
    assert "diagnosis" in result.stderr, "the failure must run the measured diagnosis, not just fail"


def test_it_retries_and_recovers(tmp_path):
    """Hub redirects to one CDN or the other per request, so a later attempt genuinely can land on
    the reachable one. Retrying the FETCH is legitimate; retrying a test until it passes is not."""
    counter = tmp_path / "n"
    result = _run(tmp_path,
                  f'n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n'
                  'test "$n" -ge 2\n')
    assert result.returncode == 0, result.stderr
    assert "attempt 1/3 failed" in result.stderr
    assert "ok (attempt 2/3)" in result.stdout


def test_finding_no_images_fails_instead_of_reporting_success_over_nothing(tmp_path):
    """Run where the Dockerfiles do not exist. An empty image set means the script measured
    nothing, and 'pulled everything I found' over an empty set is a vacuous pass — the exact shape
    this script exists to stop."""
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    result = _run(tmp_path, "exit 0\n", cwd=empty)
    assert result.returncode != 0
    assert "no FROM lines found" in result.stderr
    assert "refusing" in result.stderr


def test_the_pinned_digest_matches_the_dockerfiles():
    """The script greps the Dockerfiles precisely so there is no second copy of the pin to desync.
    This asserts the set it would read is non-empty and fully digest-pinned — a floating tag would
    make the release stack irreproducible and reintroduce the metadata fetch this avoids."""
    files = ["okengine-reader/Dockerfile", "okengine-cockpit/Dockerfile",
             "okengine-mcp/Dockerfile", "okengine-mcp/Dockerfile.review",
             "okengine-operations/Dockerfile", "tests/e2e/smoke/Dockerfile.fault-gateway"]
    found = set()
    for rel in files:
        path = REPO / rel
        if not path.is_file():
            continue
        found.update(re.findall(
            r"^ARG +PYTHON_BASE_IMAGE=(\S+)", path.read_text(encoding="utf-8"), re.M))
    assert found, "no base images found in the release-stack Dockerfiles"
    assert all("@sha256:" in image for image in found), f"unpinned base image(s): {sorted(found)}"


def _pulled(result) -> list[str]:
    """The images the script reported pulling, in order. It logs `prepull: ok (attempt i/n) IMAGE`
    for each, so this is the exact set -- not a substring search over the whole log, which is how
    the test below used to pass for the wrong reason."""
    return [line.rsplit(" ", 1)[-1] for line in result.stdout.splitlines()
            if line.startswith("prepull: ok ")]


def test_with_both_ci_overrides_only_the_mirrors_are_pulled(tmp_path):
    """Release CI sets both. Then NOTHING may come from a Dockerfile default -- those are the public
    images CI exists to avoid. This previously asserted only that "docker.io" was absent from the
    log, which a default like `node@sha256:...` satisfies while still being pulled from Docker Hub;
    it compares the exact set of pulled images instead."""
    py = "registry.example/base/python:readable@sha256:" + "a" * 64
    node = "registry.example/base/node:readable@sha256:" + "b" * 64
    result = _run(tmp_path, "exit 0\n",
                  extra_env={"OKENGINE_PYTHON_BASE_IMAGE": py, "OKENGINE_NODE_BASE_IMAGE": node})
    assert result.returncode == 0, result.stderr
    assert sorted(_pulled(result)) == sorted([py, node]), result.stdout


def test_an_override_replaces_its_own_default_and_no_other(tmp_path):
    """Each override stands in for ONE image. Setting only the Python mirror must not suppress the
    Node base image -- a build still needs it, and silently skipping the prepull would move its
    fetch into the build, where a CDN failure reads as a test failure again."""
    py = "registry.example/base/python:readable@sha256:" + "a" * 64
    result = _run(tmp_path, "exit 0\n", extra_env={"OKENGINE_PYTHON_BASE_IMAGE": py})
    assert result.returncode == 0, result.stderr
    pulled = _pulled(result)
    assert py in pulled
    assert not any(image.startswith("python:") for image in pulled), "the Python default leaked in"
    assert any(image.startswith("node@sha256:") for image in pulled), "the Node base was dropped"


def test_the_node_base_image_is_prepulled_from_the_dockerfiles(tmp_path):
    """Without an override, the Node base comes from the Dockerfiles' own ARG, exactly as Python's
    does -- so a Node pin bump cannot desync from what gets prepulled."""
    result = _run(tmp_path, "exit 0\n")
    assert result.returncode == 0, result.stderr
    assert any(image.startswith("node@sha256:") for image in _pulled(result)), result.stdout


def test_ci_override_must_remain_digest_pinned(tmp_path):
    result = _run(tmp_path, "exit 0\n",
                  extra_env={"OKENGINE_PYTHON_BASE_IMAGE": "registry.example/base/python:latest"})
    assert result.returncode != 0
    assert "not digest-pinned" in result.stderr


# --- the measured diagnosis (okengine#556) -------------------------------------------------------
#
# The message this replaced asserted "IPv6 is enabled in the dind namespace" on ANY pull failure. It
# happened to be pointing at the right area, but it was a fixed string, not a measurement -- it would
# have said the same thing about an expired token or a moved digest. These tests pin that the
# diagnosis MEASURES, and in particular that it is capable of saying "not this".

def _diagnose(tmp_path, *, cloudfront_has_a, v6_route, resolver="10.11.12.13"):
    """Run the script with a failing docker and a SIMULATED resolver, so the diagnosis is testable
    without depending on the live network -- a test that only runs on one LAN never runs in CI."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    a_branch = ('echo "1.2.3.4 STREAM $2"' if cloudfront_has_a else "exit 2")
    (bindir / "getent").write_text(
        "#!/bin/sh\ncase \"$1:$2\" in\n"
        f"  ahostsv4:production.cloudfront.docker.com) {a_branch} ;;\n"
        '  ahostsv4:*) echo "1.2.3.4 STREAM $2" ;;\n'
        '  ahostsv6:production.cloudfront.docker.com) echo "2600:9000::1 STREAM $2" ;;\n'
        '  ahostsv6:*) echo "::ffff:1.2.3.4 STREAM $2" ;;\n'
        "esac\n", encoding="utf-8")
    # `ip -6 route show default` printing nothing is how "no default route" looks.
    (bindir / "ip").write_text(
        "#!/bin/sh\n" + ('echo "default via fe80::1 dev eth0"\n' if v6_route else ""), encoding="utf-8")
    for name in ("docker", "getent", "ip"):
        (bindir / name).chmod(0o755)
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(f"nameserver {resolver}\n", encoding="utf-8")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
               PREPULL_ATTEMPTS="1", PREPULL_SLEEP="0",
               PREPULL_GETENT=str(bindir / "getent"), PREPULL_IP=str(bindir / "ip"),
               PREPULL_RESOLV=str(resolv))
    return subprocess.run(["bash", str(SCRIPT)], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=60)


def test_the_diagnosis_names_the_host_without_an_a_record(tmp_path):
    """The okengine#556 condition. Naming the host is the whole value: the failure otherwise reads
    as a generic image-pull error and gets retried for weeks."""
    result = _diagnose(tmp_path, cloudfront_has_a=False, v6_route=False)
    assert result.returncode != 0
    assert "production.cloudfront.docker.com" in result.stderr
    assert "NO A RECORD" in result.stderr
    assert "ENVIRONMENT failure" in result.stderr
    assert "okengine#556" in result.stderr


def test_the_diagnosis_names_the_resolver_that_answered(tmp_path):
    """Measured 2026-08-22: the fix was applied to one LAN resolver while the runner queried a
    different one, so the name still failed. Without the nameserver in the output, the only place it
    appeared was the raw docker error -- and the wrong box got fixed."""
    result = _diagnose(tmp_path, cloudfront_has_a=False, v6_route=False, resolver="10.9.9.9")
    assert "resolver(s) in use: 10.9.9.9" in result.stderr
    assert "NAMED ABOVE" in result.stderr, "it must point at the resolver it actually measured"


def test_the_diagnosis_refuses_to_blame_ipv6_when_every_host_resolves(tmp_path):
    """The anti-rubber-stamp test, and the reason this is a measurement rather than a string. A
    diagnosis that always names the same cause is worth nothing as evidence."""
    result = _diagnose(tmp_path, cloudfront_has_a=True, v6_route=False)
    assert result.returncode != 0, "the pull still failed; only the ATTRIBUTION should change"
    assert "is NOT the cause" in result.stderr
    assert "ENVIRONMENT failure" not in result.stderr
    assert "credentials" in result.stderr, "it should point somewhere useful instead"


def test_an_ipv6_route_makes_a_missing_a_record_survivable(tmp_path):
    """An IPv6-only host is only fatal without IPv6. With a route it is reachable, so the diagnosis
    must not claim the fetch 'cannot succeed' -- that would send the reader after the wrong fix."""
    result = _diagnose(tmp_path, cloudfront_has_a=False, v6_route=True)
    assert "HAS an IPv6 route" in result.stderr
    assert "cannot succeed" not in result.stderr


def test_the_diagnosis_only_runs_when_something_actually_failed(tmp_path):
    """It is failure evidence. Printing it on a clean run would train everyone to ignore it."""
    result = _run(tmp_path, "exit 0\n")
    assert result.returncode == 0
    assert "environment diagnosis" not in result.stderr
