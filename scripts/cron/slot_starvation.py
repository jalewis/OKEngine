#!/usr/bin/env python3
"""Runs killed by the hard timeout having executed ZERO tool-call turns (okengine#614).

A hard-timeout kill reads as "this lane's work is too big". A kill with zero turns cannot mean
that: the lane never received a single model response, so it did no work to be too big for. It
spent its entire ceiling queuing for an inference slot. That is a CAPACITY signal wearing a
timeout's error message.

Nothing reported it. On one deployment 1,558 such runs across 42 lanes accumulated over roughly
six weeks, at a near-uniform 19-20% kill rate across unrelated scripts -- the signature of one
shared endpoint, not of per-lane work. A killed run leaves no artifact, so its absence is
indistinguishable from "nothing to report", which is why the pile-up went unseen.

This lives in ONE place because it has two consumers with different cadences:

  * `fleet_health.py` -- the scheduled no_agent lane. This is what makes the class permanently
    watched rather than rediscovered.
  * `fleet_status.py` -- the operator-invoked view, which is streamed into the gateway on stdin
    and so has no importable siblings; it resolves this module by path.

Two copies of a measurement drift, and a metric that disagrees with itself is worse than one that
is merely absent -- hence a library both call rather than a reimplementation in each.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime


#: A run that could not get an inference slot raises this by name. Matching the message is
#: deliberate: it is the ONLY signal that separates "never reached the model" from "reached it
#: and got nothing", and those two need opposite remedies.
_SLOT_UNAVAILABLE = "model slot unavailable"


def scan(data_dir: str, cutoff: float, agent_lanes: set[str]) -> dict:
    """-> {starved, stalled, killed, runs, measurable, lanes, stalled_lanes}

    TWO failures live here, and conflating them sends the operator at the wrong fix:

    * ``starved``  -- the run never got an inference slot. `model_slot` says so by name, with the
      endpoint identity and the limit. Remedy: capacity (raise model_concurrency, or route lanes
      off that endpoint).
    * ``stalled``  -- the run HELD a slot and produced no tool-call turn before its deadline. The
      model was reachable and answered with nothing usable in the time available. Remedy: latency
      or the lane itself. Raising concurrency does nothing.

    Zero turns was once a fair proxy for starvation, because the slot wait outlived the hard
    timeout and every starved run died as a generic "exceeded hard timeout" with no turns. Capping
    the wait below that ceiling (the fix this detector shipped alongside) means starvation now
    reports itself explicitly -- and the proxy became wrong the moment that landed. Measured on
    five live deployments afterwards: one showed 0 starvation against 15 stalls, and the old
    reading told its operator to raise concurrency, which would have changed nothing.

    `measurable` distinguishes "no run records" from "records say nothing starved". An empty scan
    is UNKNOWN, never a clean bill of health -- reporting 0 for a directory that does not exist is
    how a detector becomes decoration.

    Only lanes in `agent_lanes` can stall: a no_agent lane has no turns to execute, so zero proves
    nothing there and counting it would manufacture a stall on every deterministic script that
    merely overran. Starvation needs no such guard -- a no_agent lane never takes a slot, so it
    can never raise the slot error in the first place.
    """
    out = {"starved": 0, "stalled": 0, "killed": 0, "runs": 0, "measurable": False,
           "lanes": {}, "stalled_lanes": {}}
    pattern = os.path.join(data_dir, "cron-plus", "runs", "*", "*.json")
    # SORTED, not raw glob order. A measurement whose result depends on filesystem iteration
    # order is not reproducible, and the difference is observable: a bug that stops the scan
    # early (rather than skipping one record) shows up or hides depending on which file the
    # directory happened to yield first. Deterministic order makes the failure deterministic too.
    for path in sorted(glob.glob(pattern)):  # glob-ok: fixed cron-plus run layout, not a wiki namespace
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            record = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        out["measurable"] = True
        out["runs"] += 1
        if record.get("status") != "failed":
            continue
        error = str(record.get("error") or "")
        lane = str(record.get("lane") or os.path.basename(os.path.dirname(path)))
        if _SLOT_UNAVAILABLE in error.lower():
            out["starved"] += 1
            out["lanes"][lane] = out["lanes"].get(lane, 0) + 1
            continue
        if "hard timeout" not in error.lower():
            continue
        out["killed"] += 1
        if lane in agent_lanes and record.get("executed_tool_call_turns") == 0:
            out["stalled"] += 1
            out["stalled_lanes"][lane] = out["stalled_lanes"].get(lane, 0) + 1
    return out


def agent_lanes_of(jobs) -> set[str]:
    """Lane names that can hold a model slot. A no_agent lane never does."""
    return {j.get("name") for j in jobs
            if isinstance(j, dict) and j.get("no_agent") is not True and j.get("name")}


def timeout_pressure(data_dir: str, cutoff: float, jobs: list[dict], minimum_runs: int = 20) -> dict:
    """Successful duration p99 versus configured hard timeout, keyed by pressured lane."""
    ceilings = {str(job.get("name")): float(job["timeout"]) for job in jobs
                if isinstance(job, dict) and job.get("name") and job.get("timeout")}
    durations: dict[str, list[float]] = {}
    pattern = os.path.join(data_dir, "cron-plus", "runs", "*", "*.json")
    for path in sorted(glob.glob(pattern)):  # glob-ok: fixed flat run-record layout
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            record = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if record.get("status") != "succeeded":
            continue
        lane = str(record.get("lane") or os.path.basename(os.path.dirname(path)))
        if lane not in ceilings:
            continue
        seconds = record.get("duration_seconds")
        if not isinstance(seconds, (int, float)):
            try:
                start = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00"))
                finish = datetime.fromisoformat(str(record["finished_at"]).replace("Z", "+00:00"))
                seconds = (finish - start).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue
        durations.setdefault(lane, []).append(float(seconds))
    pressured = {}
    for lane, values in durations.items():
        if len(values) < minimum_runs:
            continue
        ordered = sorted(values)
        p99 = ordered[max(0, (99 * len(ordered) + 99) // 100 - 1)]
        ratio = p99 / ceilings[lane]
        if ratio >= 0.9:
            pressured[lane] = {"runs": len(values), "p99": round(p99),
                               "timeout": round(ceilings[lane]), "ratio": ratio}
    return pressured
