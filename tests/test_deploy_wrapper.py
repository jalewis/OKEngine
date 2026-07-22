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


def test_deploy_recomposes_schema_artifact():  # invariant-audit #12
    """deploy.sh must recompose <pack>/.okengine/composed-schema.yaml, else a schema.yaml edit is
    silently ignored on the enforced write path (which prefers the frozen artifact) until the next
    `framework extensions enable/disable`."""
    dp = SCRIPT.read_text()
    assert "write_composed_schema" in dp, \
        "deploy.sh no longer recomposes the schema artifact — schema.yaml edits won't reach the write path"


def test_deploy_recompose_error_is_fatal():  # invariant-audit HIGH #4
    """A recompose ERROR must ABORT the deploy, not WARN-and-continue. write_composed_schema writes
    NOTHING on error and leaves the stale artifact, which the enforced write path keeps using
    unconditionally — a broken/renamed extension fragment would silently freeze the governing
    schema forever. deploy.sh's 1b step must exit non-zero on errors (no 'non-fatal' escape)."""
    dp = SCRIPT.read_text()
    b1 = dp[dp.index("1b."):dp.index("[2/6]")]
    assert "non-fatal" not in b1, "recompose step still treats errors as non-fatal"
    assert "sys.exit(1 if errs else 0)" in b1 and "exit 1" in b1, \
        "recompose step must exit non-zero on a fragment error, not just print a warning"


def test_write_composed_schema_errors_on_a_broken_fragment(tmp_path):
    """The behaviour deploy.sh now gates on: a missing/unparseable enabled fragment yields errors
    and leaves the artifact untouched (write-nothing-on-error)."""
    import importlib.util, sys, yaml
    def _load(n):
        s = importlib.util.spec_from_file_location(n, REPO / "scripts" / f"{n}.py")
        m = importlib.util.module_from_spec(s); sys.modules[n] = m; s.loader.exec_module(m); return m
    comp = _load("extension_compose"); disc = _load("extension_discovery")
    pack = tmp_path / "pack"; (pack / "wiki").mkdir(parents=True)
    (pack / "schema.yaml").write_text(yaml.safe_dump(
        {"apply_under": ["wiki/"], "partitioning": {"namespaces": {"entities": {}}},
         "types": {"entity": {"required": ["type"]}}}))
    d = pack / "extensions" / "demo.pred"; (d / "schema").mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump({
        "id": "demo.pred", "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
        "requires": {"engine": ">=0.3.0"}, "capabilities": {"read": ["wiki/**"], "write": ["x/**"]},
        "schema": ["schema/missing.yaml"],   # <-- fragment file does NOT exist
        "operation": {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                      "entrypoint": {"script": "run.py"}}}))
    disc.set_enabled(pack, "demo.pred", True)
    errs = comp.write_composed_schema(pack)
    assert errs, "a missing enabled fragment must produce errors"
    assert not (pack / ".okengine" / "composed-schema.yaml").is_file(), "must write nothing on error"


def test_deploy_rebuilds_sibling_images():  # invariant-audit #8/#23
    """deploy.sh step 4 must `up -d --build`. Plain `up -d` builds an image only when ABSENT, so
    the reader/mcp/cockpit images (the only services with a compose build:) freeze after the first
    deploy and ship stale baked code — a fix to app.py/server.py/tier_lib.py would look deployed
    (the gateway visibly rebuilds) but not actually run. --build rebuilds the changed siblings; the
    gateway has no compose build: so it's untouched (built separately in step 3)."""
    dp = SCRIPT.read_text()
    assert "docker compose up -d --build" in dp, \
        "deploy.sh step 4 must use --build, or the sibling images run stale baked code after deploy 1"


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
    # #7 (invariant-audit): the resolution now prefers an existing .env pin, then the invoking
    # uid — `${HERMES_UID:-${_env_uid:-$(id -u)}}` — so the ULTIMATE fallback is still $(id -u),
    # never a fixed uid. Assert the invoking-uid fallback survives and no fixed default crept in.
    assert "$(id -u)" in dp and "$(id -g)" in dp
    assert "HERMES_UID:-10000" not in dp, "deploy.sh must not default HERMES_UID to a fixed 10000"
    assert ":-1003" not in dp, "deploy.sh must not hardcode an operator-specific uid"
    er = (REPO / "scripts" / "ensure-runtime.sh").read_text()
    # invariant-audit HIGH #1: ensure-runtime now resolves the uid via the SHARED resolver (env >
    # .env pin > tree owner) — the way compose/deploy do — instead of a bare $(id -u) that ignored
    # the .env pin, and it pins the result into .env so bare compose uses the same uid.
    assert "resolve_hermes_uid" in er, "ensure-runtime.sh must use the shared uid resolver"
    assert "HERMES_UID:-10000" not in er and ":-1003" not in er, "no fixed/operator uid default"


def test_skip_validate_proceeds_past_gate(tmp_path):
    """--skip-validate bypasses the gate; the run then proceeds to seeding (step 2)
    even for an otherwise-incomplete dir (it'll fail later at docker, not here)."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    r = _run(["--skip-validate", "--skip-build", "--no-crons", str(tmp_path)])
    # We can't assert success (docker may be absent), but it must get PAST validate
    # to the seed step — proving the gate was skipped.
    assert "[2/" in r.stdout and "seed runtime" in r.stdout   # past the gate, at the seed step


def test_deploy_reconciles_engine_pin_before_validate():  # okengine#359
    """A pack pinning an OLDER engine release must not dead-end at the 'different release series' FAIL.
    deploy.sh reconciles the pin via `framework upgrade --apply` (step [0/6]) BEFORE the validate gate,
    unless --no-upgrade — so an engine bump doesn't force every lagging pack onto --skip-validate (which
    bypasses ALL checks). okengine#359."""
    dp = SCRIPT.read_text()
    assert "NO_UPGRADE" in dp and "--no-upgrade" in dp, "no --no-upgrade opt-out"
    assert 'framework.py" upgrade "$PACK" --apply' in dp, "deploy no longer runs framework upgrade to reconcile the pin"
    i0, i1 = dp.find("[0/6]"), dp.find("[1/6]")
    assert 0 <= i0 < i1, "the pin reconcile ([0/6]) must run BEFORE the validate gate ([1/6])"
    # reconcile is guarded on the pack actually having a pin to reconcile
    assert '[ -f "$PACK/engine.version" ]' in dp
