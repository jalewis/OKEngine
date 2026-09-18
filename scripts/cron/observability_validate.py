#!/usr/bin/env python3
"""Validate that live observability surfaces exist, are fresh, and agree."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
DATA = Path(os.environ.get("HERMES_HOME", "/opt/data"))
WIKI = VAULT / "wiki"
FLEET = WIKI / "dashboards" / "fleet-health.md"
LANES = WIKI / "dashboards" / ".fleet-lanes.json"
SEARCH = DATA / "qmd" / "search-telemetry.json"
DEPLOYMENT = WIKI / "operational" / "deployment-validation.md"
METRICS = DATA / "metrics" / "okengine.prom"
OUTPUT = WIKI / "operational" / "observability-validation.md"
FLEET_MAX_AGE = 3600
SEARCH_MAX_AGE = 1800
DEPLOYMENT_MAX_AGE = 108000
METRICS_MAX_AGE = 3600
# Must equal fleet_health.LANE_BUCKETS: a bucket missing here makes every lane fleet-health files
# under it read as absent (#757, `undetectable`). A test pins the two together.
LANE_KEYS = ("ok", "stale", "critical-stale", "errored", "timed-out",
             "saturated", "undetectable", "invalid-schedule", "off-model", "never-run")
STAMP = "%Y-%m-%dT%H:%M:%SZ"
# fleet_health.qmd_search_health's detail line; an unmeasured p95 renders as an em dash.
_SEARCH_LINE = re.compile(
    r"search (\d+) call\(s\) p95 (\d+|—)ms · maintenance (\d+) p95 (\d+|—)ms · "
    r"saturated (\d+), search timeouts \d+, maintenance errors \d+, "
    r"maintenance timeouts \d+, concurrency (\d+)")
_SEARCH_STATE = re.compile(r"^\S+ \*\*(ok|warn|unknown)\*\* — (.*)$", re.MULTILINE)


def _age(path: Path, now: float) -> Optional[float]:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def _read(path: Path, errors: List[str]) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        errors.append(f"{path}: unavailable ({exc})")
        return ""


def _json(path: Path, errors: List[str]) -> Dict:
    text = _read(path, errors)
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: malformed JSON ({exc.msg})")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{path}: root must be an object")
        return {}
    return value


def _metric(container: Dict, field: str, label: str, errors: List[str]) -> Optional[int]:
    value = container.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"{label}.{field} must be non-negative integer")
        return None
    return value


def _bucket(value: Dict, kind: str, label: str, errors: List[str]) -> Optional[tuple]:
    """(calls, p95_ms or None) for one telemetry bucket.

    The read-MCP publishes percentiles only once a bucket has latency samples: an empty bucket
    carries no `samples`/`p95_ms` keys at all. That is the normal state after every read-MCP
    restart, so it is valid and means "unmeasured" — not malformed (#757).
    """
    bucket = value.get(kind)
    if not isinstance(bucket, dict):
        errors.append(f"{label} {kind!r} bucket is absent")
        return None
    name = f"{label} {kind}"
    calls = _metric(bucket, "calls", name, errors)
    published = [field for field in ("samples", "p95_ms") if field in bucket]
    if not published:
        return None if calls is None else (calls, None)
    if len(published) == 1:
        errors.append(f"{name} samples and p95_ms must be published together")
        return None
    samples = _metric(bucket, "samples", name, errors)
    p95 = _metric(bucket, "p95_ms", name, errors)
    if calls is None or samples is None or p95 is None:
        return None
    if not 1 <= samples <= calls:
        errors.append(f"{name}.samples must be between 1 and calls when p95_ms is published")
        return None
    return calls, p95


def _telemetry(value: Dict, label: str, errors: List[str]) -> Optional[tuple]:
    """(search calls, search p95, maintenance calls, maintenance p95, saturated, concurrency)."""
    search = _bucket(value, "search", label, errors)
    maintenance = _bucket(value, "maintenance", label, errors)
    saturated = _metric(value, "saturated", label, errors)
    concurrency = _metric(value, "concurrency_limit", label, errors)
    if search is None or maintenance is None or saturated is None or concurrency is None:
        return None
    return (*search, *maintenance, saturated, concurrency)


def _stamp(value: Dict, label: str, errors: List[str]) -> Optional[float]:
    try:
        parsed = datetime.strptime(str(value.get("updated")), STAMP)
    except ValueError:
        errors.append(f"{label}.updated must be a {STAMP} timestamp")
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def _counters(values: tuple) -> tuple:
    return values[0], values[2], values[4]


def _validate_search(fleet: str, lanes: Dict, current: float, errors: List[str]) -> None:
    search = _json(SEARCH, errors)
    search_age = _age(SEARCH, current)
    if search_age is None:
        if not any(str(SEARCH) in error for error in errors):
            errors.append(f"{SEARCH}: freshness unknown")
    elif search_age > SEARCH_MAX_AGE:
        errors.append(f"{SEARCH}: stale ({search_age:.0f}s > {SEARCH_MAX_AGE}s)")
    artifact = _telemetry(search, "search telemetry", errors)
    artifact_stamp = _stamp(search, "search telemetry", errors) if search else None

    snapshot = lanes.get("search_telemetry")
    if not isinstance(snapshot, dict):
        errors.append("fleet sidecar has no search telemetry snapshot")
        return
    fleet_stamp = _stamp(lanes, "fleet sidecar", errors)
    _, heading, section = fleet.partition("## Search layer")
    state = _SEARCH_STATE.search(section) if heading else None
    if not snapshot:
        if state is None or state.group(1) != "unknown":
            errors.append("empty fleet search snapshot is not rendered as unknown")
        if artifact_stamp is not None and fleet_stamp is not None and artifact_stamp < fleet_stamp:
            errors.append("fleet-health recorded no search telemetry although the qmd artifact "
                          "predates its run")
        return

    recorded = _telemetry(snapshot, "fleet search snapshot", errors)
    recorded_stamp = _stamp(snapshot, "fleet search snapshot", errors)
    if recorded is None:
        return
    # The dashboard line and the snapshot come from ONE read in ONE fleet-health run, so they must
    # agree exactly.
    rendered = _SEARCH_LINE.search(state.group(2)) if state else None
    if rendered is None:
        errors.append("fleet dashboard has no parseable search/maintenance telemetry")
    elif tuple(int(value) if value.isdigit() else None for value in rendered.groups()) != recorded:
        errors.append("fleet dashboard search telemetry disagrees with its snapshot")
    if artifact is None or artifact_stamp is None or recorded_stamp is None or fleet_stamp is None:
        return
    # The live artifact is republished on every qmd call, so it only equals the snapshot when it
    # has not changed since fleet-health read it. A version stamped in an earlier second than the
    # run began is still the version that run read; anything later may legitimately be ahead.
    if artifact_stamp < fleet_stamp:
        if (artifact_stamp, artifact) != (recorded_stamp, recorded):
            errors.append("fleet search snapshot disagrees with the qmd artifact it read")
    elif any(now < then for then, now in zip(_counters(recorded), _counters(artifact))):
        errors.append("qmd artifact counters went backwards since the fleet snapshot (read-MCP "
                      "restart, or fleet-health read a different file)")


def validate(*, now: Optional[float] = None) -> List[str]:
    current = now if now is not None else datetime.now(timezone.utc).timestamp()
    errors: List[str] = []
    fleet = _read(FLEET, errors)
    lanes = _json(LANES, errors)

    for path, ceiling in ((FLEET, FLEET_MAX_AGE), (LANES, FLEET_MAX_AGE)):
        age = _age(path, current)
        if age is None:
            if not any(str(path) in error for error in errors):
                errors.append(f"{path}: freshness unknown")
        elif age > ceiling:
            errors.append(f"{path}: stale ({age:.0f}s > {ceiling}s)")

    counts: Dict[str, int] = {}
    for key in LANE_KEYS:
        values = lanes.get(key)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            errors.append(f"fleet sidecar {key!r} must be a string list")
            continue
        counts[key] = len(values)
        label = "timeout" if key == "timed-out" else key
        match = re.search(rf"{re.escape(label)}:\s*(\d+)", fleet)
        if not match:
            errors.append(f"fleet dashboard count {key!r} is absent")
        elif int(match.group(1)) != counts[key]:
            errors.append(
                f"fleet dashboard/sidecar disagree for {key}: {match.group(1)} != {counts[key]}")
    if counts and sum(counts.values()) == 0:
        errors.append("fleet sidecar contains zero classified lanes")

    _validate_search(fleet, lanes, current, errors)

    deployment = _read(DEPLOYMENT, errors)
    deployment_age = _age(DEPLOYMENT, current)
    if deployment_age is not None and deployment_age > DEPLOYMENT_MAX_AGE:
        errors.append(
            f"{DEPLOYMENT}: stale ({deployment_age:.0f}s > {DEPLOYMENT_MAX_AGE}s)")
    verdict = re.search(r"\*\*(PASS|FAIL)\*\*\s+—\s+(\d+) fail", deployment)
    if not verdict:
        errors.append("deployment validation has no parseable verdict")
    else:
        failed = verdict.group(1) == "FAIL" or int(verdict.group(2)) > 0
        unhealthy = set(lanes.get("errored") or []).union(lanes.get("timed-out") or [])
        classified = set().union(*(set(lanes.get(key) or []) for key in LANE_KEYS))
        if "deployment-validate" not in classified:
            errors.append("deployment-validate is absent from fleet-health classifications")
        elif failed and "deployment-validate" not in unhealthy:
            errors.append("deployment validation failure is not visible as an unhealthy fleet lane")
        elif not failed and "deployment-validate" in unhealthy:
            errors.append("deployment validation is PASS but fleet-health classifies it unhealthy")
        elif not failed and "deployment-validate" in (lanes.get("undetectable") or []):
            errors.append("deployment validation is PASS but fleet-health cannot see its verdict")

    metrics = _read(METRICS, errors)
    metrics_age = _age(METRICS, current)
    if metrics_age is not None and metrics_age > METRICS_MAX_AGE:
        errors.append(f"{METRICS}: stale ({metrics_age:.0f}s > {METRICS_MAX_AGE}s)")
    for metric in ("okengine_health_export_timestamp_seconds", "okengine_health_monitor_stale"):
        if not re.search(rf"^{metric}\s+\d+(?:\.\d+)?$", metrics, re.MULTILINE):
            errors.append(f"Prometheus surface is missing {metric}")
    return errors


def _write(errors: List[str]) -> None:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    status = "FAIL" if errors else "PASS"
    lines = [
        "---", "type: dashboard", 'title: "Observability validation"',
        f"updated: {now}", "---", "", f"# Observability validation — {now}", "",
        f"**{status}** — {len(errors)} error(s)", "",
    ]
    lines.extend((f"- {error}" for error in errors) if errors else [
        "- Fleet dashboard and identity sidecar are fresh, non-empty, and consistent.",
        "- Search and maintenance telemetry are fresh, distinct, and reflected in fleet health.",
        "- Deployment validation is fresh and its verdict is visible in fleet health.",
        "- Prometheus export and monitor heartbeat are fresh and parseable.",
    ])
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    errors = validate()
    try:
        _write(errors)
    except OSError as exc:
        print(f"observability-validation: cannot publish result: {exc}", file=sys.stderr)
        return 2
    print(f"observability-validation: {'FAIL' if errors else 'PASS'} ({len(errors)} error(s))")
    for error in errors:
        print(f"  {error}")
    print(json.dumps({"wakeAgent": False, "errors": len(errors)}))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
