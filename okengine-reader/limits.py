"""okengine-reader request limits — pure-stdlib config + rate limiting.

Kept free of any web-framework import so the policy logic is unit-testable in
isolation (see tests/test_reader_limits.py). app.py builds its config from these
helpers and wraps the expensive endpoints with a RateLimiter + concurrency caps.
"""
from __future__ import annotations

import os
import threading
import time


def flag(name: str, default: bool) -> bool:
    """Read a 0/1 env flag; blank/unset -> default."""
    v = os.environ.get(name)
    return default if v in (None, "") else v == "1"


def intenv(name: str, default: int, lo: int = 0) -> int:
    """Read an int env var, clamped to >= lo; bad/unset -> default."""
    try:
        return max(lo, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


class RateLimiter:
    """Per-key fixed-window (60s) request limiter. `per_min <= 0` disables it
    (always allows). Thread-safe; bounds its own memory by evicting idle keys."""

    def __init__(self, per_min: int):
        self.per_min = per_min
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        if self.per_min <= 0:
            return True
        now = time.monotonic() if now is None else now
        cutoff = now - 60.0
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(hits) >= self.per_min:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 4096:                      # opportunistic cleanup
                for k in [k for k, v in self._hits.items()
                          if not any(t > cutoff for t in v)]:
                    self._hits.pop(k, None)
            return True
