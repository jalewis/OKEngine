#!/usr/bin/env python3
"""Governed asynchronous API for declarative OKEngine operations."""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request


ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "scripts"))
import framework_operations as operations  # noqa: E402


DEPLOYMENT = Path(os.environ.get("WIKI_PATH") or "/opt/vault").resolve()
TOKEN = os.environ.get("OKENGINE_OPERATION_TOKEN", "")
ALLOWED = {value.strip() for value in
           os.environ.get("OKENGINE_OPERATION_ALLOW", "").split(",") if value.strip()}
REQUESTS = DEPLOYMENT / ".okengine/operations/requests"
_REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
TERMINAL = {"succeeded", "failed", "canceled", "planned"}
_PROCESSES: dict[str, subprocess.Popen] = {}


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


app = FastAPI(title="OKEngine operation runner", docs_url=None, redoc_url=None)
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


def receipt_for_pid(pid: int) -> dict[str, Any] | None:
    base = DEPLOYMENT / ".okengine/operations/runs"
    # glob-ok: receipt namespace is operation/run.json by contract
    for path in sorted(base.glob("*/*.json"), reverse=True) if base.is_dir() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("pid") == pid:
            return value
    return None


def pid_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        if len(stat) > 2 and stat[2] == "Z":
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def reap(request_id: str, process: subprocess.Popen) -> None:
    process.wait()
    _PROCESSES.pop(request_id, None)


def refresh(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    receipt = receipt_for_pid(int(value.get("pid") or 0))
    if receipt:
        value["run_id"] = receipt.get("run_id")
        value["status"] = receipt.get("status")
        value["progress"] = {
            "actors": len(receipt.get("actor_inventory") or []),
            "lanes_complete": sum(1 for row in receipt.get("lanes") or []
                                  if row.get("status") in {"succeeded", "not-applicable"}),
            "lanes_total": len(receipt.get("dimensions") or []),
        }
        value["receipt"] = (f".okengine/operations/runs/{receipt.get('operation')}/"
                            f"{receipt.get('run_id')}.json")
    if value.get("status") not in TERMINAL and not pid_alive(int(value.get("pid") or 0)):
        output_path = Path(str(value.get("stdout") or ""))
        result = operations.result_from_output(
            output_path.read_text(encoding="utf-8", errors="replace")
            if output_path.is_file() else "")
        value["status"] = str((result or {}).get("status") or "failed")
        value["run_id"] = (result or {}).get("run_id") or value.get("run_id")
        value["finished_at"] = now()
    atomic_json(path, value)
    return value


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
    command, env = operations.operation_command(
        DEPLOYMENT, item, arguments(data), plan=True, source="cockpit")
    completed = subprocess.run(command, cwd=DEPLOYMENT, env=env, text=True,
                               capture_output=True, timeout=300, check=False)
    result = operations.result_from_output(completed.stdout)
    if completed.returncode or result is None:
        raise HTTPException(400, (completed.stderr or completed.stdout or "planning failed")[-2000:])
    return result


@app.post("/operations/{name}/run", status_code=202)
async def run_operation(name: str, request: Request):
    item = manifest(name)
    data = await request.json()
    requested_digest = str(data.get("plan_digest") or "") if isinstance(data, dict) else ""
    if not requested_digest:
        raise HTTPException(409, "a current plan digest is required before execution")
    plan_command, plan_env = operations.operation_command(
        DEPLOYMENT, item, arguments(data), plan=True, source="cockpit")
    planned = subprocess.run(plan_command, cwd=DEPLOYMENT, env=plan_env, text=True,
                             capture_output=True, timeout=300, check=False)
    plan_result = operations.result_from_output(planned.stdout)
    if planned.returncode or plan_result is None:
        raise HTTPException(409, "the operation plan could not be revalidated")
    if not hmac.compare_digest(str(plan_result.get("snapshot_digest") or ""), requested_digest):
        raise HTTPException(409, "the operation inputs changed; plan the scope again")
    command, env = operations.operation_command(
        DEPLOYMENT, item, arguments(data), source="cockpit")
    env["OKENGINE_OPERATION_PLAN_DIGEST"] = requested_digest
    request_id = uuid.uuid4().hex
    REQUESTS.mkdir(parents=True, exist_ok=True)
    stdout = REQUESTS / f"{request_id}.stdout.log"
    stderr = REQUESTS / f"{request_id}.stderr.log"
    out_handle = stdout.open("w", encoding="utf-8")
    err_handle = stderr.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(command, cwd=DEPLOYMENT, env=env, text=True,
                                   stdout=out_handle, stderr=err_handle, start_new_session=True)
    finally:
        out_handle.close()
        err_handle.close()
    value = {"request_id": request_id, "operation": name, "source": "cockpit",
             "status": "running", "requested_at": now(), "pid": process.pid,
             "arguments": arguments(data), "stdout": str(stdout), "stderr": str(stderr)}
    atomic_json(request_path(request_id), value)
    _PROCESSES[request_id] = process
    threading.Thread(target=reap, args=(request_id, process), daemon=True,
                     name=f"operation-{request_id[:8]}").start()
    return value


@app.get("/operations/requests/{request_id}")
def operation_request(request_id: str):
    path, value = load_request(request_id)
    return refresh(path, value)
