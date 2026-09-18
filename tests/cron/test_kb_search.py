"""Tier-filter contract for the qmd wrapper."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/cron/kb_search.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_tier_filter_uses_deployment_day_and_walkup_config(tmp_path, monkeypatch):
    module = _load("kb_search_tier_scope")
    wiki = tmp_path / "wiki"
    (wiki / "sec").mkdir(parents=True)
    (wiki / "sec/schema.yaml").write_text("tier: {}\n")
    monkeypatch.setattr(module, "_WIKI", wiki)
    monkeypatch.setattr(module.tz_lib, "deployment_today", lambda: date(2026, 8, 3))
    calls = []
    monkeypatch.setattr(
        module.tier_lib,
        "load_cfg",
        lambda vault, namespace="": calls.append((Path(vault), namespace)) or {
            "hot_days": 7 if namespace else 30,
            "warm_days": 365,
            "namespaces": {"entities": {"date_field": "updated"}},
        },
    )
    monkeypatch.setattr(module.tier_lib, "fm_of", lambda path: {
        "updated": "2026-07-20" if "sec" in path.parts else "2026-08-01"
    })
    output = (
        "qmd://wiki/entities/a/root.md:1\nroot\n\n"
        "qmd://wiki/sec/entities/a/sub.md:1\nsub\n"
    )
    filtered, dropped = module._filter_by_tier(output, {"hot"})
    assert "root.md" in filtered
    assert "sub.md" not in filtered
    assert dropped == 1
    assert calls == [(tmp_path, ""), (tmp_path, "sec")]


def test_tier_filter_fallback_preamble_and_untiered(tmp_path, monkeypatch):
    module = _load("kb_search_filter_edges")
    monkeypatch.setattr(module, "tier_lib", None)
    assert module._filter_by_tier("raw", {"hot"}) == ("raw", 0)

    class Tiers:
        @staticmethod
        def load_cfg(vault, namespace=""):
            return {}

        @staticmethod
        def fm_of(path):
            return {}

        @staticmethod
        def tier_of(rel, fm, cfg, today):
            return None

    monkeypatch.setattr(module, "tier_lib", Tiers)
    monkeypatch.setattr(module.tz_lib, "deployment_today", lambda: date(2026, 8, 4))
    monkeypatch.setattr(module, "_WIKI", tmp_path / "wiki")
    output = ("preamble\n\nqmd://wiki/entities/a.md:1\nbody\n"
              "qmd://wiki/entities/b.md:1\nmore\n")
    filtered, dropped = module._filter_by_tier(output, {"hot"})
    assert filtered == output and dropped == 0


def test_main_success_raw_filter_and_errors(monkeypatch, capsys):
    module = _load("kb_search_main")
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="qmd://wiki/a.md:1\nhit\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "_filter_by_tier", lambda out, tiers: ("filtered\n", 2))
    assert module.main(["--mode", "search", "--limit", "3", "--tier", "HOT,cold", "needle"]) == 0
    out = capsys.readouterr().out
    assert "KB search (search): needle" in out and "2 hit(s) filtered out" in out
    assert calls[0][0] == ["qmd", "search", "needle", "--limit", "3"]
    assert calls[0][1]["timeout"] == 180 and calls[0][1]["env"]["QMD_FORCE_CPU"] == "1"

    monkeypatch.setattr(module, "_filter_by_tier", lambda out, tiers: ("unfiltered\n", 0))
    assert module.main(["--tier", "hot", "needle"]) == 0
    assert "hit(s) filtered out" not in capsys.readouterr().out

    assert module.main(["--raw", "--tier", "hot", "needle"]) == 0
    assert capsys.readouterr().out == "qmd://wiki/a.md:1\nhit\n"

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert module.main(["needle"]) == 2
    assert "not installed" in capsys.readouterr().err
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("qmd", 1))
    )
    assert module.main(["needle"]) == 2
    assert "timed out" in capsys.readouterr().err
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="qmd failed\n"),
    )
    assert module.main(["needle"]) == 2
    assert capsys.readouterr().err == "qmd failed\n"
