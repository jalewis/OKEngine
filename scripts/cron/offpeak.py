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
import sys
from datetime import datetime, timezone


def _spec_has_valid_window(spec: str) -> bool:
    """True if `spec` contains at least ONE parseable hour value/range. A non-empty spec that parses
    to ZERO valid parts (wrong separator like ';', an HH:MM value, an en-dash) would make
    in_defer_window return False for every hour — silently NEVER deferring, the opposite of intent
    (invariant-audit #19, same silent-never-defers class as the #178 wraparound)."""
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                # a==b is a DEGENERATE range: in_defer_window treats it as empty (neither the a<b nor
                # the a>b wrap branch fires), so a spec of only '9-9' would validate here yet defer no
                # hour — the very "silently never defers" class this guard exists to catch (require
                # a != b so an all-degenerate spec trips offpeak_defer's loud warning — invariant-audit #351).
                if a != b and 0 <= a <= 23 and 0 <= b <= 24:
                    return True
            elif 0 <= int(part) <= 23:
                return True
        except ValueError:
            continue
    return False


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
    if not _spec_has_valid_window(spec):
        # A non-empty but unparseable spec would silently never defer (bulk drains run at full peak
        # price — the opposite of intent). Fail LOUD rather than silent (invariant-audit #19).
        sys.stderr.write(
            f"offpeak: CRON_DEFER_UTC_HOURS={spec!r} has no valid UTC-hour window "
            f"(expected e.g. '1-4,6-10', hours 0-23, comma-separated) — NOT deferring; fix the spec.\n")
        return False
    h = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).hour
    return in_defer_window(h, spec)
