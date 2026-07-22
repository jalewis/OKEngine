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
    """Numeric hours a cron expr's HOUR field fires at. Handles a single hour, comma lists
    ('3,9,15,21'), ranges ('1-5'), and steps over a range or star ('1-23/2', '*/2') — so a range/step
    expr that fires IN the DST transition window is not silently missed (okengine#326 [28]: the old
    '[\\d,]+' regex returned [] for '0 1-23/2 * * *', letting a job that fires at 01:00 escape the
    guard). A BARE '*' (every hour) returns [] — an every-hour job is not a fixed-hour schedule."""
    import re
    m = re.match(r"^\S+\s+(\S+)\s", str(expr or ""))
    if not m:
        return []
    hours: set[int] = set()
    for part in m.group(1).split(","):
        part = part.strip()
        if part == "*":
            continue                                  # bare '*' = every hour, not a fixed-hour slot
        step, base = 1, part
        if "/" in part:
            base, _, s = part.partition("/")
            if not (s.isdigit() and int(s) >= 1):
                continue
            step = int(s)
        if base == "*":                               # '*/N'
            lo, hi = 0, 23
        elif "-" in base:                             # 'A-B' or 'A-B/N'
            a, _, b = base.partition("-")
            if not (a.isdigit() and b.isdigit()):
                continue
            lo, hi = int(a), int(b)
        elif base.isdigit():
            lo = hi = int(base)
        else:
            continue                                  # unrecognized token — skip this part
        hours.update(range(lo, hi + 1, step))
    return sorted(h for h in hours if 0 <= h <= 23)


def test_fixed_hours_expands_ranges_and_steps():  # okengine#326 [28]
    """The hour-field parser must expand ranges + steps so a range/step expr firing in the DST window
    is caught. Regression for the '[\\d,]+'-only parser that returned [] for '1-23/2'."""
    assert _fixed_hours("0 5 * * *") == [5]
    assert _fixed_hours("0 3,9,15,21 * * *") == [3, 9, 15, 21]
    assert _fixed_hours("0 1-5 * * *") == [1, 2, 3, 4, 5]
    assert _fixed_hours("0 1-23/2 * * *") == [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23]  # includes 1
    assert _fixed_hours("0 */6 * * *") == [0, 6, 12, 18]
    assert _fixed_hours("0 */2 * * *") == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]      # includes 2
    assert _fixed_hours("0 * * * *") == []        # bare '*' every-hour is not a fixed-hour schedule
    assert _fixed_hours("") == [] and _fixed_hours(None) == []


def test_no_fixed_hour_cron_in_dst_transition_window():
    """okengine invariant-audit + #326 [28]: the deployment default TZ is America/New_York, whose DST
    transitions fall in 01:00-02:59 (fall-back repeats the 1am hour → a 01:xx job double-fires;
    spring-forward skips 2am → a 02:xx job is silently missed).

    The cron-plus scheduler now PREVENTS the fall-back double-fire for every expr (jalewis/hermes-cron-plus,
    okengine#326 [28]), so the residual risk is a LOW-FREQUENCY job whose single fire is SKIPPED on
    spring-forward. Flag a job in the window only when it fires infrequently (≤4×/day) — a nightly or
    few-times lane where a missed run actually matters. The every-2h maintenance lanes (`*/2`,
    `1-23/2`; 12×/day) inherently span the window (any 2h step hits hour 1 or 2), are idempotent, and
    are covered by the next run 2h later, so they are exempt. (`_fixed_hours` now expands ranges +
    steps so those step lanes are visible here at all — the [28] detector gap; checks every hour in a
    comma list too, so '0 1,13 * * *' can't escape.)"""
    bad = []
    for j in _JOBS:
        sch = j.get("schedule")
        expr = sch.get("expr") if isinstance(sch, dict) else sch
        hours = _fixed_hours(expr)
        if len(hours) <= 4 and any(h in (1, 2) for h in hours):
            bad.append((j.get("name"), expr))
    assert not bad, f"low-frequency (≤4×/day) crons in the DST transition window (01:xx/02:xx): {bad}"


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
