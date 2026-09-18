import importlib.util
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


MODULE = Path(__file__).parents[2] / "patches" / "cron-plus" / "run_timeout.py"
spec = importlib.util.spec_from_file_location("run_timeout", MODULE)
timeout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timeout)


def test_default_and_job_override():
    assert timeout.run_timeout_seconds({}) == 1200
    assert timeout.run_timeout_seconds({"timeout": 17}) == 17
    assert timeout.run_timeout_seconds({"timeout": "bad"}) == 1200


def test_deadline_interrupts_stuck_work():
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="hard timeout of 1s"):
        with timeout.run_deadline({"timeout": 1}):
            time.sleep(5)
    assert time.monotonic() - started < 2
