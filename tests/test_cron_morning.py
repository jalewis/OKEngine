"""@morning schedule sentinel — one deployment knob for when daily briefs run (#177).

Brief lanes ship `@morning[:MM]`; it expands at deploy to `MM <BRIEF_HOUR> * * *`
so every deployment clusters its reader-facing briefs in the operator's morning
without forking any schedule. Mirrors the @jitter sentinel."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("cron_jitter", REPO / "scripts" / "cron_jitter.py")
cj = importlib.util.module_from_spec(spec); sys.modules["cron_jitter"] = cj; spec.loader.exec_module(cj)


def test_morning_expands_to_the_brief_hour():
    assert cj.expand_morning_one("@morning", 7) == "0 7 * * *"
    assert cj.expand_morning_one("@morning:30", 7) == "30 7 * * *"
    assert cj.expand_morning_one("@morning:15", 6) == "15 6 * * *"
    assert cj.expand_morning_one("@morning:45", 8) == "45 8 * * *"


def test_non_morning_expr_untouched():
    assert cj.expand_morning_one("0 12 * * *", 7) is None
    assert cj.expand_morning_one("@jitter:daily", 7) is None
    assert not cj.is_morning_sentinel("30 7 * * *")
    assert cj.is_morning_sentinel("@morning:30")


def test_expand_brief_jobs_in_place_and_counts():
    jobs = [
        {"name": "daily-brief", "schedule": {"kind": "cron", "expr": "@morning:30"}},
        {"name": "messaging", "schedule": {"kind": "cron", "expr": "@morning:15"}},
        {"name": "index", "schedule": {"kind": "cron", "expr": "0 1 * * *"}},   # untouched
    ]
    n = cj.expand_brief_jobs(jobs, 7)
    assert n == 2
    assert jobs[0]["schedule"]["expr"] == "30 7 * * *"
    assert jobs[1]["schedule"]["expr"] == "15 7 * * *"
    assert jobs[2]["schedule"]["expr"] == "0 1 * * *"


def test_engine_daily_brief_ships_the_sentinel():
    """The engine's own daily-brief stub must use @morning (not a hardcoded hour),
    so a fresh deployment gets morning briefs by default."""
    import json
    crons = json.loads((REPO / "config" / "engine-crons.json").read_text())
    brief = next(j for j in crons if j["name"] == "daily-brief")
    assert brief["schedule"]["expr"] == "@morning:30", brief["schedule"]


def test_malformed_morning_fails_loud():
    """A @morning-prefixed but unexpandable expr (a typo) must fail loud at the deploy expander —
    else the raw sentinel ships to cron-plus and the brief lane silently never fires (invariant-audit).
    The sibling @jitter path already raises; @morning had no guard at any layer."""
    import pytest
    with pytest.raises(ValueError, match="malformed @morning"):
        cj.expand_brief_jobs([{"id": "x", "schedule": {"expr": "@morning:7:30"}}], 7)
    # well-formed @morning still expands; a plain cron is left untouched
    jobs = [{"id": "a", "schedule": {"expr": "@morning:30"}}, {"id": "b", "schedule": {"expr": "0 9 * * *"}}]
    assert cj.expand_brief_jobs(jobs, 7) == 1
    assert jobs[0]["schedule"]["expr"] == "30 7 * * *"
    assert jobs[1]["schedule"]["expr"] == "0 9 * * *"


def test_page_quality_enrich_off_the_morning_stagger():  # invariant-audit #54
    """The @morning stagger reserves :00/:15/:30/:45 at BRIEF_HOUR (default 7) so slow-local-model
    agent lanes don't contend. page-quality-enrich (an agent lane) was pinned to `0 7 * * *` —
    byte-identical to @morning:0 (positioning) and concept-backfill's minute-0. It must sit clear."""
    import json
    d = json.loads((REPO / "config" / "engine-crons.json").read_text())
    def _expr(j):
        s = j.get("schedule")
        return s.get("expr") if isinstance(s, dict) else s
    pqe = next(j for j in d if j.get("name") == "page-quality-enrich")
    pqe_expr = _expr(pqe)
    assert pqe_expr != "0 7 * * *", "page-quality-enrich collides with the @morning:0 slot"
    # not on ANY morning-stagger slot (minute 0/15/30/45 at BRIEF_HOUR default 7)
    assert pqe_expr not in {f"{mm} 7 * * *" for mm in (0, 15, 30, 45)}, \
        f"page-quality-enrich sits on a @morning stagger slot ({pqe_expr})"
    # and its expr isn't shared by any OTHER enabled engine lane (no new collision from the move)
    others = [_expr(j) for j in d if j.get("enabled", True) and j.get("name") != "page-quality-enrich"]
    assert pqe_expr not in others, f"page-quality-enrich expr {pqe_expr} collides with another lane"


def test_out_of_range_minute_fails_loud():  # invariant-audit #351
    """`@morning:75` MATCHES _MORNING_RE (\\d{1,2}) but 75 > 59. Silently wrapping (75 % 60 = :15)
    would ship the WRONG minute; instead expand_morning_one returns None so expand_brief_jobs trips
    its fail-loud 'malformed @morning' guard — like every other unparseable sentinel."""
    import pytest
    assert cj.expand_morning_one("@morning:75", 7) is None
    with pytest.raises(ValueError, match="malformed @morning"):
        cj.expand_brief_jobs([{"schedule": {"expr": "@morning:75"}}], 7)
    # an in-range minute still expands normally
    assert cj.expand_morning_one("@morning:45", 7) == "45 7 * * *"
