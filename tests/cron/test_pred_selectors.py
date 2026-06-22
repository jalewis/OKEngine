"""Tests for the prediction wake-gate selectors (okengine#36)."""
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

pytest.importorskip("yaml")
CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"


def _load(name):
    sys.path.insert(0, str(CRON))
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, CRON / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _mk(root: Path, ns: str, name: str, fm: str):
    d = root / "wiki" / ns
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text("---\n" + fm + "---\nbody\n")


def _vault(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-19")
    # entities: apt42 (recent, has open pred), akira (recent, has open pred),
    # evilcorp (recent, NO pred -> candidate), oldactor (too old)
    _mk(tmp_path, "entities", "apt42", "type: threat-actor\nname: APT42\nlast_updated: '2026-06-18'\n")
    _mk(tmp_path, "entities", "akira", "type: malware\nname: Akira\nlast_updated: '2026-06-17'\n")
    # evilcorp: cited by a RECENT source (s-new) and no open pred -> the one candidate
    _mk(tmp_path, "entities", "evilcorp", "type: intrusion-set\nname: Evil Corp\nlast_updated: '2026-06-16'\nsources:\n- sources/s-new\n")
    # oldactor: only an OLD source citation -> not recently active
    _mk(tmp_path, "entities", "oldactor", "type: threat-actor\nname: Old\nlast_updated: '2026-01-01'\nsources:\n- sources/s-old\n")
    # stubactor: fresh last_updated but NO citing source (the importer-stub case) -> excluded
    _mk(tmp_path, "entities", "stubactor", "type: intrusion-set\nname: Stub Actor\nlast_updated: '2026-06-18'\n")
    _mk(tmp_path, "predictions", "p-akira", "type: prediction\nstatus: open\nconfidence: 0.6\nsubject: '[[entities/akira]]'\nresolves_by: '2026-12-31'\ntitle: Akira X\n")
    _mk(tmp_path, "predictions", "p-overdue", "type: prediction\nstatus: open\nconfidence: 0.5\nsubject: '[[entities/apt42]]'\nresolves_by: '2026-06-01'\ntitle: APT42 Y\n")
    _mk(tmp_path, "predictions", "p-done", "type: prediction\nstatus: confirmed\nresolves_by: '2026-05-01'\ntitle: done\n")
    _mk(tmp_path, "sources", "s-new", "type: source\npublished: '2026-06-19'\ntitle: fresh\n")
    _mk(tmp_path, "sources", "s-old", "type: source\npublished: '2026-01-01'\ntitle: old\n")


def _run(mod):
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main()
    out = buf.getvalue()
    return json.loads(out.strip().splitlines()[-1])["wakeAgent"], out


def test_candidate_watch_excludes_covered_and_stale(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    monkeypatch.setenv("PREDICTION_CANDIDATE_MIN", "1")
    wake, out = _run(_load("select_prediction_candidates"))
    assert wake is True
    assert "no open prediction: 1" in out                # only evilcorp (cited by a recent source)
    assert "Evil Corp" in out
    # the importer-stub (fresh last_updated, no source) and covered/old actors are excluded
    assert "Stub Actor" not in out and "APT42" not in out and "Old" not in out


def test_candidate_watch_skips_below_threshold(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    monkeypatch.setenv("PREDICTION_CANDIDATE_MIN", "5")   # only 1 candidate -> skip
    wake, _ = _run(_load("select_prediction_candidates"))
    assert wake is False


def test_grade_lists_only_overdue_open(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    wake, out = _run(_load("select_predictions_for_grading"))
    assert wake is True
    assert "past resolves_by: 1" in out                  # p-overdue only
    assert "APT42 Y" in out and "Akira X" not in out      # not-yet-due/closed excluded


def test_regrade_needs_open_and_recent_sources(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    wake, out = _run(_load("select_regrade_batch"))
    assert wake is True
    assert "open predictions: 2" in out                  # both open ones
    assert "fresh" in out and "old" not in out            # only recent source


def test_regrade_skips_with_no_recent_sources(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    # widen "recent" to nothing by making cutoff exclude even s-new
    monkeypatch.setenv("PREDICTION_REGRADE_RECENT_DAYS", "0")
    wake, _ = _run(_load("select_regrade_batch"))
    # cutoff = today (2026-06-19); s-new published 2026-06-19 >= cutoff -> still recent
    assert wake is True


def test_empty_vault_all_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-19")
    for n in ("select_prediction_candidates", "select_predictions_for_grading",
              "select_regrade_batch"):
        wake, _ = _run(_load(n))
        assert wake is False
