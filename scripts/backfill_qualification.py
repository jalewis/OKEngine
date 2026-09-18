#!/usr/bin/env python3
"""Fleet qualification report for every active agent-backed *backfill* lane."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_ENGINE = Path(__file__).resolve().parent.parent
_REVIEW_SPEC = importlib.util.spec_from_file_location(
    "okengine_qualification_review_context", _ENGINE / "scripts/review_context.py")
review_context = importlib.util.module_from_spec(_REVIEW_SPEC)
assert _REVIEW_SPEC.loader
_REVIEW_SPEC.loader.exec_module(review_context)

_STAMP = re.compile(r"-(\d{8})-(\d{6})\.log$")
_TRANSPORT = re.compile(
    r"Inference transport selected: api_mode=(\S+) endpoint=(\S+) "
    r"model=(\S+) provider=(\S+)"
)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # The CLI contract says the cutoff is UTC. ``astimezone`` on a naive value
    # interprets it in the host's local timezone, silently shifting a clean
    # window by four hours on the current deployment host.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _log_time(path: Path) -> datetime | None:
    if not _STAMP.search(path.name):
        return None
    # Filenames use the gateway's configured local timezone, which differs by
    # deployment. Filesystem mtime is an absolute epoch and is therefore the
    # only fleet-safe clean-window boundary.
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def _receipt_time(path: Path) -> datetime | None:
    try:
        datetime.strptime(path.stem, "%Y-%m-%d_%H-%M-%S")
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except (ValueError, OSError):
        return None


def inventory(pack: Path) -> list[dict]:
    path = pack / ".hermes-data" / "cron-plus" / "jobs.json"
    raw = json.loads(path.read_text())
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    return [
        job for job in jobs
        if isinstance(job, dict)
        and job.get("enabled", True)
        and not job.get("no_agent")
        and (
            "backfill" in str(job.get("name", "")).lower()
            or job.get("name") == "page-quality-enrich"
        )
    ]


def qualify(pack: Path, since: datetime) -> list[dict]:
    data = pack / ".hermes-data"
    rows = []
    for job in inventory(pack):
        name, lane_id = job["name"], job["id"]
        # Extension lane names are canonicalized as ``extension:lane`` in the
        # inventory but cron-plus replaces the namespace separator in log
        # filenames. Match the actual flat-log naming contract.
        log_name = name.replace(":", "_")
        # glob-ok: cron-plus logs are a deliberately flat run directory.
        log_paths = sorted(
            (data / "logs" / "cron-plus").glob(f"{log_name}-*.log"), reverse=True)  # glob-ok: flat run logs
        log_paths = [path for path in log_paths
                     if (stamp := _log_time(path)) is not None and stamp >= since]
        calls = []
        for path in log_paths:
            for api_mode, endpoint, model, provider in _TRANSPORT.findall(
                    path.read_text(errors="replace")):
                calls.append({
                    "api_mode": api_mode, "provider": provider,
                    "endpoint": endpoint, "model": model,
                })
        # glob-ok: one lane's receipt files are flat and never namespace-sharded.
        receipt_paths = sorted(
            (data / "cron-plus" / "receipts" / lane_id).glob("*.json"), reverse=True)  # glob-ok: flat lane receipts
        receipts = []
        for path in receipt_paths:
            stamp = _receipt_time(path)
            if stamp is None or stamp < since:
                continue
            try:
                receipts.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                receipts.append({"valid": False, "errors": ["unreadable receipt"]})
        receipt_required = job.get("receipt_mode") == "enforce"
        errors = []
        if not log_paths:
            errors.append("not exercised in qualification window")
        if log_paths and not calls:
            errors.append("no model identity observed")
        bad_calls = [call for call in calls if
                     "qwen" not in call["model"].lower()
                     or call["provider"].lower() in {"deepseek", "openrouter", "anthropic"}
                     or call["api_mode"] != "codex_responses"
                     or not call["endpoint"].rstrip("/").endswith("/v1/responses")]
        if bad_calls:
            errors.append("non-Qwen or non-Responses model call observed")
        if receipt_required and not receipts:
            errors.append("no receipt in qualification window")
        if any(not receipt.get("valid") for receipt in receipts):
            errors.append("invalid receipt")
        undisposed = sum(int((receipt.get("counts") or {}).get("undisposed") or 0)
                         for receipt in receipts)
        if undisposed:
            errors.append(f"{undisposed} selected item(s) undisposed")
        rows.append({
            "pack": pack.name, "lane": name, "lane_id": lane_id,
            "receipt_required": receipt_required, "runs": len(log_paths),
            "model_calls": len(calls), "receipts": len(receipts),
            "valid_receipts": sum(bool(item.get("valid")) for item in receipts),
            "undisposed": undisposed,
            "models": sorted({call["model"] for call in calls}),
            "providers": sorted({call["provider"] for call in calls}),
            "endpoints": sorted({call["endpoint"] for call in calls}),
            "api_modes": sorted({call["api_mode"] for call in calls}),
            "errors": errors, "verdict": "pass" if not errors else "fail",
        })
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack", action="append", required=True)
    parser.add_argument("--since", required=True, help="ISO-8601 UTC qualification cutoff")
    parser.add_argument("--output")
    parser.add_argument("--engine-dir", default=str(_ENGINE))
    parser.add_argument("--allow-unattributable", action="store_true")
    args = parser.parse_args(argv)
    source_context = review_context.collect(Path(args.engine_dir))
    context_errors = review_context.problems(source_context)
    override = args.allow_unattributable
    if context_errors and not override:
        print(review_context.render(source_context, context_errors), file=sys.stderr)
        return 2
    source_context.update(problems=context_errors, override=override and bool(context_errors))
    since = _dt(args.since)
    rows = [row for value in args.pack for row in qualify(Path(value), since)]
    report = {
        "api": 1, "source_context": source_context,
        "since": since.isoformat(), "generated_at": datetime.now(
            timezone.utc).isoformat(),
        "summary": {
            "lanes": len(rows),
            "passed": sum(row["verdict"] == "pass" for row in rows),
            "failed": sum(row["verdict"] == "fail" for row in rows),
        },
        "lanes": rows,
    }
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    print(text, end="")
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
