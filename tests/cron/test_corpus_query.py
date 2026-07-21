import importlib.util
import json
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load(name):
    path = REPO / "scripts" / "cron" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_dynamic_kinds_and_source_filters(tmp_path):
    module = _load("corpus_query")
    index = tmp_path / "index"
    _jsonl(index / "sources.jsonl", [
        {"stem": "a", "frontmatter": {
            "signal_class": "current", "publisher": "MITRE", "ingested": "2026-07-10",
            "basis": ["[[predictions/p1]]"],
        }},
        {"stem": "b", "frontmatter": {
            "signal_class": "reference", "publisher": "Vendor", "ingested": "2026-06-01",
        }},
    ])
    _jsonl(index / "questions.jsonl", [])

    assert module.available_kinds(index) == {"sources", "questions"}
    rows = list(module.query_sources(
        signal_class="current",
        since=date(2026, 7, 1),
        has_basis_in_predictions=True,
        index_dir=index,
    ))
    assert [row["stem"] for row in rows] == ["a"]


def test_prediction_near_due_and_invalid_fraction(tmp_path):
    import pytest

    module = _load("corpus_query")
    index = tmp_path / "index"
    _jsonl(index / "predictions.jsonl", [
        {"stem": "near", "frontmatter": {
            "status": "open", "made_on": "2026-01-01", "resolves_by": "2026-01-11",
        }},
        {"stem": "far", "frontmatter": {
            "status": "open", "made_on": "2026-01-01", "resolves_by": "2026-02-10",
        }},
    ])
    assert [row["stem"] for row in module.query_predictions(
        status="open", near_due_pct=0.8, today=date(2026, 1, 9), index_dir=index
    )] == ["near"]
    with pytest.raises(ValueError, match="between 0 and 1"):
        list(module.query_predictions(near_due_pct=2, index_dir=index))


def test_event_query_uses_scoring_substrate(tmp_path):
    module = _load("corpus_query")
    path = tmp_path / "event-scores.jsonl"
    _jsonl(path, [
        {"id": "a", "event_type": "product-event", "entities": ["acme"],
         "date": "2026-07-10", "scores": {"materiality": 0.8}},
        {"id": "b", "event_type": "capital-event", "entities": ["other"],
         "date": "2026-06-01", "aggregate_score": 0.2},
    ])
    rows = list(module.query_events(
        entity="acme", event_type="product-event", since=date(2026, 7, 1),
        min_score=0.5, event_index=path,
    ))
    assert [row["id"] for row in rows] == ["a"]


def test_question_lookup_and_stable_digest(tmp_path):
    module = _load("corpus_lookup")
    questions = tmp_path / "wiki" / "questions"
    questions.mkdir(parents=True)
    (questions / "board-risk.md").write_text(
        "---\n"
        "type: board-question\n"
        "status: active\n"
        "asker: board\n"
        "question: Are we exposed?\n"
        "canonical_form: Are we exposed to Acme?\n"
        "related_entities: ['[[entities/a/acme]]']\n"
        "---\n"
    )
    (questions / "retired.md").write_text(
        "---\ntype: board-question\nstatus: retired\n"
        "related_entities: ['[[entities/a/acme]]']\n---\n"
    )

    rows = module.find_matching_questions({"acme"}, vault=tmp_path)
    assert len(rows) == 1
    assert rows[0]["related_matched"] == ["acme"]
    digest = module.format_questions_for_digest(rows)
    assert "[[questions/board-risk]]" in digest
    assert "Are we exposed to Acme?" in digest
