#!/usr/bin/env python3
"""Off-peak deferral gate for bulk cron drains — shared by the backfill selectors.

DeepSeek (and similar providers) bill peak UTC hours at a premium (2x from mid-2026). A
high-frequency bulk drain (raw/entity/concept-backfill) that fires across the whole day
crosses those windows. `offpeak_defer()` lets such a drain skip its run during a configured
peak window so it defers to the next off-peak fire — no data loss (the queue just
accumulates), no model call at premium price. cron-plus wakes the agent only on non-empty
stdout, so a deferring selector simply emits nothing and exits 0.

Config: env CRON_DEFER_UTC_HOURS, a comma list of UTC hour ranges/values, half-open
(`a-b` = hours [a, b)). DeepSeek's mid-2026 peak (01:00-04:00 + 06:00-10:00 UTC):
    CRON_DEFER_UTC_HOURS=1-4,6-10
Unset/empty -> never defer (engine default; generic + provider-agnostic, TZ-independent
since the check is in UTC regardless of the deployment's CRON_TZ).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone


def in_defer_window(hour: int, spec: str) -> bool:
    """True if `hour` (0-23 UTC) is in deferral `spec` (comma list; `a-b` half-open [a,b)).

    A range where a>b WRAPS past midnight — `20-6` means [20,24)∪[0,6), the natural way to spell
    an overnight peak window. Without this, `20 <= hour < 6` is unsatisfiable for every hour, so
    an overnight spec silently NEVER defers and bulk drains run at full peak price — the opposite
    of intent, with no error (okengine#178)."""
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                # a<b: [a,b); a>b: overnight wrap [a,24)∪[0,b); a==b: empty
                if (a < b and a <= hour < b) or (a > b and (hour >= a or hour < b)):
                    return True
            elif hour == int(part):
                return True
        except ValueError:
            continue
    return False


def offpeak_defer(now: datetime | None = None) -> bool:
    """True if the current UTC hour is inside CRON_DEFER_UTC_HOURS (caller should defer).
    `now` overridable for tests. Empty/unset env -> always False."""
    spec = os.environ.get("CRON_DEFER_UTC_HOURS", "").strip()
    if not spec:
        return False
    h = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).hour
    return in_defer_window(h, spec)
