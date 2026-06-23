"""Smoke tests for scripts/deploy.sh — the one-command pack bring-up. Only the
paths that don't need Docker are exercised: arg/guard handling and the validate
gate aborting BEFORE any docker call (so a broken pack never reaches compose)."""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "deploy.sh"


def _run(args, cwd=None):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=60, cwd=cwd,
                          env={"PYTHON": sys.executable, "PATH": __import__("os").environ["PATH"]})


def test_script_exists_and_parses():
    assert SCRIPT.is_file()
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_unknown_flag_exits_2():
    r = _run(["--bogus"])
    assert r.returncode == 2
    assert "unknown flag" in r.stderr


def test_no_compose_file_errors(tmp_path):
    """A dir without docker-compose.yml is rejected before anything runs."""
    r = _run([str(tmp_path)])
    assert r.returncode == 1
    assert "no docker-compose.yml" in r.stderr


def test_validate_gate_aborts_before_docker(tmp_path):
    """A broken pack (has compose but fails validate) aborts at step 1 — the run
    never reaches the compose step, so Docker is never invoked."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")  # passes the file guard
    r = _run([str(tmp_path)])
    assert r.returncode == 1
    assert "[1/" in r.stdout and "validate" in r.stdout   # reached the validate step (count-agnostic)
    assert "validation failed" in r.stderr
    assert "[4/" not in r.stdout        # never reached the docker compose up step


def test_image_provenance_and_staleness_wired():
    """build-engine-image stamps version/sha/hermes labels; deploy.sh compares the
    image's git_sha label to the current checkout to detect a stale image (#14)."""
    bi = (REPO / "scripts" / "build-engine-image.sh").read_text()
    for label in ("org.okengine.release", "org.okengine.git_sha", "org.okengine.hermes_pin"):
        assert label in bi, f"build-engine-image missing label {label}"
    dp = SCRIPT.read_text()
    assert 'org.okengine.git_sha' in dp and "STALE" in dp


def test_default_image_tag_tracks_release_not_a_literal():
    """okengine#101: the default OKENGINE_TAG must derive from the manifest's engine_release,
    never a hardcoded vX.Y.Z literal (which goes stale and mis-tags images on every bump)."""
    import re
    bi = (REPO / "scripts" / "build-engine-image.sh").read_text()
    assert 'OKENGINE_TAG:-okengine-$RELEASE' in bi, "default tag should be okengine-$RELEASE"
    # no hardcoded okengine-vX.Y.Z literal as a tag default anywhere in the script
    assert not re.search(r'OKENGINE_TAG:-okengine-v[0-9]', bi), "default tag still hardcodes a version literal"


def test_default_uid_is_invoking_user_not_fixed_10000():
    """okengine#102: deploy defaults HERMES_UID/GID to the invoking user's uid, so a
    clone-as-yourself pack tree is writable out of the box instead of aborting the #33
    writability guard. A fixed uid stays available as an explicit override."""
    dp = SCRIPT.read_text()
    assert "HERMES_UID:-$(id -u)" in dp and "HERMES_GID:-$(id -g)" in dp
    assert "HERMES_UID:-10000" not in dp, "deploy.sh must not default HERMES_UID to a fixed 10000"
    er = (REPO / "scripts" / "ensure-runtime.sh").read_text()
    assert "HERMES_UID:-$(id -u)" in er, "ensure-runtime.sh should default to the invoking uid too"


def test_skip_validate_proceeds_past_gate(tmp_path):
    """--skip-validate bypasses the gate; the run then proceeds to seeding (step 2)
    even for an otherwise-incomplete dir (it'll fail later at docker, not here)."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    r = _run(["--skip-validate", "--skip-build", "--no-crons", str(tmp_path)])
    # We can't assert success (docker may be absent), but it must get PAST validate
    # to the seed step — proving the gate was skipped.
    assert "[2/" in r.stdout and "seed runtime" in r.stdout   # past the gate, at the seed step
