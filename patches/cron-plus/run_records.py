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


def begin_run_record(home: Path, job: dict, started_at: datetime) -> Path:
    """Write the running marker before job environment or agent startup."""
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
