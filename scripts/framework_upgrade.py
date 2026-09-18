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
import hashlib
import importlib.util
import json
import os
import re
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
SNAPSHOT_EXCLUDES = {".git", ".hermes-data", "tmp", "logs",
                     "node_modules", ".venv", "__pycache__",
                     "rolled-back"}   # quarantine of files a rollback set aside (#32) — never re-scoped


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
    source_path: str = "unknown"
    source_sha256: str = "unknown"
    source_tier: str = "external"


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
        migration_id = getattr(mod, "ID", None)
        if not isinstance(migration_id, str) or not migration_id.strip():
            raise RuntimeError(
                f"migration {f.name} must declare a stable non-empty ID; filename-derived "
                "identities are rename-sensitive and can replay an applied migration"
            )
        out.append(Migration(
            id=migration_id.strip(),
            from_version=str(getattr(mod, "FROM", "")),
            to_version=str(getattr(mod, "TO", "")),
            description=str(getattr(mod, "DESCRIPTION", "")),
            apply_fn=getattr(mod, "apply", lambda pack, dry: []),
            source_path=str(f.resolve()),
            source_sha256="sha256:" + hashlib.sha256(f.read_bytes()).hexdigest(),
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
    status: str                 # current | compatible | upgrade | ahead | unknown
    pin: Optional[str]
    target: Optional[str]
    target_hermes: Optional[str]
    migrations: list = field(default_factory=list)        # pending (not yet applied)
    already_applied: list = field(default_factory=list)   # ids already in state


def plan_upgrade(pack: Path, target, target_hermes, migrations, meta) -> Plan:
    pin, _ = read_pin(pack)
    state = read_state(pack)
    applied = set(state.get("applied", []))
    if pin is None or target is None or meta._semver(pin) is None or meta._semver(target) is None:
        return Plan("unknown", pin, target, target_hermes)
    if pin == target:
        return Plan("current", pin, target, target_hermes)
    if meta._semver(pin) > meta._semver(target):
        # This command has no down-migrations. Treating an ahead pin as an "upgrade" selected an
        # empty migration range and then silently rewrote the pack to the older running engine.
        return Plan("ahead", pin, target, target_hermes)
    status = "compatible" if meta.satisfies_pin(pin, target) else "upgrade"
    migs = applicable(migrations, pin, target, meta)
    recorded = state.get("applied_provenance") or {}
    for migration in migs:
        prior = recorded.get(migration.id)
        if migration.id in applied and isinstance(prior, dict):
            current = migration_provenance(migration)
            identity_fields = ("tier", "sha256", "to_version")
            if any(prior.get(key) != current[key] for key in identity_fields):
                raise ValueError(
                    f"applied migration {migration.id!r} provenance changed: recorded "
                    f"{prior.get('tier')} {prior.get('sha256')} -> current "
                    f"{current['tier']} {current['sha256']}; do not silently substitute or "
                    "re-run migration code — reconcile the migration explicitly"
                )
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


def migration_provenance(migration: Migration) -> dict:
    """Stable identity of the exact migration implementation that supplied an applied ID."""
    return {
        "tier": migration.source_tier,
        "path": migration.source_path,
        "sha256": migration.source_sha256,
        "to_version": migration.to_version,
    }


def record_state(pack: Path, target: str, applied_ids: list, now_iso: str,
                 *, provenance: Optional[dict] = None) -> None:
    f = pack / STATE_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    state = read_state(pack)
    state["engine_version"] = target
    state["applied"] = sorted(set(state.get("applied", [])) | set(applied_ids))
    if provenance:
        state.setdefault("applied_provenance", {}).update(provenance)
    state.setdefault("history", []).append(
        {"at": now_iso, "to": target, "migrations": list(applied_ids),
         **({"migration_provenance": provenance} if provenance else {})})
    f.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def apply_upgrade(pack: Path, plan: Plan, now_iso: str) -> list:
    changes: list = []
    for m in plan.migrations:
        for c in (m.apply_fn(pack, False) or []):
            changes.append(f"[{m.id}] {c}")
        # Record each migration's id in `applied` IMMEDIATELY after its side effects land, not once
        # after the whole batch. plan_upgrade selects pending work by `m.id not in applied` (NOT by
        # engine_version), so a hard-kill (SIGKILL / power loss) mid-batch now leaves the completed
        # migrations recorded and only the in-flight one replays — instead of the ENTIRE batch
        # re-applying, which for a non-idempotent migration (e.g. a partition reshelve, #54) corrupts
        # the vault. The roll-forward snapshot only unwinds on a raised exception, not a hard-kill, so
        # this narrows the crash window that the snapshot can't cover (invariant-audit #351).
        record_state(
            pack, plan.target, [m.id], now_iso,
            provenance={m.id: migration_provenance(m)},
        )
    write_pin(pack, plan.target, plan.target_hermes)
    return changes


# --- pack-aware migrations · dry-run preview · roll-forward gate (Phase 2) ----

def pack_migrations_dir(pack: Path) -> Path:
    """A pack ships its own migrations here, run alongside the engine's."""
    return pack / ".okengine" / "migrations"


def load_all_migrations(engine_dir: Path, pack: Path) -> list:
    """Engine migrations + the pack's own, merged and ordered by to_version (then id).
    A pack-local migration with the same id as an engine one may INTENTIONALLY override it (pack
    wins) — but ONLY when they share a to_version; a same-id/DIFFERENT-to_version pair is an accidental
    id collision that would silently suppress the engine migration forever (invariant-audit B5.2),
    so fail loud and make the author pick a unique id."""
    # Fail loud on a duplicate id WITHIN the engine migrations dir — a plain dict comprehension
    # silently kept the later-sorted file and dropped the earlier, so a copy-paste id collision made
    # one engine migration never run for ANY pack, with migrations-state recording success
    # (invariant-audit — asymmetric with the pack-vs-engine collision guard below, which IS loud).
    eng: dict = {}
    for m in load_migrations(engine_dir):
        m.source_tier = "engine"
        if m.id in eng:
            raise SystemExit(f"duplicate engine migration id {m.id!r} in {engine_dir} — two "
                             "migration files share an ID; give each a unique ID.")
        eng[m.id] = m
    by_id = dict(eng)
    meta = _engine_meta()

    def _same_version(a: str, b: str) -> bool:
        # Compare on a NORMALIZED spelling, not the lossy 3-tuple _semver (invariant-audit B5.2, two
        # re-verify rounds): a legit override may spell the shared version differently ("v0.6.0" vs
        # "0.6.0" vs "engine-v0.6.0") and must NOT trip the collision guard — but _semver captures
        # only X.Y.Z, so it wrongly equated "0.6.0" with a genuinely-different "0.6.0.1"/"0.6.0-rc1"
        # and let a real suppression through. Strip only the prefix noise (case, "engine-", leading
        # "v"); everything after the patch component stays significant.
        def _norm(v: str) -> str:
            return str(v).strip().lower().removeprefix("engine-").lstrip("v")
        return _norm(a) == _norm(b)

    pack_seen: set[str] = set()
    pack_dir = pack_migrations_dir(pack)
    for m in load_migrations(pack_dir):      # pack-aware hook
        m.source_tier = "pack-override" if m.id in eng else "pack"
        if m.id in pack_seen:
            raise SystemExit(
                f"duplicate pack migration id {m.id!r} in {pack_dir} — two pack migration "
                "files share an ID, so one would be silently discarded; give each a unique ID.")
        pack_seen.add(m.id)
        prior = eng.get(m.id)
        if prior is not None and not _same_version(prior.to_version, m.to_version):
            raise SystemExit(
                f"pack migration id {m.id!r} (to_version {m.to_version}) collides with the ENGINE "
                f"migration of the same id (to_version {prior.to_version}) — the pack would silently "
                "SUPPRESS the engine migration. Give the pack migration a UNIQUE id.")
        by_id[m.id] = m
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
    """Roll-forward gate — STRUCTURAL half: re-run `framework validate`. The page-conformance
    REGRESSION half (`_conformance_regressions`) is done separately in the orchestration, where the
    pre-upgrade snapshot is available as a baseline — it MUST have one, else pre-existing
    non-conformant pages (a real vault has them: older/agent-authored pages missing `id`, etc.)
    false-roll-back a legitimate upgrade (the fleet-roll regression the exhaustive scan caused)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "framework_validate", _HERE / "framework_validate.py")
        fv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fv)
        rc = fv.main([str(pack), "--quiet"])
        if rc != 0:
            return (False, f"framework validate → exit {rc}")
        return (True, "framework validate → exit 0")
    except Exception as e:                  # never let a broken validator wedge an upgrade
        return (True, f"validation skipped ({e})")


def _page_failure_map(root: Path) -> dict:
    """{wiki-relative-posix: reason} for EVERY non-conformant page under `root/wiki` (root = a pack
    OR a snapshot's `tree/`). Complete (no cap): the failing set must be whole to diff before/after.
    Fail-open on import error; a per-page read/parse error is skipped, never aborts the scan."""
    try:
        sv = importlib.util.spec_from_file_location(
            "schema_validator", _HERE.parent / "tools" / "schema_validator.py")
        schema_validator = importlib.util.module_from_spec(sv)
        sv.loader.exec_module(schema_validator)
    except Exception:
        return {}
    wiki = root / "wiki"
    if not wiki.is_dir():
        return {}
    out: dict = {}
    for p in sorted(wiki.rglob("*.md")):
        if p.name.startswith(("_", ".")) or p.name.startswith("INDEX"):
            continue
        try:
            reason = schema_validator.schema_reject_reason(str(p), p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            reason = None
        if reason:
            out[p.relative_to(wiki).as_posix()] = reason
    return out


def _unknown_type_map(root: Path) -> dict:
    """{unknown_type: [wiki-relative page paths]} under ``root/wiki``.

    This is deliberately separate from the runtime conformance profile:
    ``schema_reject_reason`` remains fail-open for unknown types, while an upgrade can safely
    compare this map with its pre-migration snapshot. The validator's own schema resolver is used
    so each page sees the same root/sub-domain composed schema, exclusions, and reserved-file
    rules as the write path. Alias keys and values are accepted alongside canonical ``types``.

    Fail-open on validator/schema/read errors. A broken audit must never create a false rollback;
    the normal structural/conformance gates still run independently.
    """
    try:
        sv = importlib.util.spec_from_file_location(
            "schema_validator_unknown_types", _HERE.parent / "tools" / "schema_validator.py")
        schema_validator = importlib.util.module_from_spec(sv)
        sv.loader.exec_module(schema_validator)
    except Exception:
        return {}
    wiki = root / "wiki"
    if not wiki.is_dir():
        return {}
    out: dict[str, list[str]] = {}
    for p in sorted(wiki.rglob("*.md")):
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            kind, _reason = schema_validator._evaluate(str(p), content)
            if kind != "ok":
                continue
            match = schema_validator._FM_RE.match(content)
            fm = schema_validator.yaml.safe_load(match.group(1)) if match else None
            if not isinstance(fm, dict):
                continue
            typ = str(fm.get("type") or "").strip()
            if not typ:
                continue
            schema_path = schema_validator._find_schema(str(p))
            schema = schema_validator._load_schema(schema_path) if schema_path else None
            if not isinstance(schema, dict):
                continue
            effective = schema_validator._base_merged(schema)
            aliases = effective.get("type_aliases") or {}
            known = set((effective.get("types") or {}).keys())
            if isinstance(aliases, dict):
                known.update(str(k) for k in aliases)
                known.update(str(v) for v in aliases.values())
            if typ not in known:
                out.setdefault(typ, []).append(p.relative_to(wiki).as_posix())
        except Exception:
            continue
    return out


def _unknown_type_regressions(before_root: Path, after_root: Path, cap: int = 300) -> list:
    """Newly-unknown type values introduced by a migration.

    Compare VALUES, not only paths: moving/resharding a page carrying a pre-existing unknown type
    must not false-roll-back an unrelated upgrade. Conversely, removing a formerly-known type from
    the composed taxonomy is caught because it was absent from the before-unknown set. Returns up
    to ``cap`` ``path: unknown type '…'`` examples.
    """
    before_types = set(_unknown_type_map(before_root))
    after = _unknown_type_map(after_root)
    out = []
    for typ in sorted(set(after) - before_types):
        for rel in after[typ]:
            out.append(f"{rel}: unknown type '{typ}' newly introduced by migration")
            if len(out) >= cap:
                return out
    return out


def _sample_page_failures(pack: Path, cap: int = 300) -> list:
    """EXHAUSTIVE OKF conformance scan of `pack/wiki`; up to `cap` `path: reason` strings for pages
    that violate their type's schema (missing/invalid required field, bad YAML, absent type). This is
    the raw after-scan; the roll-forward GATE diffs it against the pre-upgrade baseline via
    `_conformance_regressions` so pre-existing failures don't roll back a legit upgrade. Unknown
    types remain outside this raw runtime-profile scan; the snapshot-aware
    `_unknown_type_regressions` gate handles only migration-introduced values (okengine#207)."""
    return [f"{rel}: {reason}" for rel, reason in sorted(_page_failure_map(pack).items())[:cap]]


def _conformance_regressions(before_root: Path, after_root: Path, cap: int = 300) -> list:
    """Pages that REGRESSED conformance across the migration: non-conformant AFTER but conformant
    (or failing DIFFERENTLY) BEFORE. Pre-existing failures with the same reason on both sides are NOT
    the migration's fault and are excluded — else a legitimate upgrade rolls back over stale data
    (the false-rollback the baseline-less exhaustive scan caused on every real vault). Returns up to
    `cap` `path: reason` strings."""
    before = _page_failure_map(before_root)
    after = _page_failure_map(after_root)
    out = []
    for rel, reason in sorted(after.items()):
        if before.get(rel) != reason:       # NEW failure, or a page now failing for a DIFFERENT reason
            out.append(f"{rel}: {reason}")
            if len(out) >= cap:
                break
    return out


# Overridable so callers/tests can inject a validator without a full pack on disk.
VALIDATOR = _default_validator


def _recompose_schema(pack: Path) -> None:
    """Refresh the schema artifact before any post-migration validation reads it."""
    source_root = str(_ROOT / "src")
    if source_root not in sys.path:
        sys.path.insert(0, source_root)
    spec = importlib.util.spec_from_file_location(
        "extension_compose_upgrade", _HERE / "extension_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    errors = module.write_composed_schema(pack) or []
    if errors:
        raise RuntimeError("composed-schema regeneration failed: " + "; ".join(map(str, errors)))


# Injectable for contract tests, like VALIDATOR. Production always uses the real composer above.
RECOMPOSE_SCHEMA = _recompose_schema


# --- snapshot + automatic rollback (Phase 3) ---------------------------------

def _scope_files(root: Path, snapshots_abs: Path):
    """Relative paths of files under `root`, skipping SNAPSHOT_EXCLUDES + the snapshots dir."""
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames
                       if d not in SNAPSHOT_EXCLUDES and (dp / d).resolve() != snapshots_abs]
        for f in filenames:
            yield (dp / f).relative_to(root)
    # The gateway's operator-owned config is runtime state, but engine migrations may perform
    # narrowly-scoped model-ID migrations on it. Include only this file so rollback remains
    # transactional without copying caches, cron state, databases, or other .hermes-data content.
    runtime_config = root / ".hermes-data" / "config.yaml"
    if runtime_config.is_file():
        yield runtime_config.relative_to(root)


def _copy_scoped_file(src: Path, dst: Path) -> None:
    """Copy one snapshot member without following symlinks outside the pack.

    Symlinks are preserved as links, including dangling runtime links. Replacing
    an existing destination explicitly also makes rollback idempotent.
    """
    if src.is_symlink():
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(os.readlink(src))
        try:
            shutil.copystat(src, dst, follow_symlinks=False)
        except OSError:
            pass  # link metadata is optional; link identity is the rollback contract
        return
    shutil.copy2(src, dst)


def snapshot(pack: Path, snap_id: str, meta_obj: Optional[dict] = None) -> Path:
    """Copy the pack SOURCE (minus runtime/VCS/snapshots) to .okengine/snapshots/<id>/tree.
    Returns the snapshot dir. Cheap-ish: a file copy of the source, not the runtime."""
    snap_dir = pack / SNAPSHOTS_REL / snap_id
    tree = snap_dir / "tree"
    snapshots_abs = (pack / SNAPSHOTS_REL).resolve()
    scoped = list(_scope_files(pack, snapshots_abs))
    # Disk-space precheck: the snapshot copies the whole source (wiki can be 11k–64k pages) onto the
    # SAME filesystem the live vault writes to. Without a check, an ENOSPC mid-copy left a partial,
    # manifest-less snapshot behind AND filled the disk (invariant-audit #31). Refuse up front.
    need = 0
    for rel in scoped:
        try:
            need += (pack / rel).lstat().st_size
        except OSError:
            pass
    try:
        free = shutil.disk_usage(pack).free
    except OSError:
        free = None
    if free is not None and need > free * 0.95:          # keep a little headroom
        raise OSError(f"insufficient disk space for the pre-upgrade snapshot: need ~{need} bytes, "
                      f"{free} free on {pack}'s filesystem. Free space, or re-run with --no-snapshot.")
    tree.mkdir(parents=True, exist_ok=True)
    try:
        for rel in scoped:
            dst = tree / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            _copy_scoped_file(pack / rel, dst)
        # manifest.json is written LAST — its presence marks a COMPLETE snapshot.
        (snap_dir / "manifest.json").write_text(
            json.dumps(meta_obj or {}, indent=2) + "\n", encoding="utf-8")
    except BaseException:
        # ENOSPC or an operator Ctrl-C mid-copy (minutes on a real vault) leaves a partial,
        # manifest-less snapshot that nothing cleaned, marked invalid, or excluded from prune
        # retention (invariant-audit #31). Remove it so it can't be mistaken for a restore point.
        shutil.rmtree(snap_dir, ignore_errors=True)
        raise
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


def changed_since_snapshot(pack: Path, snap_dir: Path) -> set:
    """Rel paths present in BOTH the pack and the snapshot whose bytes now differ — the files the
    just-run migration MODIFIED. Capture this right after apply (alongside added_since_snapshot,
    before the roll-forward gate) so a rollback reverts ONLY the migration's own edits. A page that
    a concurrent writer (cron content lane / MCP write) modifies during the slow validation window
    is NOT in this frozen set, so restore() leaves it untouched instead of clobbering it back to the
    pre-upgrade snapshot (invariant-audit — #12 only covered ADDED files, not modified ones)."""
    tree = snap_dir / "tree"
    snap_set = set(_scope_files(tree, (tree / SNAPSHOTS_REL).resolve()))
    out = set()
    for rel in snap_set:
        cur = pack / rel
        if not cur.exists() and not cur.is_symlink():
            continue                                             # deleted -> restore() always recreates
        try:
            snap = tree / rel
            if cur.is_symlink() or snap.is_symlink():
                if not (cur.is_symlink() and snap.is_symlink()
                        and os.readlink(cur) == os.readlink(snap)):
                    out.add(rel)
            elif cur.read_bytes() != snap.read_bytes():
                out.add(rel)
        except OSError:
            out.add(rel)                                         # unreadable now -> treat as changed
    return out


ROLLBACK_PLAN = "rollback-plan.json"


def _path_identity(path: Path) -> str | None:
    """Stable post-migration identity without following symlinks."""
    try:
        if path.is_symlink():
            return "symlink:" + os.readlink(path)
        if path.is_file():
            return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return None


def record_rollback_plan(pack: Path, snap_dir: Path, added: set, modified: set) -> Path:
    """Persist the migration delta and its post-image for safe out-of-band restore."""
    snap_dir.mkdir(parents=True, exist_ok=True)
    tree = snap_dir / "tree"
    snap_set = set(_scope_files(tree, (tree / SNAPSHOTS_REL).resolve()))
    deleted = sorted(str(rel) for rel in snap_set
                     if not (pack / rel).exists() and not (pack / rel).is_symlink())
    plan = {
        "schema_version": 1,
        "added": {str(rel): _path_identity(pack / rel) for rel in sorted(added)},
        "modified": {str(rel): _path_identity(pack / rel) for rel in sorted(modified)},
        "deleted": deleted,
    }
    path = snap_dir / ROLLBACK_PLAN
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    directory_fd = os.open(snap_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path


def restore_saved_snapshot(pack: Path, snap_dir: Path) -> tuple[int, list[str]]:
    """Restore only unchanged migration outputs recorded in a retained snapshot.

    Any path edited after migration is a conflict. Refuse the whole restore before
    mutating anything so an out-of-band rollback cannot erase later cron/MCP work.
    """
    try:
        plan = json.loads((snap_dir / ROLLBACK_PLAN).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return 0, [f"snapshot has no valid {ROLLBACK_PLAN}: {exc}"]
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        return 0, [f"snapshot {ROLLBACK_PLAN} has an unsupported schema"]
    added = plan.get("added")
    modified = plan.get("modified")
    deleted = plan.get("deleted")
    if not isinstance(added, dict) or not isinstance(modified, dict) or not isinstance(deleted, list):
        return 0, [f"snapshot {ROLLBACK_PLAN} is malformed"]
    raw_paths = [*added, *modified, *deleted]
    unsafe = [raw for raw in raw_paths if not isinstance(raw, str) or Path(raw).is_absolute()
              or any(part in {"", ".", ".."} for part in Path(raw).parts)]
    if unsafe:
        return 0, [f"snapshot {ROLLBACK_PLAN} contains unsafe path(s): {unsafe}"]
    conflicts = []
    for group, rows in (("added", added), ("modified", modified)):
        for raw, expected in rows.items():
            rel = Path(raw)
            observed = _path_identity(pack / rel)
            if observed != expected:
                conflicts.append(
                    f"{raw}: post-migration {group} path changed again "
                    f"(expected {expected!r}, observed {observed!r})"
                )
    for raw in deleted:
        target = pack / Path(raw)
        if target.exists() or target.is_symlink():
            conflicts.append(f"{raw}: migration-deleted path was recreated after migration")
    if conflicts:
        return 0, conflicts
    changed = restore(
        pack, snap_dir, added={Path(raw) for raw in added},
        modified={Path(raw) for raw in modified},
    )
    return changed, []


def restore(pack: Path, snap_dir: Path, added: Optional[set] = None,
            modified: Optional[set] = None) -> int:
    """Roll the pack source back to a snapshot: QUARANTINE files the migration added (move them under
    .okengine/rolled-back/<snap_id>/ — never delete, so a misclassified concurrent write survives),
    then restore the snapshotted files it changed (reverting modifications and recreating deletions).
    Returns #changes.

    `added` is the migration's added-set captured right after apply (see added_since_snapshot):
    when given, ONLY those files are removed, so live-vault writes made after the capture point
    survive the rollback (invariant-audit #12). When None, fall back to 'everything newer than the
    snapshot' is intentionally unsupported: callers MUST pass the frozen set, or use
    restore_saved_snapshot() with the persisted post-image plan.

    `modified` is the migration's changed-set (see changed_since_snapshot), captured at the same
    point. When given, a snapshotted file that STILL EXISTS is reverted only if it is in this set —
    so a page that a concurrent writer edits during the validation window (and which the migration
    never touched) keeps its new content instead of being clobbered back to the snapshot. Deleted
    snapshotted files are always recreated regardless. When None, every snapshotted file is reverted
    (the static-pack fallback is intentionally unsupported)."""
    if added is None or modified is None:
        raise ValueError(
            "restore requires explicit migration added/modified sets; for a retained snapshot "
            "use restore_saved_snapshot() or `framework upgrade --restore-snapshot ID`"
        )
    tree = snap_dir / "tree"
    snapshots_abs = (pack / SNAPSHOTS_REL).resolve()
    snap_set = set(_scope_files(tree, (tree / SNAPSHOTS_REL).resolve()))
    # QUARANTINE (never unlink) the `added` set. The added/modified capture is a non-atomic walk at
    # real vault scale, so a page a concurrent lane/MCP writes during the apply→validate window can be
    # misclassified as migration-added; deleting it would destroy live content (invariant-audit #32).
    # Move such files aside under .okengine/rolled-back/<snap_id>/ so a rollback is NEVER lossy — the
    # operator can recover a false positive; SNAPSHOT_EXCLUDES keeps the quarantine out of every scope.
    quarantine = pack / ".okengine" / "rolled-back" / snap_dir.name
    n = 0
    for rel in added:                                         # migration-added -> set aside
        if rel not in snap_set:                               # never touch a file the snapshot restores
            f = pack / rel
            if f.exists() or f.is_symlink():
                q = quarantine / rel
                q.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(q))
                n += 1
    for rel in snap_set:                                      # restore content (+ recreate deleted)
        dst = pack / rel
        if modified is not None and (dst.exists() or dst.is_symlink()) and rel not in modified:
            continue                                          # concurrent write to an untouched file — keep it
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_scoped_file(tree / rel, dst)
        n += 1
    return n


def confirm_snapshot_good(snap_dir: Path, now_iso: str) -> None:
    """Mark a complete pre-migration snapshot prune-eligible after observation."""
    manifest = snap_dir / "manifest.json"
    if snap_dir.is_symlink() or not (snap_dir / "tree").is_dir() or not manifest.is_file():
        raise ValueError(f"complete retained snapshot not found: {snap_dir.name}")
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"snapshot {snap_dir.name} has an invalid manifest: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"snapshot {snap_dir.name} manifest is not an object")
    metadata["confirmed_good_at"] = now_iso
    tmp = manifest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(manifest)


def prune_snapshots(pack: Path, keep: int) -> int:
    """Prune only confirmed-good snapshots; unconfirmed recovery points are never count-pruned."""
    base = pack / SNAPSHOTS_REL
    if not base.is_dir() or keep < 0:
        return 0
    snaps = []
    for directory in base.iterdir():
        manifest = directory / "manifest.json"
        if directory.is_symlink() or not directory.is_dir() or not manifest.is_file():
            continue
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(metadata, dict) and metadata.get("confirmed_good_at"):
            snaps.append(directory)
    snaps.sort(key=lambda directory: directory.name)
    removed = 0
    for d in snaps[:-keep] if keep else snaps:
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


# --- pack-VERSION migrations on update (okengine#312) ------------------------
# The runner above triggers on ENGINE-pin reconciliation. Packs also ship their own
# migrations — `<pack>/migrations/m_*.py`, same module contract, keyed on PACK versions
# (okpacks-library VERSIONING.md). `framework pull --update` / `install-domain` over an
# existing member call run_pack_migrations() so a pack update carries its transforms
# through the SAME snapshot / dry-run / roll-forward machinery.

PACK_MIGRATIONS_REL = Path("migrations")


def installed_pack_version(vault: Path, name: str, fallback: Optional[str] = None) -> Optional[str]:
    """The recorded installed version of pack `name` in this vault, else `fallback`.
    The state record is authoritative over pack.yaml: after a dry-run surfaces pending
    migrations, `framework reconcile` may accept pack.yaml.upstream (new version) before
    anyone applies them — the recorded floor is what keeps the span alive."""
    v = (read_state(vault).get("pack_versions") or {}).get(name)
    return str(v) if v else fallback


def _write_state(vault: Path, state: dict) -> None:
    """Atomically durably replace migration state so a hard kill cannot leave partial JSON."""
    f = vault / STATE_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(state, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, f)
    directory_fd = os.open(f.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def record_pack_version(vault: Path, name: str, version: str) -> None:
    state = read_state(vault)
    state.setdefault("pack_versions", {})[name] = version
    _write_state(vault, state)


def _record_pack_applied(vault: Path, name: str, version: str,
                         applied_ids: list, now_iso: str) -> None:
    """Record pack migrations under pack identity; the flat set remains compatibility metadata."""
    state = read_state(vault)
    state.setdefault("pack_versions", {})[name] = version
    state["applied"] = sorted(set(state.get("applied", [])) | set(applied_ids))
    by_pack = state.setdefault("pack_applied", {})
    by_pack[name] = sorted(set(by_pack.get(name, [])) | set(applied_ids))
    state.setdefault("history", []).append(
        {"at": now_iso, "pack": name, "to": version, "migrations": list(applied_ids)})
    _write_state(vault, state)


def _pack_applied_ids(state: dict, name: str) -> set[str]:
    """Pack-scoped applied IDs, including records written before ``pack_applied`` existed."""
    applied = set((state.get("pack_applied") or {}).get(name, []))
    for row in state.get("history") or []:
        if isinstance(row, dict) and row.get("pack") == name:
            applied.update(str(mid) for mid in (row.get("migrations") or []))
    return applied


def _unfinished_pack_snapshot(vault: Path, name: str, incoming: str) -> Optional[Path]:
    """Return the newest durable, unconfirmed transaction for this pack release."""
    base = vault / SNAPSHOTS_REL
    if not base.is_dir():
        return None
    matches = []
    for directory in base.iterdir():
        manifest = directory / "manifest.json"
        if directory.is_symlink() or not manifest.is_file():
            continue
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(metadata, dict) and metadata.get("pack") == name
                and str(metadata.get("to")) == incoming
                and not metadata.get("confirmed_good_at")):
            matches.append(directory)
    return max(matches, key=lambda item: item.name) if matches else None


def _rollback_pack_snapshot(vault: Path, snap: Path) -> int:
    plan = json.loads((snap / ROLLBACK_PLAN).read_text(encoding="utf-8"))
    added = {Path(value) for value in plan.get("added", {})}
    modified = {Path(value) for value in plan.get("modified", {})}
    return restore(vault, snap, added=added, modified=modified)


def changelog_impact(changelog_text: Optional[str], installed: str, incoming: str, meta) -> list:
    """`version: migration-impact` lines for the CHANGELOG sections in (installed, incoming].
    A schema-touching release must carry a `Migration impact:` line (VERSIONING.md R3) — a
    section without one is surfaced as such rather than silently skipped."""
    if not changelog_text:
        return []
    lo, hi = meta._semver(installed), meta._semver(incoming)
    if lo is None or hi is None:
        return []
    out = []
    section_ver, section_lines = None, []

    def _flush():
        if section_ver is None:
            return
        impact = [ln.strip() for ln in section_lines if "migration impact" in ln.lower()]
        out.append(f"{section_ver}: " + ("; ".join(impact) if impact
                                         else "(no migration-impact line in CHANGELOG)"))
    for ln in changelog_text.splitlines():
        m2 = re.match(r"##\s+v?(\d+\.\d+\.\d+)\b", ln)
        if m2:
            _flush()
            v = meta._semver(m2.group(1))
            section_ver = m2.group(1) if (v and lo < v <= hi) else None
            section_lines = []
        elif section_ver is not None:
            section_lines.append(ln)
    _flush()
    return out


def run_pack_migrations(vault: Path, name: str, installed: Optional[str], incoming: Optional[str],
                        *, apply: bool, migrations_dir: Optional[Path] = None,
                        changelog_text: Optional[str] = None, keep_snapshots: int = 3,
                        no_validate: bool = False, record: bool = True,
                        apply_hint: str = "--apply-migrations") -> int:
    """Plan/apply the pack-version migration span (installed, incoming] against `vault`.

    Mirrors the engine-pin apply path: dry-run preview by default; on `apply` — snapshot,
    run, record state, roll-forward gate (structural validate + conformance/unknown-type
    regressions vs the snapshot), automatic rollback on any failure. `record=False` makes a
    dry-run fully write-free (install-domain's plan mode). Returns 0 ok / 1 failed+rolled-back
    or packaging error."""
    meta = _engine_meta()
    if not incoming or meta._semver(incoming) is None:
        return 0                                    # upstream has no comparable version
    if not installed or installed == "0.0.0" or meta._semver(installed) is None:
        # Pre-versioning install (or no record): no span to compute. NEVER guess one from
        # 0.0.0 — that would replay every migration ever shipped onto a live vault. Baseline
        # at the incoming version and tell the operator to follow the CHANGELOG by hand once.
        if record:
            record_pack_version(vault, name, incoming)
        print(f"  pack version: {name} installed version unknown — baselined at {incoming}; "
              f"apply any older CHANGELOG migration steps manually")
        return 0
    iv, nv = meta._semver(installed), meta._semver(incoming)
    if iv == nv:
        if record and installed_pack_version(vault, name) is None:
            record_pack_version(vault, name, incoming)
        return 0
    if iv > nv:
        print(f"  ⚠ pack version: installed {installed} is NEWER than incoming {incoming} "
              f"({name}) — downgrade migrations are unsupported; nothing run")
        return 0
    mdir = migrations_dir if migrations_dir is not None else vault / PACK_MIGRATIONS_REL
    try:
        migs = load_migrations(mdir)
        span = applicable(migs, installed, incoming, meta)   # raises on unparseable to_version (#178)
    except (RuntimeError, ValueError) as e:
        print(f"ERROR: pack migrations ({name}): {e}", file=sys.stderr)
        return 1
    applied = _pack_applied_ids(read_state(vault), name)
    pending = [m for m in span if m.id not in applied]
    print(f"  pack version: {installed} → {incoming}  ({name})")
    for line in changelog_impact(changelog_text, installed, incoming, meta):
        print(f"    changelog {line}")
    if not pending:
        # A prior hard kill may have checkpointed every migration ID while deliberately leaving
        # the installed version at the old floor. Finish the interrupted roll-forward gates before
        # advancing the version; otherwise retry silently certifies uncomposed/unvalidated state.
        recovery_snap = _unfinished_pack_snapshot(vault, name, incoming)
        recovering = bool(recovery_snap or (
            span and applied.issuperset(m.id for m in span) and iv < nv
        ))
        if recovering:
            try:
                if recovery_snap and not (recovery_snap / ROLLBACK_PLAN).is_file():
                    record_rollback_plan(
                        vault,
                        recovery_snap,
                        added_since_snapshot(vault, recovery_snap),
                        changed_since_snapshot(vault, recovery_snap),
                    )
                RECOMPOSE_SCHEMA(vault)
                ok, summary = (True, "validation skipped (--no-validate)") \
                    if no_validate else VALIDATOR(vault)
                if ok and not no_validate and recovery_snap:
                    reg = _conformance_regressions(recovery_snap / "tree", vault, cap=300)
                    unknown = _unknown_type_regressions(recovery_snap / "tree", vault, cap=300)
                    if reg:
                        ok, summary = False, f"{len(reg)}+ page(s) regressed conformance"
                    elif unknown:
                        ok, summary = False, f"{len(unknown)}+ page(s) gained an unknown type"
            except (Exception, KeyboardInterrupt) as exc:
                if recovery_snap:
                    _rollback_pack_snapshot(vault, recovery_snap)
                    shutil.rmtree(recovery_snap, ignore_errors=True)
                print(f"ERROR: interrupted pack migration recovery ({name}): {exc}", file=sys.stderr)
                return 1
            print(f"    interrupted migration recovery check: {summary}")
            if not ok:
                if recovery_snap:
                    _rollback_pack_snapshot(vault, recovery_snap)
                    shutil.rmtree(recovery_snap, ignore_errors=True)
                return 1
        if record:
            record_pack_version(vault, name, incoming)
        if recovery_snap:
            confirm_snapshot_good(recovery_snap, datetime.now(timezone.utc).isoformat())
            prune_snapshots(vault, keep_snapshots)
        skipped = f" ({len(span)} already applied)" if span else ""
        print(f"    no pending pack migrations{skipped} — recorded version {incoming}")
        return 0
    print(f"    pack migrations to apply ({len(pending)}):")
    for m in pending:
        print(f"      • {m.id}: {m.from_version}→{m.to_version}  {m.description}")
    if not apply:
        for m in pending:
            for c in (m.apply_fn(vault, True) or []):
                print(f"      [{m.id}] {c}")
        if record and installed_pack_version(vault, name) is None:
            # Floor the span NOW: reconcile may accept pack.yaml.upstream before the operator
            # applies, and without this record the next update would see old==new and skip.
            record_pack_version(vault, name, installed)
        print(f"    (dry-run — re-run with {apply_hint} to perform, with snapshot + rollback)")
        return 0
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    snap = snapshot(vault, now.strftime("%Y%m%dT%H%M%S"),
                    {"at": now_iso, "pack": name, "from": installed, "to": incoming,
                     "migrations": [m.id for m in pending]})
    print(f"    snapshot: .okengine/snapshots/{snap.name} (pre-migration source)")
    try:
        changes = []
        checkpoint_version = installed
        for index, m in enumerate(pending):
            for c in (m.apply_fn(vault, False) or []):
                changes.append(f"[{m.id}] {c}")
            # Match the engine-pin runner's crash contract: once one migration's side effects
            # land, persist its id and intermediate version immediately. A hard kill bypasses the
            # rollback handler; without this checkpoint the completed non-idempotent migration
            # would run again on retry.
            # Several independently authored migrations may share one release target. Advancing
            # pack_versions after the first makes a hard-kill resume see installed == incoming and
            # skip its siblings forever. Persist every completed id immediately, but advance the
            # version checkpoint only after the last pending migration for that target succeeds.
            if (meta._semver(m.to_version) < nv
                    and not any(meta._semver(later.to_version) == meta._semver(m.to_version)
                                for later in pending[index + 1:])):
                checkpoint_version = m.to_version
            _record_pack_applied(vault, name, checkpoint_version, [m.id], now_iso)
        added = added_since_snapshot(vault, snap)
        modified = changed_since_snapshot(vault, snap)
        record_rollback_plan(vault, snap, added, modified)
        RECOMPOSE_SCHEMA(vault)
        ok, summary = (True, "validation skipped (--no-validate)") \
            if no_validate else VALIDATOR(vault)
        if ok and not no_validate:
            reg = _conformance_regressions(snap / "tree", vault, cap=300)
            if reg:
                ok, summary = False, (f"{len(reg)}+ page(s) REGRESSED conformance after the "
                                      f"migration (e.g. {reg[0]}) — rolling back")
            else:
                unknown = _unknown_type_regressions(snap / "tree", vault, cap=300)
                if unknown:
                    ok, summary = False, (f"{len(unknown)}+ page(s) gained a NEW out-of-taxonomy "
                                          f"type after the migration (e.g. {unknown[0]}) — rolling back")
        print(f"    roll-forward check: {summary}")
        if not ok:
            raise RuntimeError(summary)
        record_pack_version(vault, name, incoming)
        confirm_snapshot_good(snap, datetime.now(timezone.utc).isoformat())
    except (Exception, KeyboardInterrupt) as e:
        n = restore(vault, snap, added=added_since_snapshot(vault, snap),
                    modified=changed_since_snapshot(vault, snap))
        shutil.rmtree(snap, ignore_errors=True)
        print(f"    ✗ pack migration FAILED mid-apply: {e}")
        print(f"    ↩ ROLLED BACK ({n} files restored) — vault unchanged; added files "
              f"quarantined under .okengine/rolled-back/, not deleted.")
        return 1
    # Freeze added/modified BEFORE the slow validation gate — same live-vault-write
    # protection as the engine path (#12). The state record above is inside `modified`,
    # so a rollback reverts it too and the span stays pending for a retry.
    for c in changes:
        print(f"    {c}")
    print(f"    state recorded in .okengine/migrations-state.json ({name} → {incoming})")
    prune_snapshots(vault, keep_snapshots)
    return 0


# --- CLI ---------------------------------------------------------------------

def render(plan: Plan) -> str:
    L = [f"engine pin: {plan.pin or '(none)'}   →   engine release: {plan.target or '(unknown)'}"]
    if plan.status == "current":
        L.append("  ✓ pin matches the running engine — nothing to do.")
    elif plan.status == "compatible":
        L.append("  ~ compatible (same series, engine is patch-newer); --apply records the exact pin.")
    elif plan.status == "upgrade":
        L.append("  ⤴ minor/major bump — pin is STALE; `validate` FAILs until upgraded.")
    elif plan.status == "ahead":
        L.append("  ✗ pack pin is NEWER than the running engine; downgrade is unsupported.")
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
    ap.add_argument("--restore-snapshot", metavar="ID",
                    help="safely restore one retained migration snapshot by exact directory ID")
    ap.add_argument("--confirm-snapshot-good", metavar="ID",
                    help="mark an observed-good snapshot eligible for count-based pruning")
    ap.add_argument("--migrations-dir", default=str(DEFAULT_MIGRATIONS_DIR),
                    help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    pack = Path(a.pack)
    if not pack.is_dir():
        print(f"ERROR: pack dir not found: {pack}", file=sys.stderr)
        return 2
    if a.restore_snapshot and a.confirm_snapshot_good:
        print("ERROR: restore and confirm snapshot operations are mutually exclusive", file=sys.stderr)
        return 2
    if a.confirm_snapshot_good:
        if a.apply or a.no_validate or a.no_snapshot:
            print("ERROR: --confirm-snapshot-good cannot be combined with apply/validation/snapshot "
                  "flags", file=sys.stderr)
            return 2
        snap_id = a.confirm_snapshot_good
        if Path(snap_id).name != snap_id or snap_id in {".", ".."}:
            print(f"ERROR: invalid snapshot ID: {snap_id!r}", file=sys.stderr)
            return 2
        try:
            confirm_snapshot_good(
                pack / SNAPSHOTS_REL / snap_id, datetime.now(timezone.utc).isoformat()
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Snapshot {snap_id} marked confirmed-good and is now eligible for pruning.")
        return 0
    if a.restore_snapshot:
        if a.apply or a.no_validate or a.no_snapshot:
            print("ERROR: --restore-snapshot cannot be combined with apply/validation/snapshot flags",
                  file=sys.stderr)
            return 2
        snap_id = a.restore_snapshot
        if Path(snap_id).name != snap_id or snap_id in {".", ".."}:
            print(f"ERROR: invalid snapshot ID: {snap_id!r}", file=sys.stderr)
            return 2
        snap_dir = pack / SNAPSHOTS_REL / snap_id
        if snap_dir.is_symlink() or not (snap_dir / "tree").is_dir() \
                or not (snap_dir / "manifest.json").is_file():
            print(f"ERROR: complete retained snapshot not found: {snap_id}", file=sys.stderr)
            return 2
        changed, conflicts = restore_saved_snapshot(pack, snap_dir)
        if conflicts:
            print("ERROR: safe restore refused; files changed after the migration:", file=sys.stderr)
            for conflict in conflicts:
                print(f"  - {conflict}", file=sys.stderr)
            print("Resolve or preserve those files explicitly, then retry; nothing was changed.",
                  file=sys.stderr)
            return 1
        print(f"Restored {changed} migration-owned path(s) from snapshot {snap_id}; "
              "migration-added files were quarantined, not deleted.")
        return 0
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
    if plan.status == "ahead":
        print("ERROR: refusing to rewrite a newer pack with an older engine. "
              "Use the matching/newer engine checkout.", file=sys.stderr)
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
    try:
        changes = apply_upgrade(pack, plan, now_iso)
        # Validation and conformance checks prefer this artifact when it exists. Rebuild it inside
        # the transaction so the roll-forward gate cannot validate the pre-upgrade taxonomy.
        RECOMPOSE_SCHEMA(pack)
    except Exception as e:
        # A migration that mutates several files THEN raises (bad transform, missing file, a perms
        # error on one page) would leave the pack HALF-UPGRADED. The Phase-3 doc promises auto-rollback
        # on a bad migration, but that path was wired ONLY to the roll-forward VALIDATION result — an
        # apply-time exception fell straight through to a CLI stack trace, snapshot untouched
        # (invariant-audit). Roll back on ANY apply failure, not just a failed gate.
        if snap:
            n = restore(pack, snap, added=added_since_snapshot(pack, snap),
                        modified=changed_since_snapshot(pack, snap))
            shutil.rmtree(snap, ignore_errors=True)
            print(f"\n  ✗ migration FAILED mid-apply: {e}")
            print(f"  ↩ ROLLED BACK to the pre-upgrade source ({n} files restored) — pack unchanged.")
            print(f"    (any migration-added files were quarantined under .okengine/rolled-back/, "
                  f"not deleted — recover from there if a concurrent write was caught up.)")
        else:
            print(f"\n  ✗ migration FAILED mid-apply: {e}; --no-snapshot was set, so NO automatic "
                  "rollback. Restore the pack manually.")
        return 1
    # Freeze the migration's added- AND modified-sets NOW — before the (slower) validation gate — so
    # a rollback removes only what the migration added and reverts only what it changed, never
    # live-vault writes (new OR edited pages) made during that window (#12 + the modified-set gap).
    added = added_since_snapshot(pack, snap) if snap else None
    modified = changed_since_snapshot(pack, snap) if snap else None
    if snap:
        record_rollback_plan(pack, snap, added, modified)
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
    ok, summary = VALIDATOR(pack)               # roll-forward gate (structural: framework validate)
    # Page-conformance REGRESSION check — ONLY when a migration actually transformed pages (a
    # pin-bump-only upgrade changed nothing, so it can't corrupt anything) AND we have a snapshot to
    # diff against. Fail only on pages the migration made non-conformant, never on pre-existing
    # failures a real vault carries — else a legit upgrade rolls back over stale data (the fleet-roll
    # regression the baseline-less exhaustive scan caused).
    if ok and snap and plan.migrations:
        reg = _conformance_regressions(snap / "tree", pack, cap=300)
        if reg:
            ok = False
            summary = (f"{len(reg)}+ vault page(s) REGRESSED conformance after the migration "
                       f"(e.g. {reg[0]}) — rolling back")
        else:
            unknown = _unknown_type_regressions(snap / "tree", pack, cap=300)
            if unknown:
                ok = False
                summary = (f"{len(unknown)}+ vault page(s) gained a NEW out-of-taxonomy type "
                           f"after the migration (e.g. {unknown[0]}) — rolling back")
    print(f"\nRoll-forward check: {summary}")
    if not ok:
        if snap:
            n = restore(pack, snap, added=added, modified=modified)
            shutil.rmtree(snap, ignore_errors=True)   # the failed attempt's snapshot is spent
            print(f"  ↩ ROLLED BACK to the pre-upgrade source ({n} files restored) — pack unchanged.")
            print(f"    (migration-added files were quarantined under .okengine/rolled-back/, not "
                  f"deleted — recover from there if a concurrent write was caught up.)")
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
