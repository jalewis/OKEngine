import importlib.util
import json
import runpy
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
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


def test_available_and_load_validation_failures(tmp_path):
    module = _load("corpus_query")
    assert module.available_kinds(tmp_path / "missing") == set()
    index = tmp_path / "index"
    index.mkdir()
    (index / ".hidden.jsonl").write_text("")
    (index / "bad name.jsonl").write_text("")
    (index / "directory.jsonl").mkdir()
    assert module.available_kinds(index) == set()
    with pytest.raises(ValueError, match="invalid corpus-index kind"):
        list(module.load("../bad", index_dir=index))
    with pytest.raises(FileNotFoundError, match="run corpus_indexer"):
        list(module.load("sources", index_dir=index))
    (index / "known.jsonl").write_text("")
    with pytest.raises(FileNotFoundError, match="available"):
        list(module.load("sources", index_dir=index))
    (index / "sources.jsonl").write_text("\n{\n")
    with pytest.raises(ValueError, match="invalid JSONL"):
        list(module.load("sources", index_dir=index))
    (index / "sources.jsonl").write_text("[]\n")
    with pytest.raises(ValueError, match="must be an object"):
        list(module.load("sources", index_dir=index))


def test_date_and_prediction_basis_all_shapes():
    module = _load("corpus_query")
    moment = datetime(2026, 1, 2, 3, 4)
    assert module._parse_date(moment) == date(2026, 1, 2)
    assert module._parse_date(date(2026, 1, 3)) == date(2026, 1, 3)
    assert module._parse_date("bad") is None
    assert module._parse_date(3) is None
    assert module._has_prediction_basis({"basis": "[[predictions/p1]]"})
    assert module._has_prediction_basis(
        {"basis_in": ["[[p1|Prediction one]]"]}
    )
    assert not module._has_prediction_basis({"basis": [3, "sources/x"]})


def test_all_source_filter_rejection_paths(tmp_path):
    module = _load("corpus_query")
    index = tmp_path / "index"
    _jsonl(index / "sources.jsonl", [
        {"id": "signal", "frontmatter": {"signal_class": "wrong"}},
        {"id": "publisher", "frontmatter": {
            "signal_class": "ok", "publisher": "wrong"}},
        {"id": "kind", "frontmatter": {
            "signal_class": "ok", "publisher": "P", "source_kind": "wrong"}},
        {"id": "date-missing", "frontmatter": {
            "signal_class": "ok", "publisher": "P", "source_kind": "K"}},
        {"id": "date-old", "frontmatter": {
            "signal_class": "ok", "publisher": "P", "source_kind": "K",
            "published": "2020-01-01"}},
        {"id": "basis", "frontmatter": {
            "signal_class": "ok", "publisher": "P", "source_kind": "K",
            "published": "2026-01-01"}},
        {"id": "keep", "frontmatter": {
            "signal_class": "ok", "publisher": "P", "source_kind": "K",
            "ingested": "2026-01-02", "basis": ["predictions/p1"]}},
    ])
    rows = list(module.query_sources(
        signal_class="ok", publisher="P", source_kind="K",
        since=date(2026, 1, 1), has_basis_in_predictions=True, index_dir=index,
    ))
    assert [x["id"] for x in rows] == ["keep"]
    assert [x["id"] for x in module.query_sources(
        has_basis_in_predictions=False, index_dir=index
    )] == ["signal", "publisher", "kind", "date-missing", "date-old", "basis"]


def test_prediction_filter_and_invalid_window_paths(tmp_path):
    module = _load("corpus_query")
    index = tmp_path / "index"
    _jsonl(index / "predictions.jsonl", [
        {"id": "status", "frontmatter": {"status": "closed"}},
        {"id": "horizon", "frontmatter": {"status": "open", "horizon": "long"}},
        {"id": "missing", "frontmatter": {"status": "open", "horizon": "short"}},
        {"id": "reverse", "frontmatter": {
            "status": "open", "horizon": "short", "made_on": "2026-02-01",
            "resolves_by": "2026-01-01"}},
        {"id": "early", "frontmatter": {
            "status": "open", "horizon": "short", "made_on": "2026-01-01",
            "resolves_by": "2026-01-11"}},
        {"id": "keep", "frontmatter": {
            "status": "open", "horizon": "short", "made_on": "2026-01-01",
            "resolves_by": "2026-01-11"}},
    ])
    rows = list(module.query_predictions(
        status="open", horizon="short", near_due_pct=.8,
        today=date(2026, 1, 9), index_dir=index,
    ))
    assert [x["id"] for x in rows] == ["early", "keep"]
    assert list(module.query_predictions(
        status="open", horizon="short", near_due_pct=.9,
        today=date(2026, 1, 5), index_dir=index,
    )) == []
    assert len(list(module.query_predictions(index_dir=index))) == 6


def test_event_query_missing_bad_json_and_every_filter(tmp_path):
    module = _load("corpus_query")
    missing = tmp_path / "missing.jsonl"
    assert list(module.query_events(event_index=missing)) == []
    path = tmp_path / "events.jsonl"
    path.write_text("\n{\n")
    with pytest.raises(ValueError, match="invalid JSONL"):
        list(module.query_events(event_index=path))
    _jsonl(path, [
        {"id": "entity", "entity": "other"},
        {"id": "type", "entity": "x", "type": "other"},
        {"id": "date", "entity": "x", "typed_event": "wanted"},
        {"id": "old", "entity": "x", "typed_event": "wanted", "date": "2020-01-01"},
        {"id": "score-type", "entity": "x", "typed_event": "wanted",
         "date": "2026-01-01", "score": "high"},
        {"id": "score-low", "entity": "x", "typed_event": "wanted",
         "date": "2026-01-01", "scores": {"signal_strength": .1}},
        {"id": "keep", "related_entities": ["x"], "event_type": "wanted",
         "observed_at": "2026-01-01", "aggregate_score": .9},
    ])
    rows = list(module.query_events(
        entity="x", event_type="wanted", since=date(2026, 1, 1),
        min_score=.5, event_index=path,
    ))
    assert [x["id"] for x in rows] == ["keep"]
    assert [x["id"] for x in module.query_events(event_index=path)] == [
        "entity", "type", "date", "old", "score-type", "score-low", "keep"
    ]


def test_main_all_commands_and_entrypoint(tmp_path, monkeypatch, capsys):
    module = _load("corpus_query")
    index = tmp_path / "index"
    _jsonl(index / "sources.jsonl", [
        {"frontmatter": {"signal_class": "a"}},
        {"frontmatter": {}},
    ])
    _jsonl(index / "predictions.jsonl", [{
        "rel_path": "predictions/p.md", "frontmatter": {
            "status": "open", "made_on": "2020-01-01", "resolves_by": "2020-01-02",
        },
    }])
    monkeypatch.setattr(module, "INDEX_DIR", index)
    assert module.main(["kinds"]) == 0
    assert "sources" in capsys.readouterr().out
    assert module.main(["sources-by-class"]) == 0
    assert "a\t1" in capsys.readouterr().out
    assert module.main(["predictions-near-due", "--near-due-pct", "0"]) == 0
    assert "predictions/p.md" in capsys.readouterr().out

    monkeypatch.setenv("HERMES_DATA", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [str(REPO / "scripts/cron/corpus_query.py"), "kinds"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts/cron/corpus_query.py"), run_name="__main__")
    assert exc.value.code == 0


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


def test_corpus_lookup_parsing_and_wikilink_edges():
    module = _load("corpus_lookup")
    assert module._parse_frontmatter("plain") is None
    assert module._parse_frontmatter("---\na: [broken\n---\n") is None
    assert module._parse_frontmatter("---\n- scalar\n---\n") is None
    assert module._wikilink_slugs(3) == set()
    assert module._wikilink_slugs([
        "text [[entities/a/acme.md#section|Acme]] and [[concepts/risk]]",
        None,
    ]) == {"acme", "risk"}
    assert module._wikilink_slugs("[[ ]]") == set()


def test_question_lookup_filters_and_tolerates_bad_pages(tmp_path, monkeypatch):
    module = _load("corpus_lookup")
    assert module.find_matching_questions(set(), vault=tmp_path) == []
    assert module.find_matching_questions({"x"}, vault=tmp_path) == []
    qdir = tmp_path / "wiki/questions"
    qdir.mkdir(parents=True)
    (qdir / "_index.md").write_text("ignored")
    (qdir / "INDEX-other.md").write_text("ignored")
    (qdir / "plain.md").write_text("plain")
    (qdir / "wrong.md").write_text("---\ntype: note\n---\n")
    (qdir / "asker.md").write_text(
        "---\ntype: question\nasker: staff\nstatus: active\n"
        "related_concepts: ['[[concepts/x]]']\n---\n")
    (qdir / "retired.md").write_text(
        "---\ntype: question\nasker: board\nstatus: retired\n"
        "related_concepts: ['[[concepts/x]]']\n---\n")
    (qdir / "other.md").write_text(
        "---\ntype: question\nasker: board\nstatus: active\n"
        "related_concepts: ['[[concepts/y]]']\n---\n")
    assert module.find_matching_questions(
        {"x"}, asker="board", vault=tmp_path) == []
    rows = module.find_matching_questions(
        {"x"}, asker="staff", status=None, vault=tmp_path)
    assert len(rows) == 1 and rows[0]["stem"] == "asker"

    original = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda path, *a, **k: (_ for _ in ()).throw(OSError())
                        if path.name == "asker.md" else original(path, *a, **k))
    assert module.find_matching_questions(
        {"x"}, asker="staff", status=None, vault=tmp_path) == []


def test_question_digest_empty_fallback_keys_and_cap():
    module = _load("corpus_lookup")
    assert "no matching" in module.format_questions_for_digest([])
    rows = [
        {"rel": f"questions/q{i}.md", "question": f"Question {i}",
         "related_slugs": ["x"]}
        for i in range(3)
    ]
    digest = module.format_questions_for_digest(rows, cap=2)
    assert "asker=?" in digest
    assert "Q: Question 0" in digest
    assert "and 1 more" in digest
    assert "Q:" not in module.format_questions_for_digest([
        {"rel": "questions/empty.md", "related_slugs": []}
    ])
