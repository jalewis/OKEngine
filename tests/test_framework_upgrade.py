"""framework upgrade — pin reconciliation + migration registry + state (okengine#66 Phase 1)."""
import importlib.util
import json
import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mod():
    return _load("framework_upgrade")


def _meta():
    return _load("engine_meta")


def _pack(tmp_path, version, hermes=None):
    p = tmp_path / "pack"
    p.mkdir()
    body = f"version: {version}\n" + (f"hermes_pin: {hermes}\n" if hermes else "")
    (p / "engine.version").write_text(body, encoding="utf-8")
    return p


def _migration(dir_, *, id_, frm, to, marker="MIGRATED.txt"):
    dir_.mkdir(exist_ok=True)
    (dir_ / f"m_{id_}.py").write_text(
        f'ID = "{id_}"\nFROM = "{frm}"\nTO = "{to}"\nDESCRIPTION = "test"\n'
        f'def apply(pack, dry_run):\n'
        f'    if not dry_run: (pack / "{marker}").write_text("done")\n'
        f'    return ["wrote {marker}"]\n', encoding="utf-8")
    return dir_


# --- read_pin ---------------------------------------------------------------

def test_read_pin_parses_version_and_hermes(tmp_path):
    m = _mod()
    p = _pack(tmp_path, "v0.4.0", hermes="v2026.6.19")
    assert m.read_pin(p) == ("v0.4.0", "v2026.6.19")


def test_read_pin_missing(tmp_path):
    m = _mod()
    (tmp_path / "empty").mkdir()
    assert m.read_pin(tmp_path / "empty") == (None, None)


# --- plan_upgrade -----------------------------------------------------------

def test_plan_current_when_pin_matches(tmp_path):
    m, meta = _mod(), _meta()
    pl = m.plan_upgrade(_pack(tmp_path, "v0.5.0"), "v0.5.0", None, [], meta)
    assert pl.status == "current"


def test_plan_compatible_for_patch_newer(tmp_path):
    m, meta = _mod(), _meta()
    pl = m.plan_upgrade(_pack(tmp_path, "v0.5.0"), "v0.5.2", None, [], meta)
    assert pl.status == "compatible"          # same 0.5 series, engine patch-newer


def test_plan_upgrade_for_minor_bump(tmp_path):
    m, meta = _mod(), _meta()
    pl = m.plan_upgrade(_pack(tmp_path, "v0.4.0"), "v0.5.0", None, [], meta)
    assert pl.status == "upgrade"             # 0.4 -> 0.5 is the breaking unit


def test_plan_ahead_refuses_inverted_upgrade_range(tmp_path):
    m, meta = _mod(), _meta()
    pl = m.plan_upgrade(_pack(tmp_path, "v0.6.0"), "v0.5.0", None, [], meta)
    assert pl.status == "ahead"
    assert pl.migrations == []
    assert "NEWER" in m.render(pl)


def test_plan_unknown_when_no_pin(tmp_path):
    m, meta = _mod(), _meta()
    (tmp_path / "p").mkdir()
    pl = m.plan_upgrade(tmp_path / "p", "v0.5.0", None, [], meta)
    assert pl.status == "unknown"


# --- migration registry + applicability -------------------------------------

def test_applicable_selects_to_in_range(tmp_path):
    m, meta = _mod(), _meta()
    md = _migration(tmp_path / "migs", id_="x", frm="v0.4.0", to="v0.5.0")
    migs = m.load_migrations(md)
    assert [x.id for x in m.applicable(migs, "v0.4.0", "v0.5.0", meta)] == ["x"]
    assert m.applicable(migs, "v0.5.0", "v0.5.0", meta) == []     # to == pin -> excluded


def test_broken_migration_fails_loud(tmp_path):
    m = _mod()
    md = tmp_path / "migs"; md.mkdir()
    (md / "m_bad.py").write_text("raise ValueError('boom')\n", encoding="utf-8")
    try:
        m.load_migrations(md)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "m_bad.py" in str(e)


# --- apply + state idempotency ----------------------------------------------

def test_apply_bumps_pin_runs_migration_records_state(tmp_path):
    m, meta = _mod(), _meta()
    pack = _pack(tmp_path, "v0.4.0")
    md = _migration(tmp_path / "migs", id_="m1", frm="v0.4.0", to="v0.5.0")
    plan = m.plan_upgrade(pack, "v0.5.0", "v2026.6.19", m.load_migrations(md), meta)
    changes = m.apply_upgrade(pack, plan, "2026-06-25T00:00:00+00:00")

    assert m.read_pin(pack) == ("v0.5.0", "v2026.6.19")          # pin bumped
    assert (pack / "MIGRATED.txt").is_file()                      # transform ran
    assert any("m1" in c for c in changes)
    state = json.loads((pack / ".okengine" / "migrations-state.json").read_text())
    assert state["engine_version"] == "v0.5.0" and "m1" in state["applied"]
    assert state["history"][0]["to"] == "v0.5.0"


def test_reapply_is_idempotent(tmp_path):
    m, meta = _mod(), _meta()
    pack = _pack(tmp_path, "v0.4.0")
    md = _migration(tmp_path / "migs", id_="m1", frm="v0.4.0", to="v0.5.0")
    migs = m.load_migrations(md)
    m.apply_upgrade(pack, m.plan_upgrade(pack, "v0.5.0", None, migs, meta), "t0")
    # second plan sees it already applied -> nothing pending
    again = m.plan_upgrade(pack, "v0.5.0", None, migs, meta)
    assert again.status == "current" and again.migrations == []


def test_apply_records_state_incrementally_per_migration(tmp_path):  # invariant-audit #351
    """apply_upgrade records each migration's id as it COMPLETES, so a crash part-way through a batch
    leaves the finished migrations recorded and only the in-flight one replays. Here the 2nd migration
    raises: m1 must already be in `applied` (so it won't re-run), m2 must not. Before the fix,
    record_state ran once AFTER the loop, so a mid-batch failure recorded NOTHING and m1 re-applied on
    the next run — harmful for a non-idempotent migration."""
    m, meta = _mod(), _meta()
    pack = _pack(tmp_path, "v0.4.0")
    md = tmp_path / "migs"
    _migration(md, id_="m1", frm="v0.4.0", to="v0.5.0")
    (md / "m_m2.py").write_text(                                   # m2 blows up mid-apply
        'ID = "m2"\nFROM = "v0.5.0"\nTO = "v0.6.0"\nDESCRIPTION = "boom"\n'
        'def apply(pack, dry_run):\n    raise RuntimeError("hard fail mid-batch")\n',
        encoding="utf-8")
    plan = m.plan_upgrade(pack, "v0.6.0", None, m.load_migrations(md), meta)
    assert [mig.id for mig in plan.migrations] == ["m1", "m2"]     # both pending, ordered
    with pytest.raises(RuntimeError):
        m.apply_upgrade(pack, plan, "t0")
    state = json.loads((pack / ".okengine" / "migrations-state.json").read_text())
    assert "m1" in state["applied"], f"completed migration not recorded: {state}"
    assert "m2" not in state["applied"], f"failed migration wrongly recorded: {state}"


# --- CLI main ---------------------------------------------------------------

def test_main_dryrun_does_not_change_pin(tmp_path, capsys):
    m = _mod()
    pack = _pack(tmp_path, "v0.1.0")          # definitely older than any real engine release
    rc = m.main([str(pack), "--migrations-dir", str(tmp_path / "none")])
    out = capsys.readouterr().out
    assert rc == 0 and "dry-run" in out
    assert m.read_pin(pack)[0] == "v0.1.0"    # unchanged


def test_main_apply_bumps_to_running_engine(tmp_path):
    m, meta = _mod(), _meta()
    target = meta.engine_release()
    if not target:
        return                                 # no manifest in this checkout — skip
    pack = _pack(tmp_path, "v0.1.0")
    # --no-validate: a bare fixture isn't a full pack, so skip the roll-forward gate here
    # (the gate is covered by test_main_apply_gate_*); this asserts the pin bump.
    rc = m.main([str(pack), "--apply", "--no-validate", "--migrations-dir", str(tmp_path / "none")])
    assert rc == 0
    assert m.read_pin(pack)[0] == target       # bumped to the real engine release


def test_main_refuses_to_downgrade_pack_ahead_of_running_engine(tmp_path, capsys):
    m = _mod()
    pack = _pack(tmp_path, "v999.0.0")

    assert m.main([
        str(pack), "--apply", "--no-validate", "--migrations-dir", str(tmp_path / "none")
    ]) == 2

    captured = capsys.readouterr()
    assert "downgrade is unsupported" in captured.out
    assert "refusing to rewrite a newer pack" in captured.err
    assert m.read_pin(pack)[0] == "v999.0.0"
    assert not (pack / m.SNAPSHOTS_REL).exists()
    assert not (pack / m.STATE_REL).exists()


# --- Phase 2: real transforms, dry-run preview, pack hooks, roll-forward gate ---

def _transform_migration(dir_, *, id_, frm, to, target="data.txt"):
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"m_{id_}.py").write_text(
        f'ID = "{id_}"\nFROM = "{frm}"\nTO = "{to}"\nDESCRIPTION = "transform"\n'
        f'def apply(pack, dry_run):\n'
        f'    p = pack / "{target}"\n'
        f'    if not dry_run: p.write_text("transformed")\n'
        f'    return ["would set {target}" if dry_run else "set {target}"]\n',
        encoding="utf-8")
    return dir_


def test_preview_is_dry_run_and_performs_nothing(tmp_path):
    m, meta = _mod(), _meta()
    pack = _pack(tmp_path, "v0.4.0")
    md = _transform_migration(tmp_path / "migs", id_="t1", frm="v0.4.0", to="v0.5.0")
    plan = m.plan_upgrade(pack, "v0.5.0", None, m.load_migrations(md), meta)
    out = m.preview_upgrade(pack, plan)
    assert any("would set" in c for c in out)
    assert not (pack / "data.txt").exists()         # preview mutated nothing


def test_apply_runs_real_transform(tmp_path):
    m, meta = _mod(), _meta()
    pack = _pack(tmp_path, "v0.4.0")
    md = _transform_migration(tmp_path / "migs", id_="t1", frm="v0.4.0", to="v0.5.0")
    plan = m.plan_upgrade(pack, "v0.5.0", None, m.load_migrations(md), meta)
    m.apply_upgrade(pack, plan, "t0")
    assert (pack / "data.txt").read_text() == "transformed"


def test_pack_local_migrations_discovered(tmp_path):
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    eng = _transform_migration(tmp_path / "eng", id_="e1", frm="v0.4.0", to="v0.5.0")
    _transform_migration(m.pack_migrations_dir(pack), id_="p1", frm="v0.4.0", to="v0.5.0")
    ids = {x.id for x in m.load_all_migrations(eng, pack)}
    assert {"e1", "p1"} <= ids                      # both engine + pack-local


def test_pack_migration_overrides_engine_by_id(tmp_path):
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    eng = _transform_migration(tmp_path / "eng", id_="dup", frm="v0.4.0", to="v0.5.0", target="ENGINE.txt")
    _transform_migration(m.pack_migrations_dir(pack), id_="dup", frm="v0.4.0", to="v0.5.0", target="PACK.txt")
    dup = [x for x in m.load_all_migrations(eng, pack) if x.id == "dup"]
    assert len(dup) == 1                             # de-duped by id
    dup[0].apply_fn(pack, False)
    assert (pack / "PACK.txt").exists() and not (pack / "ENGINE.txt").exists()   # pack won


def test_pack_migration_diff_toversion_id_collision_fails_loud(tmp_path):  # invariant-audit B5.2
    """A pack migration reusing an ENGINE migration's id but with a DIFFERENT to_version is an
    accidental collision that would silently suppress the engine migration forever. Fail loud."""
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    eng = _transform_migration(tmp_path / "eng", id_="dup", frm="v0.4.0", to="v0.5.0", target="ENGINE.txt")
    _transform_migration(m.pack_migrations_dir(pack), id_="dup", frm="v0.5.0", to="v0.6.0", target="PACK.txt")
    with pytest.raises(SystemExit, match="collides with the ENGINE"):
        m.load_all_migrations(eng, pack)


def test_roll_forward_gate_samples_page_conformance(tmp_path, monkeypatch):  # invariant-audit B5.3
    """The roll-forward gate must catch a migration that corrupted vault pages — `framework validate`
    only checks wiki/ exists. _sample_page_failures runs pages through the OKF conformance gate."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    pack = tmp_path / "pack"
    (pack / "wiki" / "entities").mkdir(parents=True)
    (pack / "schema.yaml").write_text("types:\n  entity: {required: [type]}\n", encoding="utf-8")
    (pack / "wiki" / "entities" / "good.md").write_text("---\ntype: entity\nid: entities:good\nname: Good\n---\nbody\n", encoding="utf-8")
    (pack / "wiki" / "entities" / "bad.md").write_text("no frontmatter — a migration mangled this\n", encoding="utf-8")
    fails = m._sample_page_failures(pack, cap=300)
    assert any("bad.md" in f for f in fails), fails
    assert not any("good.md" in f for f in fails), fails


def test_main_apply_gate_fails_returns_one(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path, "v0.1.0")
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (False, "stub: broken"))
    assert m.main([str(pack), "--apply", "--migrations-dir", str(tmp_path / "none")]) == 1


def test_main_apply_gate_passes_returns_zero(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path, "v0.1.0")
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (True, "stub: ok"))
    assert m.main([str(pack), "--apply", "--migrations-dir", str(tmp_path / "none")]) == 0


def test_main_no_validate_skips_gate(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path, "v0.1.0")
    seen = {"n": 0}
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (seen.__setitem__("n", seen["n"] + 1), (False, "x"))[1])
    rc = m.main([str(pack), "--apply", "--no-validate", "--migrations-dir", str(tmp_path / "none")])
    assert rc == 0 and seen["n"] == 0               # gate not invoked


# --- Phase 3: snapshot + automatic rollback ---------------------------------

def test_snapshot_copies_source_excludes_runtime(tmp_path):
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    (pack / "schema.yaml").write_text("x: 1\n")
    (pack / "data").mkdir(); (pack / "data" / "reference.json").write_text('{"version": 1}\n')
    (pack / ".git").mkdir(); (pack / ".git" / "HEAD").write_text("ref")
    (pack / ".hermes-data").mkdir(); (pack / ".hermes-data" / "big.log").write_text("noise")
    snap = m.snapshot(pack, "snap1", {"to": "v0.5.0"})
    tree = snap / "tree"
    assert (tree / "schema.yaml").read_text() == "x: 1\n"
    assert (tree / "data" / "reference.json").read_text() == '{"version": 1}\n'
    assert (tree / "engine.version").exists()
    assert not (tree / ".git").exists()                  # VCS excluded
    assert not (tree / ".hermes-data").exists()          # runtime excluded
    assert json.loads((snap / "manifest.json").read_text())["to"] == "v0.5.0"


def test_restore_reverts_pack_data_source(tmp_path):
    """`<pack>/data/` is distributable pack source, not `.hermes-data` runtime state."""
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    source = pack / "data" / "reference.json"
    source.parent.mkdir()
    source.write_text('{"version": 1}\n')
    snap = m.snapshot(pack, "s-data")

    source.write_text('{"version": 2}\n')
    added = pack / "data" / "migration-added.json"
    added.write_text('{"new": true}\n')
    m.restore(pack, snap)

    assert source.read_text() == '{"version": 1}\n'
    assert not added.exists()


def test_restore_reverts_modify_add_delete(tmp_path):
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    (pack / "keep.txt").write_text("orig")
    (pack / "gone.txt").write_text("present")
    snap = m.snapshot(pack, "s1")
    (pack / "keep.txt").write_text("CHANGED")            # modify
    (pack / "added.txt").write_text("new")               # add
    (pack / "gone.txt").unlink()                         # delete
    m.restore(pack, snap)
    assert (pack / "keep.txt").read_text() == "orig"     # reverted
    assert not (pack / "added.txt").exists()             # added file removed
    assert (pack / "gone.txt").read_text() == "present"  # deleted file recreated


def test_prune_keeps_newest_n(tmp_path):
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    base = pack / m.SNAPSHOTS_REL; base.mkdir(parents=True)
    for ts in ["20260101T000000", "20260102T000000", "20260103T000000", "20260104T000000"]:
        (base / ts).mkdir()
    assert m.prune_snapshots(pack, keep=2) == 2
    assert sorted(d.name for d in base.iterdir()) == ["20260103T000000", "20260104T000000"]


def test_main_apply_gate_fail_auto_rolls_back(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    md = _transform_migration(tmp_path / "migs", id_="t1", frm="v0.4.0", to="v0.5.0")
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (False, "stub: broken"))
    rc = m.main([str(pack), "--apply", "--migrations-dir", str(md)])
    assert rc == 1
    assert not (pack / "data.txt").exists()              # transform rolled back
    assert m.read_pin(pack)[0] == "v0.4.0"               # pin reverted
    assert not (pack / m.STATE_REL).exists()             # state write undone too (.okengine/migrations-state.json)
    # the spent snapshot was cleaned up
    snaps = pack / m.SNAPSHOTS_REL
    assert not snaps.is_dir() or not any(snaps.iterdir())


def test_rollback_preserves_concurrent_vault_write(tmp_path, monkeypatch):  # invariant-audit #12
    """A content lane / MCP write that lands DURING the roll-forward gate (after the snapshot)
    must SURVIVE an automatic rollback — restore() may remove only the migration's own additions,
    never live-vault pages written by another process. Before the fix restore() deleted every file
    'newer than the snapshot', destroying the concurrent write."""
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    md = _transform_migration(tmp_path / "migs", id_="t1", frm="v0.4.0", to="v0.5.0")  # adds data.txt
    live = pack / "wiki" / "a" / "concurrent-page.md"

    def validator(p):
        # simulate a concurrent live-vault write during the (slower) validation window, then FAIL
        live.parent.mkdir(parents=True, exist_ok=True)
        live.write_text("live content written after the snapshot", encoding="utf-8")
        return (False, "stub: broken")

    monkeypatch.setattr(m, "VALIDATOR", validator)
    rc = m.main([str(pack), "--apply", "--migrations-dir", str(md)])
    assert rc == 1
    assert not (pack / "data.txt").exists()              # migration's own file rolled back
    assert m.read_pin(pack)[0] == "v0.4.0"               # pin reverted
    assert live.read_text() == "live content written after the snapshot"  # concurrent write SURVIVES


def test_rollback_preserves_concurrent_modification(tmp_path, monkeypatch):  # invariant-audit M8
    """The #12 sibling for MODIFY, not ADD: a content lane / MCP write that EDITS a pre-existing
    page during the roll-forward gate must survive rollback. The migration never touched that page,
    so restore() must not revert it to the snapshot. Before the fix restore() rewrote every
    snapshotted file to snapshot content, silently reverting the concurrent edit."""
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    live = pack / "wiki" / "a" / "existing-page.md"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("pre-upgrade content", encoding="utf-8")   # exists BEFORE the snapshot
    md = _transform_migration(tmp_path / "migs", id_="t1", frm="v0.4.0", to="v0.5.0")  # touches only data.txt

    def validator(p):
        live.write_text("edited by a concurrent lane during validation", encoding="utf-8")
        return (False, "stub: broken")

    monkeypatch.setattr(m, "VALIDATOR", validator)
    rc = m.main([str(pack), "--apply", "--migrations-dir", str(md)])
    assert rc == 1
    assert not (pack / "data.txt").exists()                    # migration's own file rolled back
    assert m.read_pin(pack)[0] == "v0.4.0"                     # pin reverted
    assert live.read_text() == "edited by a concurrent lane during validation"  # concurrent EDIT survives


def test_main_apply_no_snapshot_does_not_rollback(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    md = _transform_migration(tmp_path / "migs", id_="t1", frm="v0.4.0", to="v0.5.0")
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (False, "stub: broken"))
    rc = m.main([str(pack), "--apply", "--no-snapshot", "--migrations-dir", str(md)])
    assert rc == 1
    assert m.read_pin(pack)[0] != "v0.4.0"               # not rolled back (pin stayed bumped)


def test_main_apply_success_keeps_changes_and_prunes(tmp_path, monkeypatch):
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    md = _transform_migration(tmp_path / "migs", id_="t1", frm="v0.4.0", to="v0.5.0")
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (True, "stub: ok"))
    rc = m.main([str(pack), "--apply", "--migrations-dir", str(md)])
    assert rc == 0
    assert (pack / "data.txt").read_text() == "transformed"   # change persisted
    assert m.read_pin(pack)[0] != "v0.4.0"
    snaps = pack / m.SNAPSHOTS_REL
    assert snaps.is_dir() and len(list(snaps.iterdir())) == 1  # kept (default keep=3)


def test_unparseable_migration_version_fails_loud(tmp_path):  # invariant-audit #25
    """A migration whose TO doesn't parse to vX.Y.Z would sort to (0,0,0) and be SILENTLY dropped
    from every (pin, target] range — it never runs, the pack is marked upgraded but un-migrated.
    A misdeclared to_version is a packaging bug: applicable() must fail loud, not no-op."""
    import pytest
    m, meta = _mod(), _meta()
    md = _migration(tmp_path / "migrations", id_="bad", frm="v0.9.0", to="v0.10")  # missing patch
    migs = m.load_migrations(md)
    with pytest.raises(ValueError, match="unparseable to_version"):
        m.applicable(migs, "v0.9.0", "v0.10.0", meta)


def _raising_migration(dir_, *, id_, frm, to):
    dir_.mkdir(exist_ok=True)
    (dir_ / f"m_{id_}.py").write_text(
        f'ID = "{id_}"\nFROM = "{frm}"\nTO = "{to}"\nDESCRIPTION = "raises mid-apply"\n'
        f'def apply(pack, dry_run):\n'
        f'    if not dry_run:\n'
        f'        (pack / "half.txt").write_text("partial")\n'
        f'        raise IOError("boom on page 137")\n'
        f'    return ["would write half.txt"]\n', encoding="utf-8")
    return dir_


def test_migration_raising_mid_apply_rolls_back(tmp_path, monkeypatch):
    """A migration that mutates files THEN raises must auto-rollback (the Phase-3 promise), not leave
    the pack half-upgraded. Regression: rollback was wired only to the roll-forward gate, so an
    apply-time exception fell through to a stack trace with the snapshot untouched (invariant-audit)."""
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    (pack / "orig.txt").write_text("before")
    md = _raising_migration(tmp_path / "migs", id_="boom", frm="v0.4.0", to="v0.5.0")
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (True, "ok"))   # never reached — apply raises first
    rc = m.main([str(pack), "--apply", "--migrations-dir", str(md)])
    assert rc == 1                                      # apply failed
    assert not (pack / "half.txt").exists()             # the partial write was rolled back
    assert (pack / "orig.txt").read_text() == "before"  # untouched
    assert m.read_pin(pack)[0] == "v0.4.0"              # pin NOT bumped


def test_pack_migration_override_semver_spelling_not_a_collision(tmp_path):  # invariant-audit B5.2 re-verify
    """The collision guard must compare to_version SEMANTICALLY, not by raw string. A LEGIT override
    (same id, same FROM, same version) that merely SPELLS the version differently — "v0.6.0" vs
    "0.6.0" — is the sanctioned same-to_version override and must NOT trip the collision SystemExit."""
    m = _mod()
    pack = _pack(tmp_path, "v0.5.0")
    eng = _transform_migration(tmp_path / "eng", id_="dup", frm="v0.5.0", to="v0.6.0", target="ENGINE.txt")
    # pack spells the SAME target version without the leading 'v'
    _transform_migration(m.pack_migrations_dir(pack), id_="dup", frm="v0.5.0", to="0.6.0", target="PACK.txt")
    merged = m.load_all_migrations(eng, pack)            # must NOT raise
    dup = [x for x in merged if x.id == "dup"]
    assert len(dup) == 1                                 # de-duped, pack won — a real override
    dup[0].apply_fn(pack, False)
    assert (pack / "PACK.txt").exists() and not (pack / "ENGINE.txt").exists()


def test_collision_guard_distinguishes_fourth_version_component(tmp_path):  # invariant-audit B5.2 re-verify
    """The semantic compare must NOT be the lossy 3-tuple _semver: "0.6.0" and "0.6.0.1" are
    GENUINELY different versions that happen to share X.Y.Z, so an id-reuse across them is a real
    accidental collision and must still fail loud (not be waved through as a same-version override)."""
    m = _mod()
    pack = _pack(tmp_path, "v0.5.0")
    eng = _transform_migration(tmp_path / "eng", id_="dup", frm="v0.5.0", to="0.6.0", target="ENGINE.txt")
    _transform_migration(m.pack_migrations_dir(pack), id_="dup", frm="v0.5.0", to="0.6.0.1", target="PACK.txt")
    with pytest.raises(SystemExit, match="collides with the ENGINE"):
        m.load_all_migrations(eng, pack)


def test_roll_forward_catches_minority_type_corruption(tmp_path, monkeypatch):  # invariant-audit B5.3
    """A migration corrupts by TYPE, and a single OKF namespace holds MANY types. The exhaustive scan
    must catch a MINORITY type corrupted inside a large mixed namespace — 6 `metric` pages stripped
    of a required field among 200 conformant `vendor` pages, in ONE namespace."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    pack = tmp_path / "pack"
    ns = pack / "wiki" / "landscape"
    ns.mkdir(parents=True)
    (pack / "schema.yaml").write_text(
        "types:\n  vendor: {required: [type]}\n  metric: {required: [type, value]}\n", encoding="utf-8")
    for i in range(200):
        (ns / f"v{i:03d}.md").write_text(
            f"---\ntype: vendor\nid: landscape:v{i:03d}\nname: V{i}\n---\nbody\n", encoding="utf-8")
    for i in range(6):
        (ns / f"m{i:02d}.md").write_text(     # type:metric but the required `value` was dropped
            f"---\ntype: metric\nid: landscape:m{i:02d}\nname: M{i}\n---\nbody\n", encoding="utf-8")
    fails = m._sample_page_failures(pack, cap=20)
    assert any("landscape/m" in f for f in fails), f"scan missed the corrupt minority type: {fails}"
    assert not any("landscape/v" in f for f in fails), fails         # majority type not falsely flagged


def test_roll_forward_catches_partial_within_type_corruption(tmp_path, monkeypatch):  # invariant-audit B5.3 (3 re-verify rounds)
    """The case every SAMPLED approach missed: a PARTIAL / retype corruption — only 6 pages of a
    10,000-page type are broken (a botched retype that omitted a newly-required field). A per-type
    strided sample skips them ~96% of the time; the exhaustive scan must catch them. Downsized to 400
    conformant + 6 corrupt pages of the SAME type (so no stratification could isolate the 6)."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    pack = tmp_path / "pack"
    ns = pack / "wiki" / "indicators"
    ns.mkdir(parents=True)
    (pack / "schema.yaml").write_text(
        "types:\n  indicator: {required: [type, value]}\n", encoding="utf-8")
    for i in range(400):
        (ns / f"i{i:04d}.md").write_text(
            f"---\ntype: indicator\nid: indicators:i{i:04d}\nvalue: v{i}\n---\nbody\n", encoding="utf-8")
    for i in range(6):                        # same type, missing the required `value` — a partial break
        (ns / f"bad{i:02d}.md").write_text(
            f"---\ntype: indicator\nid: indicators:bad{i:02d}\n---\nbody\n", encoding="utf-8")
    fails = m._sample_page_failures(pack, cap=50)
    assert any("indicators/bad" in f for f in fails), \
        f"exhaustive scan missed a partial-within-type corruption: {fails}"


def test_raw_conformance_scan_stays_fail_open_for_unknown_type(tmp_path, monkeypatch):
    """The runtime-profile raw scan remains fail-open; #207 adds a separate snapshot diff gate."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    pack = tmp_path / "pack"
    ns = pack / "wiki" / "actors"
    ns.mkdir(parents=True)
    (pack / "schema.yaml").write_text("types:\n  actor: {required: [type, id]}\n", encoding="utf-8")
    for i in range(20):
        (ns / f"a{i:02d}.md").write_text(
            f"---\ntype: actor\nid: actors:a{i:02d}\n---\nbody\n", encoding="utf-8")
    for i in range(6):     # retyped to a type absent from schema.yaml, but both required keys present
        (ns / f"r{i:02d}.md").write_text(
            f"---\ntype: threatactor\nid: actors:r{i:02d}\n---\nbody\n", encoding="utf-8")
    fails = m._sample_page_failures(pack, cap=50)
    # fail-open by design: the unknown type is NOT reported (documents the boundary, not an endorsement)
    assert not any("actors/r" in f for f in fails), \
        f"runtime-profile scan unexpectedly became strict: {fails}"


def test_unknown_type_regression_detects_new_value_without_flagging_preexisting(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    before = tmp_path / "before"
    after = tmp_path / "after"
    for root in (before, after):
        ns = root / "wiki" / "actors"
        ns.mkdir(parents=True)
        (root / "schema.yaml").write_text(
            "types:\n  actor: {required: [type, id]}\n", encoding="utf-8")
        (ns / "legacy.md").write_text(
            "---\ntype: legacy_unknown\nid: actors:legacy\n---\nold debt\n", encoding="utf-8")
    (before / "wiki" / "actors" / "changed.md").write_text(
        "---\ntype: actor\nid: actors:changed\n---\nbefore\n", encoding="utf-8")
    (after / "wiki" / "actors" / "changed.md").write_text(
        "---\ntype: threatactor\nid: actors:changed\n---\nafter\n", encoding="utf-8")

    regressions = m._unknown_type_regressions(before, after)
    assert regressions == [
        "actors/changed.md: unknown type 'threatactor' newly introduced by migration"
    ]
    assert not any("legacy_unknown" in item for item in regressions)


def test_unknown_type_regression_accepts_composed_extension_type_and_alias(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    before = tmp_path / "before"
    after = tmp_path / "after"
    for root in (before, after):
        (root / "wiki" / "items").mkdir(parents=True)
        (root / "schema.yaml").write_text(
            "types:\n  item: {required: [type]}\n", encoding="utf-8")
        composed = root / ".okengine" / "composed-schema.yaml"
        composed.parent.mkdir(parents=True)
        composed.write_text(
            "types:\n"
            "  item: {required: [type]}\n"
            "  extension_event: {required: [type]}\n"
            "type_aliases:\n"
            "  ext-event: extension_event\n",
            encoding="utf-8",
        )
    (after / "wiki" / "items" / "canonical.md").write_text(
        "---\ntype: extension_event\n---\nvalid extension type\n", encoding="utf-8")
    (after / "wiki" / "items" / "alias.md").write_text(
        "---\ntype: ext-event\n---\nvalid alias\n", encoding="utf-8")

    assert m._unknown_type_regressions(before, after) == []


# --- roll-forward is a REGRESSION gate, not an absolute-conformance gate (fleet-roll regression) ---

def _vault_pack(tmp_path, version="v0.11.2"):
    """A pack with engine.version + schema.yaml + a wiki/, for exercising the conformance gate."""
    pack = _pack(tmp_path, version)
    (pack / "schema.yaml").write_text("types:\n  entity: {required: [type, id]}\n", encoding="utf-8")
    ent = pack / "wiki" / "entities"
    ent.mkdir(parents=True)
    return pack, ent


def _corrupting_migration(dir_, *, id_, frm, to, target_rel):
    """A migration that overwrites `target_rel` (wiki-relative) with a non-conformant page."""
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"m_{id_}.py").write_text(
        f'ID = "{id_}"\nFROM = "{frm}"\nTO = "{to}"\nDESCRIPTION = "corrupt"\n'
        f'def apply(pack, dry_run):\n'
        f'    p = pack / "{target_rel}"\n'
        f'    if not dry_run: p.write_text("no frontmatter — corrupted by a bad migration\\n")\n'
        f'    return ["corrupted {target_rel}"]\n', encoding="utf-8")
    return dir_


def test_pin_bump_no_migration_ignores_preexisting_nonconformance(tmp_path, monkeypatch):  # fleet-roll regression
    """A pin-bump-only upgrade (no migrations) transformed nothing, so it must NOT roll back over
    PRE-EXISTING non-conformant pages — a real vault has them (older/agent-authored pages missing
    `id`). This is the exact regression the baseline-less exhaustive scan caused: it flagged 100+
    pre-existing pages and rolled back a clean pin bump on every fleet deployment."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (True, "ok"))   # bypass structural framework-validate noise
    pack, ent = _vault_pack(tmp_path)
    (ent / "good.md").write_text("---\ntype: entity\nid: entities:good\n---\nx\n", encoding="utf-8")
    (ent / "stale.md").write_text("---\ntype: entity\n---\npre-existing: missing id\n", encoding="utf-8")
    rc = m.main([str(pack), "--apply", "--migrations-dir", str(tmp_path / "none")])   # no migrations
    assert rc == 0, "pin-bump-only upgrade rolled back over pre-existing non-conformance"
    assert (ent / "stale.md").exists()                            # nothing rolled back / deleted


def test_conformance_regression_rolls_back(tmp_path, monkeypatch):  # roll-forward gate still works
    """A migration that MAKES a page non-conformant (conformant before, broken after) is a genuine
    regression — the gate must catch it and roll back."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (True, "ok"))
    pack, ent = _vault_pack(tmp_path)
    (ent / "x.md").write_text("---\ntype: entity\nid: entities:x\n---\nfine before\n", encoding="utf-8")
    md = _corrupting_migration(tmp_path / "migs", id_="wreck", frm="v0.11.2", to="v0.11.3",
                               target_rel="wiki/entities/x.md")
    rc = m.main([str(pack), "--apply", "--migrations-dir", str(md)])
    assert rc == 1                                                # regression → rolled back
    assert "frontmatter" in (ent / "x.md").read_text() or "type: entity" in (ent / "x.md").read_text()
    # after rollback the page is conformant again
    assert m._page_failure_map(pack) == {}, "rollback left a corrupted page behind"


def test_new_unknown_type_regression_rolls_back(tmp_path, monkeypatch):
    """A migration may not silently retype a page outside the composed taxonomy."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (True, "ok"))
    pack, ent = _vault_pack(tmp_path)
    page = ent / "x.md"
    page.write_text("---\ntype: entity\nid: entities:x\n---\nfine before\n", encoding="utf-8")
    md = tmp_path / "migs"
    md.mkdir()
    (md / "m_retype.py").write_text(
        'ID = "retype"\nFROM = "v0.11.2"\nTO = "v0.11.3"\nDESCRIPTION = "bad retype"\n'
        'def apply(pack, dry_run):\n'
        '    p = pack / "wiki/entities/x.md"\n'
        '    if not dry_run: p.write_text("---\\ntype: entitty\\nid: entities:x\\n---\\nbad\\n")\n'
        '    return ["retyped x.md"]\n',
        encoding="utf-8",
    )

    rc = m.main([str(pack), "--apply", "--migrations-dir", str(md)])

    assert rc == 1
    assert "type: entity\n" in page.read_text(encoding="utf-8")
    assert "entitty" not in page.read_text(encoding="utf-8")


def test_conformance_regression_ignores_preexisting_failure(tmp_path, monkeypatch):  # no false-rollback
    """A migration that touches OTHER pages must not roll back because a PRE-EXISTING page is already
    non-conformant — only pages the migration regressed count."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    monkeypatch.setattr(m, "VALIDATOR", lambda p: (True, "ok"))
    pack, ent = _vault_pack(tmp_path)
    (ent / "stale.md").write_text("---\ntype: entity\n---\npre-existing: missing id\n", encoding="utf-8")
    # a benign migration that writes a NEW, conformant page (doesn't touch stale.md)
    md = tmp_path / "migs"
    md.mkdir()
    (md / "m_add.py").write_text(
        'ID = "add"\nFROM = "v0.11.2"\nTO = "v0.11.3"\nDESCRIPTION = "add"\n'
        'def apply(pack, dry_run):\n'
        '    p = pack / "wiki/entities/new.md"\n'
        '    if not dry_run: p.write_text("---\\ntype: entity\\nid: entities:new\\n---\\nok\\n")\n'
        '    return ["added new.md"]\n', encoding="utf-8")
    rc = m.main([str(pack), "--apply", "--migrations-dir", str(md)])
    assert rc == 0, "rolled back over a pre-existing failure the migration didn't cause"
    assert (ent / "new.md").exists()                             # the migration's change stuck


def test_conformance_regressions_diff_is_before_after(tmp_path, monkeypatch):  # unit
    """`_conformance_regressions` reports only NEW or CHANGED failures, never unchanged pre-existing ones."""
    m = _mod()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    before = tmp_path / "before"; after = tmp_path / "after"
    for r in (before, after):
        (r / "wiki" / "entities").mkdir(parents=True)
        (r / "schema.yaml").write_text("types:\n  entity: {required: [type, id]}\n", encoding="utf-8")
    be, ae = before / "wiki" / "entities", after / "wiki" / "entities"
    # a page that is BROKEN on both sides (pre-existing) — must NOT be reported
    for e in (be, ae):
        (e / "stale.md").write_text("---\ntype: entity\n---\nno id\n", encoding="utf-8")
    # a page conformant before, broken after — a regression → reported
    (be / "reg.md").write_text("---\ntype: entity\nid: entities:reg\n---\nok\n", encoding="utf-8")
    (ae / "reg.md").write_text("no frontmatter\n", encoding="utf-8")
    out = m._conformance_regressions(before, after)
    assert any("reg.md" in x for x in out)
    assert not any("stale.md" in x for x in out), f"pre-existing failure leaked into regressions: {out}"


# --- invariant-audit v0.11.5 batch-4 -----------------------------------------

def test_snapshot_refuses_when_disk_full(tmp_path, monkeypatch):  # invariant-audit #31
    """snapshot() copies the whole source onto the vault's own filesystem; without a space check an
    ENOSPC mid-copy left a partial, manifest-less snapshot AND filled the disk. Refuse up front."""
    import collections
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    (pack / "big.md").write_text("x" * 1000)
    D = collections.namedtuple("D", "total used free")
    monkeypatch.setattr(m.shutil, "disk_usage", lambda p: D(0, 0, 10))   # ~no free space
    with pytest.raises(OSError):
        m.snapshot(pack, "s1")
    assert not (pack / m.SNAPSHOTS_REL / "s1").exists()                  # no partial snapshot left


def test_snapshot_cleans_partial_on_copy_failure(tmp_path, monkeypatch):  # invariant-audit #31
    """A copy failure (ENOSPC / Ctrl-C) mid-snapshot must remove the partial dir so nothing mistakes
    a manifest-less husk for a valid restore point or skews prune retention."""
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    (pack / "a.md").write_text("a")
    (pack / "b.md").write_text("b")
    real = m.shutil.copy2
    state = {"n": 0}
    def boom(src, dst):
        state["n"] += 1
        if state["n"] >= 2:
            raise OSError("ENOSPC")
        return real(src, dst)
    monkeypatch.setattr(m.shutil, "copy2", boom)
    with pytest.raises(OSError):
        m.snapshot(pack, "s2")
    assert not (pack / m.SNAPSHOTS_REL / "s2").exists()                  # partial removed


def test_rollback_quarantines_added_files_never_deletes(tmp_path):  # invariant-audit #32
    """The added/modified capture is non-atomic at vault scale, so a page a concurrent lane/MCP wrote
    during the window can be misclassified as migration-added. Rollback must QUARANTINE such files
    (move them under .okengine/rolled-back/<snap>/), never unlink — a rollback must not be lossy."""
    m = _mod()
    pack = _pack(tmp_path, "v0.4.0")
    snap = m.snapshot(pack, "s1")
    (pack / "wiki").mkdir(exist_ok=True)
    (pack / "wiki" / "live.md").write_text("concurrent content")        # a live write, not in snapshot
    m.restore(pack, snap, added={Path("wiki/live.md")}, modified=set())
    assert not (pack / "wiki" / "live.md").exists()                     # removed from its place
    q = pack / ".okengine" / "rolled-back" / "s1" / "wiki" / "live.md"
    assert q.read_text() == "concurrent content"                        # preserved, not destroyed


def test_duplicate_engine_migration_id_fails_loud(tmp_path):
    """invariant-audit: two engine migration files sharing an ID silently dropped one (dict-comp
    last-wins), so a real transform never ran for any pack while state recorded success. Must raise."""
    m = _mod()
    d = tmp_path / "migrations"
    _migration(d, id_="dup", frm="v0.4.0", to="v0.5.0", marker="A.txt")
    # a second file with the SAME id (copy-paste collision), different to_version
    (d / "m_dup2.py").write_text(
        'ID = "dup"\nFROM = "v0.5.0"\nTO = "v0.6.0"\nDESCRIPTION = "copy-paste collision"\n'
        'def apply(pack, dry_run):\n    return []\n', encoding="utf-8")
    pack = _pack(tmp_path, "v0.4.0")
    import pytest as _pytest
    with _pytest.raises(SystemExit):
        m.load_all_migrations(d, pack)
