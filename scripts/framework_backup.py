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

Secrets (`.env`, `.hermes-data/auth.json`) are EXCLUDED by default — restore re-provisions keys.
Pass `--include-secrets` to capture them (the archive is then sensitive; store it accordingly).
"""
import argparse
import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# directory names skipped anywhere in the tree
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
# path prefixes (relative to the pack) skipped — transient / heavy / recursive
_SKIP_PREFIXES = (".okengine/snapshots", ".okengine/backups", ".hermes-data/logs", "tmp")
# sensitive files skipped unless --include-secrets
_SECRET_PATHS = {".env", ".hermes-data/auth.json"}
# config.yaml is KEPT (restorable runtime config) but its live MCP Bearer token is REDACTED in a
# no-secrets backup: ensure-runtime.sh rewrites its Authorization header to `Bearer <token>`, so an
# un-redacted no-secrets archive would leak that token while the CLI prints "(secrets excluded)"
# (okengine invariant-audit). --include-secrets keeps the token verbatim.
_REDACT_PATHS = {".hermes-data/config.yaml"}
# The live secret in config.yaml is the MCP `Bearer <token>` — redact the token after "Bearer".
_REDACT_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{6,}")
_MANIFEST = "MANIFEST.json"


def _excluded(rel: Path, include_secrets: bool) -> bool:
    if any(p in _SKIP_DIRS for p in rel.parts):
        return True
    s = rel.as_posix()
    for pre in _SKIP_PREFIXES:
        if s == pre or s.startswith(pre + "/"):
            return True
    if not include_secrets and s in _SECRET_PATHS:
        return True
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


def _file_bytes(pack: Path, rel: Path, include_secrets: bool) -> bytes:
    """The bytes to archive for `rel`. For a redact-path (config.yaml) in a no-secrets backup, the
    live MCP Bearer token is stripped so the archive honors "secrets excluded" without dropping the
    restorable runtime config. Manifest + tar both go through this, so the digest stays consistent."""
    raw = (pack / rel).read_bytes()
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


# --- create ------------------------------------------------------------------

def default_dest(pack: Path) -> Path:
    """Backups go beside the pack (survive losing the pack dir), not inside it."""
    return pack.parent / f"{pack.name}-backups"


def create(pack: Path, dest_dir: Path, include_secrets: bool, stamp: str) -> tuple:
    files = iter_files(pack, include_secrets)
    manifest = build_manifest(pack, files, include_secrets)
    manifest.update(created=stamp, pack=pack.name, include_secrets=include_secrets)
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / f"{pack.name}-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for rel in files:
            data = _file_bytes(pack, rel, include_secrets)
            info = tarfile.TarInfo(rel.as_posix())
            info.size = len(data)
            info.mode = (pack / rel).stat().st_mode & 0o777
            tar.addfile(info, io.BytesIO(data))
        data = (json.dumps(manifest, indent=2) + "\n").encode()
        info = tarfile.TarInfo(_MANIFEST)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return archive, manifest


# --- verify ------------------------------------------------------------------

def verify(archive: Path) -> tuple:
    """(ok, manifest, problems). Recompute every archived file's sha256 vs the manifest."""
    problems = []
    with tarfile.open(archive, "r:gz") as tar:
        try:
            manifest = json.loads(tar.extractfile(_MANIFEST).read())
        except Exception as e:
            return False, {}, [(_MANIFEST, f"unreadable: {e}")]
        names = set(tar.getnames())
        for name, expected in manifest.get("files", {}).items():
            if name not in names:
                problems.append((name, "missing from archive"))
                continue
            got = hashlib.sha256(tar.extractfile(name).read()).hexdigest()
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
    return True, manifest, []


# --- list / prune ------------------------------------------------------------

def list_backups(dest_dir: Path) -> list:
    if not dest_dir.is_dir():
        return []
    return sorted((f for f in dest_dir.glob("*.tar.gz") if f.is_file()), key=lambda f: f.name)  # glob-ok: flat backups dir, not a sharded namespace


def prune_backups(dest_dir: Path, keep: int) -> int:
    backups = list_backups(dest_dir)
    removed = 0
    for f in (backups[:-keep] if keep else backups):
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
    return f"{n}B"


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
        archive, manifest = create(pack, dest, a.include_secrets, stamp)
        size = archive.stat().st_size
        print(f"backup: {archive}")
        print(f"  {manifest['count']} files · {_human(size)} · digest {manifest['digest'][:12]}"
              + ("  ⚠ includes secrets" if a.include_secrets else "  (secrets excluded)"))
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
        backups = list_backups(dest)
        print(f"{len(backups)} backup(s) in {dest}:")
        for f in backups:
            print(f"  {f.name}  ({_human(f.stat().st_size)})")
        return 0

    if a.action == "prune":
        pack = Path(a.pack)
        dest = Path(a.dest) if a.dest else default_dest(pack)
        n = prune_backups(dest, a.keep)
        print(f"pruned {n} backup(s) in {dest} (kept newest {a.keep})")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
