"""Detector for okengine#301 — no hardcoded 'UTC' display label in a cron script.

Content a human reads must carry the DEPLOYMENT timezone, not UTC. The recurring bug was a dashboard/
index footer built as `datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")` — a hardcoded UTC
label that ignores the deployment's `TZ`. The fix is `tz_lib.deployment_now().strftime("... %Z")`
(the zone abbreviation renders as EDT/UTC/… for whatever the deployment is set to).

This guards the class: a `strftime(...)` whose FORMAT-STRING LITERAL contains the bare word `UTC`
(rather than `%Z`) is a hardcoded label and fails here. Internal `datetime.now(timezone.utc)` used for
computation/comparison (recency windows, selection horizons, validation bounds, UTC-native NVD/EPSS
data, the offpeak defer window) is fine and NOT flagged — only the human-visible display label is.
"""
import re
from pathlib import Path

CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"

# a strftime(...) call whose format-string literal contains a bare "UTC" token
_BAD = re.compile(r"""strftime\(\s*["'][^"']*\bUTC\b[^"']*["']""")


def test_no_hardcoded_utc_display_label_in_cron_scripts():
    offenders = []
    for p in sorted(CRON.glob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _BAD.search(line):
                offenders.append(f"{p.name}:{i}: {line.strip()}")
    assert not offenders, (
        "hardcoded 'UTC' display label(s) — use tz_lib.deployment_now().strftime('... %Z') so the "
        "timestamp renders in the deployment timezone (okengine#301):\n  " + "\n  ".join(offenders)
    )
