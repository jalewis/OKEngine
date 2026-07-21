"""Clamped confidence-delta recommender contract (#212)."""
import importlib.util
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
EXT = REPO / "extensions" / "okengine.predictions"


def _load():
    sys.path.insert(0, str(EXT))
    spec = importlib.util.spec_from_file_location("confidence_recommender_test",
                                                  EXT / "confidence_recommender.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _prediction(vault: Path, confidence: float, evidence: list[dict]) -> Path:
    path = vault / "wiki" / "predictions" / "p.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\n" + yaml.safe_dump({
        "type": "prediction", "status": "open", "subject": "entities/x",
        "confidence": confidence, "evidence": evidence,
    }, sort_keys=False) + "---\n# claim\n", encoding="utf-8")
    return path


def _scores(data: Path, rows: list[dict]) -> None:
    path = data / "state" / "okengine.events" / "event-scores.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_rule_clamps_events_cycle_bounds_and_preserves_drivers(tmp_path, monkeypatch):
    data = tmp_path / "data"
    evidence = [
        {"source": "sources/a", "direction": "reinforces", "confidence_before": .9,
         "confidence_after": .9, "note": "pending"},
        {"source": "sources/b", "direction": "reinforces", "confidence_before": .9,
         "confidence_after": .9, "note": "pending"},
    ]
    pred = _prediction(tmp_path, .9, evidence)
    before = pred.read_text()
    score = {"signal_strength": 1, "source_reliability_score": 1, "corroboration_count": 5}
    _scores(data, [
        {"event_id": "events/a", "source": "sources/a", "scores": score},
        {"event_id": "events/b", "source": "sources/b", "scores": score},
    ])
    monkeypatch.setenv("HERMES_DATA", str(data))
    mod = _load()
    rows = mod.recommendations(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert [event["per_event_delta"] for event in row["events"]] == [.15, .15]
    assert row["delta_suggested"] == .05 and row["confidence_after_suggested"] == .95
    assert row["events"][0]["update_driver"] == {
        "source_quality": 1.0, "signal_strength": 1.0,
        "corroboration": 5, "contradiction_penalty": 0.0,
    }
    assert row["rule_inputs"]["per_cycle_cap"] == .2
    assert pred.read_text() == before, "recommendation computation must never mutate the claim"


def test_contradiction_clamps_negative_and_disposed_evidence_is_ignored(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _prediction(tmp_path, .1, [
        {"source": "sources/a", "direction": "contradicts", "confidence_before": .1,
         "confidence_after": .1},
        {"source": "sources/b", "direction": "reinforces", "confidence_before": .1,
         "confidence_after": .2},
    ])
    score = {"signal_strength": 0, "source_reliability_score": 0, "corroboration_count": 0}
    _scores(data, [{"event_id": "events/a", "source": "sources/a", "scores": score},
                   {"event_id": "events/b", "source": "sources/b", "scores": score}])
    monkeypatch.setenv("HERMES_DATA", str(data))
    row = _load().recommendations(tmp_path)[0]
    assert len(row["events"]) == 1 and row["events"][0]["per_event_delta"] == -.15
    assert row["confidence_after_suggested"] == .05  # final confidence floor


def test_source_scoped_score_feeds_recommender_without_event_overlap(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _prediction(tmp_path, .5, [
        {"source": "[[sources/report]]", "direction": "reinforces",
         "confidence_before": .5, "confidence_after": .5},
    ])
    score = {"signal_strength": .9, "source_reliability_score": 1,
             "corroboration_count": 0}
    _scores(data, [
        {"event_id": "sources/report", "event_type": "source-evidence",
         "source": "sources/report", "score_scope": "source", "scores": score},
    ])
    monkeypatch.setenv("HERMES_DATA", str(data))

    rows = _load().recommendations(tmp_path)

    assert len(rows) == 1
    assert rows[0]["events"][0]["event_id"] == "sources/report"
    assert rows[0]["delta_suggested"] > 0


def test_source_score_prevents_duplicate_moves_from_event_rows(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _prediction(tmp_path, .5, [
        {"source": "sources/a", "direction": "reinforces",
         "confidence_before": .5, "confidence_after": .5},
    ])
    score = {"signal_strength": 1, "source_reliability_score": 1, "corroboration_count": 0}
    _scores(data, [
        {"event_id": "events/one", "source": "sources/a", "scores": score},
        {"event_id": "events/two", "source": "sources/a", "scores": score},
        {"event_id": "sources/a", "source": "sources/a", "score_scope": "source",
         "scores": score},
    ])
    monkeypatch.setenv("HERMES_DATA", str(data))

    row = _load().recommendations(tmp_path)[0]

    assert len(row["events"]) == 1
    assert row["events"][0]["event_id"] == "sources/a"


def test_abstains_without_complete_scored_inputs_and_writes_empty_outputs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _prediction(tmp_path, .5, [{"source": "sources/a", "direction": "reinforces",
                                "confidence_before": .5, "confidence_after": .5}])
    _scores(data, [{"event_id": "events/a", "source": "sources/a",
                   "scores": {"signal_strength": .8}}])
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("HERMES_DATA", str(data))
    mod = _load()
    assert mod.recommendations(tmp_path) == []
    assert mod.main() == 0
    assert (data / "state" / "okengine.predictions" /
            "confidence-recommendations.jsonl").read_text() == ""
    assert "No pending evidence" in (tmp_path / "wiki" / "dashboards" /
                                      "confidence-recommendations.md").read_text()


def test_equal_confidence_disposition_markers_close_recommendations(tmp_path, monkeypatch):
    data = tmp_path / "data"
    _prediction(tmp_path, .5, [
        {"source": "sources/a", "direction": "reinforces", "confidence_before": .5,
         "confidence_after": .5, "note": "No movement [recommender-accepted]"},
        {"source": "sources/b", "direction": "reinforces", "confidence_before": .5,
         "confidence_after": .5,
         "note": "Analyst holds [recommender-deviation: conflicting direct telemetry]"},
    ])
    score = {"signal_strength": 1, "source_reliability_score": 1, "corroboration_count": 5}
    _scores(data, [
        {"event_id": "events/a", "source": "sources/a", "scores": score},
        {"event_id": "events/b", "source": "sources/b", "scores": score},
    ])
    monkeypatch.setenv("HERMES_DATA", str(data))
    assert _load().recommendations(tmp_path) == []


def test_manifest_config_knobs_are_real_runtime_env_reads():
    manifest = yaml.safe_load((EXT / "extension.yaml").read_text())
    recommender = (EXT / "confidence_recommender.py").read_text()
    selector = (EXT / "select_regrade_batch.py").read_text()
    for key in manifest["config"]:
        env = "PREDICTION_" + key.upper()
        assert env in recommender or env in selector, f"config key {key} has no runtime env read"
