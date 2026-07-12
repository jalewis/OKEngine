"""okengine#193 / #67 — offline regression for two post_deploy_verify.sh gates that used to be
blind, driven with a controllable fake `docker` on PATH (NO live stack — the sibling
test_post_deploy_verify.py needs one and is normally skipped).

  #5  the cron-plus ownership gate must stat jobs.json itself (file-level), matching the
      deployment_validate #193 guard — a root-owned jobs.json inside a well-owned dir is the exact
      fleet-stall poison and must FAIL here (this is the only gate reachable when the fleet stalls).
  #23 the qmd check must probe writability so a PERMANENTLY unwritable /opt/data/qmd (PermissionError,
      index empty forever) is distinguished from a benign still-building index — and must NOT point at
      a non-existent "corpus-indexer cron".
"""
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VERIFY = REPO / "scripts" / "post_deploy_verify.sh"

# A fake `docker` whose per-check answers are driven by env vars. Defaults model a HEALTHY deploy
# (dir + jobs.json owned by 1003, index populated & writable); each test overrides one var.
FAKE_DOCKER = r"""#!/usr/bin/env bash
args="$*"
case "$args" in
  "compose ps"*)   printf 'gateway\nokengine-mcp\nokengine-reader\n'; exit 0 ;;
  "compose port"*) exit 0 ;;                       # unpublished -> skips reader/mcp curl probes
  "compose exec"*) : ;;                            # fall through to command dispatch
  *) exit 0 ;;
esac
case "$args" in
  *okengine-write*)                 exit 0 ;;      # write path registered
  *write_server.py*)                exit 0 ;;      # write_server present
  *.pdv_wtest*)                     exit "${FAKE_QMD_WTEST_RC:-0}" ;;   # writability probe
  *qmd\ status*)  echo "${FAKE_NDOCS:-42}"; exit 0 ;;   # already post-pipeline (grep runs in sh -c)
  *HERMES_UID*)                     [ -n "${FAKE_UID_EMPTY:-}" ] && exit 1; echo "${FAKE_WANT_UID:-1003}"; exit 0 ;;
  *python3*jobs.json*|*jobs.json*python3*) echo "${FAKE_NJOBS:-5}"; exit 0 ;;
  *stat\ -c*jobs.json*)             [ -n "${FAKE_UID_EMPTY:-}" ] && exit 1; echo "${FAKE_JOB_UID:-1003}"; exit 0 ;;
  *stat\ -c*cron-plus*)             [ -n "${FAKE_UID_EMPTY:-}" ] && exit 1; echo "${FAKE_DIR_UID:-1003}"; exit 0 ;;
  *.tick.lock*)                     exit 0 ;;
  *cron-plus*)                      exit 0 ;;       # config.yaml grep for cron-plus plugin
  *) exit 0 ;;
esac
"""


def _run(tmp_path, **env_overrides):
    """Run the real verifier in a throwaway deploy dir with the fake docker shadowing PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fd = bindir / "docker"
    fd.write_text(FAKE_DOCKER)
    fd.chmod(0o755)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    import os

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env.update({k: str(v) for k, v in env_overrides.items()})
    r = subprocess.run(
        ["bash", str(VERIFY)], cwd=tmp_path, capture_output=True, text=True, env=env
    )
    return r.returncode, r.stdout + r.stderr


pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")


# --- #5: file-level jobs.json ownership -------------------------------------------------------
def test_root_owned_jobs_json_fails_the_ownership_gate(tmp_path):
    """dir owned by lane uid but jobs.json is root -> the #193 poison must FAIL here."""
    _, out = _run(tmp_path, FAKE_WANT_UID=1003, FAKE_DIR_UID=1003, FAKE_JOB_UID=0)
    assert "jobs.json owned by uid 0" in out, out
    assert "okengine#193" in out
    # it must be a FAIL (red), not a silent PASS
    assert "FAIL" in out


def test_well_owned_jobs_json_passes(tmp_path):
    """dir AND jobs.json owned by the gateway uid -> PASS, no ownership FAIL."""
    _, out = _run(tmp_path, FAKE_WANT_UID=1003, FAKE_DIR_UID=1003, FAKE_JOB_UID=1003)
    assert "jobs.json owned by uid" not in out, out
    assert "runtime dir + jobs.json owned by the gateway uid" in out


# --- #23: qmd writability probe + honest remediation --------------------------------------------
def test_empty_and_unwritable_qmd_is_a_permission_fail(tmp_path):
    """0 docs + non-writable qmd dir -> a permanent-permission FAIL, not a 'wait' WARN."""
    _, out = _run(tmp_path, FAKE_NDOCS=0, FAKE_QMD_WTEST_RC=1)
    assert "NOT writable" in out, out
    assert "PermissionError" in out
    assert "corpus-indexer" not in out  # the false remedy is gone everywhere


def test_empty_but_writable_qmd_is_a_still_building_warn(tmp_path):
    """0 docs + writable qmd dir -> a benign 'still building' WARN, no permission FAIL."""
    _, out = _run(tmp_path, FAKE_NDOCS=0, FAKE_QMD_WTEST_RC=0)
    assert "still building" in out, out
    assert "NOT writable" not in out
    assert "corpus-indexer" not in out


def test_populated_qmd_index_passes(tmp_path):
    _, out = _run(tmp_path, FAKE_NDOCS=137)
    assert "qmd index ready (137 files indexed)" in out, out


# --- #48: ownership gate must not report a vacuous PASS when the gateway is not exec-able ---------
def test_unexecable_gateway_warns_not_vacuous_pass(tmp_path):  # invariant-audit #48
    """When `docker compose exec` fails (gateway crash-looping/stopped — the very uid-desync 5c
    hunts), the uid probes come back EMPTY. The gate must WARN 'cannot verify ... undetectable', NOT
    print a green PASS with uid '?', which violates the repo's 'missing key = WARN, never a vacuous
    pass' rule in the one gate reachable when the fleet stalls."""
    _, out = _run(tmp_path, FAKE_UID_EMPTY=1)
    assert "cannot verify runtime ownership" in out and "not a pass" in out, out
    assert "owned by the gateway uid (?)" not in out, "still reports a vacuous PASS with uid '?'"
