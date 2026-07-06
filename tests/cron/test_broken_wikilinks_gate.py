"""broken-wikilinks-drain wake gate — the briefing exemption from MIN_INBOUND.

Live incident (okcti, 2026-07-06): the daily brief shipped 4 invented-slug links; each had
exactly ONE inbound reference, so the >=3-inbound gate classified them as orphan noise and
the drain never woke to repair the one page a human reads daily. The gate now treats ANY
broken target cited from briefings/ as high-impact, while the sources tree keeps the
threshold (single-orphan links there really are noise at 10k+ pages).

The drain is a stdout wake-gate script (prints a wakeAgent JSON tail line) — run it as a
subprocess against a scratch vault, exactly as cron-plus does.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DRAIN = REPO / "scripts" / "cron" / "select_broken_wikilinks_drain.py"


def _run(vault: Path) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, str(DRAIN)], capture_output=True, text=True,
                       env={"WIKI_PATH": str(vault), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    last = r.stdout.strip().splitlines()[-1]
    return bool(json.loads(last)["wakeAgent"]), r.stdout


def _page(vault: Path, rel: str, body: str) -> None:
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: entity\nname: x\n---\n{body}\n", encoding="utf-8")


def test_single_ref_source_link_stays_below_gate(tmp_path):
    """A lone broken link from a SOURCE page is orphan noise — no wake (threshold intact)."""
    _page(tmp_path, "sources/2026/06/report.md", "mentions [[entities/nope-nothing]]")
    wake, _ = _run(tmp_path)
    assert not wake


def test_single_ref_briefing_link_wakes_the_drain(tmp_path):
    """THE regression: one broken link on a briefing must be high-impact despite 1 inbound."""
    _page(tmp_path, "briefings/daily-2026-07-06.md", "new RAT [[entities/q/quimarat]] spotted")
    wake, out = _run(tmp_path)
    assert wake, out
    assert "entities/q/quimarat" in out


def test_threshold_still_wakes_for_source_pileups(tmp_path):
    """>=MIN_INBOUND (3) citing sources still wakes — the original contract is unchanged."""
    for i in range(3):
        _page(tmp_path, f"sources/2026/06/r{i}.md", "all cite [[entities/missing-actor]]")
    wake, out = _run(tmp_path)
    assert wake, out
