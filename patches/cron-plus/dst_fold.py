"""Fall-back fold policy for cron-plus ``compute_next_run`` (okengine#1055, okengine#1061 [4]).

When clocks fall back, one wall-clock hour happens twice (01:00-01:59 EDT, then again as EST in
America/New_York). croniter, given a base inside or before that hour, can return a slot in the
second pass. Whether that slot should fire depends on the hour field, as in Vixie cron and cronie:

- a star hour (``*`` or ``*/N``) means "every matching hour", and the second 01:12 EST is a real
  hour an hour after 01:12 EDT, so it fires;
- an explicit hour (``1``, ``1-23/2``, ``0-23``) names a wall-clock time, which happens once; the
  second pass is the same wall time again and is skipped.

The decision depends only on the candidate slot, never on when the previous run was claimed. The
guard this replaces compared against the claim time, so a claim one minute late re-fired the slot
(#1055), and it also dropped the genuine second-pass fire of hourly lanes (#1061 [4]).
"""
from __future__ import annotations

from datetime import datetime, timezone

#: croniter aliases that schedule every hour; every other alias names fixed hours.
_HOURLY_ALIASES = frozenset({"@hourly"})
#: A repeated wall-clock span is at most a few hours of minutes (seconds with a 6-field expr):
#: four hours of seconds. The bound only stops a runaway loop on a broken tz database.
MAX_SKIPS = 14_400


def hour_field_is_star(expr: str) -> bool:
    """True when ``expr`` fires every matching hour (hour field starts with ``*``, or @hourly)."""
    text = (expr or "").strip().lower()
    if text.startswith("@"):
        return text in _HOURLY_ALIASES
    fields = text.split()
    return len(fields) >= 2 and fields[1].startswith("*")


def is_repeated_wall_time(moment: datetime) -> bool:
    """True when ``moment``'s wall time occurs twice in its zone and ``moment`` is the second pass."""
    zone = moment.tzinfo
    if zone is None:
        return False
    wall = moment.replace(tzinfo=None)
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    # A repeat is a fall-back: the first pass carries the larger offset. Equal offsets mean the
    # wall time happens once; a smaller first offset means it is a spring-forward gap (never twice).
    if first.utcoffset() <= second.utcoffset():
        return False
    return moment.astimezone(timezone.utc) == second.astimezone(timezone.utc)


def next_fire(it, expr: str) -> datetime:
    """The next slot from croniter iterator ``it``, skipping second-pass wall times for an
    explicit-hour ``expr``. Raises RuntimeError if the skip bound is exhausted."""
    nxt = it.get_next(datetime)
    if hour_field_is_star(expr):
        return nxt
    for _ in range(MAX_SKIPS):
        if not is_repeated_wall_time(nxt):
            return nxt
        nxt = it.get_next(datetime)
    raise RuntimeError(f"cron expr {expr!r}: no slot outside a repeated wall-clock span "
                       f"within {MAX_SKIPS} candidates")
