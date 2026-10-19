"""Runner-owned execution records for every cron-plus lane invocation."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _record(job: dict, started_at: datetime) -> dict:
    deterministic = job.get("no_agent") is True
    required_path = (job.get("output_contract") or {}).get("required_write_path")
    expected = []
    if isinstance(required_path, str) and required_path:
        local_date = started_at.astimezone().strftime("%Y-%m-%d")
        expected = [required_path.replace("{date}", local_date)]
    return {
        "api": 1,
        "job_id": str(job.get("id") or ""),
        "lane": str(job.get("name") or job.get("id") or ""),
        "completion": (job.get("output_contract") or {}).get("completion"),
        "contract_digest": job.get("output_contract_digest"),
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "ended_at": None,
        "status": "running",
        "error": None,
        "delivery_error": None,
        "model": None if deterministic else job.get("model"),
        "provider": None if deterministic else job.get("provider"),
        "executed_tool_call_turns": 0,
        "writes": [],
        "artifacts": [],
        "expected_artifacts": expected,
        "pid": os.getpid(),
    }


def _atomic_write(path: Path, record: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(record, indent=2) + "\n")
    temp.replace(path)
    return path


def _worker_alive(pid) -> bool:
    """Is the process that owns a `running` record still there?

    Conservative in BOTH directions. A pid owned by another user raises PermissionError, which
    proves it exists, so it counts as alive. Anything we cannot interpret also counts as alive: the
    cost of leaving a stale record is a noisy healthcheck, while the cost of closing out a live run
    is a false failure on work that is still happening.
    """
    if not isinstance(pid, int) or pid <= 0:
        return True
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _read_running(path: Path) -> dict | None:
    """The record at ``path`` if it still reads ``running``; anything else is not ours."""
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):   # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None                   # unreadable is not ours to rewrite
    if not isinstance(record, dict) or record.get("status") != "running":
        return None
    return record


def _close_if_orphaned(path: Path) -> bool:
    """Mark a `running` record failed when its worker is gone. Returns whether it closed one."""
    record = _read_running(path)
    if record is None or _worker_alive(record.get("pid")):
        return False
    # Read again now that the worker is known to be gone: it can no longer write, so this read
    # sees its final state. A run that closed its own record between the first read and the probe
    # (the scheduler tick races live runners) keeps its own outcome instead of becoming `failed`.
    record = _read_running(path)
    if record is None:
        return False
    record["status"] = "failed"
    record["ended_at"] = datetime.now(timezone.utc).isoformat()
    record["error"] = (
        f"run reconciled as orphaned: worker pid {record.get('pid')} is gone and the record "
        "was never closed out, so it still read \"running\""
    )
    try:
        _atomic_write(path, record)
    except OSError:
        return False                  # a read-only or racing record stays as it was
    return True


def reconcile_orphaned_runs(home: Path, job_id: str) -> int:
    """Close out records this lane left `running` when its worker died.

    `finish_run_record` deliberately leaves `running` visible after a hard kill, so a killed run is
    not silently lost — but nothing ever revisited it, so the marker stayed `running` forever. The
    gateway healthcheck fails on any `running` record older than an hour, so a single killed worker
    made a deployment permanently unhealthy: okcti (policy-audit) and okpack-ai-research
    (entity-backfill, raw-backfill) both sat that way for a day, on the same afternoon, until an
    operator edited the JSON by hand.

    Runs on each new start of the SAME lane, which is where the knowledge and the write
    permissions already are. Returns how many it closed.
    """
    directory = Path(home) / "runs" / str(job_id or "")
    if not directory.is_dir():
        return 0
    closed = 0
    for path in sorted(directory.glob("*.json")):  # glob-ok: one lane's flat record directory
        if _close_if_orphaned(path):
            closed += 1
    return closed


def reconcile_all_orphaned_runs(home: Path) -> int:
    """Close out every lane's orphaned newest record; the scheduler calls this on each tick.

    The lane-start path alone left a killed run `running` until the same lane started again, a
    day for a daily lane, with the gateway healthcheck failing all that time (okengine#1010: four
    runners killed at once by the gateway's MCP connect cleanup). Only each lane's newest record
    is read, so a tick costs one directory listing and at most one read per lane: an older orphan
    is already closed by the newer run's own start, and a lane runs one worker at a time.
    Liveness keeps the conservative `_worker_alive` semantics. Returns how many it closed.
    """
    closed = 0
    for lane in sorted((Path(home) / "runs").glob("*/")):  # glob-ok: flat per-lane record dirs
        newest = max(lane.glob("*.json"), default=None)  # glob-ok: one lane's flat record directory
        if newest is not None and _close_if_orphaned(newest):
            closed += 1
    return closed


def begin_run_record(home: Path, job: dict, started_at: datetime) -> Path:
    """Write the running marker before job environment or agent startup."""
    reconcile_orphaned_runs(home, str(job.get("id") or ""))
    stamp = started_at.astimezone(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S-%f")
    target = Path(home) / "runs" / str(job.get("id") or "") / f"{stamp}.json"
    return _atomic_write(target, _record(job, started_at))


def finish_run_record(path: Path, job: dict, current: dict | None) -> Path:
    """Finalize a marker; a hard-killed runner leaves ``running`` visible."""
    try:
        record = json.loads(path.read_text())
    # Keep these handlers separate. Cosmic Ray 8.4.3's ExceptionReplacer crashes while
    # mutating the second member of an exception tuple, making the campaign incomplete.
    except OSError:
        record = _record(job, datetime.now(timezone.utc))
    except json.JSONDecodeError:
        record = _record(job, datetime.now(timezone.utc))
    state = current or {}
    record.update({
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "status": "succeeded" if state.get("last_run_success") is True else "failed",
        "error": state.get("last_error"),
        "delivery_error": state.get("last_delivery_error"),
        "executed_tool_call_turns": int(job.get("_okengine_executed_tool_calls") or 0),
        "writes": list(job.get("_okengine_executed_writes") or []),
        "artifacts": list(job.get("_okengine_artifacts") or []),
    })
    return _atomic_write(path, record)


def write_run_record(home: Path, job: dict, started_at: datetime, current: dict | None) -> Path:
    """Compatibility helper for callers that only need a completed record."""
    return finish_run_record(begin_run_record(home, job, started_at), job, current)
