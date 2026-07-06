"""framework upgrade — pin reconciliation + migration registry + state (okengine#66 Phase 1)."""
import importlib.util
import json
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
    (pack / ".git").mkdir(); (pack / ".git" / "HEAD").write_text("ref")
    (pack / ".hermes-data").mkdir(); (pack / ".hermes-data" / "big.log").write_text("noise")
    snap = m.snapshot(pack, "snap1", {"to": "v0.5.0"})
    tree = snap / "tree"
    assert (tree / "schema.yaml").read_text() == "x: 1\n"
    assert (tree / "engine.version").exists()
    assert not (tree / ".git").exists()                  # VCS excluded
    assert not (tree / ".hermes-data").exists()          # runtime excluded
    assert json.loads((snap / "manifest.json").read_text())["to"] == "v0.5.0"


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
