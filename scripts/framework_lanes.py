#!/usr/bin/env python3
"""`framework lanes` — per-lane health from artefacts the runner already produces.

okengine#483. Every number this prints was reconstructed by hand during the #477/#478
investigation, one `docker exec … grep` at a time, and the most important one — that 84% of
all completion receipts were failing — nobody had at all until someone went looking. Two
whole failure classes accumulated for weeks in plain sight.

Reads the cron-plus **receipts** directory (one JSON per run, already written by the
runner) and, when asked, the run logs. Nothing new is instrumented.

Deliberately reads WRITE COUNTS from receipt/telemetry records rather than grepping logs
for "tool … completed": a REFUSED write is also a completed tool call, so log-grepping
overstates success. That mistake produced two different wrong write counts in one morning.

Usage:
  framework lanes <pack-dir>... [--lane NAME] [--limit N] [--json]
  framework lanes <pack-dir>... --check       # non-zero exit if a threshold trips
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Thresholds for --check. Conservative: they exist to catch a REGRESSION loudly, not to
# grade a lane. A dashboard tells you what is happening once you already suspect
# something; a threshold tells you when it started.
MIN_VALID_RATE = float(os.environ.get("LANES_MIN_VALID_RATE", "0.5"))
ZERO_WRITE_RUNS = int(os.environ.get("LANES_ZERO_WRITE_RUNS", "5"))
MAX_CONTEXT_RATIO = float(os.environ.get("LANES_MAX_CONTEXT_RATIO", "0.80"))
MIN_COMPLETION_RATIO = float(os.environ.get("LANES_MIN_COMPLETION_RATIO", "0.25"))
CONTEXT_WINDOW = int(os.environ.get("LANES_CONTEXT_WINDOW", "65536"))
STALE_RUNNING_SECONDS = int(os.environ.get("LANES_STALE_RUNNING_SECONDS", "3600"))

_API = re.compile(r"API call #(\d+): model=(\S+) provider=(\S+) in=(\d+) out=(\d+)")
_INIT = re.compile(r"agent_init.*?provider=(\S+) base_url=(\S+) model=(\S+)")
_COMPRESS = re.compile(r"context compression done:.*?messages=(\d+)->(\d+)")
_TURN = re.compile(r"Turn ended: reason=(.*?) model=\S+ api_calls=(\d+)/(\d+)")
_FAIL = re.compile(r"agent run failed:\s*(.+)")


def _data_dir(pack: Path) -> Path:
    return pack / ".hermes-data"


def _job_names(pack: Path) -> dict[str, str]:
    jobs = _data_dir(pack) / "cron-plus" / "jobs.json"
    try:
        raw = json.loads(jobs.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    items = raw.get("jobs") if isinstance(raw, dict) else raw
    return {j.get("id"): j.get("name") for j in items or [] if isinstance(j, dict)}


def _jobs(pack: Path) -> dict[str, dict]:
    path = _data_dir(pack) / "cron-plus" / "jobs.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    items = raw.get("jobs") if isinstance(raw, dict) else raw
    return {str(j.get("id")): j for j in items or []
            if isinstance(j, dict) and j.get("id")}


def parse_log(path: Path) -> dict:
    """Extract bounded health facts from one runner log."""
    result = {
        "log": path.name, "model": None, "provider": None, "endpoint": None,
        "api_calls": 0, "api_budget": None, "peak_context": 0,
        "peak_output": 0, "compressions": 0, "compression_noops": 0,
        "termination_reason": "unknown",
    }
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return result
    for match in _INIT.finditer(text):
        result["provider"], result["endpoint"], result["model"] = match.groups()
    for match in _API.finditer(text):
        call, model, provider, tokens_in, tokens_out = match.groups()
        result["api_calls"] = max(result["api_calls"], int(call))
        result["model"], result["provider"] = model, provider
        result["peak_context"] = max(result["peak_context"], int(tokens_in))
        result["peak_output"] = max(result["peak_output"], int(tokens_out))
    compressions = list(_COMPRESS.finditer(text))
    result["compressions"] = len(compressions)
    result["compression_noops"] = sum(
        1 for match in compressions if match.group(1) == match.group(2))
    turns = list(_TURN.finditer(text))
    if turns:
        reason, calls, budget = turns[-1].groups()
        result["termination_reason"] = reason
        result["api_calls"], result["api_budget"] = int(calls), int(budget)
    failures = list(_FAIL.finditer(text))
    if failures:
        result["termination_reason"] = failures[-1].group(1).strip()
    return result


def _logs(pack: Path, name: str, limit: int | None) -> list[dict]:
    root = _data_dir(pack) / "logs" / "cron-plus"
    log_name = name.replace(":", "_")
    # glob-ok: cron-plus logs are a deliberately flat run directory.
    files = sorted(root.glob(f"{log_name}-*.log"), reverse=True) if root.is_dir() else []
    return [parse_log(path) for path in files[:limit] if path.is_file()]


def collect(pack: Path, lane: str | None = None, limit: int | None = None) -> dict:
    """Per-lane rollup of stored receipts. Newest first when limited."""
    jobs = _jobs(pack)
    names = {job_id: job.get("name", job_id) for job_id, job in jobs.items()}
    root = _data_dir(pack) / "cron-plus" / "receipts"
    lanes: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "valid": 0, "writes": 0, "errors": Counter(), "recent": [],
                 "executions": 0, "execution_successes": 0, "execution_failures": 0,
                 "execution_running": 0, "stale_running": 0, "completion": None})
    run_root = _data_dir(pack) / "cron-plus" / "runs"
    # When a caller asks for the latest N executions, receipts must be scoped to
    # those SAME executions. A successful wakeAgent=false run deliberately emits
    # no receipt; independently taking the newest N receipt files would attach an
    # older failure to that new no-work execution and report a failure that did
    # not happen (#498).
    selected_windows: dict[str, list[tuple[float, float]]] = defaultdict(list)
    # glob-ok: runner records use a deliberately flat <job-id>/<run>.json layout.
    for job_dir in sorted(run_root.glob("*")) if run_root.is_dir() else []:
        name = names.get(job_dir.name, job_dir.name)
        if lane and name != lane:
            continue
        # glob-ok: each job directory contains only flat per-run JSON records.
        files = sorted(job_dir.glob("*.json"), reverse=True)
        if limit:
            files = files[:limit]
        for path in files:
            try:
                record = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            row = lanes[name]
            row["executions"] += 1
            status = record.get("status")
            succeeded = status == "succeeded"
            row["execution_successes"] += int(succeeded)
            row["execution_failures"] += int(status == "failed")
            row["execution_running"] += int(status == "running")
            if status == "running":
                try:
                    started = datetime.fromisoformat(record["started_at"])
                    age = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
                    row["stale_running"] += int(age > STALE_RUNNING_SECONDS)
                except (KeyError, TypeError, ValueError):
                    row["stale_running"] += 1
            row["completion"] = record.get("completion")
            if limit and status != "running":
                try:
                    started = datetime.fromisoformat(record["started_at"]).timestamp()
                    ended = datetime.fromisoformat(record["ended_at"]).timestamp()
                    selected_windows[job_dir.name].append((started, ended))
                except (KeyError, TypeError, ValueError):
                    pass
    # cron-plus receipts/ is a FLAT dir of <lane_id>/ dirs, not a sharded content namespace.
    # glob-ok: recursing would descend into the per-run JSON files themselves.
    for job_dir in sorted(root.glob("*")) if root.is_dir() else []:
        name = names.get(job_dir.name, job_dir.name)
        if lane and name != lane:
            continue
        current_completion = (
            (jobs.get(job_dir.name, {}).get("output_contract") or {}).get("completion")
        )
        # Old fixture/deployment job stores may predate the completion field.
        # Only an explicit current run-mode declaration retires old receipts.
        if current_completion == "run":
            continue
        # glob-ok: receipts/<lane_id>/ is a FLAT per-run dir (one JSON per run), not sharded.
        files = sorted(job_dir.glob("*.json"), reverse=True)
        if limit and job_dir.name in selected_windows:
            windows = selected_windows[job_dir.name]
            files = [
                path for path in files
                if any(started <= path.stat().st_mtime <= ended
                       for started, ended in windows)
            ]
        if limit:
            files = files[:limit]
        for f in files:
            try:
                d = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            row = lanes[name]
            row["completion"] = current_completion
            row["runs"] += 1
            ok = bool(d.get("valid"))
            row["valid"] += 1 if ok else 0
            # Writes from the RECEIPT's accepted items, never from a log grep.
            n = sum(len(i.get("writes") or [])
                    for i in ((d.get("receipt") or {}).get("items") or [])
                    if isinstance(i, dict) and i.get("disposition") == "accepted")
            row["writes"] += n
            if not ok:
                for e in (d.get("errors") or [])[:1]:
                    row["errors"][str(e).split(":")[0][:58]] += 1
            counts = d.get("counts") or {}
            selected = int(counts.get("selected") or 0)
            completed = sum(int(counts.get(key) or 0)
                            for key in ("accepted", "duplicate", "skipped", "rejected", "failed"))
            row["recent"].append({
                "stamp": f.stem, "valid": ok, "writes": n,
                "source": d.get("receipt_source"),
                "selected": selected,
                "completed": completed,
                "completion_ratio": (completed / selected) if selected else None,
            })
    for name, row in lanes.items():
        row["logs"] = _logs(pack, name, limit)
    return dict(lanes)


def _fmt(lanes: dict) -> str:
    if not lanes:
        return "no receipts found — has any per-selected-item lane run yet?"
    out = [f"{'lane':<38} {'exec':>5} {'ok':>5} {'rcpt':>5} {'valid':>6} {'writes':>7}",
           "-" * 76]
    for name in sorted(lanes):
        r = lanes[name]
        out.append(
            f"{name[:38]:<38} {r['executions']:>5} {r['execution_successes']:>5} "
            f"{r['runs']:>5} {r['valid']:>6} {r['writes']:>7}"
        )
    tot_r = sum(r["runs"] for r in lanes.values())
    tot_v = sum(r["valid"] for r in lanes.values())
    tot_w = sum(r["writes"] for r in lanes.values())
    tot_e = sum(r["executions"] for r in lanes.values())
    tot_e_ok = sum(r["execution_successes"] for r in lanes.values())
    out.append("-" * 76)
    out.append(
        f"{'TOTAL':<38} {tot_e:>5} {tot_e_ok:>5} {tot_r:>5} {tot_v:>6} {tot_w:>7}"
    )
    errs: Counter = Counter()
    for r in lanes.values():
        errs.update(r["errors"])
    if errs:
        out.append("\ntop failure classes:")
        for e, n in errs.most_common(6):
            out.append(f"  {n:>5}  {e}")
    return "\n".join(out)


def check(lanes: dict) -> list[str]:
    """Standing thresholds. Returns human-readable breaches, empty when healthy."""
    breaches = []
    for name in sorted(lanes):
        r = lanes[name]
        if r["execution_failures"]:
            breaches.append(
                f"{name}: {r['execution_failures']} failed runner-owned execution record(s)"
            )
        if r["stale_running"]:
            breaches.append(
                f"{name}: {r['stale_running']} stale/interrupted runner-owned execution(s)"
            )
        if r["runs"] >= 3:
            rate = r["valid"] / r["runs"]
            if rate < MIN_VALID_RATE:
                breaches.append(
                    f"{name}: receipt-valid rate {rate:.0%} over {r['runs']} runs "
                    f"(threshold {MIN_VALID_RATE:.0%})")
        recent = r["recent"][:ZERO_WRITE_RUNS]
        if len(recent) >= ZERO_WRITE_RUNS and not any(x["writes"] for x in recent):
            breaches.append(
                f"{name}: zero writes across the last {ZERO_WRITE_RUNS} runs")
        for log in r.get("logs") or []:
            if log["compression_noops"]:
                breaches.append(
                    f"{name}: {log['compression_noops']} compression no-op(s) in {log['log']}")
            ratio = log["peak_context"] / CONTEXT_WINDOW
            if ratio > MAX_CONTEXT_RATIO:
                breaches.append(
                    f"{name}: peak context {log['peak_context']}/{CONTEXT_WINDOW} "
                    f"({ratio:.0%}, threshold {MAX_CONTEXT_RATIO:.0%}) in {log['log']}")
        ratios = [item["completion_ratio"] for item in r["recent"]
                  if item["completion_ratio"] is not None]
        if ratios and (sum(ratios) / len(ratios)) < MIN_COMPLETION_RATIO:
            breaches.append(
                f"{name}: completion ratio {sum(ratios) / len(ratios):.0%} "
                f"(threshold {MIN_COMPLETION_RATIO:.0%})")
    return breaches


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="framework lanes",
                                 description="per-lane health from stored receipts")
    ap.add_argument("pack", nargs="+", help="one or more pack/deployment directories")
    ap.add_argument("--lane", help="restrict to one lane name")
    ap.add_argument("--limit", type=int, help="only the N most recent runs per lane")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if a standing threshold is breached")
    a = ap.parse_args(argv)

    packs = [Path(value).expanduser() for value in a.pack]
    for pack in packs:
        if not _data_dir(pack).is_dir():
            print(f"ERROR: no .hermes-data under {pack}", file=sys.stderr)
            return 2
    if len(packs) == 1:
        lanes = collect(packs[0], a.lane, a.limit)
    else:
        lanes = {}
        for pack in packs:
            for name, row in collect(pack, a.lane, a.limit).items():
                lanes[f"{pack.name}/{name}"] = row

    if a.as_json:
        print(json.dumps({k: {**v, "errors": dict(v["errors"])} for k, v in lanes.items()},
                         indent=2))
    else:
        print(_fmt(lanes))

    if a.check:
        breaches = check(lanes)
        if breaches:
            print("\nTHRESHOLD BREACHES:", file=sys.stderr)
            for b in breaches:
                print(f"  {b}", file=sys.stderr)
            return 1
        print("\nthresholds: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
