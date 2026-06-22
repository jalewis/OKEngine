#!/usr/bin/env python3
"""Per-install schedule jitter for domain cron jobs.

Packs ship enabled crons whose `schedule.expr` is a **jitter sentinel** rather
than a concrete cron expression — e.g. `@jitter:2h`. At install time
(`framework init` / `framework pull`) the sentinel is expanded to a concrete
expression with a RANDOM minute, so no two installs fire on the same minute.
This is the herd defense for the moment an operator populates `feeds.opml`:
empty feeds make zero upstream calls, but once feeds are live, jittered
schedules keep thousands of installs from hitting the same publishers at `:00`.

The definition (committed `crons/domain-crons.json`) keeps the sentinel, so a
herd-prone round schedule (`0 */2 * * *`) can never be committed. Expansion is a
one-time, per-install mutation written back into the deployed pack.

Sentinel grammar:  @jitter:<base>   base ∈ {hourly, 2h, 4h, 6h, 12h, daily, weekly}
Local-only crons (e.g. a daily brief that reads the vault) are jittered too —
harmless, and it keeps the validate rule uniform.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

_SENTINEL_RE = re.compile(r"^@jitter:(hourly|2h|4h|6h|12h|daily|weekly)$")

# Default hour (UTC) for once-a-day / weekly jobs. ~09:00 US-Eastern in summer;
# operators retune per timezone. Minute is always jittered.
_DEFAULT_HOUR = 13
_DEFAULT_DOW = 1  # Monday, for weekly


def is_sentinel(expr: str) -> bool:
    return isinstance(expr, str) and bool(_SENTINEL_RE.match(expr.strip()))


def expand_one(expr: str, minute: int, *, hour: int = _DEFAULT_HOUR,
               dow: int = _DEFAULT_DOW) -> str | None:
    """Expand a single `@jitter:<base>` sentinel to a concrete cron expr, or None
    if `expr` is not a sentinel. `minute` must be 0-59."""
    m = _SENTINEL_RE.match((expr or "").strip())
    if not m:
        return None
    base = m.group(1)
    minute %= 60
    return {
        "hourly": f"{minute} * * * *",
        "2h":     f"{minute} */2 * * *",
        "4h":     f"{minute} */4 * * *",
        "6h":     f"{minute} */6 * * *",
        "12h":    f"{minute} */12 * * *",
        "daily":  f"{minute} {hour} * * *",
        "weekly": f"{minute} {hour} * * {dow}",
    }[base]


def expand_jobs(jobs: list, rng: random.Random | None = None) -> int:
    """Expand every jitter-sentinel schedule in a list of cron job dicts, in
    place. Returns the number of jobs expanded. Each job gets its own random
    minute so a pack's own jobs are also spread out."""
    rng = rng or random.Random()
    n = 0
    for job in jobs:
        sched = job.get("schedule") or {}
        expr = sched.get("expr")
        if is_sentinel(expr):
            concrete = expand_one(expr, rng.randint(0, 59))
            sched["expr"] = concrete
            job["schedule"] = sched
            n += 1
    return n


def expand_file(path: str | Path, rng: random.Random | None = None) -> int:
    """Expand jitter sentinels in a domain-crons.json file, writing it back only
    if something changed. Returns the count expanded (0 = no-op). Tolerates a
    missing/empty file."""
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        jobs = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0
    if not isinstance(jobs, list):
        return 0
    n = expand_jobs(jobs, rng)
    if n:
        p.write_text(json.dumps(jobs, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    return n


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        c = expand_file(arg)
        print(f"  jittered {c} cron(s) in {arg}")
