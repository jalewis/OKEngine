"""The daily brief's step budget must cover the work its own digest asks for.

At `max_iterations: 12` the brief ran out of steps before writing on three consecutive days
(ai-research, 2026-09-22..24): the model read the listed sources, composed the whole brief, and
stopped at the tool budget. Its response said "I burned the tool-call budget on discovery", and
the run-completion check correctly failed it. okcti passed only because its model batches reads
into fewer steps; the budget cannot depend on that.

Pin the relationship rather than a literal: the digest can list BRIEF_MAX_ITEMS sources, and the
lane needs a step per source read in the worst case (no batching) plus reading the prior brief,
the write, and a verification.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _selector_max_items() -> int:
    text = (REPO / "scripts/cron/select_daily_brief.py").read_text()
    return int(re.search(r'BRIEF_MAX_ITEMS", "(\d+)"', text).group(1))


def test_the_daily_brief_budget_covers_its_listed_sources_plus_the_write():
    jobs = json.loads((REPO / "config/engine-crons.json").read_text())
    brief = next(j for j in jobs if j.get("name") == "daily-brief")
    assert brief["max_iterations"] >= _selector_max_items() + 5, brief["max_iterations"]
