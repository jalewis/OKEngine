#!/usr/bin/env python3
"""Corpus-wide fencing, epochs, and secret-free mutation receipts.

Markdown remains canonical.  This module adds a small consistency protocol around it: writers hold
one advisory fence, readers hold its shared side, and every completed mutation advances a durable
epoch and appends a hash-only journal record.  An active marker lets the next participant finish
the receipt after a writer is killed between publishing files and advancing the epoch.

Commit ordering is prepared marker (atomic rename + file/directory fsync), epoch (same), journal
append (file fsync), active-marker unlink (directory fsync), then prepared-marker unlink (directory
fsync). Recovery replays that prepared identity and de-duplicates by transaction_id. This provides
idempotence after a process crash and orders metadata for local-filesystem power-loss recovery;
it cannot strengthen durability guarantees of the filesystem or storage device beneath fsync.
Legacy active markers without a prepared record remain recoverable through the same preparation
path, so the on-disk protocol is backward compatible.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Iterator


DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
LOCK_TIMEOUT_ENV = "OKENGINE_CORPUS_LOCK_TIMEOUT_SECONDS"

# Tracking modes for `mutation()` (okengine#666). "snapshot" diffs a full before/after digest of
# every wiki page: right for batch writers that may touch anything (operations, reconciliation),
# and O(vault) twice per transaction. "touched" journals only the paths the writer declared with
# `touch()` before mutating them: O(files touched). The per-tool MCP fence uses "touched" — the
# snapshot mode there cost 1.3 s per no-op call at 20k pages and 3.5 s at 60k, serialized
# fleet-wide under the exclusive lock, on every rejected call too.
TRACKING_MODES = ("snapshot", "touched")
_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("okengine_corpus_active", default=None)


class CorpusLockTimeout(TimeoutError):
    """Raised when the corpus fence cannot be acquired within its deadline."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _state(deployment: Path) -> Path:
    return deployment / ".okengine" / "corpus"


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def snapshot(deployment: Path) -> dict[str, str]:
    """Return content identities only; neither the marker nor journal contains corpus text."""
    wiki = deployment / "wiki"
    if not wiki.is_dir():
        return {}
    result: dict[str, str] = {}
    for path in sorted(wiki.rglob("*.md")):
        if path.is_file():
            try:
                result[path.relative_to(deployment).as_posix()] = _digest(path)
            except OSError:
                continue
    return result


def _digest_or_none(path: Path) -> str | None:
    try:
        return _digest(path) if path.is_file() else None
    except OSError:
        return None


def touch(path: Path | str) -> None:
    """Declare that `path` is about to be mutated by the current touched-mode transaction.

    Records the page's before-digest once and persists it into the active marker so a killed
    writer's recovery replays exactly this set. A no-op outside a touched-mode transaction (batch
    writers keep the full snapshot) and for anything that is not a wiki Markdown page — the
    journal has always been about canonical Markdown only.
    """
    ctx = _ACTIVE.get()
    if ctx is None:
        return
    candidate = Path(path)
    try:
        rel = candidate.resolve().relative_to(ctx["deployment"]).as_posix()
    except ValueError:
        return
    if not (rel.startswith("wiki/") and rel.endswith(".md")) or rel in ctx["touched"]:
        return
    ctx["touched"][rel] = _digest_or_none(candidate)
    ctx["active"]["touched"] = ctx["touched"]
    _atomic_json(ctx["state"] / "active.json", ctx["active"])


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lock_timeout(value: float | None) -> float:
    if value is None:
        raw = os.environ.get(LOCK_TIMEOUT_ENV, str(DEFAULT_LOCK_TIMEOUT_SECONDS))
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{LOCK_TIMEOUT_ENV} must be a positive number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"corpus lock timeout must be positive, got {value}")
    return value


def _owner_detail(state: Path) -> str:
    owner_path = state / "lock-owner.json"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "owner metadata unavailable"
    fields = ("pid", "hostname", "command", "writer", "operation", "mode", "acquired_at")
    rendered = ", ".join(f"{key}={owner[key]!r}" for key in fields if key in owner)
    if rendered and isinstance(owner.get("acquired_at"), str):
        try:
            acquired = dt.datetime.fromisoformat(owner["acquired_at"])
            age = max(0, int((dt.datetime.now(dt.timezone.utc) - acquired).total_seconds()))
            rendered += f", age_seconds={age}"
        except ValueError:
            rendered += ", age_seconds=unknown"
    return rendered or "owner metadata unavailable"


def _acquire(lock, state: Path, mode: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock.fileno(), mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = _owner_detail(state)
                raise CorpusLockTimeout(
                    f"timed out after {timeout:g}s acquiring corpus lock {state / 'lock'}; {detail}. "
                    "Inspect the holder and restart its unhealthy service if stale; do not delete "
                    "the lock file because removing it cannot release a kernel lock"
                ) from None
            time.sleep(min(0.05, remaining))


def _record_lock_owner(state: Path, *, writer: str, operation: str, mode: str) -> str:
    token = secrets.token_hex(16)
    _atomic_json(state / "lock-owner.json", {
        "token": token,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "command": Path(sys.argv[0]).name,
        "writer": writer,
        "operation": operation,
        "mode": mode,
        "acquired_at": _now(),
    })
    return token


def _clear_lock_owner(state: Path, token: str) -> None:
    owner_path = state / "lock-owner.json"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        if owner.get("token") == token:
            owner_path.unlink()
    except (OSError, ValueError, TypeError):
        return


def read_epoch(deployment: Path) -> int:
    try:
        return int((_state(deployment) / "epoch").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def _changed(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str | None]]:
    return [
        {"path": path, "before_sha256": before.get(path), "after_sha256": after.get(path)}
        for path in sorted(before.keys() | after.keys()) if before.get(path) != after.get(path)
    ]


def _append(state: Path, record: dict) -> None:
    journal = state / "journal.jsonl"
    # A process can die during append. Discard only an unterminated tail before retrying; every
    # fsynced newline-delimited record remains immutable and discoverable by transaction id.
    with journal.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if size:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.seek(0)
                content = stream.read()
                newline = content.rfind(b"\n")
                stream.truncate(newline + 1 if newline >= 0 else 0)
                stream.seek(0, os.SEEK_END)
        stream.write(
            (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )
        stream.flush()
        os.fsync(stream.fileno())


def _journal_transaction(state: Path, transaction_id: str) -> dict | None:
    try:
        lines = (state / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and record.get("transaction_id") == transaction_id:
            return record
    return None


def _unlink_durable(path: Path) -> None:
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _finalize_prepared(state: Path, active_path: Path, prepared_path: Path,
                       prepared: dict) -> int:
    record = prepared.get("record")
    if not isinstance(record, dict) or not isinstance(record.get("epoch"), int):
        raise ValueError(f"invalid prepared corpus commit: {prepared_path}")
    planned_epoch = record["epoch"]
    observed_epoch = read_epoch(state.parent.parent)
    if observed_epoch > planned_epoch:
        raise RuntimeError(
            f"corpus epoch {observed_epoch} advanced beyond prepared epoch {planned_epoch}"
        )
    if observed_epoch < planned_epoch:
        _atomic_json(state / "epoch", planned_epoch)
    if _journal_transaction(state, str(record.get("transaction_id"))) is None:
        _append(state, record)
    _unlink_durable(active_path)
    _unlink_durable(prepared_path)
    return planned_epoch


def _finish_active(deployment: Path, *, recovered: bool = False, failed: bool = False) -> int:
    state = _state(deployment)
    active_path = state / "active.json"
    prepared_path = state / "commit.json"
    if not active_path.exists():
        # Crash after active-marker removal but before prepared-marker cleanup. The journal and
        # epoch are already durable; remove the leftover only after confirming that identity.
        if prepared_path.exists():
            prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
            record = prepared.get("record") if isinstance(prepared, dict) else None
            if isinstance(record, dict) and _journal_transaction(
                state, str(record.get("transaction_id"))
            ) is not None:
                _unlink_durable(prepared_path)
        return read_epoch(deployment)
    active = json.loads(active_path.read_text(encoding="utf-8"))
    if prepared_path.exists():
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        record = prepared.get("record") if isinstance(prepared, dict) else None
        if not isinstance(record, dict) or record.get("transaction_id") != active.get(
            "transaction_id"
        ):
            raise RuntimeError("prepared corpus commit does not match the active transaction")
        return _finalize_prepared(state, active_path, prepared_path, prepared)
    if active.get("tracking") == "touched":
        touched = active.get("touched") or {}
        after = {rel: _digest_or_none(deployment / rel) for rel in touched}
        changes = _changed(touched, after)
    else:
        changes = _changed(active.get("before", {}), snapshot(deployment))
    epoch = read_epoch(deployment)
    if changes:
        epoch += 1
    record = {
        "transaction_id": active["transaction_id"], "writer": active["writer"],
        "operation": active["operation"], "started_at": active["started_at"],
        "finished_at": _now(), "epoch": epoch, "affected_paths": changes,
        "status": "recovered" if recovered else ("failed" if failed else "committed"),
    }
    prepared = {"version": 1, "record": record}
    _atomic_json(prepared_path, prepared)
    return _finalize_prepared(state, active_path, prepared_path, prepared)


@contextlib.contextmanager
def mutation(
    deployment: Path, *, writer: str, operation: str, lock_timeout_seconds: float | None = None,
    tracking: str = "snapshot",
) -> Iterator[str]:
    """Fence an in-process or subprocess writer and journal canonical Markdown changes.

    `tracking="snapshot"` (default) diffs the whole wiki; `tracking="touched"` journals only the
    paths the writer declares with `touch()` — see TRACKING_MODES.
    """
    if tracking not in TRACKING_MODES:
        raise ValueError(f"tracking must be one of {TRACKING_MODES}, got {tracking!r}")
    deployment = Path(deployment).resolve()
    state = _state(deployment)
    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / "lock"
    with lock_path.open("a+b") as lock:
        _acquire(lock, state, fcntl.LOCK_EX, timeout=_lock_timeout(lock_timeout_seconds))
        owner_token = _record_lock_owner(
            state, writer=str(writer), operation=str(operation), mode="exclusive",
        )
        _finish_active(deployment, recovered=True)
        transaction_id = secrets.token_hex(16)
        active = {
            "transaction_id": transaction_id, "writer": str(writer),
            "operation": str(operation), "started_at": _now(), "tracking": tracking,
        }
        if tracking == "snapshot":
            active["before"] = snapshot(deployment)
        else:
            active["touched"] = {}
        _atomic_json(state / "active.json", active)
        ctx_token = None
        if tracking == "touched":
            ctx_token = _ACTIVE.set({
                "deployment": deployment, "state": state, "active": active,
                "touched": active["touched"],
            })
        try:
            yield transaction_id
        except BaseException:
            _finish_active(deployment, failed=True)
            raise
        else:
            _finish_active(deployment)
        finally:
            if ctx_token is not None:
                _ACTIVE.reset(ctx_token)
            _clear_lock_owner(state, owner_token)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

@contextlib.contextmanager
def stable_corpus(deployment: Path, *, lock_timeout_seconds: float | None = None) -> Iterator[int]:
    """Hold a stable corpus epoch for an audit/read traversal.

    Recovery takes the exclusive fence first so a killed writer's partial publication is never
    mistaken for a stable old epoch.  Callers may retry at a higher level if they cannot hold this
    context for the complete traversal.
    """
    deployment = Path(deployment).resolve()
    state = _state(deployment)
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a+b") as lock:
        timeout = _lock_timeout(lock_timeout_seconds)
        _acquire(lock, state, fcntl.LOCK_EX, timeout=timeout)
        owner_token = _record_lock_owner(
            state, writer="stable_corpus", operation="audit/read traversal", mode="exclusive",
        )
        _finish_active(deployment, recovered=True)
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            yield read_epoch(deployment)
        finally:
            _clear_lock_owner(state, owner_token)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
