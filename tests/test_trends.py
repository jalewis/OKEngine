"""okengine#37 — trends/ namespace synthesis.

The engine ships a generic delta-selector (`select_trend_deltas.py`) as an engine-template lane;
the pack supplies the `trend` type + voice. Guards: the selector detects a rising entity, and the
engine + skeleton wiring is present.
"""
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SEL = ROOT / "scripts" / "cron" / "select_trend_deltas.py"


def _write(p: Path, body: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_selector_detects_a_rising_entity(tmp_path):
    wiki = tmp_path / "wiki"
    # three RECENT sources (this window) + one OLD (prior window)
    for i, d in enumerate(("2026-06-20", "2026-06-21", "2026-06-22"), 1):
        _write(wiki / f"sources/2026/06/s{i}.md", f"---\ntype: source\npublished: '{d}'\n---\n")
    _write(wiki / "sources/2026/04/old.md", "---\ntype: source\npublished: '2026-04-01'\n---\n")
    # an entity citing the three recent sources → rising (3 this, 0 prior)
    _write(wiki / "entities/r/riser.md",
           "---\ntype: model\nsources:\n- '[[sources/2026/06/s1]]'\n- '[[sources/2026/06/s2]]'\n"
           "- '[[sources/2026/06/s3]]'\n---\n# Riser\n")
    # a quiet entity citing only the old source
    _write(wiki / "entities/q/quiet.md",
           "---\ntype: model\nsources:\n- '[[sources/2026/04/old]]'\n---\n# Quiet\n")

    out = subprocess.run(
        [sys.executable, str(SEL)],
        env={"WIKI_PATH": str(tmp_path), "TREND_NOW": "2026-06-26",
             "TREND_MIN_THIS": "2", "TREND_MIN_MOVERS": "1", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    ).stdout
    assert "entities/r/riser" in out, out
    assert "entities/q/quiet" not in out          # quiet entity must NOT be flagged
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}


def test_selector_skips_when_no_movers(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki / "sources/2026/04/old.md", "---\ntype: source\npublished: '2026-04-01'\n---\n")
    _write(wiki / "entities/q/quiet.md",
           "---\ntype: model\nsources:\n- '[[sources/2026/04/old]]'\n---\n# Quiet\n")
    out = subprocess.run(
        [sys.executable, str(SEL)],
        env={"WIKI_PATH": str(tmp_path), "TREND_NOW": "2026-06-26", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    ).stdout
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_engine_ships_trends_lane():
    crons = json.loads((ROOT / "config" / "engine-crons.json").read_text())
    jobs = crons["jobs"] if isinstance(crons, dict) else crons
    tr = next((j for j in jobs if j["name"] == "trends-refresh"), None)
    assert tr and tr["script"] == "select_trend_deltas.py"
    tiers = yaml.safe_load((ROOT / "config" / "cron-tiers.yaml").read_text())
    assert "trends-refresh" in tiers["engine-template"]


def test_skeleton_scaffolds_trend():
    s = yaml.safe_load((ROOT / "templates/pack/skeleton/schema.yaml").read_text())
    # the `trend` TYPE is core (engine-owned, okengine#90); the skeleton supplies the trends
    # namespace + the ANALYSIS rail + the trends-refresh prompt.
    import sys
    sys.path.insert(0, str(ROOT / "scripts" / "cron"))
    import schema_lib
    assert "trend" in schema_lib.base_schema()["types"]
    assert "trends" in s["partitioning"]["namespaces"]
    assert "trends" in (s["rail_top_section"]["namespaces"])
    prompts = json.loads((ROOT / "templates/pack/skeleton/crons/engine-template-prompts.json").read_text())
    assert "trends-refresh" in prompts and "trend" in prompts["trends-refresh"].lower()
