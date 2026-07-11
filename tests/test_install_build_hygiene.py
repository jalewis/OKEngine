"""invariant-audit batch 7 — install / build / verify hygiene.

These guard host-side install + image-build scripts against silent no-ops and integrity gaps that a
unit suite otherwise never exercises (they run outside Docker, on the operator's host):
  B7.1 install-cron-plus.sh  — git ops on the managed clone must not brick on "dubious ownership"
  B7.2 install-extract-cron.sh — refuse to install a host cron with no WIKI_PATH (would no-op)
  B7.3 framework budget --status/--resume — fail loud on the host, not silently no-op
  B7.4 build-engine-image.sh — a reused HERMES_SRC checkout must be CLEAN, not just at the pin sha
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ── B7.1 install-cron-plus.sh: safe.directory, no chown needed ───────────────

def test_install_cron_plus_scopes_safe_directory():
    sh = (REPO / "scripts" / "install-cron-plus.sh").read_text()
    assert 'safe.directory="$DEST"' in sh, "managed-clone git ops don't scope safe.directory"
    assert "gitd()" in sh, "no gitd wrapper — git ops may hit 'dubious ownership' and brick the deploy"
    # the managed-clone operations must go through the wrapper, not a bare `git -C "$DEST"` that
    # would abort on an ownership mismatch (the whole point of B7.1)
    for op in ("gitd rev-parse", "gitd fetch", "gitd checkout", "gitd status"):
        assert op in sh, f"managed-clone op not routed through the safe.directory wrapper: {op}"
    assert 'git -C "$DEST" checkout' not in sh and 'git -C "$DEST" fetch' not in sh, \
        "a bare `git -C $DEST` remains — can still brick on dubious ownership"


# ── B7.2 install-extract-cron.sh: refuse without WIKI_PATH ───────────────────

def test_install_extract_cron_refuses_without_wiki_path(tmp_path):
    """Without WIKI_PATH the scheduled extract-raw.sh falls back to the container-only /opt/vault and
    no-ops every run. The installer must refuse, not write a dead schedule."""
    env = {k: v for k, v in os.environ.items() if k != "WIKI_PATH"}
    r = subprocess.run(["bash", str(REPO / "scripts" / "install-extract-cron.sh")],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0, "installer accepted an install with no WIKI_PATH"
    assert "WIKI_PATH is not set" in (r.stderr + r.stdout)


# ── B7.3 framework budget: fail loud on the host ─────────────────────────────

def _budget():
    spec = importlib.util.spec_from_file_location("framework_budget", REPO / "scripts" / "framework_budget.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_budget"] = m
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("flag", ["--status", "--resume"])
def test_budget_fails_loud_when_state_dir_absent(flag, tmp_path, monkeypatch, capsys):
    """budget --status/--resume is the documented manual-recovery path. Run on the host with the
    container-path default, the state file is absent -> --status printed 'not paused' and --resume
    no-op'd, even while the deployment WAS paused. It must fail loud (exit 2) and point at the data
    dir, not silently mislead."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "does-not-exist"))
    m = _budget()
    rc = m.main([flag])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found on this host" in err and "HERMES_HOME" in err


def test_budget_runs_when_state_dir_exists(tmp_path, monkeypatch):
    """The guard must NOT block when the state dir is present (e.g. inside the gateway or pointed at
    a real .hermes-data) — only when it's absent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))          # exists, but no state file -> "not paused"
    m = _budget()
    assert m.main(["--status"]) == 0                          # resolved, ran, reported not-paused


# ── B7.4 build-engine-image.sh: reused checkout must be clean ────────────────

def test_build_image_rejects_dirty_reused_checkout():
    sh = (REPO / "scripts" / "build-engine-image.sh").read_text()
    # after the pin-sha verification there must be a working-tree cleanliness gate
    assert "status --porcelain" in sh, "no working-tree cleanliness check — a dirty HERMES_SRC bakes edits"
    assert "UNCOMMITTED" in sh, "dirty-tree branch has no explicit refusal"
    # the cleanliness check sits inside the pinned-sha block (integrity), after the sha compare
    idx_sha = sh.index("expected pinned commit")
    idx_clean = sh.index("status --porcelain")
    assert idx_clean > idx_sha, "cleanliness check should follow the sha verification"
