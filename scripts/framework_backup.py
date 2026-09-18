"""framework backup — disaster-recovery snapshots of a vault + runtime (okengine#65).

    framework backup create  <pack> [--dest DIR] [--include-secrets]
    framework backup verify  <archive>
    framework backup restore <archive> <target> [--force] [--no-validate]
    framework backup list    <pack> [--dest DIR]
    framework backup prune   <pack> [--dest DIR] --keep N

A backup is a single `.tar.gz` of the pack SOURCE + runtime state — the vault (`wiki/`, schema,
`pack.yaml`, crons, feeds, config) and `.hermes-data` (config.yaml, cron-plus state, the qmd
index) — MINUS heavy/transient/secret files (`.git`, logs, snapshots/backups, `__pycache__`,
`.env`, `auth.json`). It carries a `MANIFEST.json` of per-file sha256 digests + a roll-up digest,
so a backup can be integrity-verified before any restore. Restore extracts into a target dir only
after the manifest verifies, then (by default) re-runs `framework validate` on the result.

Secrets (`.env`, `.hermes-data/auth.json`, and generated extension credentials) are EXCLUDED by
default — restore re-provisions keys.
Pass `--include-secrets` to capture them (the archive is then sensitive; store it accordingly).
"""
import argparse
import collections
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from okengine.corpus_transaction import CorpusLockTimeout, stable_corpus

_HERE = Path(__file__).resolve().parent

# directory names skipped anywhere in the tree
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
# SQLite databases are captured via the online-backup API for a CONSISTENT snapshot even under
# concurrent writes (the qmd index is written by the MCP's _index_maintainer while a nightly backup
# runs — a plain file copy captures torn pages and a db/-wal pair that never coexisted, so verify()
# rejects the archive or restore yields a corrupt index; invariant-audit #33/#34). Their transient
# -wal/-shm/-journal sidecars are EXCLUDED — the backup API folds the WAL into the snapshot, so a
# raw sidecar copy would be both redundant and inconsistent with the checkpointed main db.
_SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
# path prefixes (relative to the pack) skipped — transient / heavy / recursive
_SKIP_PREFIXES = (".okengine/snapshots", ".okengine/backups", ".hermes-data/logs", "tmp")
# sensitive files skipped unless --include-secrets
_SECRET_PATHS = {
    ".env",
    ".hermes-data/auth.json",
    ".okengine/extension-secrets.json",
    ".okengine/generated/sidecars.compose.yml",
}
_CORPUS_TRANSIENT_PATHS = {
    ".okengine/corpus/active.json",
    ".okengine/corpus/lock",
    ".okengine/corpus/lock-owner.json",
}
# config.yaml is KEPT (restorable runtime config) but its live MCP Bearer token is REDACTED in a
# no-secrets backup: ensure-runtime.sh rewrites its Authorization header to `Bearer <token>`, so an
# un-redacted no-secrets archive would leak that token while the CLI prints "(secrets excluded)"
# (okengine invariant-audit). --include-secrets keeps the token verbatim.
_REDACT_PATHS = {".hermes-data/config.yaml"}
# The live secret in config.yaml is the MCP `Bearer <token>` — redact the token after "Bearer".
# The class must cover every realistic token charset in ONE match, not just hex/urlsafe-base64:
# OKENGINE_MCP_TOKEN is operator-settable to any string, so a STANDARD-base64 token
# (e.g. `openssl rand -base64`) carries `+` `/` `=` — omitting those redacts only the leading run
# and leaks the tail into a "(secrets excluded)" archive. A quote/whitespace/newline still ends the
# match, so adding them can't over-run past the quoted value.
_REDACT_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]{6,}")
_MANIFEST = "MANIFEST.json"


def _is_sqlite(rel: Path) -> bool:
    return rel.suffix.lower() in _SQLITE_SUFFIXES


def _excluded(rel: Path, include_secrets: bool) -> bool:
    if any(p in _SKIP_DIRS for p in rel.parts):
        return True
    s = rel.as_posix()
    for pre in _SKIP_PREFIXES:
        if s == pre or s.startswith(pre + "/"):
            return True
    # qmd's cache co-locates the restorable SQLite search index with ~2 GB of downloaded GGUF model
    # blobs and other rebuildable caches. Keep SQLite databases (captured consistently by the online
    # backup path below), but never copy the heavyweight model/cache payload into every vault backup.
    if s.startswith(".hermes-data/qmd/cache/") and not _is_sqlite(rel):
        return True
    if not include_secrets and s in _SECRET_PATHS:
        return True
    if s in _CORPUS_TRANSIENT_PATHS:
        return True
    if any(rel.name.endswith(sc) for sc in _SQLITE_SIDECARS):
        return True                     # transient sqlite WAL/journal — folded into the db snapshot
    if s.startswith(".hermes-data/cron-plus/") and rel.name.startswith("jobs.json."):
        return True                     # superseded runtime copy; may be root-only and is not active state
    if s.startswith(".hermes-data/cron-plus/pids/") or s == ".hermes-data/cron-plus/.tick.lock":
        return True                     # transient scheduler runtime — restoring a stale pidfile or the
                                        # tick lock verbatim permanently orphans a lane (blocks re-trigger); #326 [26]
    return False


def iter_files(pack: Path, include_secrets: bool):
    """Relative paths of files to back up, sorted (deterministic manifest/digest)."""
    out = []
    for p in pack.rglob("*"):
        if p.is_file() and not p.is_symlink():
            rel = p.relative_to(pack)
            if not _excluded(rel, include_secrets):
                out.append(rel)
    return sorted(out, key=lambda r: r.as_posix())


def skipped_symlinks(pack: Path, include_secrets: bool):
    """Symlinked files/dirs that iter_files silently drops (invariant-audit #60). Their content is
    NOT in the archive — surfaced as a create-time WARN so a restore isn't quietly missing pages."""
    out = []
    for p in pack.rglob("*"):
        if p.is_symlink():
            rel = p.relative_to(pack)
            if not _excluded(rel, include_secrets):
                out.append(rel)
    return sorted(out, key=lambda r: r.as_posix())


def _sqlite_snapshot_bytes(src: Path) -> bytes:
    """A CONSISTENT copy of a live SQLite db via the online-backup API (WAL folded, no torn pages)."""
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "snap.sqlite"
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        try:
            dst = sqlite3.connect(str(snap))
            try:
                con.backup(dst)
            finally:
                dst.close()
        finally:
            con.close()
        return snap.read_bytes()


def _file_bytes(pack: Path, rel: Path, include_secrets: bool) -> bytes:
    """The bytes to archive for `rel`. For a redact-path (config.yaml) in a no-secrets backup, the
    live MCP Bearer token is stripped so the archive honors "secrets excluded" without dropping the
    restorable runtime config. Manifest + tar both go through this, so the digest stays consistent."""
    src = pack / rel
    if _is_sqlite(rel):
        # Online-backup snapshot for a consistent copy under concurrent writes (#33/#34). Fall back to
        # a plain read if the file isn't actually a usable sqlite db (never silently drop content).
        try:
            return _sqlite_snapshot_bytes(src)
        except Exception:
            pass
    raw = src.read_bytes()
    if not include_secrets and rel.as_posix() in _REDACT_PATHS:
        return _REDACT_RE.sub(r"\1<redacted>", raw.decode("utf-8", "replace")).encode("utf-8")
    return raw


def build_manifest(pack: Path, files, include_secrets: bool = True) -> dict:
    entries = {}
    for rel in files:
        entries[rel.as_posix()] = hashlib.sha256(_file_bytes(pack, rel, include_secrets)).hexdigest()
    rollup = hashlib.sha256(
        "\n".join(f"{k} {v}" for k, v in sorted(entries.items())).encode()).hexdigest()
    return {"files": entries, "digest": rollup, "count": len(entries)}


def _deployment_identity(pack: Path) -> tuple[int | None, int | None]:
    """Return the portable gateway uid/gid pin without archiving dotenv secrets."""
    values: dict[str, int] = {}
    env_path = pack / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^\s*(HERMES_UID|HERMES_GID)\s*=\s*([0-9]+)\s*(?:#.*)?$", line)
            if match and int(match.group(2)) > 0:
                values[match.group(1)] = int(match.group(2))
    runtime = pack / ".hermes-data"
    owner_path = runtime if runtime.exists() else pack
    stat = owner_path.stat()
    return values.get("HERMES_UID", stat.st_uid or None), values.get("HERMES_GID", stat.st_gid or None)


def _restore_deployment_identity(target: Path, manifest: dict) -> None:
    """Bind a restore to the target host's extracted runtime owner.

    Numeric uid/gid values are host-local. Reusing the source host's values can make the gateway
    start as an identity that cannot write the files just extracted. The restored runtime directory
    is the target-host authority; a minimal archive without it falls back to the target directory.
    """
    owner = target / ".hermes-data"
    owner = owner if owner.exists() else target
    owner_stat = owner.stat()
    replacements = {
        "HERMES_UID": f"HERMES_UID={owner_stat.st_uid}",
        "HERMES_GID": f"HERMES_GID={owner_stat.st_gid}",
    }
    env_path = target / ".env"
    if not env_path.exists():
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(
                "# Restored non-secret deployment identity; add deployment secrets below.\n"
                + "\n".join(replacements.values()) + "\n"
            )
    else:
        existing = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        output = []
        seen = set()
        for line in existing:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in replacements:
                if key not in seen:
                    output.append(replacements[key])
                    seen.add(key)
            else:
                output.append(line)
        output.extend(replacements[key] for key in ("HERMES_UID", "HERMES_GID") if key not in seen)
        env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    env_path.chmod(0o600)


# --- create ------------------------------------------------------------------

def default_dest(pack: Path) -> Path:
    """Backups go beside the pack (survive losing the pack dir), not inside it."""
    return pack.parent / f"{pack.name}-backups"


def create(pack: Path, dest_dir: Path, include_secrets: bool, stamp: str,
           lock_timeout_seconds: float | None = None) -> tuple:
    # SINGLE-PASS: hash each file from the SAME bytes we write into the tar. The old two-pass path
    # (build_manifest read every file, then the tar re-read every file) hashed and archived snapshots
    # taken minutes apart on a 10k-page + ~2GB-qmd vault — any file that changed between its two reads
    # produced an archive whose bytes no longer matched its manifest sha256, which verify()/restore()
    # then rejected as corrupt (invariant-audit HIGH). Reading once closes the window.
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / f"{pack.name}-{stamp}.tar.gz"
    fd, partial_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", suffix=".partial", dir=dest_dir
    )
    os.close(fd)
    Path(partial_name).unlink()
    try:
        with stable_corpus(pack, lock_timeout_seconds=lock_timeout_seconds) as corpus_epoch:
            files = iter_files(pack, include_secrets)
            entries = {}
            with tarfile.open(partial_name, "w:gz") as tar:
                for rel in files:
                    data = _file_bytes(pack, rel, include_secrets)
                    entries[rel.as_posix()] = hashlib.sha256(data).hexdigest()
                    st = (pack / rel).stat()
                    info = tarfile.TarInfo(rel.as_posix())
                    info.size = len(data)
                    info.mode = st.st_mode & 0o777
                    info.mtime = int(st.st_mtime)
                    tar.addfile(info, io.BytesIO(data))
                rollup = hashlib.sha256(
                    "\n".join(f"{k} {v}" for k, v in sorted(entries.items())).encode()
                ).hexdigest()
                hermes_uid, hermes_gid = _deployment_identity(pack)
                manifest = {
                    "files": entries, "digest": rollup, "count": len(entries),
                    "created": stamp, "pack": pack.name, "include_secrets": include_secrets,
                    "corpus_epoch": corpus_epoch,
                    "consistency": {
                        "canonical_markdown_schema_config": "corpus-fenced",
                        "sqlite": "online-backup-per-database",
                        "runtime_files": "best-effort-per-file",
                    },
                    "hermes_uid": hermes_uid, "hermes_gid": hermes_gid,
                }
                data = (json.dumps(manifest, indent=2) + "\n").encode()
                info = tarfile.TarInfo(_MANIFEST)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        os.replace(partial_name, archive)
    finally:
        Path(partial_name).unlink(missing_ok=True)
    return archive, manifest


# --- verify ------------------------------------------------------------------

def verify(archive: Path) -> tuple:
    """(ok, manifest, problems). Recompute every archived file's sha256 vs the manifest."""
    problems = []
    try:
        tar = tarfile.open(archive, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        return False, {}, [(archive.name, f"unreadable archive: {exc}")]
    with tar:
        try:
            manifest_member = tar.extractfile(_MANIFEST)
            if manifest_member is None:
                raise ValueError("manifest is not a regular file")
            manifest = json.loads(manifest_member.read())
            declared = manifest.get("files", {})
            if not isinstance(declared, dict):
                raise ValueError("files must be an object")
        except Exception as exc:
            return False, {}, [(_MANIFEST, f"unreadable: {exc}")]

        member_names = tar.getnames()
        names = set(member_names)
        expected_names = set(declared) | {_MANIFEST}
        for name in sorted(names - expected_names):
            problems.append((name, "not declared in manifest"))
        for name in sorted(expected_names - names):
            problems.append((name, "missing from archive"))
        duplicates = sorted(name for name, count in collections.Counter(member_names).items()
                            if count > 1)
        for name in duplicates:
            problems.append((name, "duplicate archive member"))

        for name, expected in declared.items():
            if name not in names:
                continue
            member = tar.extractfile(name)
            if member is None:
                problems.append((name, "not a regular file"))
                continue
            got = hashlib.sha256(member.read()).hexdigest()
            if got != expected:
                problems.append((name, "checksum mismatch"))
    return (not problems), manifest, problems


# --- restore -----------------------------------------------------------------

def _validator(target: Path):
    """Post-restore conformance: re-run `framework validate`. (ok, summary)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "framework_validate", _HERE / "framework_validate.py")
        fv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fv)
        rc = fv.main([str(target), "--quiet"])
        return (rc == 0, f"framework validate → exit {rc}")
    except Exception as e:
        return (True, f"validation skipped ({e})")


VALIDATOR = _validator   # overridable for tests


def _rearm_restored_schedule(target: Path) -> None:
    """Force cron-plus to compute future fire times after a point-in-time restore.

    The historical run/result fields remain useful DR state, but an archived
    ``next_run_at`` is necessarily relative to the backup instant. Replaying it
    days later makes every overdue lane fire together on the first scheduler
    tick. A null value is cron-plus's supported recomputation signal.
    """
    jobs_path = target / ".hermes-data" / "cron-plus" / "jobs.json"
    if not jobs_path.is_file():
        return
    state = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = state.get("jobs") if isinstance(state, dict) and set(state) == {"jobs"} else state
    if not isinstance(jobs, list):
        raise ValueError(f"restored cron state is not a job list: {jobs_path}")
    for job in jobs:
        if isinstance(job, dict):
            job["next_run_at"] = None
    staged = jobs_path.with_suffix(".json.restore-staged")
    staged.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    staged.replace(jobs_path)


def restore(archive: Path, target: Path, force: bool) -> tuple:
    """Verify the archive, then extract into target (must be empty unless force)."""
    ok, manifest, problems = verify(archive)
    if not ok:
        return False, manifest, problems
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()) and not force:
        raise FileExistsError(f"target not empty: {target} (use --force to overwrite)")
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name != _MANIFEST]
        tar.extractall(target, members=members, filter="data")   # 'data' = path-traversal safe
    _rearm_restored_schedule(target)
    _restore_deployment_identity(target, manifest)
    return True, manifest, []


# --- list / prune ------------------------------------------------------------

def list_backups(dest_dir: Path, pack_name: "str | None" = None) -> list:
    """Backups in dest_dir, oldest→newest by MTIME. `pack_name` restricts to `{pack_name}-*.tar.gz`
    — REQUIRED for prune when a dest is SHARED across packs (the documented `--dest /backups`
    pattern): archives are named `{pack.name}-{stamp}`, so a name sort is dominated by the pack
    prefix and would prune across pack boundaries, silently destroying another pack's DR history
    (invariant-audit HIGH #3). mtime sort also survives a same-second re-created archive."""
    if not dest_dir.is_dir():
        return []
    pat = f"{pack_name}-*.tar.gz" if pack_name else "*.tar.gz"
    return sorted((f for f in dest_dir.glob(pat) if f.is_file()), key=lambda f: f.stat().st_mtime)  # glob-ok: flat backups dir, not a sharded namespace


def prune_backups(dest_dir: Path, keep: int, pack_name: "str | None" = None) -> int:
    """Delete all but the newest `keep` backups OF THIS PACK. keep < 1 is refused — retaining zero
    backups is never what an operator means and `--keep 0` used to silently wipe the whole dest."""
    if keep < 1:
        raise ValueError("prune --keep must be >= 1 (retaining 0 backups deletes your DR history)")
    backups = list_backups(dest_dir, pack_name)
    removed = 0
    for f in backups[:-keep]:
        f.unlink()
        removed += 1
    return removed


# --- CLI ---------------------------------------------------------------------

def _human(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{f:.0f}{u}" if u == "B" else f"{f:.1f}{u}"
        f /= 1024
    return f"{n}B"  # pragma: no cover - the loop's TB arm is unconditional


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="framework backup",
                                 description="Backup / restore / verify a vault + runtime (okengine#65).")
    sub = ap.add_subparsers(dest="action", required=True)
    c = sub.add_parser("create", help="create a backup archive")
    c.add_argument("pack")
    c.add_argument("--dest", default=None)
    c.add_argument("--include-secrets", action="store_true")
    v = sub.add_parser("verify", help="integrity-check an archive")
    v.add_argument("archive")
    r = sub.add_parser("restore", help="restore an archive into a target dir")
    r.add_argument("archive")
    r.add_argument("target")
    r.add_argument("--force", action="store_true")
    r.add_argument("--no-validate", action="store_true")
    li = sub.add_parser("list", help="list a pack's backups")
    li.add_argument("pack")
    li.add_argument("--dest", default=None)
    pr = sub.add_parser("prune", help="keep the newest N backups")
    pr.add_argument("pack")
    pr.add_argument("--dest", default=None)
    pr.add_argument("--keep", type=int, required=True)
    a = ap.parse_args(argv)

    if a.action == "create":
        pack = Path(a.pack)
        if not pack.is_dir():
            print(f"ERROR: pack dir not found: {pack}", file=sys.stderr)
            return 2
        dest = Path(a.dest) if a.dest else default_dest(pack)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            archive, manifest = create(pack, dest, a.include_secrets, stamp)
        except CorpusLockTimeout as exc:
            print(f"ERROR: backup could not acquire a stable corpus state: {exc}", file=sys.stderr)
            return 1
        size = archive.stat().st_size
        print(f"backup: {archive}")
        print(f"  {manifest['count']} files · {_human(size)} · digest {manifest['digest'][:12]}"
              + ("  ⚠ includes secrets" if a.include_secrets else "  (secrets excluded)"))
        # Symlinks are NOT dereferenced into the archive; warn loudly so a restore isn't silently
        # missing content (verify still passes on what WAS captured — invariant-audit #60).
        syms = skipped_symlinks(pack, a.include_secrets)
        if syms:
            print(f"  ⚠ {len(syms)} symlink(s) EXCLUDED (content not backed up): "
                  + ", ".join(s.as_posix() for s in syms[:8])
                  + (" …" if len(syms) > 8 else ""), file=sys.stderr)
        return 0

    if a.action == "verify":
        ok, manifest, problems = verify(Path(a.archive))
        if not manifest:
            print("✗ not a valid backup archive", file=sys.stderr)
            return 2
        print(f"archive: {a.archive}  ·  {manifest.get('count')} files  ·  created {manifest.get('created')}")
        if ok:
            print(f"  ✓ integrity OK — digest {manifest['digest'][:12]}")
            return 0
        print(f"  ✗ {len(problems)} problem(s):")
        for name, why in problems[:20]:
            print(f"    {why}: {name}")
        return 1

    if a.action == "restore":
        try:
            ok, manifest, problems = restore(Path(a.archive), Path(a.target), a.force)
        except FileExistsError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        if not ok:
            print(f"✗ refusing to restore — archive failed integrity ({len(problems)} problem(s)):",
                  file=sys.stderr)
            for name, why in problems[:10]:
                print(f"    {why}: {name}", file=sys.stderr)
            return 1
        print(f"restored {manifest['count']} files → {a.target}  (integrity ✓)")
        if not a.no_validate:
            okv, summary = VALIDATOR(Path(a.target))
            print(f"  post-restore: {summary}")
            if not okv:
                print("  ⚠ restored vault does not validate — inspect before deploying.")
                return 1
        return 0

    if a.action == "list":
        pack = Path(a.pack)
        dest = Path(a.dest) if a.dest else default_dest(pack)
        backups = list_backups(dest, pack_name=pack.name)   # pack-scoped: a shared dest holds others' too
        print(f"{len(backups)} '{pack.name}' backup(s) in {dest}:")
        for f in backups:
            print(f"  {f.name}  ({_human(f.stat().st_size)})")
        return 0

    if a.action == "prune":  # pragma: no branch - argparse restricts action to the handled choices
        pack = Path(a.pack)
        dest = Path(a.dest) if a.dest else default_dest(pack)
        try:
            n = prune_backups(dest, a.keep, pack_name=pack.name)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"pruned {n} '{pack.name}' backup(s) in {dest} (kept newest {a.keep})")
        return 0

    return 2  # pragma: no cover - required subparser choices are exhaustively handled above


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
