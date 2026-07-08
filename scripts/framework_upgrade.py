"""framework upgrade — reconcile a pack's engine pin to the running engine (okengine#66).

Today an engine minor bump (e.g. v0.4 -> v0.5) means hand-editing every pack's `engine.version`
or `framework validate` FAILs — with no path for vault-side changes that ride along. This makes
it one command:

    framework upgrade <pack>            # dry-run: show the gap + migrations that would apply
    framework upgrade <pack> --apply    # bump the pin, run registered migrations, record state

What it does on --apply: bumps `<pack>/engine.version` to this engine's release, runs any
registered migrations in (pinned, target] in order, and records what was applied to THIS vault
in `<pack>/.okengine/migrations-state.json` (so a re-run is idempotent). Migrations may carry
real vault/schema transforms: dry-run (the default) PREVIEWS them by calling each with
dry_run=True; --apply snapshots the pack source first, performs them, then runs a roll-forward
validation gate — and if the gate FAILS, it AUTOMATICALLY ROLLS BACK to the snapshot so a bad
migration never leaves a half-upgraded pack. A pack can ship its own migrations under
`<pack>/.okengine/migrations/`, merged with the engine's by to_version.

A migration is a module `migrations/m_*.py` exposing: ID, FROM, TO, DESCRIPTION, and
`apply(pack: Path, dry_run: bool) -> list[str]` (return human-readable change descriptions;
perform them only when dry_run is False).
"""
# NB: no `from __future__ import annotations` — the dataclasses below load via importlib
# (framework.py's loader doesn't register modules in sys.modules), and string annotations
# would make @dataclass's KW_ONLY check fail on the absent module entry. Real annotations
# sidestep it.
import argparse
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
DEFAULT_MIGRATIONS_DIR = _ROOT / "migrations"
STATE_REL = Path(".okengine") / "migrations-state.json"
SNAPSHOTS_REL = Path(".okengine") / "snapshots"
# Runtime/generated/VCS trees the snapshot+rollback scope skips — migrations transform the pack
# SOURCE (schema, crons, wiki, configs, .okengine state), not these.
SNAPSHOT_EXCLUDES = {".git", ".hermes-data", "data", "tmp", "logs",
                     "node_modules", ".venv", "__pycache__"}


def _engine_meta():
    spec = importlib.util.spec_from_file_location("engine_meta", _HERE / "engine_meta.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# --- migration registry ------------------------------------------------------

@dataclass
class Migration:
    id: str
    from_version: str
    to_version: str
    description: str
    apply_fn: Callable[[Path, bool], list]


def load_migrations(migrations_dir: Path) -> list:
    """Discover migration modules (`m_*.py`) in a directory, sorted by filename."""
    out: list = []
    if not migrations_dir.is_dir():
        return out
    # Let a migration `import import_lib` (and other engine helpers) — the engine scripts/ dir,
    # where this file lives — without each migration hard-coding a path (okengine#154).
    _eng = str(Path(__file__).resolve().parent)
    if _eng not in sys.path:
        sys.path.insert(0, _eng)
    for f in sorted(migrations_dir.glob("m_*.py")):  # glob-ok: flat migrations dir, not a sharded namespace
        spec = importlib.util.spec_from_file_location(f.stem, f)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:  # a broken migration must fail loudly, not silently skip
            raise RuntimeError(f"migration {f.name} failed to load: {e}")
        out.append(Migration(
            id=str(getattr(mod, "ID", f.stem)),
            from_version=str(getattr(mod, "FROM", "")),
            to_version=str(getattr(mod, "TO", "")),
            description=str(getattr(mod, "DESCRIPTION", "")),
            apply_fn=getattr(mod, "apply", lambda pack, dry: []),
        ))
    return out


# --- pure planning over versions/state (no I/O side effects) -----------------

def read_pin(pack: Path):
    """(version, hermes_pin) from <pack>/engine.version, or (None, None) if absent/unreadable."""
    ev = pack / "engine.version"
    if not ev.is_file():
        return None, None
    try:
        import yaml
        d = yaml.safe_load(ev.read_text(encoding="utf-8"))
    except Exception:
        d = None
    if isinstance(d, dict):
        return (str(d.get("version") or "").strip() or None,
                str(d.get("hermes_pin") or "").strip() or None)
    return None, None


def read_state(pack: Path) -> dict:
    f = pack / STATE_REL
    if f.is_file():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def applicable(migrations: list, pin: str, target: str, meta) -> list:
    """Migrations whose to_version is in (pin, target], ascending by to_version."""
    def key(v):
        return meta._semver(v) or (0, 0, 0)
    # A migration whose to_version doesn't parse to vX.Y.Z would fall back to (0,0,0), so
    # `pin < (0,0,0)` is always False and it is SILENTLY dropped from every (pin, target] range —
    # it never runs, no error, the pack is marked upgraded but un-migrated. A misdeclared
    # to_version is a packaging bug: fail loud instead of shipping a silent no-op (okengine#178).
    bad = [getattr(m, "id", None) or getattr(m, "to_version", "?")
           for m in migrations if meta._semver(getattr(m, "to_version", None)) is None]
    if bad:
        raise ValueError(f"migration(s) with an unparseable to_version (need vX.Y.Z): {bad}")
    pinv, tgtv = key(pin), key(target)
    sel = [m for m in migrations if pinv < key(m.to_version) <= tgtv]
    return sorted(sel, key=lambda m: key(m.to_version))


@dataclass
class Plan:
    status: str                 # current | compatible | upgrade | unknown
    pin: Optional[str]
    target: Optional[str]
    target_hermes: Optional[str]
    migrations: list = field(default_factory=list)        # pending (not yet applied)
    already_applied: list = field(default_factory=list)   # ids already in state


def plan_upgrade(pack: Path, target, target_hermes, migrations, meta) -> Plan:
    pin, _ = read_pin(pack)
    applied = set(read_state(pack).get("applied", []))
    if pin is None or target is None or meta._semver(pin) is None or meta._semver(target) is None:
        return Plan("unknown", pin, target, target_hermes)
    if pin == target:
        return Plan("current", pin, target, target_hermes)
    status = "compatible" if meta.satisfies_pin(pin, target) else "upgrade"
    migs = applicable(migrations, pin, target, meta)
    pending = [m for m in migs if m.id not in applied]
    return Plan(status, pin, target, target_hermes,
                pending, [m.id for m in migs if m.id in applied])


# --- apply (I/O) -------------------------------------------------------------

def write_pin(pack: Path, version: str, hermes_pin: Optional[str]) -> None:
    ev = pack / "engine.version"
    lines = ["# Engine release this pack targets. Managed by `framework upgrade` (okengine#66).",
             f"version: {version}"]
    if hermes_pin:
        lines.append(f"hermes_pin: {hermes_pin}")
    ev.write_text("\n".join(lines) + "\n", encoding="utf-8")


def record_state(pack: Path, target: str, applied_ids: list, now_iso: str) -> None:
    f = pack / STATE_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    state = read_state(pack)
    state["engine_version"] = target
    state["applied"] = sorted(set(state.get("applied", [])) | set(applied_ids))
    state.setdefault("history", []).append(
        {"at": now_iso, "to": target, "migrations": list(applied_ids)})
    f.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def apply_upgrade(pack: Path, plan: Plan, now_iso: str) -> list:
    changes: list = []
    for m in plan.migrations:
        for c in (m.apply_fn(pack, False) or []):
            changes.append(f"[{m.id}] {c}")
    write_pin(pack, plan.target, plan.target_hermes)
    record_state(pack, plan.target, [m.id for m in plan.migrations], now_iso)
    return changes


# --- pack-aware migrations · dry-run preview · roll-forward gate (Phase 2) ----

def pack_migrations_dir(pack: Path) -> Path:
    """A pack ships its own migrations here, run alongside the engine's."""
    return pack / ".okengine" / "migrations"


def load_all_migrations(engine_dir: Path, pack: Path) -> list:
    """Engine migrations + the pack's own, merged and ordered by to_version (then id).
    A pack-local migration with the same id as an engine one overrides it (pack wins)."""
    by_id = {}
    for m in load_migrations(engine_dir):
        by_id[m.id] = m
    for m in load_migrations(pack_migrations_dir(pack)):      # pack-aware hook
        by_id[m.id] = m
    meta = _engine_meta()
    return sorted(by_id.values(), key=lambda m: (meta._semver(m.to_version) or (0, 0, 0), m.id))


def preview_upgrade(pack: Path, plan: Plan) -> list:
    """Run each pending migration in DRY-RUN — collect what it WOULD do, perform nothing.
    Relies on migrations being dry-run-safe (apply(pack, True) must not mutate)."""
    out = []
    for m in plan.migrations:
        for c in (m.apply_fn(pack, True) or []):
            out.append(f"[{m.id}] {c}")
    return out


def _default_validator(pack: Path):
    """Roll-forward gate: re-run `framework validate`; returns (ok, summary)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "framework_validate", _HERE / "framework_validate.py")
        fv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fv)
        rc = fv.main([str(pack), "--quiet"])
        return (rc == 0, f"framework validate → exit {rc}")
    except Exception as e:                  # never let a broken validator wedge an upgrade
        return (True, f"validation skipped ({e})")


# Overridable so callers/tests can inject a validator without a full pack on disk.
VALIDATOR = _default_validator


# --- snapshot + automatic rollback (Phase 3) ---------------------------------

def _scope_files(root: Path, snapshots_abs: Path):
    """Relative paths of files under `root`, skipping SNAPSHOT_EXCLUDES + the snapshots dir."""
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if d not in SNAPSHOT_EXCLUDES and (dp / d).resolve() != snapshots_abs]
        for f in filenames:
            yield (dp / f).relative_to(root)


def snapshot(pack: Path, snap_id: str, meta_obj: Optional[dict] = None) -> Path:
    """Copy the pack SOURCE (minus runtime/VCS/snapshots) to .okengine/snapshots/<id>/tree.
    Returns the snapshot dir. Cheap-ish: a file copy of the source, not the runtime."""
    snap_dir = pack / SNAPSHOTS_REL / snap_id
    tree = snap_dir / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    snapshots_abs = (pack / SNAPSHOTS_REL).resolve()
    for rel in _scope_files(pack, snapshots_abs):
        dst = tree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pack / rel, dst)
    (snap_dir / "manifest.json").write_text(
        json.dumps(meta_obj or {}, indent=2) + "\n", encoding="utf-8")
    return snap_dir


def added_since_snapshot(pack: Path, snap_dir: Path) -> set:
    """Rel paths present in the pack now but absent from the snapshot — the files the just-run
    migration ADDED. Capture this right after apply (before the roll-forward gate) so a later
    rollback removes only the migration's own additions and never live-vault content that a
    concurrent writer (cron content lane / MCP write) creates during the — much slower —
    validation window (invariant-audit #12)."""
    tree = snap_dir / "tree"
    snap_set = set(_scope_files(tree, (tree / SNAPSHOTS_REL).resolve()))
    snapshots_abs = (pack / SNAPSHOTS_REL).resolve()
    return {rel for rel in _scope_files(pack, snapshots_abs) if rel not in snap_set}


def restore(pack: Path, snap_dir: Path, added: Optional[set] = None) -> int:
    """Roll the pack source back to a snapshot: delete files the migration added, then restore
    every snapshotted file (reverting modifications and recreating deletions). Returns #changes.

    `added` is the migration's added-set captured right after apply (see added_since_snapshot):
    when given, ONLY those files are removed, so live-vault writes made after the capture point
    survive the rollback (invariant-audit #12). When None, fall back to 'everything newer than the
    snapshot' — correct for a static pack, but on a LIVE vault this clobbers concurrent content
    writes, so live callers MUST pass `added`."""
    tree = snap_dir / "tree"
    snapshots_abs = (pack / SNAPSHOTS_REL).resolve()
    snap_set = set(_scope_files(tree, (tree / SNAPSHOTS_REL).resolve()))
    if added is None:
        added = {rel for rel in _scope_files(pack, snapshots_abs) if rel not in snap_set}
    n = 0
    for rel in added:                                         # migration-added -> remove
        if rel not in snap_set:                               # never delete a file the snapshot restores
            f = pack / rel
            if f.exists():
                f.unlink()
                n += 1
    for rel in snap_set:                                      # restore content (+ recreate deleted)
        dst = pack / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tree / rel, dst)
        n += 1
    return n


def prune_snapshots(pack: Path, keep: int) -> int:
    """Keep the newest `keep` snapshot dirs (by name = timestamp); remove older. Returns #removed."""
    base = pack / SNAPSHOTS_REL
    if not base.is_dir() or keep < 0:
        return 0
    snaps = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.name)
    removed = 0
    for d in snaps[:-keep] if keep else snaps:
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


# --- CLI ---------------------------------------------------------------------

def render(plan: Plan) -> str:
    L = [f"engine pin: {plan.pin or '(none)'}   →   engine release: {plan.target or '(unknown)'}"]
    if plan.status == "current":
        L.append("  ✓ pin matches the running engine — nothing to do.")
    elif plan.status == "compatible":
        L.append("  ~ compatible (same series, engine is patch-newer); --apply records the exact pin.")
    elif plan.status == "upgrade":
        L.append("  ⤴ minor/major bump — pin is STALE; `validate` FAILs until upgraded.")
    else:
        L.append("  ? cannot compare versions (missing or unparseable pin / engine release).")
    if plan.migrations:
        L.append(f"  migrations to apply ({len(plan.migrations)}):")
        for m in plan.migrations:
            L.append(f"    • {m.id}: {m.from_version}→{m.to_version}  {m.description}")
    elif plan.status in ("upgrade", "compatible"):
        L.append("  migrations to apply: none (pin bump only)")
    if plan.already_applied:
        L.append(f"  already applied (skipped): {', '.join(plan.already_applied)}")
    return "\n".join(L)


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="framework upgrade",
                                 description="Reconcile a pack's engine pin to the running engine.")
    ap.add_argument("pack", help="path to the pack/vault dir")
    ap.add_argument("--apply", action="store_true",
                    help="write the pin + run migrations (default: dry-run)")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip the post-apply roll-forward validation")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="skip the pre-apply snapshot (disables automatic rollback)")
    ap.add_argument("--keep-snapshots", type=int, default=3,
                    help="how many upgrade snapshots to retain (default 3)")
    ap.add_argument("--migrations-dir", default=str(DEFAULT_MIGRATIONS_DIR),
                    help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    pack = Path(a.pack)
    if not pack.is_dir():
        print(f"ERROR: pack dir not found: {pack}", file=sys.stderr)
        return 2
    meta = _engine_meta()
    target, htag = meta.engine_release(), meta.hermes_pin()
    try:
        migrations = load_all_migrations(Path(a.migrations_dir), pack)   # engine + pack-local
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    plan = plan_upgrade(pack, target, htag, migrations, meta)
    print(render(plan))
    if plan.status == "unknown":
        return 2
    if not a.apply:
        if plan.migrations:
            print("\n  would apply (dry-run preview):")
            for c in preview_upgrade(pack, plan):
                print(f"    {c}")
        if plan.status != "current":
            print("\n(dry-run — re-run with --apply to perform it)")
        return 0
    if plan.status == "current":
        return 0
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    snap = None
    if not a.no_snapshot:
        snap = snapshot(pack, now.strftime("%Y%m%dT%H%M%S"),
                        {"at": now_iso, "from": plan.pin, "to": target,
                         "migrations": [m.id for m in plan.migrations]})
        print(f"  snapshot: .okengine/snapshots/{snap.name} (pre-upgrade source)")
    changes = apply_upgrade(pack, plan, now_iso)
    # Freeze the migration's added-set NOW — before the (slower) validation gate — so a rollback
    # removes only what the migration added, not live-vault writes made during that window (#12).
    added = added_since_snapshot(pack, snap) if snap else None
    print(f"\nApplied → pin {target}" + (f" (hermes {htag})" if htag else ""))
    for c in changes:
        print(f"  {c}")
    if not changes:
        print("  (pin bump only — no migration transforms)")
    print("  state recorded in .okengine/migrations-state.json")
    if a.no_validate:
        if snap:
            prune_snapshots(pack, a.keep_snapshots)
        return 0
    ok, summary = VALIDATOR(pack)               # roll-forward gate
    print(f"\nRoll-forward check: {summary}")
    if not ok:
        if snap:
            n = restore(pack, snap, added=added)
            shutil.rmtree(snap, ignore_errors=True)   # the failed attempt's snapshot is spent
            print(f"  ↩ ROLLED BACK to the pre-upgrade source ({n} files restored) — pack unchanged.")
        else:
            print("  ✗ vault no longer validates, and --no-snapshot was set: NO automatic "
                  "rollback. Restore the pack manually.")
        return 1
    print("  ✓ vault still validates")
    if snap:
        prune_snapshots(pack, a.keep_snapshots)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
