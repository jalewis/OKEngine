"""invariant-audit B6.2 — the nightly LEDGER lanes must stamp the operator's LOCAL calendar day.

`lint_watcher`, `detect_field_loss` and `kb_health` each write a dated ops artifact (a queue-depth
report filed under `today`, a field-loss snapshot row `| {today} | … |`, a health dashboard/history
row). The date is a HUMAN ledger key, not a data comparison — it must be the deployment's local
calendar day. Forcing `datetime.now(timezone.utc)` files "tonight's" report under TOMORROW on the
fleet (America/New_York, behind UTC): a lane running any time after ~20:00 local is already the next
UTC day, so the snapshot row, the report filename and the history key all jump a day and the ledger
desyncs from what the operator sees.

This is a source-contract guard (the bug IS which clock the compute uses), RED before the fix and
GREEN after: the ledger `today` must be naive-local `datetime.now()`, never `datetime.now(utc)` /
`datetime.utcnow()`.
"""
import re
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parent.parent.parent / "scripts" / "cron"

# (file, the assignment token that computes the ledger date). The whole nightly snapshot COHORT
# that kb_health aggregates must agree on the calendar day — a single UTC sibling misaligns the
# trend and can overwrite a prior night's row (invariant-audit B6.2 + completeness sweep).
LEDGER_SITES = [
    ("lint_watcher.py", "today = datetime.now()"),
    ("detect_field_loss.py", "today = datetime.now()"),
    ("kb_health.py", "today = datetime.now()"),
    ("page_quality_audit.py", "today = datetime.now()"),
]


@pytest.mark.parametrize("fname, local_token", LEDGER_SITES)
def test_ledger_today_is_local_not_utc(fname, local_token):
    src = (CRON / fname).read_text()
    # the fix: a naive-local ledger date …
    assert local_token in src, (
        f"{fname}: ledger date should be computed with naive-local `datetime.now()` "
        f"(honors the gateway TZ); found no `{local_token}`")
    # … and NOT a forced-UTC one (the exact regression)
    assert not re.search(r"today\s*=\s*datetime\.now\(\s*timezone\.utc\s*\)", src), (
        f"{fname}: ledger `today` forces UTC — refiles tonight's report under tomorrow on a "
        "TZ-behind-UTC deployment (invariant-audit B6.2)")
    assert not re.search(r"today\s*=\s*datetime\.utcnow\(", src), (
        f"{fname}: ledger `today` uses utcnow() — same UTC-ledger bug")
