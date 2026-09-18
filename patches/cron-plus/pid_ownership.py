"""Ownership-safe cleanup for cron-plus runner PID records."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def recorded_pid(path: Path) -> int | None:
    """Read current JSON or legacy plain-integer PID record."""
    try:
        raw = path.read_text()
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw.strip()
        if isinstance(value, dict):
            value = value.get("pid")
        return int(value)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None


def remove_if_owned(path: Path, owner_pid: int, logger: Any = None) -> bool:
    """Remove *path* only while it still identifies ``owner_pid``.

    Another runner may have replaced the shared record while this process was
    finishing.  Removing that newer record reopens the scheduler overlap window.
    """
    current = recorded_pid(path)
    if current != owner_pid:
        if current is not None and logger is not None:
            logger.warning(
                "not removing PID file %s: ownership moved from pid=%d to pid=%d",
                path, owner_pid, current,
            )
        return False
    try:
        path.unlink()
        return True
    except (FileNotFoundError, OSError):
        return False
