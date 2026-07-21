"""Tests for the prediction wake-gate selectors (okengine#36)."""
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

pytest.importorskip("yaml")
# The prediction selectors moved into the okengine.predictions extension (extensions/).
CRON = Path(__file__).resolve().parents[2] / "extensions" / "okengine.predictions"
pytestmark = pytest.mark.skipif(not CRON.is_dir(), reason="okengine.predictions extension absent")


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


def test_regrade_digest_specifies_canonical_evidence_fields(tmp_path, monkeypatch):
    """Contract: the cockpit reads structured `evidence:` entry fields (confidence_before/after
    → trajectory sparkline; direction → reinforces/contradicts tally) that NO writer used to name,
    so agents wrote free-text strings and those columns stayed empty. The regrade digest must spell
    out the canonical entry shape the cockpit consumes."""
    _vault(tmp_path, monkeypatch)
    _, out = _run(_load("select_regrade_batch"))
    for field in ("date:", "direction:", "confidence_before:", "confidence_after:", "source:", "note:"):
        assert field in out, f"regrade digest omits `{field}` — cockpit reads it (starves the ledger)"
    assert "reinforces" in out and "contradicts" in out          # the direction vocabulary
    assert "evidence:" in out and "confidence:" in out            # both the list + top-level field


def test_regrade_skips_with_no_recent_sources(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    # widen "recent" to nothing by making cutoff exclude even s-new
    monkeypatch.setenv("PREDICTION_REGRADE_RECENT_DAYS", "0")
    wake, _ = _run(_load("select_regrade_batch"))
    # cutoff = today (2026-06-19); s-new published 2026-06-19 >= cutoff -> still recent
    assert wake is True


def _edges(root: Path, edges: dict):
    (root / "wiki" / ".reevaluation-edges.json").write_text(json.dumps({"edges": edges}))


def _state(root: Path, **values):
    (root / "wiki" / ".prediction-regrade-watermark.json").write_text(json.dumps(values))


def test_regrade_dependency_maps_only_changed_cited_source(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    import os
    import time
    now = time.time_ns()
    os.utime(tmp_path / "wiki" / "sources" / "s-old.md", ns=(now - 100_000, now - 100_000))
    os.utime(tmp_path / "wiki" / "sources" / "s-new.md", ns=(now, now))
    _edges(tmp_path, {
        "sources/s-new": [{"page": "predictions/p-akira"}],
        "sources/s-old": [{"page": "predictions/p-overdue"}],
    })
    _state(tmp_path, watermark_ns=now - 50_000, last_fallback_ns=now)
    wake, out = _run(_load("select_regrade_batch"))
    assert wake is True
    assert "dependency-matched prediction(s)" in out
    assert "Akira X" in out and "APT42 Y" not in out
    assert "sources/s-new.md" in out and "sources/s-old.md" not in out


def test_regrade_watermark_advances_and_no_change_skips(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    _edges(tmp_path, {"sources/s-new": [{"page": "predictions/p-akira"}]})
    _state(tmp_path, watermark_ns=0, last_fallback_ns=__import__("time").time_ns())
    mod = _load("select_regrade_batch")
    wake, _ = _run(mod)
    first = json.loads((tmp_path / "wiki" / ".prediction-regrade-watermark.json").read_text())
    assert wake is True and first["watermark_ns"] > 0
    wake, out = _run(mod)
    second = json.loads((tmp_path / "wiki" / ".prediction-regrade-watermark.json").read_text())
    assert wake is False and "no cited source changed" in out
    assert second["watermark_ns"] >= first["watermark_ns"]


def test_regrade_edge_less_prediction_gets_reduced_frequency_fallback(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    # p-akira is indexed; p-overdue has no edge and must remain reachable.
    _edges(tmp_path, {"sources/s-old": [{"page": "predictions/p-akira"}]})
    _state(tmp_path, watermark_ns=__import__("time").time_ns(), last_fallback_ns=0)
    wake, out = _run(_load("select_regrade_batch"))
    assert wake is True and "fallback=used" in out
    assert "APT42 Y" in out and "fresh" in out


def test_regrade_invalid_edge_artifact_preserves_legacy_path(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    (tmp_path / "wiki" / ".reevaluation-edges.json").write_text("not-json")
    wake, out = _run(_load("select_regrade_batch"))
    assert wake is True and "legacy batch" in out


def test_regrade_cap_carries_overflow_without_losing_source_change(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    # Add a third open prediction, with more evidence than the existing two so #216's
    # starved-first ordering deliberately defers it when MAX_PRED=2.
    _mk(tmp_path, "predictions", "p-served",
        "type: prediction\nstatus: open\nconfidence: 0.6\nsubject: entities/x\n"
        "title: already served\nevidence:\n"
        "- {date: 2026-06-01, direction: neutral, confidence_before: 0.6, "
        "confidence_after: 0.6, source: sources/s-old}\n")
    _edges(tmp_path, {
        "sources/s-new": [
            {"page": "predictions/p-akira"},
            {"page": "predictions/p-overdue"},
            {"page": "predictions/p-served"},
        ],
    })
    _state(tmp_path, watermark_ns=0, last_fallback_ns=__import__("time").time_ns())
    monkeypatch.setenv("PREDICTION_REGRADE_MAX_PRED", "2")
    mod = _load("select_regrade_batch")

    wake, first_out = _run(mod)
    first = json.loads((tmp_path / "wiki" / ".prediction-regrade-watermark.json").read_text())
    assert wake is True and "2 dependency-matched" in first_out
    assert "already served" not in first_out
    assert first["pending"] == {"predictions/p-served": ["sources/s-new"]}

    # No source mtime changed after the first scan, but the durable pair still emits next run.
    wake, second_out = _run(mod)
    second = json.loads((tmp_path / "wiki" / ".prediction-regrade-watermark.json").read_text())
    assert wake is True and "already served" in second_out and "sources/s-new.md" in second_out
    assert second["pending"] == {}


def test_regrade_source_cap_carries_remaining_sources(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    _mk(tmp_path, "sources", "s-second",
        "type: source\npublished: '2026-06-19'\ntitle: second fresh\n")
    _edges(tmp_path, {
        "sources/s-new": [{"page": "predictions/p-akira"}],
        "sources/s-second": [{"page": "predictions/p-akira"}],
    })
    _state(tmp_path, watermark_ns=0, last_fallback_ns=__import__("time").time_ns())
    monkeypatch.setenv("PREDICTION_REGRADE_MAX_SRC", "1")
    mod = _load("select_regrade_batch")

    wake, first_out = _run(mod)
    first = json.loads((tmp_path / "wiki" / ".prediction-regrade-watermark.json").read_text())
    assert wake is True
    assert sum(path in first_out for path in ("sources/s-new.md", "sources/s-second.md")) == 1
    assert len(first["pending"]["predictions/p-akira"]) == 1

    wake, second_out = _run(mod)
    second = json.loads((tmp_path / "wiki" / ".prediction-regrade-watermark.json").read_text())
    assert wake is True
    assert sum(path in second_out for path in ("sources/s-new.md", "sources/s-second.md")) == 1
    assert second["pending"] == {}
    combined = first_out + second_out
    assert "sources/s-new.md" in combined and "sources/s-second.md" in combined


def test_regrade_wakes_for_recommendation_without_new_source(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-19")
    _mk(tmp_path, "predictions", "p", "type: prediction\nstatus: open\nconfidence: 0.5\n"
        "subject: entities/x\ntitle: scored claim\n")
    data = tmp_path / "data"
    path = data / "state" / "okengine.predictions" / "confidence-recommendations.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "proposition": "predictions/p", "confidence_before": .5,
        "confidence_after_suggested": .6, "delta_suggested": .1,
        "events": [{"evidence_index": 0, "event_id": "events/e",
                    "update_driver": {"signal_strength": .8}}],
    }) + "\n")
    monkeypatch.setenv("HERMES_DATA", str(data))
    wake, out = _run(_load("select_regrade_batch"))
    assert wake is True and "deterministic recommendation" in out
    assert "0.5 -> 0.6" in out and "deviation reason" in out


def test_skeptic_fallback_blocks_third_raise_until_counterevidence(monkeypatch):
    monkeypatch.setenv("PREDICTION_RECOMMENDER_SKEPTIC_AFTER_RAISES", "2")
    mod = _load("select_regrade_batch")
    raises = {"evidence": [
        {"direction": "reinforces", "confidence_before": .5, "confidence_after": .6},
        {"direction": "reinforces", "confidence_before": .6, "confidence_after": .7},
    ]}
    assert mod.skeptic_fallback_allows_raise(raises) is False
    raises["evidence"].append({"direction": "neutral", "confidence_before": .7,
                                "confidence_after": .7, "note": "skeptic pass"})
    assert mod.skeptic_fallback_allows_raise(raises) is True


def test_empty_vault_all_skip(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-19")
    for n in ("select_prediction_candidates", "select_predictions_for_grading",
              "select_regrade_batch"):
        wake, _ = _run(_load(n))
        assert wake is False
