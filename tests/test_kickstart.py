"""kickstart — opt-in one-time vault population so a fresh install isn't empty for hours/days
(okengine#109). Static guards: it exists/parses, runs the build chain in dependency order,
scopes to the pack's compose project, and is strictly opt-in via deploy.sh --kickstart.
"""
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
K = REPO / "scripts" / "kickstart.sh"
D = REPO / "scripts" / "deploy.sh"


def test_exists_executable_and_parses():
    assert K.is_file(), "scripts/kickstart.sh is missing"
    assert K.stat().st_mode & 0o111, "kickstart.sh should be executable"
    r = subprocess.run(["bash", "-n", str(K)], capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"


def test_stages_run_in_dependency_order():
    """ingest -> compile -> dashboards -> brief: feeds must precede the compile, the compile
    must precede the dashboards (they read compiled pages), and the brief reads the HOT set."""
    t = K.read_text()
    # cron names that appear only in the STAGES list, in dependency order:
    # ingest -> compile -> entities -> concepts -> dashboards
    order = ["feed-fetch", "raw-backfill", "entity-backfill", "concept-backfill", "build-hot-set"]
    idxs = [t.index(tok) for tok in order]
    assert idxs == sorted(idxs), f"kickstart crons are out of dependency order ({order})"
    # the full build chain is covered, not just a slice
    for lane in ("schema/repair", "predictions", "canonical", "quality/audit", "brief"):
        assert lane in t, f"kickstart is missing the {lane!r} lane"


def test_scopes_gateway_to_pack_project():
    # must target THIS pack's gateway, not the first gateway on a multi-pack host (#108)
    assert 'docker compose -f "$PACK_DIR/docker-compose.yml" ps -q gateway' in K.read_text()


def test_completion_keys_on_last_run_success():
    """okengine#114: agent crons advance last_run_at when the SELECTOR fires (seconds), but the
    compile finishes much later. The completion poll must require last_run_success to be SET,
    else it declares agent lanes done prematurely (the bug found dogfooding the kickstart)."""
    t = K.read_text()
    assert 'last_run_success") is not None' in t, \
        "kickstart completion must gate on last_run_success being set, not last_run_at alone"


def test_deploy_kickstart_is_opt_in():
    t = D.read_text()
    assert "KICKSTART=0" in t, "kickstart must default OFF"
    assert "--kickstart)" in t and "kickstart.sh" in t, "deploy.sh must wire the --kickstart flag"
    assert 'if [ "$KICKSTART" = 1 ]' in t, "kickstart must run only when the flag is set"
