"""Hard wall-clock deadline for one cron-plus runner invocation."""
from __future__ import annotations

import os
import signal
from contextlib import contextmanager


def run_timeout_seconds(job: dict) -> int:
    raw = job.get("timeout", os.environ.get("OKENGINE_AGENT_RUN_TIMEOUT_SECONDS", "1200"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1200
    return max(1, value)


@contextmanager
def run_deadline(job: dict):
    """Interrupt a stuck selector/model/tool loop and let runner record failure."""
    seconds = run_timeout_seconds(job)
    previous = signal.getsignal(signal.SIGALRM)
    previous_mask = None
    if hasattr(signal, "pthread_sigmask"):
        # Test runners and embedding hosts can leave SIGALRM blocked.  An armed
        # timer then expires silently, defeating the hard wall-clock contract.
        previous_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})

    def _expired(_signum, _frame):
        raise TimeoutError(f"cron-plus run exceeded hard timeout of {seconds}s")

    signal.signal(signal.SIGALRM, _expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
