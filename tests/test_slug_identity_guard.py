import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "patches" / "cron-plus" / "slug_identity_guard.py"
SPEC = importlib.util.spec_from_file_location("slug_identity_guard_test", MODULE)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

ID_SPEC = importlib.util.spec_from_file_location(
    "slug_identity_guard_id_lib", ROOT / "scripts" / "cron" / "id_lib.py"
)
id_lib = importlib.util.module_from_spec(ID_SPEC)
ID_SPEC.loader.exec_module(id_lib)
guard._ID_LIB = id_lib


def _page(wiki: Path, rel: str, *, status: str = "active", body: str = "body") -> Path:
    path = wiki / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: entity\nstatus: {status}\n---\n{body}\n")
    return path


def test_identity_library_loads_from_flat_deployed_scripts_directory(monkeypatch):
    monkeypatch.setenv("OKENGINE_CRON_SCRIPTS", str(ROOT / "scripts" / "cron"))
    monkeypatch.setattr(guard, "_ID_LIB", None)
    loaded = guard._id_lib()
    assert loaded.slug_identity("Agent-Tesla") == "agenttesla"
    assert '"/opt/data/scripts"' in MODULE.read_text()
    guard._ID_LIB = id_lib


def test_direct_writer_collision_fails_and_preserves_quarantined_content(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/a/agent-tesla.md", body="canonical")
    before = guard.snapshot(wiki)
    candidate = _page(wiki, "entities/a/agenttesla.md", body="candidate evidence")

    with pytest.raises(guard.SlugIdentityCollision, match="quarantined outside wiki"):
        guard.enforce(wiki, before, tmp_path / "quarantine", job_id="direct-writer")

    assert not candidate.exists()
    quarantined = list((tmp_path / "quarantine").glob("*/entities/a/agenttesla.md"))
    assert len(quarantined) == 1
    assert "candidate evidence" in quarantined[0].read_text()
    receipt = quarantined[0].parents[2] / "receipt.json"
    assert "direct-writer" in receipt.read_text()
    assert "entities/a/agent-tesla.md" in receipt.read_text()


def test_existing_collision_does_not_fail_a_noop_run(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/a/agent-tesla.md")
    _page(wiki, "entities/a/agenttesla.md")
    before = guard.snapshot(wiki)
    assert guard.enforce(wiki, before, tmp_path / "quarantine", job_id="noop") == []


def test_tombstone_numeric_and_cross_namespace_do_not_false_positive(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/a/agent-tesla.md", status="tombstoned")
    _page(wiki, "indicators/1/110-37-3-251.md")
    before = guard.snapshot(wiki)
    _page(wiki, "entities/a/agenttesla.md")
    _page(wiki, "concepts/a/agent-tesla.md")
    _page(wiki, "indicators/1/110373251.md")
    assert guard.enforce(wiki, before, tmp_path / "quarantine", job_id="valid") == []


def test_nested_pack_namespaces_are_isolated(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "acme").mkdir(parents=True)
    (wiki / "acme" / "schema.yaml").write_text("types: {}\n")
    _page(wiki, "entities/a/agent-tesla.md")
    before = guard.snapshot(wiki)
    _page(wiki, "acme/entities/a/agenttesla.md")
    assert guard.enforce(wiki, before, tmp_path / "quarantine", job_id="nested") == []


def test_runner_enforces_after_failed_as_well_as_successful_direct_writers():
    patch = (ROOT / "patches" / "cron-plus" / "slug-identity-guard.patch").read_text()
    assert "if slug_snapshot is not None:" in patch
    assert "if success and slug_snapshot is not None:" not in patch


def test_paginated_index_artifacts_across_shards_are_not_a_collision(tmp_path):
    """okengine#836. `INDEX-p02.md` is a PER-DIRECTORY artifact -- it is supposed to exist in every
    shard, and those files are different pages sharing a basename, not spellings of one page.

    Identity is (namespace, slug_identity), and `qualified_namespace` returns `entities` for every
    shard beneath it, so without an exemption all of them collapse onto one key and each index
    rebuild "introduces" a collision. Measured on a live vault: 1,216 of 1,420 quarantined pages
    were this class against 204 real ones, burying the genuine leak under ~180/day of noise."""
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/0-9/0/INDEX-p02.md", body="shard 0 index")
    before = guard.snapshot(wiki)
    _page(wiki, "entities/0-9/1/INDEX-p02.md", body="shard 1 index")
    _page(wiki, "entities/i/n/INDEX-p02.md", body="shard in index")

    assert guard.enforce(wiki, before, tmp_path / "q", job_id="build-index-tree") == []
    assert (wiki / "entities/0-9/1/INDEX-p02.md").exists(), "a generated artifact was quarantined"
    assert (wiki / "entities/i/n/INDEX-p02.md").exists()


def test_a_real_collision_beside_index_artifacts_is_still_caught(tmp_path):
    """The negative guard: exempting generated artifacts must not blind the check. A genuine
    separator variant still fails even when index pages are rebuilt in the same run."""
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/a/agent-tesla.md", body="canonical")
    before = guard.snapshot(wiki)
    _page(wiki, "entities/a/INDEX-p02.md", body="index")
    _page(wiki, "entities/a/agenttesla.md", body="variant")

    with pytest.raises(guard.SlugIdentityCollision) as excinfo:
        guard.enforce(wiki, before, tmp_path / "q", job_id="importer")
    assert "agenttesla" in str(excinfo.value)
    assert "INDEX" not in str(excinfo.value), "an index artifact was reported as a collision"
    assert (wiki / "entities/a/INDEX-p02.md").exists(), "the index artifact was quarantined"


def test_generated_structural_rule_matches_the_validator():
    """The rule lives in several places already; this pins the guard's copy to the canonical one so
    they cannot drift apart. A name the write path treats as generated must never be judged an
    authored page by the collision guard."""
    assert hasattr(guard, "_is_generated_structural"), (
        "the guard has no generated-structural rule, so every per-directory INDEX artifact is "
        "judged an authored page (okengine#836)")
    spec = importlib.util.spec_from_file_location(
        "schema_validator_probe", ROOT / "tools" / "schema_validator.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    names = ["INDEX.md", "INDEX-p02.md", "index-p10.md", "INDEX-tags.md", "_review-queue.md",
             ".hidden.md", "agent-tesla.md", "checkpoint.md", "indexed-attacks.md", "index.md"]
    for name in names:
        assert guard._is_generated_structural(name) == validator._is_generated_structural(name), name


# ---- okengine#883: attribute the introduction to its writer, never to a bystander ------------

import json as _json  # noqa: E402
import os as _os  # noqa: E402
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402

_DEAD_PID = 4194304 + 1   # above Linux pid_max, so no process can own it


def _run(runs: Path, job_id: str, started: _dt, ended: "_dt | None", *, pid: int = _DEAD_PID,
         name: str | None = None) -> Path:
    """A cron-plus runner-owned run record, as run_records.py writes it."""
    path = runs / job_id / (name or f"{started.strftime('%Y-%m-%d_%H-%M-%S-%f')}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({
        "api": 1, "job_id": job_id, "pid": pid, "started_at": started.isoformat(),
        "ended_at": ended.isoformat() if ended else None,
        "status": "succeeded" if ended else "running"}))
    return path


def _collision(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/c/v/CVE-2025-62673.md", body="canonical")
    before = guard.snapshot(wiki)
    _page(wiki, "entities/c/v/cve-2025-62673.md", body="importer copy")
    return wiki, before


def _receipt(quarantine: Path) -> dict:
    (receipt,) = quarantine.glob("*/receipt.json")
    return _json.loads(receipt.read_text())


def test_a_bystander_is_not_failed_for_a_page_a_concurrent_job_wrote(tmp_path):
    """The live failure: budget-guard's window overlapped nvd-import's write, so budget-guard was
    failed for nvd-import's page. With another lane running in the window, the guard cannot say
    who wrote it: it quarantines, reports the collision as unattributed, and names the candidates."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    now = _dt.now(_tz.utc)
    _run(runs, "budget-guard", now - _td(seconds=5), None, pid=_os.getpid())
    _run(runs, "nvd-import", now - _td(minutes=10), now)

    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard",
                            runs_root=runs)

    assert [r["path"] for r in records] == ["entities/c/v/cve-2025-62673.md"]
    assert records[0]["attribution"] == "unattributed"
    assert records[0]["concurrent_jobs"] == ["nvd-import"]
    assert not (wiki / "entities/c/v/cve-2025-62673.md").exists(), "the collision stayed live"
    receipt = _receipt(tmp_path / "q")
    assert receipt["collisions"][0]["attribution"] == "unattributed"
    assert "concurrent writer, unattributed" in receipt["collisions"][0]["note"]


def test_the_sole_runner_in_the_window_is_still_failed(tmp_path):
    """Negative fixture: attribution must not blind the guard. With no other cron-plus run inside
    the window (only an older, finished run), the job that ran is the writer."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    now = _dt.now(_tz.utc)
    _run(runs, "nvd-import", now - _td(seconds=5), None, pid=_os.getpid())
    _run(runs, "budget-guard", now - _td(hours=3), now - _td(hours=2))

    with pytest.raises(guard.SlugIdentityCollision, match="quarantined outside wiki"):
        guard.enforce(wiki, before, tmp_path / "q", job_id="nvd-import", runs_root=runs)
    record = _receipt(tmp_path / "q")["collisions"][0]
    assert record["attribution"] == "sole-runner" and record["concurrent_jobs"] == []


def test_a_job_that_declared_the_page_as_its_artifact_is_failed_despite_concurrency(tmp_path):
    """The run's own write receipt (an OKENGINE_ARTIFACT declaration) names the file: that is
    positive attribution, even when another lane overlapped."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    now = _dt.now(_tz.utc)
    _run(runs, "budget-guard", now - _td(seconds=30), now)
    response = ("imported 1\n" 'OKENGINE_ARTIFACT: {"path": "wiki/entities/c/v/cve-2025-62673.md",'
                ' "operation": "create"}\n' "OKENGINE_ARTIFACT: not json\n"
                'OKENGINE_ARTIFACT: ["no", "path"]\n')

    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="nvd-import", runs_root=runs,
                      response=response)
    assert _receipt(tmp_path / "q")["collisions"][0]["attribution"] == "declared-artifact"


def test_an_absolute_artifact_path_also_attributes(tmp_path):
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    now = _dt.now(_tz.utc)
    _run(runs, "budget-guard", now - _td(seconds=30), now)
    target = (wiki / "entities/c/v/cve-2025-62673.md").resolve()
    response = f'OKENGINE_ARTIFACT: {{"path": "{target}", "operation": "create"}}'
    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="nvd-import", runs_root=runs,
                      response=response)


def test_another_jobs_declaration_does_not_attribute_a_different_page(tmp_path):
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    now = _dt.now(_tz.utc)
    _run(runs, "nvd-import", now - _td(seconds=30), now)
    response = 'OKENGINE_ARTIFACT: {"path": "wiki/reports/budget.md", "operation": "replace"}'
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs,
                            response=response)
    assert records[0]["attribution"] == "unattributed"


def test_a_still_running_lane_overlaps_but_a_dead_orphan_that_started_earlier_does_not(tmp_path):
    """A `running` record with a live worker is running NOW, so it overlaps. One whose worker is
    gone and which started before the window ended at an unknown earlier time: it is not a
    candidate, otherwise one orphaned record would disable attribution forever."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    now = _dt.now(_tz.utc)
    _run(runs, "orphan", now - _td(days=2), None)                    # dead, started long ago
    _run(runs, "live-lane", now - _td(days=1), None, pid=1)          # pid 1 exists: alive
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["live-lane"]


def test_a_dead_lane_that_started_inside_the_window_overlaps(tmp_path):
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    _run(runs, "crashed-lane", _dt.now(_tz.utc), None)
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["crashed-lane"]


def test_an_unreadable_record_touched_in_the_window_is_an_unknown_writer(tmp_path):
    """An unreadable record with a quantized mtime is unknown; the hour-old one is stale."""
    runs = tmp_path / "runs"
    start = _dt(2024, 1, 1, 12, 0, 1, 500, tzinfo=_tz.utc)
    stale = runs / "old-lane" / "stale.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{not json")
    old = (start - _td(hours=1)).timestamp()
    _os.utime(stale, (old, old))
    wiki, before = _window(tmp_path, start)
    (runs / "torn-lane").mkdir()
    torn = runs / "torn-lane" / "torn.json"
    torn.write_text("{not json")
    # The kernel can stamp a write after snapshot() with a coarse time just before it (#946).
    _aged(torn, start.replace(microsecond=0))
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["attribution"] == "unattributed"
    assert records[0]["concurrent_jobs"] == ["torn-lane"]


@pytest.mark.parametrize("offset_ns,unknown", [
    (-1, False), (0, True), (999_000_000, True), (1_000_000_000, True),
    (1_999_000_000, True), (2_000_000_000, True),
])
@pytest.mark.parametrize("start", [
    _dt(2024, 1, 1, 12, 0, 1, 500, tzinfo=_tz.utc),
    _dt(2024, 1, 1, 12, 0, 1, 500, tzinfo=_tz.utc).astimezone(_tz(_td(hours=5, minutes=30))),
    _dt(1969, 1, 1, 12, 0, 1, 500, tzinfo=_tz.utc),
])
def test_unreadable_record_at_the_uncertainty_boundary(tmp_path, offset_ns, unknown, start):
    """Include the exact uncertainty edge; reject one nanosecond earlier, with no float rounding."""
    wiki, before = _window(tmp_path, start)
    torn = tmp_path / "runs" / "unknown" / "torn.json"
    torn.parent.mkdir(parents=True)
    torn.write_text("{torn")
    utc = start.astimezone(_tz.utc)
    mtime_ns = (int(utc.replace(microsecond=0).timestamp()) * 1_000_000_000
                + utc.microsecond * 1000 - 2_000_000_000 + offset_ns)
    _os.utime(torn, ns=(mtime_ns, mtime_ns))
    assert torn.stat().st_mtime_ns == mtime_ns
    if unknown:
        records = guard.enforce(wiki, before, tmp_path / "q", job_id="bystander",
                                runs_root=tmp_path / "runs")
        assert records[0]["attribution"] == "unattributed"
        assert records[0]["concurrent_jobs"] == ["unknown"]
        assert not (wiki / records[0]["path"]).exists()
        assert _receipt(tmp_path / "q")["collisions"] == records
    else:
        with pytest.raises(guard.SlugIdentityCollision):
            guard.enforce(wiki, before, tmp_path / "q", job_id="writer",
                          runs_root=tmp_path / "runs")
        record = _receipt(tmp_path / "q")["collisions"][0]
        assert record["attribution"] == "sole-runner" and record["concurrent_jobs"] == []


def test_readable_end_time_keeps_subsecond_precision(tmp_path):
    start = _dt(2024, 1, 1, 12, 0, 1, 500, tzinfo=_tz.utc)
    wiki, before = _window(tmp_path, start)
    runs = tmp_path / "runs"
    _aged(_run(runs, "finished", start - _td(minutes=1), start - _td(microseconds=1)),
          start.replace(microsecond=0))
    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="writer", runs_root=runs)
    assert _receipt(tmp_path / "q")["collisions"][0]["attribution"] == "sole-runner"


def test_quantized_unknown_record_does_not_override_declared_writer(tmp_path):
    start = _dt(2024, 1, 1, 12, 0, 1, 999999, tzinfo=_tz.utc)
    wiki, before = _window(tmp_path, start)
    torn = tmp_path / "runs" / "unknown" / "torn.json"
    torn.parent.mkdir(parents=True)
    torn.write_text("{torn")
    _aged(torn, start.replace(microsecond=0))
    response = 'OKENGINE_ARTIFACT: {"path":"wiki/entities/c/v/cve-2025-62673.md"}'
    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="writer",
                      runs_root=tmp_path / "runs", response=response)
    record = _receipt(tmp_path / "q")["collisions"][0]
    assert record["attribution"] == "declared-artifact"
    assert record["concurrent_jobs"] == ["unknown"] and "note" not in record


@pytest.mark.parametrize("offset_ns,unknown", [(0, True), (-1, False)])
def test_older_unreadable_record_uses_the_same_uncertainty_interval(tmp_path, offset_ns, unknown):
    start = _dt(2024, 1, 1, 12, 0, 1, 500, tzinfo=_tz.utc)
    wiki, before = _window(tmp_path, start)
    runs = tmp_path / "runs"
    lane = runs / "other"
    lane.mkdir(parents=True)
    torn = lane / "2022-01-01.json"
    torn.write_text("{torn finish marker")
    mtime_ns = int(start.replace(microsecond=0).timestamp()) * 1_000_000_000 - 1_999_500_000
    _os.utime(torn, ns=(mtime_ns + offset_ns, mtime_ns + offset_ns))
    earlier = _dt(2023, 1, 1, tzinfo=_tz.utc)
    _aged(_run(runs, "other", earlier, earlier, name="2023-01-01.json"), earlier)
    if unknown:
        records = guard.enforce(wiki, before, tmp_path / "q", job_id="bystander", runs_root=runs)
        assert records[0]["concurrent_jobs"] == ["other"]
        assert records[0]["attribution"] == "unattributed"
    else:
        with pytest.raises(guard.SlugIdentityCollision):
            guard.enforce(wiki, before, tmp_path / "q", job_id="writer", runs_root=runs)
        assert _receipt(tmp_path / "q")["collisions"][0]["concurrent_jobs"] == []


def test_legacy_snapshot_without_a_time_does_not_underflow_the_mtime_boundary(tmp_path):
    wiki, before = _collision(tmp_path)
    before = guard.Snapshot(before.paths)
    torn = tmp_path / "runs" / "other" / "torn.json"
    torn.parent.mkdir(parents=True)
    torn.write_text("{torn")
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="bystander",
                            runs_root=tmp_path / "runs")
    assert records[0]["attribution"] == "unattributed"


def test_malformed_record_fields_are_unknown_not_absent(tmp_path):
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    (runs / "a").mkdir(parents=True)
    (runs / "a" / "list.json").write_text("[1, 2]")
    (runs / "b").mkdir()
    (runs / "b" / "notime.json").write_text('{"job_id": "b", "started_at": "yesterday"}')
    (runs / "c").mkdir()
    (runs / "c" / "naive.json").write_text(_json.dumps(
        {"job_id": "c", "pid": _DEAD_PID, "started_at": _dt.now().isoformat(),
         "ended_at": "garbage"}))
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["a", "b", "c"]


def test_the_runner_hands_the_guard_its_run_records_and_output():
    patch = (ROOT / "patches" / "cron-plus" / "slug-identity-guard.patch").read_text()
    assert 'runs_root=jobs_mod.CRON_PLUS_HOME / "runs"' in patch
    assert "response=response" in patch


def test_snapshot_records_when_the_window_opened(tmp_path):
    before = _dt.now(_tz.utc)
    snap = guard.snapshot(tmp_path / "wiki")
    assert snap.taken_at is not None and before <= snap.taken_at <= _dt.now(_tz.utc)


def test_an_own_process_record_in_another_lane_counts_as_running(tmp_path):
    """A `running` record whose worker answers a signal is still going, so it overlaps."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    _run(runs, "sibling", _dt.now(_tz.utc) - _td(days=1), None, pid=_os.getpid())
    _run(runs, "bogus-pid", _dt.now(_tz.utc) - _td(days=1), None, pid=True)   # type: ignore[arg-type]
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["sibling"]


def test_a_record_that_vanishes_mid_scan_is_not_a_writer(tmp_path):
    """A dangling record (removed between listing and stat) describes no run in the window, both
    as a lane's newest record and as an older one."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    (runs / "gone").mkdir(parents=True)
    (runs / "gone" / "2026-01-01_00-00-00-000000.json").symlink_to(tmp_path / "missing-a")
    (runs / "gone" / "2026-01-02_00-00-00-000000.json").symlink_to(tmp_path / "missing-b")
    (runs / "stray-file.json").write_text("{}")    # not a lane directory
    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="nvd-import", runs_root=runs)
    assert _receipt(tmp_path / "q")["collisions"][0]["attribution"] == "sole-runner"


def test_missing_run_records_fall_back_to_the_legacy_contract(tmp_path):
    """No records directory means the runner is not writing them: "no other run" would be read
    off missing data, so the guard says it could not assess and keeps its legacy verdict."""
    wiki, before = _collision(tmp_path)
    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="nvd-import",
                      runs_root=tmp_path / "no-runs")
    record = _receipt(tmp_path / "q")["collisions"][0]
    assert record["attribution"] == "unassessed" and record["concurrent_jobs"] == []


def test_pages_without_readable_frontmatter_count_as_live(tmp_path):
    wiki = tmp_path / "wiki"
    for name, text in (("a/agent-tesla.md", "no frontmatter"),
                       ("b/agent_tesla.md", "---\nunterminated"),
                       ("c/agent.tesla.md", "---\n: [bad yaml\n---\n"),
                       ("d/agent tesla.md", "---\n- a list\n---\n")):
        path = wiki / "entities" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    assert guard._live(wiki / "entities/a/agent-tesla.md")
    assert guard._live(wiki / "entities/b/agent_tesla.md")
    assert guard._live(wiki / "entities/c/agent.tesla.md")
    assert guard._live(wiki / "entities/d/agent tesla.md")
    assert guard._live(wiki / "entities/missing.md")


def test_an_unloadable_identity_library_is_a_loud_error(monkeypatch, tmp_path):
    from unittest import mock
    import importlib.util as _util
    monkeypatch.setenv("OKENGINE_CRON_SCRIPTS", str(tmp_path))
    monkeypatch.setattr(guard, "_ID_LIB", None)
    monkeypatch.setattr(_util, "spec_from_file_location",
                        mock.create_autospec(_util.spec_from_file_location, return_value=None))
    with pytest.raises(RuntimeError, match="cannot load governed identity library"):
        guard._id_lib()
    monkeypatch.setattr(guard, "_ID_LIB", id_lib)


def _aged(path: Path, when: _dt) -> Path:
    _os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def _window(tmp_path, start: _dt):
    """A collision introduced in a window that opened at exactly `start`."""
    wiki, before = _collision(tmp_path)
    return wiki, guard.Snapshot(before.paths, start)


def test_a_malformed_declaration_before_the_real_one_does_not_hide_it(tmp_path):
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    _run(runs, "budget-guard", _dt.now(_tz.utc) - _td(seconds=30), _dt.now(_tz.utc))
    response = ("OKENGINE_ARTIFACT: not json\n" 'OKENGINE_ARTIFACT: {"path": 5}\n'
                'OKENGINE_ARTIFACT: {"path": "wiki/entities/c/v/cve-2025-62673.md"}\n')
    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="nvd-import", runs_root=runs,
                      response=response)


def test_nonpositive_pids_are_not_live_workers(tmp_path):
    """`os.kill(0, 0)` and `os.kill(-1, 0)` succeed (process group, every process), so a record
    carrying one must not read as a live run."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    long_ago = _dt.now(_tz.utc) - _td(days=1)
    _aged(_run(runs, "zero", long_ago, None, pid=0), long_ago)
    _aged(_run(runs, "minus-one", long_ago, None, pid=-1), long_ago)
    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="nvd-import", runs_root=runs)


def test_a_run_that_ended_exactly_when_the_window_opened_overlaps(tmp_path):
    runs = tmp_path / "runs"
    start = _dt.fromtimestamp(int(_dt.now(_tz.utc).timestamp()) - 60, _tz.utc)
    wiki, before = _window(tmp_path, start)
    _aged(_run(runs, "edge", start - _td(minutes=5), start), start - _td(hours=1))
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["edge"]


def test_an_unreadable_record_written_exactly_when_the_window_opened_counts(tmp_path):
    runs = tmp_path / "runs"
    start = _dt.fromtimestamp(int(_dt.now(_tz.utc).timestamp()) - 60, _tz.utc)
    wiki, before = _window(tmp_path, start)
    (runs / "edge").mkdir(parents=True)
    torn = runs / "edge" / "torn.json"
    torn.write_text("{torn")
    _aged(torn, start)
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["edge"]


def test_only_the_newest_record_of_a_lane_is_read_when_it_predates_the_window(tmp_path):
    """The newest record is read even when untouched since before the window: it may be a run
    still going. An older record untouched since before the window is never read."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    long_ago = _dt.now(_tz.utc) - _td(days=2)
    _aged(_run(runs, "still-going", long_ago - _td(days=1), long_ago - _td(days=1),
               name="2026-01-01_00-00-00-000000.json"), long_ago)
    _aged(_run(runs, "still-going", long_ago, None, pid=_os.getpid(),
               name="2026-01-02_00-00-00-000000.json"), long_ago)
    # An older record that would overlap IF read (it claims a live worker): it must be skipped.
    _aged(_run(runs, "finished", long_ago - _td(days=1), None, pid=_os.getpid(),
               name="2026-01-01_00-00-00-000000.json"), long_ago)
    _aged(_run(runs, "finished", long_ago, long_ago + _td(minutes=1),
               name="2026-01-02_00-00-00-000000.json"), long_ago)
    # Three records: the newest is the last, whatever the count.
    _aged(_run(runs, "three", long_ago - _td(days=3), long_ago - _td(days=3),
               name="2026-01-01_00-00-00-000000.json"), long_ago)
    _aged(_run(runs, "three", long_ago - _td(days=2), None, pid=_os.getpid(),
               name="2026-01-02_00-00-00-000000.json"), long_ago)
    _aged(_run(runs, "three", long_ago - _td(days=1), long_ago - _td(days=1),
               name="2026-01-03_00-00-00-000000.json"), long_ago)
    (runs / "empty-lane").mkdir()
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["still-going"]


def test_an_attributed_collision_carries_no_unattributed_note_and_is_not_logged(
        tmp_path, caplog):
    runs = tmp_path / "runs"
    runs.mkdir()
    wiki, before = _collision(tmp_path)
    with caplog.at_level("WARNING"), pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="nvd-import", runs_root=runs)
    assert "note" not in _receipt(tmp_path / "q")["collisions"][0]
    assert not caplog.records


def test_an_unattributed_collision_is_logged_with_the_overlapping_lanes(tmp_path, caplog):
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    _run(runs, "nvd-import", _dt.now(_tz.utc) - _td(minutes=1), _dt.now(_tz.utc))
    with caplog.at_level("WARNING"):
        records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard",
                                runs_root=runs)
    assert "while nvd-import also ran; not failing budget-guard" in records[0]["note"]
    assert [r.getMessage() for r in caplog.records] == [
        "slug-identity collision quarantined: entities/c/v/cve-2025-62673.md -> "
        "['entities/c/v/CVE-2025-62673.md', 'entities/c/v/cve-2025-62673.md'] "
        f"({records[0]['note']})"]


def test_a_worker_owned_by_another_user_counts_as_alive(tmp_path, monkeypatch):
    """`os.kill(pid, 0)` raises PermissionError when the process exists but belongs to another
    user: that worker is alive. The pid-1 test above only reaches this branch when it runs
    unprivileged (as root, signalling pid 1 simply succeeds), so pin it at the OS boundary."""
    from unittest import mock
    import os as _real_os
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    _run(runs, "other-user-lane", _dt.now(_tz.utc) - _td(days=1), None, pid=4242)
    monkeypatch.setattr(guard.os, "kill",
                        mock.create_autospec(_real_os.kill, side_effect=PermissionError))
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["other-user-lane"]


def _torn_record_at(runs: Path, lane: str, when: _dt) -> None:
    (runs / lane).mkdir(parents=True, exist_ok=True)
    torn = runs / lane / "torn.json"
    torn.write_text("{not json")
    _os.utime(torn, (when.timestamp(), when.timestamp()))


def test_a_record_stamped_just_before_the_window_by_a_coarse_clock_still_counts(tmp_path):
    """File mtimes come from the kernel's coarse clock, which can lag the precise clock the window
    start is read from by milliseconds (and some filesystems keep 1-2 s timestamps). A record
    written just after the window opened can therefore carry an mtime just before it; reading it
    as "before the window" would blame this job for a bystander's page. The CI race, pinned: the
    public-compatibility 3.11 job failed on exactly this (pipeline 11580)."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    _torn_record_at(runs, "coarse-lane", before.taken_at - _td(milliseconds=1500))
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["attribution"] == "unattributed"
    assert records[0]["concurrent_jobs"] == ["coarse-lane"]


def test_a_record_written_well_before_the_window_is_still_ignored(tmp_path):
    """Negative space for the skew allowance: it is seconds, not open-ended."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    (runs / "old-lane").mkdir(parents=True)
    _torn_record_at(runs, "old-lane", before.taken_at - _td(milliseconds=2500))
    (runs / "old-lane" / "zz-newest.json").write_text(
        '{"started_at": "2000-01-01T00:00:00+00:00", "ended_at": "2000-01-01T00:00:01+00:00"}')
    with pytest.raises(guard.SlugIdentityCollision):
        guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)


def test_a_record_stamped_exactly_at_the_skew_boundary_counts(tmp_path):
    """The allowance is inclusive: a record stamped exactly two seconds before the window start
    is inside it."""
    runs = tmp_path / "runs"
    wiki, before = _collision(tmp_path)
    (runs / "edge-lane").mkdir(parents=True)
    torn = runs / "edge-lane" / "torn.json"
    torn.write_text("{not json")
    edge = before.taken_at - _td(seconds=2)
    ns = int(edge.timestamp()) * 1_000_000_000 + edge.microsecond * 1_000
    _os.utime(torn, ns=(ns, ns))
    records = guard.enforce(wiki, before, tmp_path / "q", job_id="budget-guard", runs_root=runs)
    assert records[0]["concurrent_jobs"] == ["edge-lane"]
