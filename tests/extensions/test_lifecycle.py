"""Regression tests for the extension enable/disable + compose lifecycle (#113).

Guards the §9 invariants end to end: present≠enabled, enable namespaces+composes a
cron job, disable removes it without deleting state, fail-before-runtime on an
invalid enabled set, and generated-from-source (the deploy pass derives jobs from
manifests + enabled-state).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
DISC_PATH = REPO / "scripts" / "extension_discovery.py"
COMP_PATH = REPO / "scripts" / "extension_compose.py"
CLI_PATH = REPO / "scripts" / "framework_extensions.py"

pytestmark = pytest.mark.skipif(
    not (DISC_PATH.is_file() and COMP_PATH.is_file() and CLI_PATH.is_file()),
    reason="extension lifecycle modules not present")


@pytest.fixture(autouse=True)
def _isolate_engine_tier(monkeypatch, tmp_path):
    """These lifecycle tests use synthetic demo.* extensions; don't let shipped engine
    extensions (e.g. the core-default-on okengine.contradictions, #142) leak into the
    tier-1 scan. Point the engine-extensions root at an empty dir."""
    empty = tmp_path / "_no_engine_exts"
    empty.mkdir()
    monkeypatch.setenv("OKENGINE_ENGINE_ROOT", str(empty))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _disc():
    return _load("extension_discovery", DISC_PATH)


def _comp():
    return _load("extension_compose", COMP_PATH)


def _cli():
    return _load("framework_extensions", CLI_PATH)


def _write_ext(pack: Path, ext_id, trust="in-gateway", **op_over):
    d = pack / "extensions" / ext_id
    d.mkdir(parents=True, exist_ok=True)
    # A sidecar needs a digest-pinned image entrypoint; in-gateway needs a script.
    if trust == "sidecar":
        entrypoint = {"image": {"registry": f"reg.example.com/{ext_id}",
                                "tag": "0.1.0", "digest": "sha256:deadbeef"}}
    else:
        entrypoint = {"script": "run.py"}
    operation = {"schedule": {"kind": "cron", "expr": "17 5 * * *"},
                 "entrypoint": entrypoint, "timeout": 1800}
    operation.update(op_over)
    man = {"id": ext_id, "kind": "operation", "version": "0.1.0", "name": ext_id,
           "trust": trust, "requires": {"engine": ">=0.3.0"},
           "capabilities": {"read": ["wiki/**"], "write": [ext_id.split(".")[-1] + "/**"]},
           "operation": operation}
    (d / "extension.yaml").write_text(yaml.safe_dump(man), encoding="utf-8")
    return d


def test_set_enabled_round_trips(tmp_path):
    disc = _disc()
    pack = tmp_path / "pack"
    pack.mkdir()
    assert disc.set_enabled(pack, "demo.alpha", True, config={"horizon_days": 90}) == []
    enabled, errs = disc.load_enabled_state(pack)
    assert errs == []
    assert enabled["demo.alpha"]["config"]["horizon_days"] == 90
    # disable removes the entry but leaves the file/other entries intact
    disc.set_enabled(pack, "demo.beta", True)
    disc.set_enabled(pack, "demo.alpha", False)
    enabled2, _ = disc.load_enabled_state(pack)
    assert "demo.alpha" not in enabled2 and "demo.beta" in enabled2


def test_present_not_enabled_yields_no_jobs(tmp_path):
    comp = _comp()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")                 # present on disk, nothing enabled
    jobs, errors = comp.extension_jobs(pack)
    assert jobs == [] and errors == []


def test_enable_then_compose_emits_namespaced_job(tmp_path):
    disc, comp = _disc(), _comp()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")
    disc.set_enabled(pack, "demo.alpha", True)
    jobs, errors = comp.extension_jobs(pack, existing_names={"build-hot-set"})
    assert errors == []
    assert [j["name"] for j in jobs] == ["demo.alpha"]


def test_disable_drops_job_keeps_state_file(tmp_path):
    disc, comp = _disc(), _comp()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")
    disc.set_enabled(pack, "demo.alpha", True)
    disc.set_enabled(pack, "demo.alpha", False)
    jobs, errors = comp.extension_jobs(pack)
    assert jobs == [] and errors == []
    assert (pack / ".okengine" / "extensions.yaml").is_file()   # state preserved


def test_enabled_but_absent_is_fail_before_runtime(tmp_path):
    disc, comp = _disc(), _comp()
    pack = tmp_path / "pack"
    pack.mkdir()
    disc.set_enabled(pack, "demo.ghost", True)     # enabled but never discovered
    jobs, errors = comp.extension_jobs(pack)
    assert any("demo.ghost" in e and "not discovered" in e for e in errors)


def test_cli_enable_validates_and_writes(tmp_path, capsys):
    cli = _cli()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")
    rc = cli.main(["enable", str(pack), "demo.alpha"])
    assert rc == 0
    enabled, _ = _disc().load_enabled_state(pack)
    assert "demo.alpha" in enabled


def test_cli_enable_missing_dependency_fails_without_writing(tmp_path):
    cli, disc = _cli(), _disc()
    pack = tmp_path / "pack"
    d = _write_ext(pack, "demo.alpha")
    man = yaml.safe_load((d / "extension.yaml").read_text())
    man["requires"]["extensions"] = ["demo.missing"]
    (d / "extension.yaml").write_text(yaml.safe_dump(man), encoding="utf-8")
    rc = cli.main(["enable", str(pack), "demo.alpha"])
    assert rc == 1
    enabled, _ = disc.load_enabled_state(pack)
    assert "demo.alpha" not in enabled             # no state change on failure


def test_cli_disable_is_idempotent(tmp_path):
    cli = _cli()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")
    assert cli.main(["disable", str(pack), "demo.alpha"]) == 0   # not enabled -> no-op


def test_staging_targets_only_enabled_in_gateway(tmp_path):
    disc, comp = _disc(), _comp()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")                          # in-gateway
    _write_ext(pack, "demo.side", trust="sidecar")          # sidecar -> not staged
    _write_ext(pack, "demo.off")                            # present, not enabled
    disc.set_enabled(pack, "demo.alpha", True)
    disc.set_enabled(pack, "demo.side", True)
    targets, errors = comp.staging_targets(pack)
    assert errors == []
    ids = {t["id"] for t in targets}
    assert ids == {"demo.alpha"}                            # only enabled in-gateway op
    assert targets[0]["dir"].endswith("/extensions/demo.alpha")


def test_staging_targets_empty_when_nothing_enabled(tmp_path):
    comp = _comp()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")
    targets, errors = comp.staging_targets(pack)
    assert targets == [] and errors == []


def test_cli_stage_plan_prints_id_and_dir(tmp_path, capsys):
    cli, disc = _cli(), _disc()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")
    disc.set_enabled(pack, "demo.alpha", True)
    rc = cli.main(["stage-plan", str(pack)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    ext_id, _, ext_dir = out.partition("\t")
    assert ext_id == "demo.alpha"
    assert ext_dir.endswith("/extensions/demo.alpha")


# --- invariant-audit v0.11.5 batch-4 -----------------------------------------

def test_disable_fails_loud_when_recompose_errors(tmp_path, capsys):  # invariant-audit #38
    """`extensions disable` must NOT report clean success when write_composed_schema errors — the
    artifact is left untouched (still governing with the old set), so the enforced write path runs on
    a stale schema. The canonical trigger is ANOTHER enabled extension left undiscovered by an update."""
    disc, cli = _disc(), _cli()
    pack = tmp_path / "pack"
    _write_ext(pack, "demo.alpha")
    disc.set_enabled(pack, "demo.alpha", True)
    disc.set_enabled(pack, "demo.ghost", True)          # enabled but never discovered -> recompose errors
    rc = cli.main(["disable", str(pack), "demo.alpha"])
    assert rc == 1, "disable must fail loud when the composed-schema regen errors"
    err = capsys.readouterr().err
    assert "demo.ghost" in err and "composed-schema" in err.lower()


def test_enable_dry_run_uses_effective_set_not_just_explicit():  # invariant-audit #63
    """The enable dry-run must compose over the EFFECTIVE set (explicit opt-ins ∪ core default-ons),
    the same set write_composed_schema/deploy use — else fail-before-runtime validates a different
    composition than ships. Structural guard (a core schema-bringing extension is not constructible
    today without modifying the engine)."""
    src = CLI_PATH.read_text()
    body = src[src.index("def _cmd_enable"):src.index("def _cmd_disable")]
    assert "effective_enabled" in body, \
        "enable dry-run must build want_ids from effective_enabled, not just load_enabled_state"
