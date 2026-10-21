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
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from tarfile import TarError  # bare name: cosmic-ray cannot mutate a dotted exception

_HERE = Path(__file__).resolve().parent
# Run directly (`python scripts/framework_backup.py restore …`), the engine package under src/ is
# not on sys.path; scripts/framework.py adds it for `framework backup` (okengine#1041 item 3).
if str(_HERE.parent / "src") not in sys.path:
    sys.path.insert(0, str(_HERE.parent / "src"))

from okengine.corpus_transaction import CorpusLockTimeout, stable_corpus  # noqa: E402
from okengine.file_fingerprint import SETTLE_NS  # noqa: E402

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
_SQLITE_HEADER = b"SQLite format 3\x00"
# path prefixes (relative to the pack) skipped — transient / heavy / recursive
_SKIP_PREFIXES = (".okengine/snapshots", ".okengine/backups", ".hermes-data/logs", "tmp")
# sensitive files skipped unless --include-secrets
_SECRET_PATHS = {
    ".env",
    ".hermes-data/auth.json",
    ".okengine/extension-secrets.json",
    ".okengine/generated/sidecars.compose.yml",
    # Hermes loads $HERMES_HOME/.env (= .hermes-data/.env) over the container environment, and
    # `hermes setup` writes provider and platform credentials there (v0.15.4 release audit).
    ".hermes-data/.env",
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
_RUNTIME_DIR = ".hermes-data"
# Where create() stages its fenced snapshot: inside the pack so a hardlink never crosses a
# filesystem, and under a skipped prefix so it is neither walked nor archived.
_STAGING_DIR = ".okengine/backups/.staging"
# Archive attempts before create() accepts the files still rewritten in place and names them in
# the manifest. No attempt ever copies the whole fenced set under the fence (see create()).
_SNAPSHOT_ATTEMPTS = 3
# Files modified this recently are likely to be written again during the archive, so they are copied
# under the fence (within _COPY_BUDGET_BYTES) instead of linked.
_HOT_NS = 30 * 60 * 1_000_000_000
# Bytes the snapshot may copy under the fence for HOT files.
_COPY_BUDGET_BYTES = 64 * 1024 * 1024
# Hard cap on the files that MUST be copied (written within SETTLE_NS of the fence, known in-place
# writers, files found changed during an earlier attempt). Whatever triggers them, the copies under
# one fence never exceed this; anything beyond it is hardlinked with a fresh fence-time identity, and
# named in rewritten_after_fence if it changes again on the last attempt (QA on !1314: one
# `chmod -R` once flagged all 100k files and the rebuild copied them all for 134 s).
_FORCED_COPY_MAX_FILES = 2000
_FORCED_COPY_MAX_BYTES = 128 * 1024 * 1024
# Files the deployment rewrites or appends IN PLACE as a matter of course: the write path's log.md
# append (write_services.policy._append_log), the corpus journal, and JSON state files.
_IN_PLACE_NAMES = frozenset({"log.md"})
_IN_PLACE_SUFFIXES = (".jsonl",)
# The cron-plus job store's sidecar lock: must equal cron_plus_store.LOCK_NAME and JOBS_LOCK_FILE in
# hermes-cron-plus jobs.py, checked by install-cron-plus.sh (okengine#850; pinned by tests/test_framework_backup.py).
_CRON_PLUS_LOCK_NAME = "jobs.json.lock"
_JOBS_STORE = ".hermes-data/cron-plus/jobs.json"

# `.env` mixes secrets with non-secret deployment config. The file is excluded as a whole (above), so
# the non-secret part is carried in the manifest by an explicit per-key classification (okengine#991:
# dropping the whole file silently reverted a restored deployment's schedules to UTC). The rule FAILS
# CLOSED: only _PORTABLE_ENV_KEYS are ever carried; an unlisted key is treated as a secret.
# tests/test_framework_backup.py requires every key in the skeleton .env.example, and every key the
# deploy scripts read from .env, to be classified here.
_PORTABLE_ENV_KEYS = frozenset({
    "OKENGINE_DEPLOYMENT", "OKENGINE_UI_DISPLAY_NAME", "OKENGINE_BIND",
    "TZ", "CRON_TZ", "OKENGINE_BRIEF_HOUR", "CRON_DEFER_UTC_HOURS",
    "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL",
    "OKENGINE_PROJECTION_MAX_AGE_HOURS",
    "OKENGINE_REVIEW_API", "OKENGINE_REVIEWER_NAME", "OKENGINE_REVIEW_TRUSTED_NETWORK",
    "OKENGINE_TRUST", "OKENGINE_PROJECT_URL", "OKENGINE_OPERATION_ALLOW", "HERMES_CRON_MAX_PARALLEL",
    "OKENGINE_MCP_INDEX_MIN_UPDATE_SECONDS", "OKENGINE_MCP_INDEX_POLL_SECONDS",
    "OKENGINE_MCP_INDEX_REFRESH_HOURS", "OKENGINE_MCP_QMD_CONCURRENCY",
    "OKENGINE_MCP_SEARCH_QUEUE_SECONDS", "OKENGINE_MCP_SEARCH_TIMEOUT_SECONDS",
    "OKENGINE_PROJECTION_INTERVAL_SECONDS", "OKENGINE_PROJECTION_POLL_SECONDS",
    "OKENGINE_PROJECTION_MAX_DELETE_ABS", "OKENGINE_PROJECTION_MAX_DELETE_PCT",
    # Posture switches: dropping one on restore silently changes behavior (OKENGINE_EDITING unset
    # means editing ON, so a read-only deployment would come back writable).
    "OKENGINE_EDITING", "API_SERVER_ENABLED", "COMPOSE_PROFILES",
})
# Host-local values: rebound on the target host (HERMES_UID/GID from the restored tree's owner) or
# rewritten by deploy.sh from its verified checkout and build (ENGINE_DIR, OKENGINE_SOURCE_SHA,
# OKENGINE_GATEWAY_IMAGE, OKENGINE_ENGINE_IMAGE_TAG, and OKENGINE_TESTED_DIGESTS, the host path of
# the release's tested-digest list `deploy.sh --release-digests` rolled from, okengine#1048).
_HOST_LOCAL_ENV_KEYS = frozenset({"HERMES_UID", "HERMES_GID", "ENGINE_DIR", "OKENGINE_SOURCE_SHA",
                                  "OKENGINE_GATEWAY_IMAGE", "OKENGINE_ENGINE_IMAGE_TAG",
                                  "OKENGINE_TESTED_DIGESTS"})
# Documented secrets — never carried without --include-secrets (listed for the completeness check).
_SECRET_ENV_KEYS = frozenset({
    "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
    "TAVILY_API_KEY", "EXA_API_KEY", "OKENGINE_MCP_TOKEN", "OKENGINE_READER_PASSWORD",
    "OKENGINE_PROJECTION_WRITER_PASSWORD", "OKENGINE_PROJECTION_READER_PASSWORD",
    "OKENGINE_PROJECTION_READER_DSN", "OKENGINE_REVIEW_TOKEN", "API_SERVER_KEY",
})
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# A URL carrying userinfo (`scheme://user:pass@host`, `scheme://token@host`) holds a credential even
# under a non-secret key such as OKENGINE_PROJECT_URL, so such a value is never carried.
_URL_USERINFO_RE = re.compile(r"://[^/?#\s]*@")
_NOT_CARRIED_PRINT_LIMIT = 40
_ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*\Z")


class SQLiteSnapshotError(RuntimeError):
    """The online backup of a real SQLite database failed; the backup is refused (#1042 [6])."""


class RestoreOwnershipError(RuntimeError):
    """The restore would bind the gateway identity to root (uid or gid 0)."""


def _is_sqlite(rel: Path) -> bool:
    return rel.suffix.lower() in _SQLITE_SUFFIXES


def _excluded_dir(rel: Path) -> bool:
    """Whether `rel` lies in a tree that is never archived (a skipped directory name anywhere, or a
    skipped prefix). The walk prunes these directories instead of listing them: `.okengine/snapshots`
    alone held 518k files on a live vault, and the walk runs inside the corpus fence (okengine#1032)."""
    if any(p in _SKIP_DIRS for p in rel.parts):
        return True
    s = rel.as_posix()
    return any(s == pre or s.startswith(pre + "/") for pre in _SKIP_PREFIXES)


def _excluded(rel: Path, include_secrets: bool) -> bool:
    if _excluded_dir(rel):
        return True
    s = rel.as_posix()
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


def iter_files(pack: Path, include_secrets: bool, part: str = "all"):
    """Relative paths of files to back up, sorted (deterministic manifest/digest). Symlinks are
    never followed or archived (see skipped_symlinks). `part` splits the walk at the runtime dir:
    "corpus" is everything outside `.hermes-data` (walked inside the corpus fence), "runtime" is
    `.hermes-data` alone (cron-plus run output there held 205k files on a live vault, and none of
    it is fenced corpus state), "all" is both."""
    return _walk(pack, include_secrets, part)[0]


def _walk(pack: Path, include_secrets: bool, part: str) -> tuple:
    """(files, dirs): iter_files' files, plus every directory the walk entered, for the modes
    restore re-applies (the archive carries no directory entries). Same single pass."""
    top = pack / _RUNTIME_DIR if part == "runtime" else pack
    if top.is_symlink() or not top.is_dir():
        return [], []
    out = []
    pending = [top.relative_to(pack)]
    dirs = []
    while pending:
        base = pending.pop()
        try:
            entries = list(os.scandir(pack / base))
        except OSError:
            continue                     # unreadable directory: skipped, as os.walk/rglob did
        for entry in entries:
            rel = base / entry.name
            # DirEntry type checks come from the directory listing (no stat per file): this walk
            # runs inside the corpus fence, over 100k+ files on a large vault.
            if entry.is_dir(follow_symlinks=False):
                if not _excluded_dir(rel) and not (part == "corpus" and rel.as_posix() == _RUNTIME_DIR):
                    pending.append(rel)
                    dirs.append(rel)
            elif entry.is_file(follow_symlinks=False) and not _excluded(rel, include_secrets):
                out.append(rel)
    return sorted(out, key=lambda r: r.as_posix()), dirs


def _dir_modes(pack: Path, dirs) -> dict:
    """Permission bits of the walked directories, keyed by relative path. A directory removed
    since the walk, or replaced by anything but a directory, is left out."""
    modes = {}
    for rel in dirs:
        try:
            st = os.stat(pack / rel, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            modes[rel.as_posix()] = st.st_mode & 0o777
    return dict(sorted(modes.items()))


def _corpus_fenced(rel: Path) -> bool:
    """Whether `rel` is canonical corpus state the write fence protects: everything except the
    `.hermes-data` runtime and SQLite databases, which the manifest records as best-effort and
    which are captured on their own terms (online backup) outside the fence."""
    return rel.parts[0] != _RUNTIME_DIR and not _is_sqlite(rel)


def _written_in_place(rel: Path) -> bool:
    """A file the deployment is known to append or rewrite in place (never by rename)."""
    return (rel.name in _IN_PLACE_NAMES or rel.suffix in _IN_PLACE_SUFFIXES
            or (rel.suffix == ".json" and rel.parts[0] == ".okengine"))


def _identity(kind: str, st: os.stat_result) -> tuple:
    """The fence-time identity _changed_since compares against."""
    head = st.st_nlink if kind == "link" else st.st_ino
    return (kind, head, st.st_mtime_ns, st.st_size, st.st_ctime_ns,
            st.st_mode, st.st_uid, st.st_gid)


def _changed_since(ref: tuple, st: os.stat_result) -> bool:
    """Whether the file behind `ref` (recorded under the fence) may hold different bytes now.

    * mtime or size moved: an ordinary write. Changed.
    * A file read from the vault after the fence ("live", when it could not be linked): a new inode
      is a rename-publish. Changed.
    * Only ctime moved. That is either metadata (chmod, chown, a link added or removed elsewhere) or
      a same-size rewrite whose mtime was set back with os.utime (N1). The bytes cannot be compared:
      a hardlink shares them, and no fence-time digest exists. So:
        - mode, owner or group changed: a metadata operation (`chmod -R`, `chown -R` by
          ensure-runtime --fix-perms, ensure_readable, an operator), the common case. Unchanged.
        - a hardlink whose link count changed: a rename-publish or another backup's staging link.
          The bytes the snapshot holds are untouched. Unchanged.
        - otherwise (same mode, owner and link count): treated as changed, which closes N1. The
          rarer metadata events that land here (chown to the same owner, a link added and removed
          again) cost at most one bounded rebuild: the forced copies are capped
          (_FORCED_COPY_MAX_FILES/_BYTES) and the rest are relinked at the next fence."""
    kind, head, mtime_ns, size, ctime_ns, mode, uid, gid = ref
    if (st.st_mtime_ns, st.st_size) != (mtime_ns, size):
        return True
    if kind == "live" and st.st_ino != head:
        return True
    if st.st_ctime_ns == ctime_ns or (st.st_mode, st.st_uid, st.st_gid) != (mode, uid, gid):
        return False
    return kind == "live" or st.st_nlink == head


def _snapshot_corpus(pack: Path, files, staging: Path, *, link: bool,
                     copy: frozenset = frozenset()) -> tuple:
    """Capture the fenced files under the corpus fence — the ONLY per-file work done under it.

    Returns (staged, refs). `staged` is the set of paths now held in `staging`; everything else is
    read from the vault after the fence. `refs` maps every path that can still change after the
    fence to its fence-time identity, which _write_archive re-checks with _changed_since.

    * Most files are hardlinked (`link=True`): metadata only, seconds for 100k+ files, where a full
      copy of a live vault's 3 GB fenced set took minutes. A link keeps the fence-time bytes while
      writers publish by rename, as the enforced write path does.
    * A private copy is taken for files a writer may change in place: those written within
      file_fingerprint.SETTLE_NS of the fence (a same-tick, same-size rewrite has an identical
      stat, okengine#1017), known in-place writers (_written_in_place), and `copy` — the files
      found rewritten during an earlier attempt. Files modified within _HOT_NS are copied too,
      while _COPY_BUDGET_BYTES lasts, so the common case never needs a rebuild.
    * When hardlinks are unavailable (`link=False`: another filesystem, protected_hardlinks) or one
      link fails, the file is read from the vault after the fence and re-checked by inode as well.
      The fence never holds a copy of the whole corpus: forced copies stop at
      _FORCED_COPY_MAX_FILES/_BYTES and hot copies at _COPY_BUDGET_BYTES."""
    staged, refs = set(), {}
    made = set()
    now = time.time_ns()
    seen = []                                  # (rel, linked stat or None, source stat)
    for rel in files:
        src, dst = os.path.join(pack, rel), os.path.join(staging, rel)
        parent = os.path.dirname(dst)
        if parent not in made:
            os.makedirs(parent, exist_ok=True)
            made.add(parent)
        st = None
        if link:
            try:
                os.link(src, dst, follow_symlinks=False)
            except OSError:
                pass
            else:
                st = os.stat(dst)
        seen.append((rel, st, st if st is not None else os.stat(src)))
    forced = _allocate_forced_copies(seen, copy, now)
    budget = _COPY_BUDGET_BYTES
    for rel, st, source in seen:
        must_copy = rel in forced
        hot = not must_copy and source.st_mtime_ns >= now - _HOT_NS and source.st_size <= budget
        if must_copy or hot:
            dst = os.path.join(staging, rel)
            if st is not None:
                os.unlink(dst)
            shutil.copy2(os.path.join(pack, rel), dst, follow_symlinks=False)
            staged.add(rel)
            if hot:
                budget -= source.st_size
        elif st is not None:
            staged.add(rel)
            refs[rel] = _identity("link", st)
        else:
            refs[rel] = _identity("live", source)
    return staged, refs


def _allocate_forced_copies(seen, copy: frozenset, now: int) -> set:
    """The files copied under the fence within _FORCED_COPY_MAX_FILES/_BYTES, by priority: first
    the ones a writer may change in place right now (written within SETTLE_NS, known in-place
    writers), then `copy`, the files found rewritten on an earlier attempt. Walk order decided it
    before, so after a mass metadata event (`chown -R` to the same owner) the carried-over set
    took the whole cap and a continuously appended log.md was linked on every attempt
    (okengine#1041 item 1)."""
    first = [(rel, source) for rel, _st, source in seen
             if _written_in_place(rel) or source.st_mtime_ns >= now - SETTLE_NS]
    urgent = {rel for rel, _source in first}
    then = [(rel, source) for rel, _st, source in seen if rel in copy and rel not in urgent]
    forced = set()
    slots, size = _FORCED_COPY_MAX_FILES, _FORCED_COPY_MAX_BYTES
    for rel, source in first + then:
        if slots and source.st_size <= size:
            forced.add(rel)
            slots -= 1
            size -= source.st_size
    return forced


def _links_available(pack: Path, files, staging: Path) -> bool:
    """Probe whether the snapshot can hardlink here (same filesystem, protected_hardlinks)."""
    for rel in files[:1]:
        probe = staging / ".link-probe"
        try:
            os.link(pack / rel, probe, follow_symlinks=False)
        except OSError as exc:
            print(f"  WARN: cannot hardlink the corpus into {staging} ({exc}); unchanged files are "
                  "read from the vault after the fence and re-checked, files rewritten during the "
                  "archive are rebuilt (see docs/backup-restore.md)", file=sys.stderr)
            return False
        probe.unlink()
    return True


def _write_archive(partial: str, pack: Path, staging: Path, files, staged: set, refs: dict,
                   include_secrets: bool, manifest_fields: dict, final: bool) -> tuple:
    """Write the tar (staged files from `staging`, the rest from `pack`) and its manifest.
    Returns (manifest, rewritten): the fenced files that changed after the fence. On the `final`
    attempt the manifest names them, since the archive is then kept with their later bytes."""
    entries = {}
    rewritten = []
    with tarfile.open(partial, "w:gz") as tar:
        for rel in files:
            root = staging if rel in staged else pack
            try:
                data = _file_bytes(root, rel, include_secrets)
                st = (root / rel).stat()
            except FileNotFoundError:
                # Removed or moved after the walk. A fenced file read from the vault (no hardlink)
                # changed after the fence; a runtime file is best-effort and simply gone (N6).
                if rel in refs:
                    rewritten.append(rel.as_posix())
                continue
            if rel in refs and _changed_since(refs[rel], st):
                rewritten.append(rel.as_posix())
            entries[rel.as_posix()] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(rel.as_posix())
            info.size = len(data)
            info.mode = st.st_mode & 0o777
            info.mtime = int(st.st_mtime)
            tar.addfile(info, io.BytesIO(data))
        rollup = hashlib.sha256(
            "\n".join(f"{k} {v}" for k, v in sorted(entries.items())).encode()
        ).hexdigest()
        manifest = {"files": entries, "digest": rollup, "count": len(entries), **manifest_fields}
        manifest["consistency"]["rewritten_after_fence"] = rewritten if final else []
        data = (json.dumps(manifest, indent=2) + "\n").encode()
        info = tarfile.TarInfo(_MANIFEST)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return manifest, rewritten


_STAGING_LOCK = ".lock"


def _staging_parent_lock(staging_parent: Path, blocking: bool):
    """The parent-level lock that orders "create a staging dir and lock it" against a sweep, so a
    sweep can never see a just-created staging dir before its owner has locked it (N7). Returns the
    open descriptor, or None when `blocking` is False and another create() holds it."""
    fd = os.open(staging_parent / _STAGING_LOCK, os.O_RDONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _sweep_stale_staging(staging_parent: Path) -> None:
    """Remove staging dirs a killed create() left behind. Each live create() holds an exclusive
    flock on its own staging dir, so a dir whose lock can be taken has no owner. A stale dir pins
    old inodes (disk) and holds a copy of pack.yaml that a recursive pack discovery would find.
    Runs under the parent lock; if another create() is between creating and locking its dir right
    now, this sweep is skipped (the next one will run)."""
    parent = _staging_parent_lock(staging_parent, blocking=False)
    if parent is None:
        return
    try:
        for entry in staging_parent.iterdir():
            if not entry.name.startswith(".staging-") or entry.is_symlink() or not entry.is_dir():
                continue
            fd = os.open(entry, os.O_RDONLY)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue                 # a running create() owns it
                shutil.rmtree(entry, ignore_errors=True)
            finally:
                os.close(fd)
    finally:
        os.close(parent)


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


def _has_sqlite_header(src: Path) -> bool:
    """Whether `src` starts with the SQLite file header (a 0-byte file is not yet a database)."""
    with open(src, "rb") as stream:
        return stream.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER


def _file_bytes(pack: Path, rel: Path, include_secrets: bool) -> bytes:
    """The bytes to archive for `rel`. For a redact-path (config.yaml) in a no-secrets backup, the
    live MCP Bearer token is stripped so the archive honors "secrets excluded" without dropping the
    restorable runtime config. Manifest + tar both go through this, so the digest stays consistent."""
    src = pack / rel
    if _is_sqlite(rel) and _has_sqlite_header(src):
        # Online-backup snapshot for a consistent copy under concurrent writes (#33/#34). A real
        # database whose snapshot fails (ENOSPC in the temp dir, memory, a lock) fails the backup:
        # a raw read of the main file drops the committed rows still in the excluded -wal and would
        # be archived as `online-backup-per-database` (okengine#1042 [6]). Only a file with a SQLite
        # name that is not a database at all is archived as plain bytes.
        try:
            return _sqlite_snapshot_bytes(src)
        except Exception as exc:
            raise SQLiteSnapshotError(
                f"online backup of SQLite database {rel.as_posix()} failed "
                f"({type(exc).__name__}: {exc}); refusing to archive its raw main file without "
                "its WAL") from exc
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


def _portable_value(key: object, value: object) -> bool:
    """A value safe to write back as ONE .env line. str.isprintable() rejects every character
    str.splitlines() splits on (\\n \\r \\x0b \\x0c \\x1c-\\x1e \\x85 \\u2028 \\u2029) and NUL, so a
    value cannot smuggle a second assignment past a reader that re-splits the file."""
    return (key in _PORTABLE_ENV_KEYS and isinstance(value, str) and value.isprintable()
            and not _URL_USERINFO_RE.search(value))


def _deployment_env(pack: Path) -> tuple[dict[str, str], list[str]]:
    """(carried, not_carried): the non-secret `.env` settings a secrets-excluded backup carries
    (last value wins, as docker compose's env_file resolves a duplicate — the value the
    containers actually run with), and the NAMES of every other key
    set in `.env` so restore can tell the operator what to re-provision. Values are never recorded
    for keys that are not carried."""
    values: dict[str, str] = {}
    dropped: set[str] = set()
    env_path = pack / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _ENV_ASSIGN_RE.match(line)
            if not match:
                continue
            key, value = match.group(1), match.group(2)
            if _portable_value(key, value):
                values[key] = value
                dropped.discard(key)
            else:
                values.pop(key, None)
                dropped.add(key)
    # HERMES_UID/GID are not 'lost': restore rebinds them from the restored tree's owner.
    return values, sorted(dropped - set(values) - {"HERMES_UID", "HERMES_GID"})


def _dotenv_lib():
    """The ONE dotenv parser (scripts/cron/dotenv_lib.py), loaded by path: scripts/cron is not a
    package and is not on sys.path when the framework CLI runs."""
    spec = importlib.util.spec_from_file_location("okengine_dotenv_lib",
                                                  _HERE / "cron" / "dotenv_lib.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hermes_env_not_carried(pack: Path, include_secrets: bool) -> "list[str] | None":
    """The NAMES of the keys `.hermes-data/.env` sets, when a secrets-excluded backup leaves that
    file out as a whole (okengine#994 B). Hermes loads it over the container environment and
    `hermes setup` writes credentials there, so it is never carried; but it also holds non-secret
    Hermes settings (BROWSER_*, TERMINAL_*, *_DEBUG, pack switches) that a restore would otherwise
    drop without a word. Names only: a value is never written to the manifest. None when the file
    exists but cannot be read: which keys it held is then unknown, never "none"."""
    path = pack / ".hermes-data" / ".env"
    if include_secrets or not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sorted(_dotenv_lib().parse_dotenv(text))


def _manifest_deployment_env(manifest: dict) -> dict[str, str]:
    """The manifest's portable env block, filtered: MANIFEST.json is not covered by its own digest,
    so only allowlisted keys with single-line string values may reach the restored .env."""
    block = manifest.get("deployment_env")
    if not isinstance(block, dict):
        return {}
    return {key: value for key, value in block.items() if _portable_value(key, value)}


def _manifest_env_not_carried(manifest: dict, field: str = "deployment_env_not_carried") -> list[str]:
    names = manifest.get(field)
    if not isinstance(names, list):
        return []
    return sorted(n for n in names if isinstance(n, str) and _ENV_KEY_RE.fullmatch(n))


def _manifest_rewritten_after_fence(manifest: dict) -> list[str]:
    """The archive's own record of fenced files that kept changing after its fence (strings only:
    MANIFEST.json is not covered by its own digest)."""
    consistency = manifest.get("consistency")
    names = consistency.get("rewritten_after_fence") if isinstance(consistency, dict) else None
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _hermes_env_unknown(manifest: dict) -> str:
    """Why a restore cannot name the `.hermes-data/.env` keys it did not bring back, or '' when it
    can (or when the archive carried the file). An archive written before the record existed has
    no field at all, which means "unknown", never "there were none" (okengine#994 B)."""
    if manifest.get("include_secrets") is True:
        return ""
    if "hermes_env_not_carried" not in manifest:
        return "archive predates this record"
    if manifest["hermes_env_not_carried"] is None:
        return "it was unreadable at backup time"
    return ""


def _print_not_carried(label: str, why: str, names: list[str]) -> None:
    """Name (never value) the keys a restore did not bring back, capped for a huge file."""
    if not names:
        return
    shown = names[:_NOT_CARRIED_PRINT_LIMIT]
    more = len(names) - len(shown)
    print(f"  {label}: NOT carried ({why}): " + ", ".join(shown)
          + (f" … (+{more} more)" if more else ""))


def _root_restore_message(manifest: dict) -> str:
    return ("refusing to restore as root: extracted files would be root-owned and the gateway "
            "identity (HERMES_UID/HERMES_GID) would be pinned to 0. Run the restore as the "
            "deployment user (the archive recorded "
            f"{manifest.get('hermes_uid')}:{manifest.get('hermes_gid')}).")


def _restore_deployment_identity(target: Path, manifest: dict) -> None:
    """Bind a restore to the target host's extracted runtime owner.

    Numeric uid/gid values are host-local. Reusing the source host's values can make the gateway
    start as an identity that cannot write the files just extracted. The restored runtime directory
    is the target-host authority; a minimal archive without it falls back to the target directory.
    A root-owned runtime raises RestoreOwnershipError instead of pinning uid/gid 0. The manifest's
    portable (non-secret) `.env` settings are then added where the target .env does not set them.
    """
    owner = target / ".hermes-data"
    owner = owner if owner.exists() else target
    owner_stat = owner.stat()
    if owner_stat.st_uid == 0 or owner_stat.st_gid == 0:
        # Never pin root: deploy.sh accepts HERMES_UID=0 and the runtime-ownership check exempts
        # uid 0, so nothing downstream would catch it (okengine#991).
        raise RestoreOwnershipError(
            f"restored runtime {owner} is owned by root "
            f"({owner_stat.st_uid}:{owner_stat.st_gid}); " + _root_restore_message(manifest))
    replacements = {
        "HERMES_UID": f"HERMES_UID={owner_stat.st_uid}",
        "HERMES_GID": f"HERMES_GID={owner_stat.st_gid}",
    }
    env_path = target / ".env"
    if not env_path.exists():
        os.close(os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))   # 0600 from birth
        output = ["# Restored non-secret deployment identity and settings; add deployment secrets "
                  "below.", *replacements.values()]
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
    # Archived non-secret config fills only what the target .env does not already set.
    present = {m.group(1) for m in map(_ENV_ASSIGN_RE.match, output) if m}
    output.extend(f"{key}={value}" for key, value in _manifest_deployment_env(manifest).items()
                  if key not in present)
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    env_path.chmod(0o600)


# --- create ------------------------------------------------------------------

# What follows `{pack.name}` in an archive name: main() stamps every archive with _STAMP_FORMAT.
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_STAMP_SUFFIX = r"-\d{8}T\d{6}Z\.tar\.gz"


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
    #
    # SHORT FENCE (okengine#1032): the corpus fence is held only to list the corpus tree and
    # hardlink its files into a staging dir on the pack's own filesystem. Compression, hashing, the
    # SQLite online backups and the runtime files all happen after it is released. Holding it for
    # the whole archive blocked every write tool (LOCK_EX, 30 s timeout) for the length of the
    # backup — about 15 minutes for the fenced set alone on a live 134k-file vault — and turned the
    # gateway healthcheck red once lock-owner.json passed 300 s. If a fenced file was rewritten IN
    # PLACE after the fence (a hardlink shares the inode), the archive is rebuilt with the
    # cumulative set of such files copied under the fence. Files known or likely to be written in
    # place are copied on the first attempt, so a busy vault rarely rebuilds. The fence never holds a
    # copy of the whole corpus: after _SNAPSHOT_ATTEMPTS the archive is kept and the manifest names
    # any file that was still changing (consistency.rewritten_after_fence).
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / f"{pack.name}-{stamp}.tar.gz"
    staging_parent = pack / _STAGING_DIR
    staging_parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_staging(staging_parent)
    copy: frozenset = frozenset()
    link = None
    attempt = 0
    while True:
        attempt += 1
        final = attempt == _SNAPSHOT_ATTEMPTS
        fd, partial_name = tempfile.mkstemp(
            prefix=f".{archive.name}.", suffix=".partial", dir=dest_dir
        )
        os.close(fd)
        Path(partial_name).unlink()
        parent_lock = _staging_parent_lock(staging_parent, blocking=True)
        try:
            staging = Path(tempfile.mkdtemp(prefix=f".staging-{stamp}-", dir=staging_parent))
            owner = os.open(staging, os.O_RDONLY)
            fcntl.flock(owner, fcntl.LOCK_EX)      # marks it live for _sweep_stale_staging
        finally:
            os.close(parent_lock)
        try:
            with stable_corpus(pack, lock_timeout_seconds=lock_timeout_seconds) as corpus_epoch:
                corpus_files, corpus_dirs = _walk(pack, include_secrets, "corpus")
                fenced = [rel for rel in corpus_files if _corpus_fenced(rel)]
                if link is None:
                    link = _links_available(pack, fenced, staging)
                staged, refs = _snapshot_corpus(pack, fenced, staging, link=link, copy=copy)
            runtime_files, runtime_dirs = _walk(pack, include_secrets, "runtime")
            files = sorted(corpus_files + runtime_files, key=lambda r: r.as_posix())
            hermes_uid, hermes_gid = _deployment_identity(pack)
            env_carried, env_not_carried = _deployment_env(pack)
            manifest, rewritten = _write_archive(partial_name, pack, staging, files, staged, refs,
                                                 include_secrets, {
                "created": stamp, "pack": pack.name, "include_secrets": include_secrets,
                "corpus_epoch": corpus_epoch,
                "consistency": {
                    "canonical_markdown_schema_config": "corpus-fenced",
                    "corpus_snapshot": "hardlink" if link else "read-after-fence",
                    "copied_under_fence": len(staged) - sum(
                        1 for ref in refs.values() if ref[0] == "link"),
                    "sqlite": "online-backup-per-database",
                    "runtime_files": "best-effort-per-file",
                },
                "hermes_uid": hermes_uid, "hermes_gid": hermes_gid,
                "deployment_env": env_carried,
                "deployment_env_not_carried": env_not_carried,
                "hermes_env_not_carried": _hermes_env_not_carried(pack, include_secrets),
                "dir_modes": _dir_modes(pack, [*corpus_dirs, Path(_RUNTIME_DIR), *runtime_dirs]),
            }, final)
            if not rewritten or final:
                if rewritten:
                    print(f"  WARN: {len(rewritten)} corpus file(s) kept changing in place after "
                          f"the fence through {attempt} attempts (e.g. {rewritten[0]}); the archive "
                          "carries their later bytes and names them in MANIFEST.json "
                          "consistency.rewritten_after_fence", file=sys.stderr)
                os.replace(partial_name, archive)
                return archive, manifest
            print(f"  note: {len(rewritten)} corpus file(s) were rewritten in place after the "
                  f"backup's fence (e.g. {rewritten[0]}); rebuilding with those taken as copies "
                  "under the fence", file=sys.stderr)
            copy = copy | {Path(name) for name in rewritten}
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            os.close(owner)
            Path(partial_name).unlink(missing_ok=True)


# --- verify ------------------------------------------------------------------

def verify(archive: Path) -> tuple:
    """(ok, manifest, problems). Recompute every archived file's sha256 vs the manifest."""
    problems = []
    try:
        tar = tarfile.open(archive, "r:gz")
    except (OSError, TarError) as exc:
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
        # Fail CLOSED (okengine#991): a validator that crashed proved nothing about the restored
        # pack. `--no-validate` is the explicit operator override.
        return (False, f"framework validate crashed ({type(e).__name__}: {e}) — the restored pack "
                       "is unvalidated, so it is not accepted")


VALIDATOR = _validator   # overridable for tests


def _rearm_restored_schedule(target: Path, source: "Path | None" = None) -> None:
    """Force cron-plus to compute future fire times after a point-in-time restore.

    The historical run/result fields remain useful DR state, but an archived
    ``next_run_at`` is necessarily relative to the backup instant. Replaying it
    days later makes every overdue lane fire together on the first scheduler
    tick. A null value is cron-plus's supported recomputation signal.

    `source` is the archived store, staged beside jobs.json under another name; it is read,
    re-armed and published as jobs.json in one step under the store lock, keeping the staged
    file's mode. Without it, jobs.json itself is re-armed in place, keeping its mode and owner.
    """
    jobs_path = target / _JOBS_STORE
    source = jobs_path if source is None else source
    if not source.is_file():
        return
    # Under the scheduler's own store lock (okengine#850): a `restore --force` over a running
    # gateway must not race cron-plus's read-modify-write of the same file (okengine#1032).
    lock_fd = os.open(jobs_path.parent / _CRON_PLUS_LOCK_NAME, os.O_RDONLY | os.O_CREAT,
                      0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = json.loads(source.read_text(encoding="utf-8"))
        jobs = state.get("jobs") if isinstance(state, dict) and set(state) == {"jobs"} else state
        if not isinstance(jobs, list):
            raise ValueError(f"restored cron state is not a job list: {jobs_path}")
        for job in jobs:
            if isinstance(job, dict):
                job["next_run_at"] = None
        _replace_keeping_identity(jobs_path, json.dumps(state, indent=2) + "\n", source.stat())
    finally:
        os.close(lock_fd)       # closing the descriptor releases the flock


def _replace_keeping_identity(path: Path, text: str, original: os.stat_result) -> None:
    """Atomically replace `path` with the mode and owner of `original` (its own stat, or the staged
    archived store). jobs.json carries per-job env and prompts and every other writer keeps it
    0600; a plain write_text took the process umask (0644, world-readable) instead (okengine#1032)."""
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".restore-staged")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fchmod(stream.fileno(), original.st_mode & 0o777)
            if (original.st_uid, original.st_gid) != (os.geteuid(), os.getegid()):
                os.fchown(stream.fileno(), original.st_uid, original.st_gid)
            os.fsync(stream.fileno())
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def restore(archive: Path, target: Path, force: bool) -> tuple:
    """Verify the archive, then extract into target (must be empty unless force).

    Returns (ok, manifest, problems). When ok is False the problems are the integrity failures and
    nothing was extracted; when it is True they are (path, why) warnings about modes the restore
    could not reproduce, which the caller must surface (okengine#1044 [19]/[22])."""
    ok, manifest, problems = verify(archive)
    if not ok:
        return False, manifest, problems
    if os.geteuid() == 0:
        # The `data` filter never chowns, so a root restore yields a root-owned tree; refuse before
        # writing anything rather than leave a half-restored target (okengine#991).
        raise RestoreOwnershipError(_root_restore_message(manifest))
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()) and not force:
        raise FileExistsError(f"target not empty: {target} (use --force to overwrite)")
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name not in (_MANIFEST, _JOBS_STORE)]
        tar.extractall(target, members=members, filter="data")   # 'data' = path-traversal safe
        store = next((m for m in tar.getmembers() if m.name == _JOBS_STORE), None)
        if store is not None:
            _restore_job_store(tar, store, target)
    warnings = _restore_file_modes(target, members)
    warnings += _restore_dir_modes(target, manifest)
    # Last, and reached even when a mode could not be applied: a chmod error that escaped here
    # left .env bound to the source host's uid (okengine#1044 [22]).
    _restore_deployment_identity(target, manifest)
    return True, manifest, warnings


def _restore_job_store(tar, member, target: Path) -> None:
    """Publish the archived cron-plus store only re-armed and only under the scheduler's lock.

    Extracted in place, a running gateway's 60 s ticker could read it half-written (cron-plus
    then saves an empty store) or whole but not yet re-armed (every overdue lane fires at once)
    during a `restore --force` (okengine#1042 [7]). It is staged under another name, which the
    scheduler never reads, and published by _rearm_restored_schedule in one atomic replace."""
    jobs_path = target / _JOBS_STORE
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged = tempfile.mkstemp(dir=jobs_path.parent, prefix=f".{jobs_path.name}.",
                                  suffix=".restore-archived")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(tar.extractfile(member).read())
            # the archived mode, owner rw ensured; group/other write is kept, as for every other
            # restored file (_restore_file_modes, okengine#1044 [23])
            os.fchmod(stream.fileno(), (member.mode & 0o777) | 0o600)
        _rearm_restored_schedule(target, Path(staged))
    finally:
        os.unlink(staged)            # the archived copy; jobs.json was published from it


def _through_symlink(target: Path, parts) -> bool:
    """Whether target/parts, or any directory on the way to it, is a symlink."""
    path = target
    for part in parts:
        path = path / part
        if path.is_symlink():
            return True
    return False


def _restore_file_modes(target: Path, members) -> list:
    """Re-add the group/other write bits the `data` extraction filter strips from regular files.

    The filter extracts every file without group/other write, but dir_modes brings back a
    `--fix-perms` `.hermes-data` at 0777. The container uid then passed ensure-runtime's
    directory-only writability check and still could not write config.yaml or the state files
    (okengine#1044 [23]). Only those two bits are re-added, so the filter's other rules (no
    setuid/setgid/sticky, owner rw) stand. A path through a symlink is skipped. Returns (path, why)
    warnings for files whose mode could not be applied."""
    warnings = []
    for member in members:
        extra = member.mode & 0o022
        if not member.isreg() or not extra:
            continue
        parts = PurePosixPath(member.name).parts
        if _through_symlink(target, parts):
            continue
        path = target.joinpath(*parts)
        try:
            os.chmod(path, (path.stat().st_mode & 0o777) | extra)
        except OSError as exc:
            warnings.append((member.name, f"file mode {member.mode & 0o777:#o} not restored: "
                                          f"{exc.strerror or exc}"))
    return warnings


def _restore_dir_modes(target: Path, manifest: dict) -> list:
    """Recreate the archived directories (empty ones too) and re-apply their recorded modes.

    The archive holds only files, so extraction made every directory with the umask: a private
    `.hermes-data` (0700) came back 0775 and empty runtime dirs were dropped (okengine#1042 [29]).
    The manifest is data: a key that is not a plain relative path, or that would pass through a
    symlink in the target, is ignored. Modes are applied deepest first, so a read-only parent never
    blocks its children; setuid/setgid/sticky bits are never applied.

    Returns (path, why) warnings: an archive with no `dir_modes` (written before v0.15.13), and a
    directory that could not be recreated or whose mode could not be applied (a foreign-owned
    directory refuses chmod). A failure never stops the rest (okengine#1044 [19]/[22])."""
    recorded = manifest.get("dir_modes")
    if not isinstance(recorded, dict):
        return [("dir_modes", "the archive records no directory modes (written before v0.15.13): "
                              "directory modes were not restored and empty directories were not "
                              "recreated; directories carry the restoring user's umask")]
    wanted = []
    for rel, mode in recorded.items():
        path = PurePosixPath(rel) if isinstance(rel, str) and "\x00" not in rel else None
        if (path is not None and path.parts and not path.is_absolute() and ".." not in path.parts
                and isinstance(mode, int) and not isinstance(mode, bool)):
            wanted.append((path.parts, mode & 0o777))
    wanted.sort(key=lambda item: len(item[0]))
    usable = []
    warnings = []
    for parts, mode in wanted:
        if _through_symlink(target, parts):
            continue
        path = target.joinpath(*parts)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:        # a file sits there, or the parent is unwritable
            warnings.append(("/".join(parts), f"directory not recreated: {exc.strerror or exc}"))
            continue
        usable.append((parts, path, mode))
    for parts, path, mode in reversed(usable):
        try:
            os.chmod(path, mode)
        except OSError as exc:
            warnings.append(("/".join(parts),
                             f"directory mode {mode:#o} not restored: {exc.strerror or exc}"))
    return warnings


# --- list / prune ------------------------------------------------------------

def list_backups(dest_dir: Path, pack_name: "str | None" = None) -> list:
    """Backups in dest_dir, oldest→newest by MTIME. `pack_name` restricts to `{pack_name}-<stamp>.tar.gz`
    — REQUIRED for prune when a dest is SHARED across packs (the documented `--dest /backups`
    pattern): archives are named `{pack.name}-{stamp}`, so a name sort is dominated by the pack
    prefix and would prune across pack boundaries, silently destroying another pack's DR history
    (invariant-audit HIGH #3). mtime sort also survives a same-second re-created archive."""
    if not dest_dir.is_dir():
        return []
    if pack_name:
        # The exact `{pack}-{stamp}` name create() writes. A `{pack}-*` glob also matched a sibling
        # pack whose name extends this one (`okcti` vs `okcti-test`), so prune kept the newest N
        # across both and deleted the sibling's DR history (okengine#1042 [0]).
        own = re.compile(re.escape(pack_name) + _STAMP_SUFFIX)
        found = (f for f in dest_dir.iterdir() if own.fullmatch(f.name))
    else:
        found = dest_dir.glob("*.tar.gz")  # glob-ok: flat backups dir, not a sharded namespace
    return sorted((f for f in found if f.is_file()), key=lambda f: f.stat().st_mtime)


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
        stamp = datetime.now(timezone.utc).strftime(_STAMP_FORMAT)
        try:
            archive, manifest = create(pack, dest, a.include_secrets, stamp)
        except CorpusLockTimeout as exc:
            print(f"ERROR: backup could not acquire a stable corpus state: {exc}", file=sys.stderr)
            return 1
        except SQLiteSnapshotError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
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
        except (FileExistsError, RestoreOwnershipError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        if not ok:
            print(f"✗ refusing to restore — archive failed integrity ({len(problems)} problem(s)):",
                  file=sys.stderr)
            for name, why in problems[:10]:
                print(f"    {why}: {name}", file=sys.stderr)
            return 1
        print(f"restored {manifest['count']} files → {a.target}  (integrity ✓)")
        for name, why in problems:    # modes the restore could not reproduce (okengine#1044)
            print(f"  ⚠ {why}: {name}")
        rewritten = _manifest_rewritten_after_fence(manifest)
        if rewritten:
            shown = rewritten[:_NOT_CARRIED_PRINT_LIMIT]
            more = len(rewritten) - len(shown)
            print(f"  ⚠ {len(rewritten)} corpus file(s) were rewritten in place while this backup was "
                  f"taken (consistency.rewritten_after_fence); they carry bytes newer than corpus "
                  f"epoch {manifest.get('corpus_epoch')}: "
                  + ", ".join(shown) + (f" … (+{more} more)" if more else ""))
        carried = sorted(_manifest_deployment_env(manifest))
        print("  .env: restored non-secret settings "
              + (", ".join(carried) if carried else "(none recorded)")
              + "; add deployment secrets before deploying")
        _print_not_carried(".env", "secret, host-local, unclassified or credential-bearing; "
                           "re-provision if needed", _manifest_env_not_carried(manifest))
        _print_not_carried(".hermes-data/.env", "excluded whole: Hermes credentials live there; "
                           "re-provision these keys if needed",
                           _manifest_env_not_carried(manifest, "hermes_env_not_carried"))
        unknown = _hermes_env_unknown(manifest)
        if unknown:
            print(f"  .hermes-data/.env: NOT carried, and which keys it held is unknown ({unknown}); "
                  "re-provision it from the source deployment")
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
