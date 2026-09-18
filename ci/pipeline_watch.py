#!/usr/bin/env python3
"""Host-side GitLab pipeline watcher with durable GitLab issues.

This process deliberately runs outside GitLab CI.  A failed runner cannot execute a job that
reports its own failure, so the watcher polls the forge from a user systemd timer instead.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


FINISHED = {"success", "failed", "canceled", "skipped", "manual"}
BRANCH_SOURCES = {"push", "web", "api", "trigger", "pipeline"}
ALERT_TITLE = "CI alert: default branch or scheduled pipeline is red"
MUTATION_JOB_PREFIXES = ("mutation-critical", "mutation-full")
PROGRESS_PREFIX = "mutation-progress "
TRACE_PROGRESS = re.compile(r"^(\S+).*?mutation-progress (\{.*\})$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def glab_json(*args: str) -> Any:
    proc = subprocess.run(["glab", "api", *args], text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"glab api {' '.join(args)} failed")
    text = proc.stdout
    start = min((index for index in (text.find("{"), text.find("[")) if index >= 0),
                default=-1)
    if start < 0:
        raise RuntimeError("glab returned no JSON payload")
    return json.loads(text[start:])


def glab_text(*args: str) -> str:
    proc = subprocess.run(["glab", "api", *args], text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"glab api {' '.join(args)} failed")
    return proc.stdout


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def latest_by_kind(pipelines: list[dict]) -> dict[str, dict | None]:
    finished = [p for p in pipelines if p.get("status") in FINISHED]
    return {
        "default-branch": next(
            (p for p in finished if p.get("source") in BRANCH_SOURCES), None),
        "scheduled": next((p for p in finished if p.get("source") == "schedule"), None),
    }


def failed_lanes(jobs: list[dict]) -> list[str]:
    return sorted({str(job["name"]) for job in jobs if job.get("status") == "failed"})


def lane_streak(lane: str, current: dict, older: list[dict], jobs_for) -> tuple[int, str]:
    count = 1
    oldest = current["created_at"]
    for pipeline in older:
        if pipeline.get("source") != current.get("source"):
            continue
        if pipeline.get("status") != "failed" or lane not in failed_lanes(jobs_for(pipeline)):
            break
        count += 1
        oldest = pipeline["created_at"]
    return count, oldest


def human_age(start: str, now: dt.datetime) -> str:
    seconds = max(0, int((now - parse_time(start)).total_seconds()))
    if seconds >= 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    return f"{max(1, seconds // 60)} minutes"


def attribution(project: str, pipeline: dict) -> str:
    sha = str(pipeline.get("sha") or "")
    if not sha:
        return "Attribution unavailable: pipeline has no SHA."
    commit = glab_json(f"projects/{project}/repository/commits/{sha}")
    merge_requests = glab_json(f"projects/{project}/repository/commits/{sha}/merge_requests")
    title = str(commit.get("title") or "unknown commit")
    author = str(commit.get("author_name") or "unknown author")
    if merge_requests:
        mr = merge_requests[0]
        return (f"Introduced at `{sha[:12]}` by !{mr['iid']} — {mr['title']} "
                f"({author}).")
    return f"Current red revision: `{sha[:12]}` — {title} ({author})."


def latest_mutation_progress(trace: str) -> tuple[dt.datetime, dict] | None:
    latest = None
    for line in trace.splitlines():
        match = TRACE_PROGRESS.match(line)
        if not match:
            continue
        try:
            payload = json.loads(match.group(2))
            timestamp = parse_time(match.group(1))
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("event"), str):
            latest = (timestamp, payload)
    return latest


def running_mutation_stalls(project: str, pipelines: list[dict], now: dt.datetime,
                            max_age_seconds: int) -> list[dict]:
    stalls = []
    for pipeline in pipelines:
        if pipeline.get("status") != "running":
            continue
        jobs = glab_json(f"projects/{project}/pipelines/{pipeline['id']}/jobs?per_page=100")
        for job in jobs:
            name = str(job.get("name") or "")
            if job.get("status") != "running" or not name.startswith(MUTATION_JOB_PREFIXES):
                continue
            progress = latest_mutation_progress(
                glab_text(f"projects/{project}/jobs/{job['id']}/trace"))
            if progress is None:
                last_at = parse_time(job["started_at"])
                event = None
                target = None
            else:
                last_at, payload = progress
                event = payload["event"]
                target = payload.get("path")
            age = max(0, int((now - last_at).total_seconds()))
            if age <= max_age_seconds:
                continue
            stalls.append({
                "job_id": job["id"], "job_name": name,
                "job_url": job.get("web_url"), "pipeline_id": pipeline["id"],
                "pipeline_url": pipeline.get("web_url"), "target": target,
                "last_event": event, "last_progress_at": last_at.isoformat(),
                "silent_seconds": age,
            })
    return stalls


def runner_capacity_incident(project: str, runner_ids: list[int], expected_capacity: int,
                             now: dt.datetime, max_queue_age: int = 120) -> dict | None:
    """Detect runnable project work waiting while the configured executor pool is idle."""
    if not runner_ids or expected_capacity < 1:
        return None
    pending = glab_json(f"projects/{project}/jobs?scope[]=pending&per_page=100")
    if not pending:
        return None
    oldest = min(pending, key=lambda job: parse_time(job["created_at"]))
    queued_seconds = max(0, int((now - parse_time(oldest["created_at"])).total_seconds()))
    if queued_seconds <= max_queue_age:
        return None

    active_by_id: dict[int, dict] = {}
    managers: dict[int, dict] = {}
    for runner_id in runner_ids:
        for job in glab_json(f"runners/{runner_id}/jobs?status=running&per_page=100"):
            active_by_id[int(job["id"])] = job
        for manager in glab_json(f"runners/{runner_id}/managers"):
            managers[int(manager["id"])] = manager
    active = len(active_by_id)
    if active >= expected_capacity:
        return None
    return {
        "kind": "runner-capacity",
        "runner_ids": runner_ids,
        "manager_ids": sorted(managers),
        "manager_system_ids": sorted({str(item.get("system_id") or "unknown")
                                      for item in managers.values()}),
        "active_builds": active,
        "expected_capacity": expected_capacity,
        "pending_jobs": len(pending),
        "oldest_queued_seconds": queued_seconds,
        "oldest_job_id": oldest["id"],
        "oldest_job_name": oldest.get("name"),
        "oldest_job_url": oldest.get("web_url"),
        "pipeline_id": (oldest.get("pipeline") or {}).get("id"),
        "pipeline_url": (oldest.get("pipeline") or {}).get("web_url"),
    }


def mutation_dropout_incident(project: str, history_dir: Path,
                              threshold: int = 3) -> dict | None:
    """Run the published-summary graduation rule outside the runner it observes."""
    root = Path(__file__).resolve().parent
    fetch = subprocess.run([
        sys.executable, str(root / "fetch_mutation_summaries.py"),
        "--project", project, "--out", str(history_dir),
    ], text=True, capture_output=True)
    if fetch.returncode:
        raise RuntimeError(fetch.stderr.strip() or fetch.stdout.strip()
                           or "mutation history fetch failed")
    detector = subprocess.run([
        sys.executable, str(root / "mutation_history.py"),
        "--summaries", str(history_dir), "--critical-only",
        "--threshold", str(threshold),
    ], text=True, capture_output=True)
    if detector.stderr.strip():
        raise RuntimeError(detector.stderr.strip())
    if detector.returncode == 0:
        return None
    return {"kind": "mutation-dropout", "threshold": threshold,
            "detail": detector.stdout.strip()}


def collect(project: str, ref: str, limit: int, now: dt.datetime,
            mutation_heartbeat_max_age: int = 120) -> dict:
    pipelines = glab_json(
        f"projects/{project}/pipelines?ref={ref}&per_page={limit}&order_by=id&sort=desc"
    )
    latest = latest_by_kind(pipelines)
    cache: dict[int, list[dict]] = {}

    def jobs_for(pipeline: dict) -> list[dict]:
        pipeline_id = int(pipeline["id"])
        if pipeline_id not in cache:
            cache[pipeline_id] = glab_json(
                f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page=100"
            )
        return cache[pipeline_id]

    incidents = []
    for kind, pipeline in latest.items():
        if pipeline is None:
            incidents.append({"kind": kind, "undetectable": True,
                              "reason": "no finished pipeline in query window"})
            continue
        if pipeline.get("status") != "failed":
            continue
        lanes = []
        older = pipelines[pipelines.index(pipeline) + 1:]
        for lane in failed_lanes(jobs_for(pipeline)):
            count, oldest = lane_streak(lane, pipeline, older, jobs_for)
            lanes.append({"name": lane, "consecutive_failures": count, "failing_since": oldest,
                          "duration": human_age(oldest, now)})
        incidents.append({
            "kind": kind, "pipeline_id": pipeline["id"], "pipeline_url": pipeline["web_url"],
            "sha": pipeline.get("sha"), "created_at": pipeline["created_at"], "lanes": lanes,
            "attribution": None,
        })
        if kind == "default-branch":
            try:
                incidents[-1]["attribution"] = attribution(project, pipeline)
            except (RuntimeError, ValueError, KeyError) as exc:
                incidents[-1]["attribution"] = f"Attribution unavailable: {exc}"
    stalls = running_mutation_stalls(project, pipelines, now, mutation_heartbeat_max_age)
    if stalls:
        incidents.append({"kind": "running-mutation", "stalled_jobs": stalls})
    return {"checked_at": now.isoformat(), "latest": latest, "incidents": incidents}


def render(report: dict, owner: str) -> str:
    lines = [f"@{owner} — the external CI watcher detected a red or undetectable signal.", ""]
    for incident in report["incidents"]:
        if incident.get("kind") == "runner-capacity":
            lines.extend([
                "### Runnable jobs waiting while runner capacity is idle",
                f"- runners `{incident['runner_ids']}` / managers "
                f"`{incident['manager_ids']}` (`{incident['manager_system_ids']}`)",
                f"- active/expected builds: `{incident['active_builds']}/"
                f"{incident['expected_capacity']}`; pending project jobs: "
                f"`{incident['pending_jobs']}`",
                f"- oldest queue age: `{incident['oldest_queued_seconds']}s` — "
                f"[{incident['oldest_job_name']} job {incident['oldest_job_id']}]"
                f"({incident['oldest_job_url']}) in "
                f"[pipeline {incident['pipeline_id']}]({incident['pipeline_url']})",
                "",
            ])
            continue
        if incident.get("kind") == "running-mutation":
            lines.append("### Running mutation jobs without a fresh heartbeat")
            for job in incident["stalled_jobs"]:
                target = f" target `{job['target']}`" if job.get("target") else ""
                event = job.get("last_event") or "no progress event"
                lines.append(
                    f"- [{job['job_name']} job {job['job_id']}]({job['job_url']}):{target} "
                    f"silent for {job['silent_seconds']}s; last event `{event}` at "
                    f"{job['last_progress_at']}"
                )
            lines.append("")
            continue
        if incident.get("kind") == "mutation-dropout":
            lines.extend(["### Critical targets missing consecutive scores", "",
                          "```text", incident["detail"], "```", ""])
            continue
        if incident.get("undetectable"):
            lines.append(f"- **{incident['kind']}**: UNDETECTABLE — {incident['reason']}")
            continue
        lines.append(f"### {incident['kind']} pipeline [{incident['pipeline_id']}]"
                     f"({incident['pipeline_url']})")
        for lane in incident["lanes"]:
            lines.append(
                f"- `{lane['name']}`: {lane['consecutive_failures']} consecutive failure(s), "
                f"failing for {lane['duration']} (since {lane['failing_since']})"
            )
        if incident.get("attribution"):
            lines.extend(["", incident["attribution"]])
        lines.append("")
    lines.extend([
        f"Watcher check: `{report['checked_at']}`", "",
        "This issue is maintained by the host-side `okengine-ci-watch.timer`, not by the CI "
        "runner it observes. It closes automatically after both watched surfaces recover.",
    ])
    return "\n".join(lines)


def fingerprint(report: dict) -> str:
    stable = [{
        "kind": item.get("kind"), "pipeline_id": item.get("pipeline_id"),
        "undetectable": item.get("undetectable"),
        "lanes": [(lane["name"], lane["consecutive_failures"]) for lane in item.get("lanes", [])],
        "stalled_jobs": [(job["job_id"], job.get("target"), job.get("last_event"))
                         for job in item.get("stalled_jobs", [])],
        "dropout_detail": item.get("detail"),
        "runner_capacity": (
            item.get("runner_ids"), item.get("manager_ids"), item.get("active_builds"),
            item.get("expected_capacity"), item.get("oldest_job_id")
        ) if item.get("kind") == "runner-capacity" else None,
    } for item in report["incidents"]]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def find_alert_issue(project: str) -> dict | None:
    issues = glab_json(
        f"projects/{project}/issues?state=opened&search={ALERT_TITLE.replace(' ', '%20')}&per_page=20"
    )
    return next((issue for issue in issues if issue.get("title") == ALERT_TITLE), None)


def sync_issue(project: str, report: dict, owner: str, previous: dict) -> int | None:
    issue = find_alert_issue(project)
    if report["incidents"]:
        current = fingerprint(report)
        changed = current != previous.get("alert_fingerprint")
        if issue is None:
            body = render(report, owner)
            issue = glab_json("--method", "POST", f"projects/{project}/issues",
                              "--raw-field", f"title={ALERT_TITLE}",
                              "--raw-field", f"description={body}",
                              "--raw-field", "labels=P1,ci,detector,observability",
                              "--raw-field", f"assignee_ids[]={glab_json('user')['id']}")
        elif changed:
            body = render(report, owner)
            issue = glab_json("--method", "PUT", f"projects/{project}/issues/{issue['iid']}",
                              "--raw-field", f"description={body}")
        return int(issue["iid"])
    if issue is not None:
        glab_json("--method", "PUT", f"projects/{project}/issues/{issue['iid']}",
                  "--raw-field", "state_event=close")
    return None


def load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def check_liveness(state_path: Path, max_age: int, now: dt.datetime) -> int:
    state = load_state(state_path)
    checked = state.get("checked_at")
    if checked:
        age = (now - parse_time(checked)).total_seconds()
        if age <= max_age:
            return 0
        reason = f"last successful check was {int(age)}s ago (limit {max_age}s)"
    else:
        reason = "no successful watcher heartbeat exists"
    print("pipeline-watch-liveness: " + reason, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="24")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--owner", default="jlew")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--state", type=Path,
                        default=Path.home() / ".local/state/okengine/ci-watch.json")
    parser.add_argument("--check-liveness", action="store_true")
    parser.add_argument("--max-age-seconds", type=int, default=900)
    parser.add_argument("--mutation-heartbeat-max-age-seconds", type=int, default=120)
    parser.add_argument("--mutation-history-threshold", type=int, default=3)
    parser.add_argument(
        "--runner-ids", default=os.environ.get("OKENGINE_CI_RUNNER_IDS", ""),
        help="comma-separated runner IDs sharing the executor pool; empty disables capacity checks",
    )
    parser.add_argument(
        "--runner-capacity", type=int,
        default=int(os.environ.get("OKENGINE_CI_RUNNER_CAPACITY", "0")),
    )
    parser.add_argument("--runner-queue-max-age-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    now = utc_now()
    if args.check_liveness:
        return check_liveness(args.state, args.max_age_seconds, now)
    previous = load_state(args.state)
    report = collect(args.project, args.ref, args.limit, now,
                     args.mutation_heartbeat_max_age_seconds)
    dropout = mutation_dropout_incident(
        args.project, args.state.parent / "mutation-history",
        args.mutation_history_threshold,
    )
    if dropout:
        report["incidents"].append(dropout)
    runner_ids = [int(value) for value in args.runner_ids.split(",") if value.strip()]
    capacity = runner_capacity_incident(
        args.project, runner_ids, args.runner_capacity, now,
        args.runner_queue_max_age_seconds,
    )
    if capacity:
        report["incidents"].append(capacity)
    issue_iid = sync_issue(args.project, report, args.owner, previous)
    state = dict(report, issue_iid=issue_iid,
                 alert_fingerprint=fingerprint(report) if report["incidents"] else None)
    atomic_json(args.state, state)
    print(render(report, args.owner) if report["incidents"] else
          f"pipeline-watch: healthy at {report['checked_at']}")
    return 1 if report["incidents"] else 0


def entrypoint() -> int:
    """Reserve exit 1 for observed incidents; surface watcher failures as exit 2."""
    try:
        return main()
    except Exception as exc:
        print(f"pipeline-watch: ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(entrypoint())
