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


def _fixed_hours(expr: str) -> list[int]:
    """Numeric hours a cron expr fires at — handles single hours AND comma lists ('3,9,15,21').
    Non-numeric hour fields (*, */2, ranges) return [] — those aren't fixed-hour jobs."""
    import re
    m = re.match(r"^\S+\s+([\d,]+)\s", str(expr or ""))
    if not m:
        return []
    return [int(h) for h in m.group(1).split(",") if h.isdigit()]


def test_no_fixed_hour_cron_in_dst_transition_window():
    """okengine invariant-audit: the deployment default TZ is America/New_York, whose DST transitions
    fall in 01:00-02:59 (fall-back repeats the 1am hour → a 01:xx job double-fires; spring-forward
    skips 2am → a 02:xx job is silently missed). Engine-shipped fixed-hour crons must avoid that
    window so the nightly derived-index chain runs exactly once. Checks EVERY hour in a comma
    list — the original single-hour regex let '0 1,13 * * *' escape the guard."""
    bad = []
    for j in _JOBS:
        sch = j.get("schedule")
        expr = sch.get("expr") if isinstance(sch, dict) else sch
        if any(h in (1, 2) for h in _fixed_hours(expr)):
            bad.append((j.get("name"), expr))
    assert not bad, f"fixed-hour crons in the DST transition window (01:xx/02:xx): {bad}"


def test_index_tree_rebuilds_intraday():
    """Index freshness is an ENGINE default, not a per-pack workaround: a nightly-only
    build-index-tree meant pages ingested during the day didn't appear in namespace INDEX
    listings until the NEXT morning (hit live on cyber-market, then okcti). The default must
    fire at least every ~6h, with at least one run after the morning content lanes
    (lacuna 06:00 / daily brief 07:30) so same-day pages surface the same morning."""
    job = next(j for j in _JOBS if j.get("name") == "build-index-tree")
    hours = _fixed_hours(job["schedule"]["expr"])
    assert len(hours) >= 4, f"build-index-tree must run intraday (>=4 fixed hours), got {hours}"
    assert any(8 <= h <= 12 for h in hours), \
        f"build-index-tree needs a run after the morning content lanes (08-12), got {hours}"
