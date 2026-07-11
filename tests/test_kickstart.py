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


def test_resolves_hermes_uid_via_shared_helper():  # invariant-audit M4
    """kickstart must resolve the exec uid the way every deploy script does — the shared
    resolve_hermes_uid helper (env -> pack .env -> /opt/data tree owner, okengine#185) — NOT the
    caller's `id -u`, which picks the wrong uid when run as root/another login and stalls the
    in-container build lanes on permission-denied."""
    t = K.read_text()
    assert "lib/hermes_uid.sh" in t, "kickstart must source the shared hermes_uid helper"
    assert 'HUID="$(resolve_hermes_uid' in t, "HUID must come from resolve_hermes_uid"
    assert '$(id -u)' not in t, "kickstart must not fall back to the caller's id -u for the exec uid"


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


# --- okengine#193: kickstart must cover pack importers + tier:analyze lanes, and never SILENTLY
#     skip an enabled lane (before this it ran 42/88 lanes and reported "done"). ------------------

def test_ingest_stage_covers_pack_importers():
    """A CTI pack ingests via structured importers (attack/kev/misp/…), not just RSS feed-fetch.
    The ingest stage must match the generic `-import`/`-seed` suffixes so they run before compile."""
    t = K.read_text()
    ingest = t[t.index('("ingest"'):t.index('("compile"')]
    assert '"-import"' in ingest and '"-seed"' in ingest, \
        "ingest stage must match pack importers by -import/-seed suffix"


def test_analyze_stage_exists_after_concepts():
    t = K.read_text()
    assert '("analyze"' in t, "kickstart must have an analyze stage (tier:analyze lanes had none)"
    # ordering: analyze reads the graph, so it must come after concepts and canonical
    assert t.index('("concepts"') < t.index('("analyze"'), "analyze must run after concepts"
    assert 'okengine.lacuna' in t, "analyze stage should name the lacuna wake-gate"


def _extract_literal(text, name):
    """Pull a top-level `NAME = [ ... ]` list literal out of the embedded kickstart Python."""
    import ast
    start = text.index(f"{name} = [")
    depth, i = 0, text.index("[", start)
    for j in range(i, len(text)):
        depth += {"[": 1, "]": -1}.get(text[j], 0)
        if depth == 0:
            return ast.literal_eval(text[i:j + 1])
    raise AssertionError(f"unbalanced brackets for {name}")


def test_no_enabled_lane_is_silently_skipped():
    """Functional coverage guard: replicate kickstart's planner over the REAL STAGES/MONITORING
    literals, and assert every enabled, non-monitoring lane is either claimed by a stage or swept
    by the catch-all — a novel importer/analysis/extension lane can never be dropped without trace."""
    t = K.read_text()
    STAGES = _extract_literal(t, "STAGES")
    MONITORING = _extract_literal(t, "MONITORING")
    assert 'planned_ids' in t and 'remaining' in t, "kickstart must track planned + sweep remaining"

    jobs = [
        {"id": "j1", "name": "okpack-threat-actors-attack-import", "enabled": True},   # importer
        {"id": "j2", "name": "okengine.lacuna", "enabled": True, "tier": "analyze"},   # tier:analyze
        {"id": "j3", "name": "okpack-threat-actors-correlation", "enabled": True},     # unclassified
        {"id": "j4", "name": "deployment-validate", "enabled": True},                  # monitoring
        {"id": "j5", "name": "okpack-detections-feed-fetch", "enabled": True},         # feed
        {"id": "j6", "name": "some-disabled-lane", "enabled": False},                  # ignored
    ]
    is_mon = lambda n: any(m in n for m in MONITORING)
    planned = set()
    for _label, names, _to, _rep in STAGES:
        by_name = [j for j in jobs if j["enabled"] and any(s in j["name"] for s in names)]
        by_tier = [j for j in jobs if j["enabled"] and j.get("tier") == _label]
        for j in by_name + by_tier:
            planned.add(j["id"])
    swept = [j for j in jobs if j["enabled"] and j["id"] not in planned and not is_mon(j["name"])]
    covered = planned | {j["id"] for j in swept}

    assert "j1" in planned, "importer must be claimed by the ingest stage"
    assert "j2" in planned, "tier:analyze lane must be claimed by the analyze stage"
    assert "j5" in planned, "feed-fetch must be claimed by ingest"
    assert "j3" in covered and "j3" in {j["id"] for j in swept}, \
        "an unclassified enabled lane must be SWEPT, never silently dropped"
    assert "j4" not in covered, "monitoring lanes stay on their own schedule, not the build sweep"
    # the property that matters: nothing enabled + non-monitoring escapes coverage
    for j in jobs:
        if j["enabled"] and not is_mon(j["name"]):
            assert j["id"] in covered, f"enabled lane {j['name']} silently skipped by kickstart"
