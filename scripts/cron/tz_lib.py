"""Deployment-timezone-aware "today"/"now" for CONTENT date-stamping (okengine#301).

The cron scheduler is already TZ-aware (cron-plus honors CRON_TZ/TZ). This is the other half:
the date a script STAMPS onto content — a brief's date, a dashboard header, a `last_updated`,
a due-date — must reflect the deployment's calendar day, not UTC. Raw `date.today()` (the host's
local zone, which in a container is whatever TZ is set to) and `datetime.now(timezone.utc).date()`
(always UTC) both get this wrong: near midnight they name the wrong day for the deployment.

`deployment_today()` / `deployment_now()` read the deployment zone from `TZ` (the same var the
compose skeleton passes to gateway+reader+mcp and that cron-plus falls back to), default to UTC
when unset (the engine's zero-config public default), and are DST-safe via zoneinfo.

Use these for anything a human reads as a date. Do NOT use them where UTC is genuinely correct —
comparing against externally-UTC-dated data (NVD/EPSS/OSV), internal TTLs, or the off-peak defer
window (offpeak.py is deliberately UTC). Those keep `datetime.now(timezone.utc)` WITH a comment.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def deployment_tz() -> timezone | ZoneInfo:
    """The deployment's zone from $TZ (else UTC). Never raises — an unknown TZ falls back to UTC."""
    name = (os.environ.get("TZ") or "").strip()
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return timezone.utc


def deployment_now() -> datetime:
    """Timezone-aware `now` in the deployment zone."""
    return datetime.now(deployment_tz())


def deployment_today() -> date:
    """Today's calendar date in the deployment zone — the date to stamp on human-read content."""
    return deployment_now().date()
