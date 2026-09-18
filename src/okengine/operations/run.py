#!/usr/bin/env python3
"""Engine-owned operation run lifecycle (okengine#402, the #325 keystone).

The ENGINE — not the operation entrypoint — allocates the run id, acquires declared locks, computes
the input snapshot digest, and owns the authoritative receipt: its state machine, output validation,
and terminal immutability. The entrypoint is a worker — it receives ``OKENGINE_OPERATION_RUN_ID`` +
``--target-vault``, does its work, may emit progress, and prints a final result line; it must NOT
write the run receipt. Centralizing this makes AC #6 (locks prevent conflicting runs, recover from
stale owners) and AC #7 (a partial/child failure cannot report a successful complete) enforceable in
one place, and gives the CLI, the runner API, and scheduled lanes the same run id + receipt.

Run state (receipt JSON, events, lock files) lives under ``.okengine/operations/`` — ownership-
governed (deployment_validate.check_ownership) and excluded from ``framework backup``. Domain results
an operation produces are its declared ``outputs:`` under the git-tracked wiki (validated here before
a run may report ``succeeded``).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_TERMINAL = {"succeeded", "degraded", "failed", "canceled"}
_SECRET_HINT = re.compile(r"token|secret|password|api[_-]?key|bearer|credential", re.I)


class OperationRunError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _runs_dir(deployment: Path) -> Path:
    return deployment / ".okengine" / "operations" / "runs"


def _locks_dir(deployment: Path) -> Path:
    return deployment / ".okengine" / "operations" / "locks"


def new_run_id(name: str, *, stamp: str | None = None, token: str | None = None) -> str:
    """Engine-allocated run id: ``<name>-<utcstamp>-<rand>``. The operation never chooses its own id."""
    stamp = stamp or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{name}-{stamp}-{token or secrets.token_hex(3)}"


def receipt_path(deployment: Path, operation: str, run_id: str) -> Path:
    return _runs_dir(deployment) / operation / f"{run_id}.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def snapshot_digest(deployment: Path, inputs: list[str]) -> str:
    """A stable digest of the declared input globs (relative path + size + mtime-ns), computed by the
    ENGINE so a run cannot claim a plan digest it did not derive (principle #8)."""
    h = hashlib.sha256()
    for pattern in sorted(inputs or []):
        for p in sorted(deployment.glob(pattern)):  # glob-ok: the pattern is a manifest-DECLARED input glob (author-supplied), not a flat namespace scan
            if not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p.relative_to(deployment).as_posix()}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:32]


def _redact(arguments: list[str]) -> list[str]:
    """Redact secret-looking argument VALUES for the receipt (the flag name stays)."""
    out: list[str] = []
    redact_next = False
    for arg in arguments:
        if redact_next:
            out.append("***")
            redact_next = False
            continue
        if arg.startswith("--") and "=" in arg and _SECRET_HINT.search(arg.split("=", 1)[0]):
            out.append(arg.split("=", 1)[0] + "=***")
        elif arg.startswith("--") and _SECRET_HINT.search(arg):
            out.append(arg)
            redact_next = True
        else:
            out.append(arg)
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LockSet:
    """A set of held operation locks. The sync CLI releases via the `acquire_locks` context manager;
    the async runner service holds the LockSet for the life of the spawned run and calls `release()`
    from its reaper — the flock stays owned by the long-lived service, not the worker."""
    def __init__(self, held: list[tuple[int, Path]]):
        self._held = held

    def release(self) -> None:
        for fd, path in self._held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass
            try:
                path.unlink()
            except OSError:
                pass
        self._held = []


def acquire_lockset(deployment: Path, locks: list[str], run_id: str) -> LockSet:
    """flock each declared resource id under .okengine/operations/locks/ and return the held set. A
    conflicting run whose holder pid is still alive is refused (OperationRunError); a STALE lock
    (holder dead) is recovered and taken over. Caller MUST `.release()` the returned LockSet."""
    lock_dir = _locks_dir(deployment)
    lock_dir.mkdir(parents=True, exist_ok=True)
    held: list[tuple[int, Path]] = []
    try:
        for lock in sorted(set(locks or [])):
            path = lock_dir / (lock.replace("/", "__") + ".lock")
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                try:
                    holder = json.loads(os.pread(fd, 4096, 0).decode() or "{}")
                except (ValueError, UnicodeDecodeError, OSError):
                    holder = {}
                pid = holder.get("pid")
                if pid and _pid_alive(int(pid)):
                    os.close(fd)
                    raise OperationRunError(
                        f"operation lock '{lock}' is held by run {holder.get('run_id')} "
                        f"(pid {pid}); a conflicting operation is already running")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # stale holder gone — recover the lock
            os.ftruncate(fd, 0)
            os.pwrite(fd, json.dumps(
                {"run_id": run_id, "pid": os.getpid(), "resource": lock, "at": _now()}).encode(), 0)
            held.append((fd, path))
    except Exception:
        LockSet(held).release()
        raise
    return LockSet(held)


@contextmanager
def acquire_locks(deployment: Path, locks: list[str], run_id: str) -> Iterator[None]:
    """Context-manager form for synchronous callers (the CLI)."""
    lockset = acquire_lockset(deployment, locks, run_id)
    try:
        yield
    finally:
        lockset.release()


def _missing_outputs(deployment: Path, outputs: list[str]) -> list[str]:
    """Declared output globs that produced nothing — an operation claiming success while its declared
    outputs are absent is a partial success, not a complete one (AC #7)."""
    # glob-ok: each pattern is a manifest-DECLARED output glob (author-supplied), not a flat namespace scan
    return [pattern for pattern in (outputs or []) if not any(deployment.glob(pattern))]


def plan(deployment: Path, manifest: dict[str, Any], arguments: list[str], *,
         source: str = "cli") -> tuple[int, dict[str, Any]]:
    """Run the entrypoint's non-mutating plan, returning (exit_code, plan) with the ENGINE-computed
    snapshot digest — the runner service revalidates that digest before a run, so the digest must be
    the engine's, not the pack's self-reported one."""
    from okengine.operations.framework import operation_command, result_from_output
    command, env = operation_command(deployment, manifest, arguments, plan=True, source=source)
    completed = subprocess.run(command, cwd=deployment, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n")
    result = result_from_output(completed.stdout) or {}
    result.update(operation=manifest["name"], status=result.get("status") or "planned",
                  snapshot_digest=snapshot_digest(deployment, manifest.get("inputs") or []))
    return completed.returncode, result


def initial_receipt(deployment: Path, manifest: dict[str, Any], arguments: list[str], run_id: str, *,
                    source: str, pid: int | None = None) -> dict[str, Any]:
    """Write the `running` receipt the engine owns before the worker starts. Returns the receipt dict
    (mutated in place by `finalize`). `pid` lets the async service record the worker for reaping."""
    receipt: dict[str, Any] = {
        "run_id": run_id, "operation": manifest["name"], "operation_owner": manifest.get("owner"),
        "source": source, "deployment": str(deployment), "pid": pid,
        "arguments": _redact(list(arguments)),
        "snapshot_digest": snapshot_digest(deployment, manifest.get("inputs") or []),
        "locks": sorted(set(manifest.get("locks") or [])),
        "requested_at": _now(), "started_at": _now(), "finished_at": None, "status": "running",
    }
    _atomic_json(receipt_path(deployment, manifest["name"], run_id), receipt)
    return receipt


def finalize(deployment: Path, manifest: dict[str, Any], run_id: str, receipt: dict[str, Any], *,
             returncode: int, result: dict[str, Any] | None) -> dict[str, Any]:
    """Write the engine-owned TERMINAL receipt. The worker's own status can only DOWNGRADE, never
    upgrade, and a `succeeded` claim with an absent declared output is recorded `degraded` (AC #7)."""
    result = result or {}
    worker_status = str(result.get("status") or "")
    missing = _missing_outputs(deployment, manifest.get("outputs") or [])
    if returncode != 0 or worker_status in {"failed", "degraded"}:
        status = worker_status if worker_status in {"failed", "degraded"} else "failed"
    elif missing:
        status = "degraded"
    else:
        status = "succeeded"
    receipt.update(
        status=status, finished_at=_now(),
        result={key: value for key, value in result.items()
                if key not in {"status", "run_id", "operation"}},
        output_validation={"declared": manifest.get("outputs") or [], "missing": missing})
    _atomic_json(receipt_path(deployment, manifest["name"], run_id), receipt)
    return receipt


def run(deployment: Path, manifest: dict[str, Any], arguments: list[str], *,
        source: str = "cli", dry_run: bool = False,
        run_id: str | None = None) -> tuple[int, dict[str, Any]]:
    """Synchronous plan-or-run for the CLI. Composes the same building blocks the async runner
    service uses (`plan`/`initial_receipt`/`acquire_locks`/`finalize`)."""
    from okengine.operations.framework import operation_command, result_from_output
    if dry_run:
        return plan(deployment, manifest, arguments, source=source)

    name = manifest["name"]
    run_id = run_id or new_run_id(name)
    command, env = operation_command(deployment, manifest, arguments, source=source)
    env["OKENGINE_OPERATION_RUN_ID"] = run_id
    receipt = initial_receipt(deployment, manifest, arguments, run_id, source=source, pid=os.getpid())
    try:
        from okengine.corpus_transaction import mutation

        with acquire_locks(deployment, manifest.get("locks") or [], run_id):
            # The corpus-wide fence makes the worker's multi-file publication atomic to readers;
            # declared resource locks remain the finer-grained operation concurrency contract.
            with mutation(deployment, writer=f"operation:{name}", operation=run_id):
                completed = subprocess.run(command, cwd=deployment, env=env, text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           check=False)
    except OperationRunError as exc:
        receipt.update(status="failed", finished_at=_now(), error=str(exc))
        _atomic_json(receipt_path(deployment, name, run_id), receipt)
        return 1, receipt
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n")
    receipt = finalize(deployment, manifest, run_id, receipt, returncode=completed.returncode,
                       result=result_from_output(completed.stdout))
    return (0 if receipt["status"] in {"succeeded", "degraded"} else 1), receipt
