"""okengine#193 / #67 / #557 — offline regression for post_deploy_verify.sh gates that used to be
blind, driven with a controllable fake `docker` on PATH (NO live stack — the sibling
test_post_deploy_verify.py needs one and is normally skipped).

  #5  the cron-plus ownership gate must stat jobs.json itself (file-level), matching the
      deployment_validate #193 guard — a root-owned jobs.json inside a well-owned dir is the exact
      fleet-stall poison and must FAIL here (this is the only gate reachable when the fleet stalls).
  #23 the qmd check must probe writability so a PERMANENTLY unwritable /opt/data/qmd (PermissionError,
      index empty forever) is distinguished from a benign still-building index — and must NOT point at
      a non-existent "corpus-indexer cron".
  #557 the ownership gate must SWEEP the runtime tree, not spot-check two paths. #5 above stats only
      cron-plus/ and jobs.json, so it stayed green while 774 root-owned files accumulated across the
      fleet for a week — a gateway that could not open agent.log at all, and cron-plus/runs/<id>/
      dirs silently dropping lane receipts.
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
  *vault-ownership-peer*)
      case "${FAKE_VAULT_OWNER_STATE:-ok}" in
        fail) printf 'FAIL\townership\twiki/operational/INDEX.md (file, uid 0) not owned by lane uid\n' ;;
        err)  printf 'ERR\townership\tshared check unavailable\n' ;;
        empty) exit 1 ;;
        *)    printf 'OK\townership\tall lane-maintained vault paths owned by the gateway lane uid\n' ;;
      esac
      exit 0 ;;
  *python3*jobs.json*|*jobs.json*python3*) echo "${FAKE_NJOBS:-5}"; exit 0 ;;
  *stat\ -c*jobs.json*)             [ -n "${FAKE_UID_EMPTY:-}" ] && exit 1; echo "${FAKE_JOB_UID:-1003}"; exit 0 ;;
  *stat\ -c*cron-plus*)             [ -n "${FAKE_UID_EMPTY:-}" ] && exit 1; echo "${FAKE_DIR_UID:-1003}"; exit 0 ;;
  # 5d sweep (okengine#557). Order matters: the "! -uid" forms must match BEFORE the plain
  # total-count form, and the count/sample forms are told apart by wc-vs-head.
  *find\ /opt/data\ !\ -uid*wc*)    [ -n "${FAKE_SWEEP_EMPTY:-}" ] && exit 1; echo "${FAKE_STRAY_COUNT:-0}"; exit 0 ;;
  *find\ /opt/data\ !\ -uid*head*)  printf '%s\n' "${FAKE_STRAY_PATHS:-/opt/data/logs/agent.log}"; exit 0 ;;
  *find\ /opt/data*wc*)             echo "${FAKE_TOTAL_PATHS:-79810}"; exit 0 ;;
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


# --- #557: whole-tree ownership sweep -----------------------------------------------------------
def test_mis_owned_files_below_a_well_owned_dir_fail_the_sweep(tmp_path):
    """The exact okengine#557 shape: cron-plus/ and jobs.json are FINE, the tree underneath is not.

    5c stats only those two paths, so it stayed green while 774 root-owned files accumulated across
    the fleet for a week -- one gateway unable to open agent.log at all, and root-owned
    cron-plus/runs/<id>/ dirs silently dropping lane receipts. The sweep is what makes that loud.
    """
    _, out = _run(tmp_path, FAKE_WANT_UID=1003, FAKE_DIR_UID=1003, FAKE_JOB_UID=1003,
                  FAKE_STRAY_COUNT=774, FAKE_STRAY_PATHS="/opt/data/logs/agent.log")
    # the spot-checks still pass -- proving the sweep is what caught it, not 5c
    assert "runtime dir + jobs.json owned by the gateway uid" in out, out
    assert "774 file(s) under /opt/data are NOT owned by the gateway uid 1003" in out, out
    assert "/opt/data/logs/agent.log" in out, out
    assert "FAIL" in out, out
    # the remediation must NOT recommend the world-writable shortcut
    assert "chown -R 1003:1003" in out, out
    assert "Do NOT use ensure-runtime.sh --fix-perms" in out, out


def test_fully_owned_tree_passes_the_sweep(tmp_path):
    _, out = _run(tmp_path, FAKE_WANT_UID=1003, FAKE_DIR_UID=1003, FAKE_JOB_UID=1003,
                  FAKE_STRAY_COUNT=0, FAKE_TOTAL_PATHS=79810)
    assert "runtime tree fully owned by the gateway uid (1003)" in out, out
    assert "79810 paths swept" in out, out
    assert "NOT owned by the gateway uid" not in out, out


def test_root_owned_vault_page_fails_even_when_scheduler_peers_pass(tmp_path):
    """Negative fixture: the root-owned INDEX survives cron-plus probes but post-deploy fails."""
    _, out = _run(tmp_path, FAKE_VAULT_OWNER_STATE="fail", FAKE_DIR_UID=1003,
                  FAKE_JOB_UID=1003, FAKE_STRAY_COUNT=0)
    assert "runtime dir + jobs.json owned by the gateway uid" in out, out
    assert "vault ownership (scheduler-independent peer)" in out, out
    assert "[ownership] wiki/operational/INDEX.md" in out and "FAIL" in out, out


def test_vault_ownership_peer_passes_and_unavailable_is_not_vacuous_green(tmp_path):
    healthy_dir = tmp_path / "healthy"
    healthy_dir.mkdir()
    _, healthy = _run(healthy_dir, FAKE_VAULT_OWNER_STATE="ok")
    assert "all lane-maintained vault paths owned" in healthy, healthy
    unavailable_dir = tmp_path / "unavailable"
    unavailable_dir.mkdir()
    _, unavailable = _run(unavailable_dir, FAKE_VAULT_OWNER_STATE="empty")
    assert "cannot verify vault ownership" in unavailable, unavailable
    assert "UNDETECTABLE here, not a pass" in unavailable, unavailable
    assert "all lane-maintained vault paths owned" not in unavailable, unavailable


def test_unrunnable_sweep_probe_warns_undetectable_rather_than_passing(tmp_path):
    """An empty probe means nothing was measured. Reporting PASS there is the vacuous green the
    repo's "missing key = WARN undetectable, never a vacuous pass" rule forbids -- and it is the
    failure mode this gate exists to catch, so it must not self-silence."""
    _, out = _run(tmp_path, FAKE_WANT_UID=1003, FAKE_DIR_UID=1003, FAKE_JOB_UID=1003,
                  FAKE_SWEEP_EMPTY="1")
    assert "cannot sweep runtime-tree ownership" in out, out
    assert "UNDETECTABLE here, not a pass" in out, out
    assert "runtime tree fully owned" not in out, out


# --- okengine#665: secret file modes ------------------------------------------------------------
def test_group_or_world_readable_env_fails_the_secret_mode_gate(tmp_path):
    (tmp_path / ".env").write_text("OKENGINE_MCP_TOKEN=x\n")
    (tmp_path / ".env").chmod(0o644)
    _rc, out = _run(tmp_path)
    assert ".env is mode 644" in out and "readable by group/other" in out


def test_owner_only_env_passes_and_world_readable_config_warns(tmp_path):
    (tmp_path / ".env").write_text("OKENGINE_MCP_TOKEN=x\n")
    (tmp_path / ".env").chmod(0o600)
    rt = tmp_path / ".hermes-data"
    rt.mkdir(exist_ok=True)
    (rt / "config.yaml").write_text("mcp_servers: {}\n")
    (rt / "config.yaml").chmod(0o644)
    _rc, out = _run(tmp_path)
    assert ".env is owner-only" in out
    assert "config.yaml is mode 644" in out and "Bearer token is world-readable" in out
