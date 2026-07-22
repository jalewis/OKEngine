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


_MORNING_RE = re.compile(r"^@morning(?::(\d{1,2}))?$")
# The deployment-wide morning hour for reader-facing daily briefs (gateway-local TZ).
# One knob (OKENGINE_BRIEF_HOUR in .env) so ALL brief lanes cluster in the operator's
# morning instead of each lane hardcoding an hour — the recurring "briefs run at the
# wrong time" pain (okengine#177). Default 7 = 07:00 local.
DEFAULT_BRIEF_HOUR = 7


def is_morning_sentinel(expr: str) -> bool:
    return isinstance(expr, str) and bool(_MORNING_RE.match(expr.strip()))


def expand_morning_one(expr: str, brief_hour: int) -> str | None:
    """`@morning` / `@morning:MM` -> `MM <brief_hour> * * *` (daily). MM defaults 0.
    The minute lets several morning briefs stagger so a slow local model isn't
    contended (e.g. positioning :00, messaging :15, brief :30, threat :45).
    Returns None if `expr` is not a morning sentinel."""
    m = _MORNING_RE.match((expr or "").strip())
    if not m:
        return None
    minute = int(m.group(1) or 0)
    if not 0 <= minute <= 59:
        # `_MORNING_RE` allows `\d{1,2}` (0-99), so `@morning:75` MATCHES but is out of range. Silently
        # wrapping (75 % 60 = 15) would ship the WRONG minute; instead return None so the caller
        # (expand_brief_jobs) trips its fail-loud "malformed @morning" guard like every other
        # unparseable sentinel, rather than a lane firing 45 minutes off (invariant-audit #351).
        return None
    return f"{minute} {brief_hour % 24} * * *"


def _job_expr(job: dict):
    """The cron expression string from ANY of the three schedule shapes `framework validate`
    accepts (all three are documented in authoring-a-pack.md): a dict schedule
    `{"schedule": {"expr": ...}}`, a BARE STRING schedule `{"schedule": "0 13 * * SUN"}`, or a
    top-level `{"expr": ...}`. Returns the string, or None. Before this, the expanders did
    `(job.get("schedule") or {}).get("expr")`, which raised AttributeError on the string shape
    (aborting the whole cron deploy) and returned None for the top-level shape (silently leaving a
    `@morning`/`@jitter` sentinel unexpanded → cron-plus can't parse it → the lane never fires)."""
    sched = job.get("schedule")
    if isinstance(sched, dict):
        expr = sched.get("expr")
    elif isinstance(sched, str):
        expr = sched
    else:
        expr = job.get("expr")
    return expr if isinstance(expr, str) else None


def _set_job_expr(job: dict, expr: str) -> None:
    """Write `expr` back into whichever schedule shape `job` uses (see _job_expr), preserving it."""
    sched = job.get("schedule")
    if isinstance(sched, dict):
        sched["expr"] = expr
    elif isinstance(sched, str):
        job["schedule"] = expr
    elif "expr" in job:
        job["expr"] = expr
    else:
        job["schedule"] = {"expr": expr}


def expand_brief_jobs(jobs: list, brief_hour: int) -> int:
    """Expand every `@morning[:MM]` schedule to the deployment's brief hour, in place.
    Returns the count expanded. Call at DEPLOY time (like expand_jobs for @jitter),
    so the same source ships to every deployment and each picks its own morning."""
    n = 0
    for job in jobs:
        expr = _job_expr(job)
        concrete = expand_morning_one(expr, brief_hour)
        if concrete is not None:
            _set_job_expr(job, concrete)
            n += 1
        elif isinstance(expr, str) and expr.strip().startswith("@morning"):
            # Looks like a @morning sentinel but doesn't match @morning[:MM], so expand_morning_one
            # returned None and the raw sentinel would ship to cron-plus — which can't parse it, so
            # the brief lane silently never fires. Fail loud at the deploy gate, like @jitter above
            # (the @morning path had no guard at any layer — invariant-audit).
            raise ValueError(
                f"malformed @morning schedule {expr!r} — expected @morning or @morning:MM (MM 0-59)")
    return n


def expand_jobs(jobs: list, rng: random.Random | None = None) -> int:
    """Expand every jitter-sentinel schedule in a list of cron job dicts, in
    place. Returns the number of jobs expanded. Each job gets its own random
    minute so a pack's own jobs are also spread out."""
    rng = rng or random.Random()
    n = 0
    for job in jobs:
        expr = _job_expr(job)
        if is_sentinel(expr):
            # Jitter to minutes 1-59, never 0: a :00 minute is the herd-prone case the
            # jitter exists to avoid, and the schedule validator rejects it (okengine#103).
            concrete = expand_one(expr, rng.randint(1, 59))
            _set_job_expr(job, concrete)
            n += 1
        elif isinstance(expr, str) and expr.strip().startswith("@jitter:"):
            # Looks like a jitter sentinel but the base isn't one of the SUPPORTED set, so
            # expand_one() returned None and the raw sentinel would ship to cron-plus — which
            # can't parse it (errors every tick, the lane silently never fires, okengine#107).
            # Fail loud at the deploy/pull gate instead of shipping a dead lane (okengine#178).
            raise ValueError(
                f"unsupported @jitter base in schedule {expr!r} — supported bases: "
                "hourly, 2h, 4h, 6h, 12h, daily, weekly")
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
