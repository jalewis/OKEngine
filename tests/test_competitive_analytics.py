"""okengine.competitive-analytics (#146) — generic competitive analytics, watchlist as pack config.

Guards: the manifest shape, the deleak (NO competitor seeds shipped), and the two distinct
selector behaviours (watchlist-driven quadrants; market-wide acquirer signals).
"""
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extensions" / "okengine.competitive-analytics"


def _write(p: Path, body: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _run(script: str, env: dict) -> str:
    return subprocess.run(
        [sys.executable, str(EXT / script)],
        env={**env, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
    ).stdout


def test_manifest_shape():
    m = yaml.safe_load((EXT / "extension.yaml").read_text())
    assert m["id"] == "okengine.competitive-analytics"
    assert m["trust"] == "in-gateway"
    assert "tier" not in m  # unsupported manifest keys must not survive as ignored decoration
    assert set(m["operations"]) == {"competitor-quadrants", "sector-battle-cards", "acquirer-signals", "discover-competitors"}
    assert "watchlist_path" in m["config"]
    assert any("dashboards/" in w for w in m["capabilities"]["write"])


def test_ships_no_competitor_seeds():
    # the deleak: the extension must carry NO watchlist / seed data files
    bad = [p for p in EXT.rglob("*")
           if p.suffix in (".yaml", ".yml") and p.name != "extension.yaml"]
    bad += [p for p in EXT.rglob("*watchlist*")]
    assert not bad, f"competitive-analytics must ship no seeds, found: {bad}"


def test_quadrants_skips_without_watchlist(tmp_path):
    out = _run("select_competitor_quadrants.py",
               {"WIKI_PATH": str(tmp_path), "WATCHLIST_PATH": str(tmp_path / "nope.yaml")})
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_quadrants_marshals_segment(tmp_path):
    _write(tmp_path / "wiki/entities/o/openai.md",
           "---\ntype: lab\ntitle: OpenAI\nupdated: 2026-06-25\n---\n# OpenAI\n- shipped a new model\n")
    wl = tmp_path / "wl.yaml"
    wl.write_text(yaml.safe_dump({"segments": {"llm": {"competitors": ["openai"],
                                                       "axes": {"x": "cap", "y": "adopt"}}}}))
    out = _run("select_competitor_quadrants.py",
               {"WIKI_PATH": str(tmp_path), "WATCHLIST_PATH": str(wl)})
    assert "segment `llm`" in out and "entities/o/openai" in out
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}


def test_acquirer_signals_fires_on_movement(tmp_path):
    for i, kw in enumerate(("acquisition of a rival", "raises a funding round"), 1):
        _write(tmp_path / f"wiki/sources/2026/06/m{i}.md",
               f"---\ntype: source\ntitle: deal {i}\npublished: '2026-06-20'\n---\n"
               f"Big {kw}. [[entities/a/acme]]\n")
    out = _run("select_acquirer_signals.py",
               {"WIKI_PATH": str(tmp_path), "TREND_NOW": "2026-06-26", "ACQUIRER_MIN_HITS": "2"})
    assert "entities/a/acme" in out
    assert json.loads(out.strip().splitlines()[-1]) == {"wakeAgent": True}
