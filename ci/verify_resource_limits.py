#!/usr/bin/env python3
"""Fail closed unless the current CI container has finite cgroup resource ceilings.

The enhanced testing standard requires every ephemeral test container -- not only the services it
launches -- to have explicit memory, swap, CPU, PID, and runtime limits. GitLab Docker-executor
limits live in runner configuration, outside this repository, so CI must verify the effective
cgroup rather than trust that configuration exists.

PID LIMITS ARE A SPECIAL CASE, and getting this wrong made this check unusable (okengine#558).
A container runs in its own cgroup NAMESPACE: `/proc/self/cgroup` reads `0::/` and
`/sys/fs/cgroup/pids.max` reports only the limit set on the container's OWN cgroup. A ceiling
imposed by an ANCESTOR is enforced by the kernel but is structurally INVISIBLE from in here.

That is not hypothetical. gitlab-runner 19.2 has no `pids_limit` for the docker executor
(gitlab-runner#37791 is an open feature request; the key parses and is silently dropped), so the
fleet applies the ceiling with `cgroup_parent` pointing at a systemd slice carrying TasksMax=1024.
A job container under that slice is genuinely capped at 1024 -- and still reads 273039 here,
because 273039 is its own unset value. Asserting `pids <= max` therefore FAILS a correctly limited
runner, which is exactly what blocked the original version of this script in CI.

So a finite-but-large PID value is reported as UNDETECTABLE, not as a pass and not as a failure:
we cannot see the ancestor from inside, and claiming either verdict would be a fabrication. Only
"max" -- provably no limit anywhere -- is a hard failure. The authoritative PID check is host-side
(`systemctl show <slice> -p TasksMax` plus the container's CgroupParent); this script covers what
is actually observable from inside the job.

memory.max and cpu.max do NOT have this problem: the docker executor sets them on the container
itself (`memory`/`cpus` in [runners.docker]), so they are directly readable and are asserted hard.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


CGROUP_ROOT = Path("/sys/fs/cgroup")
# Literal avoids an equivalent-looking but catastrophically huge ``12 ** 1024**3`` mutation that
# hangs at import time; this is the exact 12 GiB policy ceiling, not a runtime calculation.
#
# This number must AGREE with `memory` in the runner's config.toml, and the repo cannot see that
# file to check. When they disagree the gate fails on every job that runs it, saying nothing about
# the change that happened to be under test: the runner was raised 8g -> 12g on 2026-08-11 with no
# matching change here, and the next pipeline failed `mutation-targets` and `pack-parity` with
# "memory limit 12884901888 exceeds 8589934592". Raise BOTH or neither.
DEFAULT_MAX_MEMORY_BYTES = 12_884_901_888
DEFAULT_MAX_CPUS = 4.0
DEFAULT_MAX_PIDS = 1024


def _read(root: Path, name: str) -> str:
    try:
        return (root / name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read cgroup v2 {name}: {exc}") from exc


def inspect_limits(root: Path = CGROUP_ROOT) -> dict:
    memory_text = _read(root, "memory.max")
    swap_text = _read(root, "memory.swap.max")
    cpu_text = _read(root, "cpu.max")
    pids_text = _read(root, "pids.max")
    if "max" in {memory_text, swap_text, pids_text}:
        raise ValueError("memory.max, memory.swap.max, and pids.max must all be finite")
    # NOTE: `pids_text == "max"` is still a hard failure above -- that is provably unbounded at
    # every level. Everything else about PIDs is decided in validate_limits, which cannot see
    # ancestor cgroups and says so rather than guessing.
    try:
        memory = int(memory_text)
        swap = int(swap_text)
        pids = int(pids_text)
        quota_text, period_text = cpu_text.split()
        if quota_text == "max":
            raise ValueError("cpu.max must have a finite quota")
        quota, period = int(quota_text), int(period_text)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "cpu.max must have a finite quota":
            raise
        raise ValueError("cgroup limits must contain valid positive integers") from exc
    if min(memory, period, quota, pids) <= 0 or swap < 0:
        raise ValueError("cgroup limits must be positive (swap may be zero)")
    return {
        "cgroup_version": 2,
        "memory_bytes": memory,
        "swap_bytes": swap,
        "cpu_quota": quota,
        "cpu_period": period,
        "cpus": quota / period,
        "pids": pids,
    }


def validate_limits(limits: dict, *, max_memory: int, max_cpus: float, max_pids: int) -> list[str]:
    errors = []
    if limits["memory_bytes"] > max_memory:
        errors.append(f"memory limit {limits['memory_bytes']} exceeds {max_memory}")
    if limits["swap_bytes"] > limits["memory_bytes"]:
        errors.append("swap allowance exceeds the memory ceiling")
    if limits["cpus"] > max_cpus:
        errors.append(f"CPU limit {limits['cpus']:.3f} exceeds {max_cpus:.3f}")
    return errors


def pid_verdict(limits: dict, *, max_pids: int) -> tuple[str, str]:
    """Classify the PID ceiling as pass / undetectable, never a fabricated verdict.

    Returns (status, message). `status` is "bounded" when a conforming limit is set on THIS
    container's own cgroup, and "undetectable" when the value we can see is larger than policy --
    because an ancestor slice may still cap it and a cgroup namespace hides that (okengine#558).
    Undetectable is deliberately NOT an error: failing here rejects a correctly limited runner,
    and passing here would claim a limit we never observed.
    """
    pids = limits["pids"]
    if pids <= max_pids:
        return "bounded", f"PID limit {pids} <= {max_pids} on this container's own cgroup"
    return "undetectable", (
        f"PID ceiling not observable from inside this container: own cgroup reports {pids} "
        f"(> policy {max_pids}), and any ancestor limit is hidden by the cgroup namespace. "
        f"Verify host-side: docker inspect -f '{{{{.HostConfig.CgroupParent}}}}' <job-container> "
        f"and systemctl show <slice> -p TasksMax"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=CGROUP_ROOT)
    parser.add_argument("--max-memory", type=int, default=int(os.environ.get(
        "OKENGINE_CI_MAX_MEMORY_BYTES", DEFAULT_MAX_MEMORY_BYTES)))
    parser.add_argument("--max-cpus", type=float, default=float(os.environ.get(
        "OKENGINE_CI_MAX_CPUS", DEFAULT_MAX_CPUS)))
    parser.add_argument("--max-pids", type=int, default=int(os.environ.get(
        "OKENGINE_CI_MAX_PIDS", DEFAULT_MAX_PIDS)))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    try:
        limits = inspect_limits(args.root)
        errors = validate_limits(limits, max_memory=args.max_memory,
                                 max_cpus=args.max_cpus, max_pids=args.max_pids)
        pid_status, pid_message = pid_verdict(limits, max_pids=args.max_pids)
    except ValueError as exc:
        print(f"FAIL: CI resource limits are not enforceable: {exc}")
        return 1
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    # Surface the PID verdict explicitly. An UNDETECTABLE ceiling must be visible in the evidence
    # record and on stdout -- a silent omission here is how "we checked" becomes indistinguishable
    # from "we could not check" in a release audit (okengine#558).
    if pid_status != "bounded":
        print(f"WARN: {pid_message}")
    payload = {**limits, "status": "bounded", "pid_status": pid_status, "pid_detail": pid_message}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print("CI resource limits: " + json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
