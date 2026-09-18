#!/usr/bin/env python3
"""Governed asynchronous API for declarative OKEngine operations."""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request


from okengine.compat import engine_root
from okengine.operations import framework as operations
from okengine.operations import run as operation_run
from okengine import corpus_transaction

ENGINE_ROOT = engine_root()


DEPLOYMENT = Path(os.environ.get("WIKI_PATH") or "/opt/vault").resolve()
TOKEN = os.environ.get("OKENGINE_OPERATION_TOKEN", "")
ALLOWED = {value.strip() for value in
           os.environ.get("OKENGINE_OPERATION_ALLOW", "").split(",") if value.strip()}
REQUESTS = DEPLOYMENT / ".okengine/operations/requests"
_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
TERMINAL = {"succeeded", "degraded", "failed", "canceled", "planned"}
_PROCESSES: dict[str, subprocess.Popen] = {}


def abort_start(process: subprocess.Popen, lockset: "operation_run.LockSet", corpus_context=None) -> None:
    """Undo a partially initialized asynchronous run without leaving a worker or flock behind."""
    try:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
                process.wait(timeout=5)
    finally:
        if corpus_context is not None:
            corpus_context.__exit__(None, None, None)
        lockset.release()


class BearerAuth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") != "/healthz":
            provided = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            expected = f"Bearer {TOKEN}"
            if not TOKEN or not hmac.compare_digest(provided, expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"detail":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def _lifespan(_app):
    """Restore request truth before the service accepts traffic."""
    reconcile_requests()
    yield


app = FastAPI(title="OKEngine operation runner", docs_url=None, redoc_url=None,
              lifespan=_lifespan)
app.add_middleware(BearerAuth)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(raw, path)
    finally:
        try:
            os.unlink(raw)
        except FileNotFoundError:
            pass


def manifest(name: str) -> dict[str, Any]:
    if name not in ALLOWED:
        raise HTTPException(403, "operation is not enabled for API execution")
    try:
        return operations._operation(DEPLOYMENT, name)
    except operations.OperationsError as exc:
        raise HTTPException(404, str(exc)) from exc


def arguments(data: Any) -> list[str]:
    raw = data.get("arguments", []) if isinstance(data, dict) else []
    if not isinstance(raw, list) or len(raw) > 40 or not all(
            isinstance(value, str) and len(value) <= 500 for value in raw):
        raise HTTPException(400, "arguments must be a bounded string list")
    return raw


def request_path(request_id: str) -> Path:
    if not _REQUEST_ID.fullmatch(request_id):
        raise HTTPException(400, "invalid request id")
    return REQUESTS / f"{request_id}.json"


def load_request(request_id: str) -> tuple[Path, dict[str, Any]]:
    path = request_path(request_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(404, "operation request not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "operation request is unreadable") from exc
    return path, value


def pid_alive(pid: int) -> bool:
    # `refresh` reaches here as pid_alive(int(value.get("pid") or 0)), so a request record with a
    # missing, null or zero pid arrives as 0 — and os.kill(0, 0) does NOT raise. It signals the
    # caller's whole process group, succeeds, and reports the absent worker as alive, so the
    # crash-safety branch below can never fire and the request hangs in `running` for good.
    # A negative pid is worse in kind than in effect: os.kill(-1, 0) probes every process this
    # user may signal. Signal 0 delivers nothing, so nothing is harmed, but a value read out of a
    # JSON file on disk should not be able to widen a single-pid liveness check into a fleet-wide
    # one. Neither value can name a worker, so neither is alive.
    if pid <= 0:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if len(stat) > 2 and stat[2] == "Z":
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, OverflowError):
        return False


def reap(request_id: str, process: subprocess.Popen, item: dict[str, Any], run_id: str,
         receipt: dict[str, Any], lockset: "operation_run.LockSet", stdout: Path,
         corpus_context=None) -> None:
    process.wait()
    try:
        if corpus_context is not None:
            corpus_context.__exit__(None, None, None)
        result = operations.result_from_output(
            stdout.read_text(encoding="utf-8", errors="replace") if stdout.is_file() else "")
        operation_run.finalize(DEPLOYMENT, item, run_id, receipt,
                               returncode=process.returncode, result=result)   # engine-owned terminal
        # Persist the terminal projection while this request is still registered
        # as reaper-owned. Removing it from _PROCESSES first leaves refresh() to
        # infer a service restart from a dead pid and can overwrite the handoff
        # as failed under load, even though finalize wrote a successful receipt.
        try:
            request_file, request_value = load_request(request_id)
            refresh(request_file, request_value)
        except HTTPException:
            pass  # Request deletion/corruption is reported by the API read path.
    finally:
        lockset.release()
        _PROCESSES.pop(request_id, None)


def refresh(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    """Project the request record from the ENGINE-owned receipt (the reaper's finalize is authoritative)."""
    run_id, op = value.get("run_id"), value.get("operation")
    rpath = operation_run.receipt_path(DEPLOYMENT, op, run_id) if run_id and op else None
    if rpath and rpath.is_file():
        try:
            receipt = json.loads(rpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = {}
        if receipt:
            # `finalize` nests the worker's own result under receipt["result"]; the domain progress
            # counters live there (the receipt top level is the engine-owned run envelope).
            worker = receipt.get("result") or {}
            value["status"] = receipt.get("status") or value.get("status")
            value["receipt"] = str(rpath.relative_to(DEPLOYMENT))
            value["progress"] = {
                "actors": len(worker.get("actor_inventory") or []),
                "lanes_complete": sum(1 for row in worker.get("lanes") or []
                                      if row.get("status") in {"succeeded", "not-applicable"}),
                "lanes_total": len(worker.get("dimensions") or []),
            }
            if receipt.get("status") in TERMINAL:
                value["finished_at"] = receipt.get("finished_at") or now()
    # Crash safety: the service RESTARTED before its reaper could finalize — the worker pid is gone,
    # the engine receipt is still `running`, and no live reaper is tracking the request (the in-memory
    # _PROCESSES map does not survive a restart). Mark it failed so it does not hang forever. A reaper
    # still finalizing in THIS process keeps the request in _PROCESSES, so we must not pre-empt it —
    # the dead-pid window between the worker exiting and finalize writing the terminal receipt is normal.
    if (value.get("status") not in TERMINAL
            and value.get("request_id") not in _PROCESSES
            and not pid_alive(int(value.get("pid") or 0))):
        value["status"] = "failed"
        value["finished_at"] = now()
    atomic_json(path, value)
    return value


def reconcile_requests() -> dict[str, int]:
    """Reconcile persisted non-terminal requests after a service restart.

    ``_PROCESSES`` is intentionally process-local. At startup it is empty, so a persisted
    non-terminal record can only remain live when its worker pid still exists or its authoritative
    receipt has advanced. ``refresh`` already owns those decisions; this sweep merely runs that
    invariant before any client has to discover the stale record by reading it.

    A corrupt record is isolated and reported instead of preventing the operation API from
    starting, because the remaining valid records can still be reconciled safely.
    """
    stats = {"checked": 0, "reconciled": 0, "errors": 0}
    if not REQUESTS.is_dir():
        return stats
    for path in sorted(REQUESTS.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request record must be a JSON object")
            if value.get("status") in TERMINAL:
                continue
            stats["checked"] += 1
            before = value.get("status")
            refreshed = refresh(path, value)
            if refreshed.get("status") != before:
                stats["reconciled"] += 1
        except (OSError, JSONDecodeError, TypeError, ValueError) as exc:
            stats["errors"] += 1
            print(f"operation startup reconciliation skipped {path.name}: {exc}", file=sys.stderr)
    return stats


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/operations")
def list_operations():
    rows = []
    for item in operations.discover(DEPLOYMENT).values():
        if item["name"] not in ALLOWED:
            continue
        rows.append({key: item.get(key) for key in
                     ("name", "owner", "title", "description", "mutates", "consequence", "supports")})
    return {"operations": rows}


@app.post("/operations/{name}/plan")
async def plan_operation(name: str, request: Request):
    item = manifest(name)
    data = await request.json()
    code, result = operation_run.plan(DEPLOYMENT, item, arguments(data), source="cockpit")
    if code:
        raise HTTPException(400, "planning failed")
    return result   # carries the ENGINE-computed snapshot_digest (same as the CLI)


@app.post("/operations/{name}/run", status_code=202)
async def run_operation(name: str, request: Request):
    item = manifest(name)
    data = await request.json()
    requested_digest = str(data.get("plan_digest") or "") if isinstance(data, dict) else ""
    if not requested_digest:
        raise HTTPException(409, "a current plan digest is required before execution")
    # Revalidate against the ENGINE-computed digest (identical to the CLI path) — reject on input drift.
    code, plan_result = operation_run.plan(DEPLOYMENT, item, arguments(data), source="cockpit")
    if code or not hmac.compare_digest(str(plan_result.get("snapshot_digest") or ""), requested_digest):
        raise HTTPException(409, "the operation inputs changed; plan the scope again")

    args = arguments(data)
    run_id = operation_run.new_run_id(name)
    command, env = operations.operation_command(DEPLOYMENT, item, args, source="cockpit")
    env["OKENGINE_OPERATION_RUN_ID"] = run_id
    env["OKENGINE_OPERATION_PLAN_DIGEST"] = requested_digest
    # Acquire declared locks, held by THIS long-lived service until the reaper finalizes — 409 on conflict.
    try:
        lockset = operation_run.acquire_lockset(DEPLOYMENT, item.get("locks") or [], run_id)
    except operation_run.OperationRunError as exc:
        raise HTTPException(409, str(exc)) from exc
    corpus_context = corpus_transaction.mutation(
        DEPLOYMENT, writer=f"operation:{name}", operation=run_id)
    corpus_context.__enter__()

    request_id = uuid.uuid4().hex
    REQUESTS.mkdir(parents=True, exist_ok=True)
    stdout = REQUESTS / f"{request_id}.stdout.log"
    stderr = REQUESTS / f"{request_id}.stderr.log"
    out_handle = stdout.open("w", encoding="utf-8")
    err_handle = stderr.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(command, cwd=DEPLOYMENT, env=env, text=True,
                                   stdout=out_handle, stderr=err_handle, start_new_session=True)
    except Exception:
        corpus_context.__exit__(*sys.exc_info())
        lockset.release()
        raise
    finally:
        out_handle.close()
        err_handle.close()
    try:
        # The ENGINE writes the run receipt (same as the CLI); the worker only emits progress + a result.
        receipt = operation_run.initial_receipt(DEPLOYMENT, item, args, run_id, source="cockpit",
                                                pid=process.pid)
        value = {"request_id": request_id, "run_id": run_id, "operation": name, "source": "cockpit",
                 "status": "running", "requested_at": now(), "pid": process.pid,
                 "arguments": operation_run._redact(args), "stdout": str(stdout), "stderr": str(stderr),
                 "receipt": str(operation_run.receipt_path(DEPLOYMENT, name, run_id).relative_to(DEPLOYMENT))}
        atomic_json(request_path(request_id), value)
        _PROCESSES[request_id] = process
        reaper = threading.Thread(
            target=reap, args=(request_id, process, item, run_id, receipt, lockset,
                               stdout, corpus_context),
            daemon=True, name=f"operation-{request_id[:8]}")
        reaper.start()
    except Exception:
        _PROCESSES.pop(request_id, None)
        abort_start(process, lockset, corpus_context)
        raise
    return value


@app.get("/operations/requests/{request_id}")
def operation_request(request_id: str):
    path, value = load_request(request_id)
    return refresh(path, value)
