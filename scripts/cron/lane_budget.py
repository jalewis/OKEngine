#!/usr/bin/env python3
"""A lane's own view of the wall-clock it has left.

cron-plus enforces a hard timeout and, with `run-deadline-env.patch`, now PUBLISHES it. Before
that a lane could not see its own ceiling, so its only available failure was being killed
mid-work: everything done so far discarded, no partial result, and a red lane whose output said
nothing about how far it got. Three weekly lanes on one deployment failed that way for a week,
each having burned ~20 minutes of model time per run to produce nothing.

Knowing the deadline turns that into a decision. A lane checks `remaining()` between items, stops
while it still has time to write, and reports what it finished — so a slow week degrades to
"3 of 5 refreshed, budget exhausted" instead of a total loss, and the truncation count becomes the
signal that the input needs re-bounding.

RESERVE is not optional. Stopping at the deadline is stopping too late: the work of writing
results, emitting a receipt, and exiting all happen after the last item.
"""
from __future__ import annotations

import os
import time

# Fraction of the ceiling held back for finishing up. A lane that spends every second on items has
# nothing left to record them with, which is the same outcome as being killed.
DEFAULT_RESERVE_FRACTION = 0.15
MIN_RESERVE_SECONDS = 20.0


def deadline_epoch() -> float | None:
    """Absolute epoch this run must finish by, or None when nothing published one."""
    raw = os.environ.get("OKENGINE_RUN_DEADLINE_EPOCH", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            return None
    # A fresh subprocess may only have the duration; treat it as starting now, which UNDERSTATES
    # the elapsed time and therefore never over-promises.
    raw = os.environ.get("OKENGINE_RUN_TIMEOUT_SECONDS", "").strip()
    try:
        return time.time() + float(raw) if raw else None
    except ValueError:
        return None


def reserve_seconds(total: float | None = None) -> float:
    if total is None:
        raw = os.environ.get("OKENGINE_RUN_TIMEOUT_SECONDS", "").strip()
        try:
            total = float(raw) if raw else 0.0
        except ValueError:
            total = 0.0
    return max(MIN_RESERVE_SECONDS, total * DEFAULT_RESERVE_FRACTION)


def remaining() -> float:
    """Seconds of USABLE budget left, reserve already deducted.

    Returns `inf` when no deadline was published — an unknown ceiling must never be mistaken for a
    ceiling of zero, which would make every lane refuse to do anything the moment this is deployed
    somewhere the patch has not reached.
    """
    end = deadline_epoch()
    if end is None:
        return float("inf")
    return max(0.0, end - time.time() - reserve_seconds())


def exhausted() -> bool:
    return remaining() <= 0.0


def fit_batch(desired: int, per_item_seconds: float) -> int:
    """How many of `desired` items fit in the remaining budget.

    Never returns 0 while any budget remains: a lane that processes nothing produces no evidence of
    what it costs, so the estimate can never improve. One item and an honest truncation beats a
    clean no-op.
    """
    left = remaining()
    if left == float("inf"):
        return desired
    if per_item_seconds <= 0:
        return desired
    return max(1, min(desired, int(left // per_item_seconds))) if left > 0 else 1
