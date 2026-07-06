"""Engine cron prompts must not tell agents to hand-write wiki/log.md: it's MCP-reserved (the
write server auto-appends one line per operation) and a file_write of it resolves outside the
vault safe-root and is denied (the /tmp/wiki/log.md failures). Agents end with a response summary
instead. Guards okengine#exec/log-hygiene."""
import json
from pathlib import Path

_C = json.loads((Path(__file__).resolve().parent.parent / "config" / "engine-crons.json").read_text())
_JOBS = _C["jobs"] if isinstance(_C, dict) else _C


def test_no_prompt_instructs_manual_logmd_write():
    offenders = [j.get("name") for j in _JOBS if "Append a `wiki/log.md`" in (j.get("prompt") or "")]
    assert not offenders, f"prompts instruct a futile manual log.md write (MCP auto-logs): {offenders}"


def test_no_fixed_hour_cron_in_dst_transition_window():
    """okengine invariant-audit: the deployment default TZ is America/New_York, whose DST transitions
    fall in 01:00-02:59 (fall-back repeats the 1am hour → a 01:xx job double-fires; spring-forward
    skips 2am → a 02:xx job is silently missed). Engine-shipped fixed-hour crons must avoid that
    window so the nightly derived-index chain runs exactly once."""
    import re
    bad = []
    for j in _JOBS:
        sch = j.get("schedule")
        expr = sch.get("expr") if isinstance(sch, dict) else sch
        m = re.match(r"^\d+\s+(\d+)\s", str(expr or ""))
        if m and int(m.group(1)) in (1, 2):
            bad.append((j.get("name"), expr))
    assert not bad, f"fixed-hour crons in the DST transition window (01:xx/02:xx): {bad}"
