import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MOD = ROOT / "scripts" / "cron" / "wiki_health_audit.py"


def _load(name="wiki_health_audit_test"):
    scripts = str(ROOT / "scripts" / "cron")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(name, MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _vault(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "dashboards").mkdir()
    (wiki / "operational").mkdir()
    (wiki / "entities" / "alpha.md").write_text(
        "---\ntype: entity\ntitle: Alpha\n---\n# Alpha\nSee [[concepts/missing]].\n"
    )
    (wiki / "dashboards" / "prediction-date-audit.md").write_text(
        "---\ntype: dashboard\n---\n# Prediction dates\n"
    )
    (wiki / "index.md").write_text("# Old index\n")
    (wiki / "log.md").write_text("# Log\n")
    (tmp_path / "schema.yaml").write_text(
        "types:\n  entity:\n    required: [title]\n"
        "partitioning:\n  namespaces:\n    entities: {}\n"
    )
    return wiki


def test_report_rotation_is_one_file_per_run(tmp_path, monkeypatch):
    wiki = _vault(tmp_path)
    m = _load()
    monkeypatch.setattr(m, "WIKI", wiki)
    (wiki / "lint-2026-07-29-v1.md").write_text("old")
    (wiki / "lint-2026-07-29-v3.md").write_text("old")

    assert m.next_report_path("2026-07-29").name == "lint-2026-07-29-v4.md"


def test_render_is_bounded_and_names_specialist_ownership(tmp_path, monkeypatch):
    wiki = _vault(tmp_path)
    m = _load("wiki_health_audit_render_test")
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "WIKI", wiki)
    changed = []
    for i in range(30):
        p = wiki / "entities" / f"p{i}.md"
        p.write_text("---\ntype: entity\ntitle: P\n---\n# P\n")
        changed.append(p)
    queues = {
        "broken-wikilinks": 2, "pages-missing-from-index": 1,
        "fm-parse-errors": 0, "yaml-invalid": 0,
    }
    details = {
        "broken-wikilinks": {
            "top_missing": [{"target": "concepts/missing", "inbound": 3}]
        },
        "publisher-drift": {"candidates": []},
        "schema-drift": {"by_type": {}},
    }
    body = m.render_report(
        wiki / "lint-2026-07-29-v1.md",
        datetime(2026, 7, 29, tzinfo=timezone.utc),
        changed, 1.0, 2.0, queues, {}, details,
        "## Frontmatter health\n\nClean.\n\n## Schema drift audit\n\nNone.\n",
    )

    assert body.count("wiki/entities/p") == m.MAX_CHANGED
    assert "and 5 more" in body
    assert "[[concepts/missing]]" in body
    assert "bounded specialist lanes" in body
    assert "[[dashboards/prediction-date-audit]]" in body
    assert "Model calls: 0" in body


def test_main_publishes_report_log_index_and_baseline_without_agent(
        tmp_path, monkeypatch, capsys):
    wiki = _vault(tmp_path)
    state = tmp_path / "runtime" / "lint-state.json"
    m = _load("wiki_health_audit_main_test")
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "WIKI", wiki)
    monkeypatch.setattr(m, "LOG", wiki / "log.md")
    monkeypatch.setattr(m, "STATE_PATH", state)
    monkeypatch.setattr(m.wiki_change_check, "STATE_PATH", state)

    assert m.main() == 0

    reports = list(wiki.glob("lint-*.md"))
    assert len(reports) == 1
    assert "Terminal state: report, log, index, and baseline committed" in reports[0].read_text()
    assert f"Full report: [[{reports[0].stem}]]" in (wiki / "log.md").read_text()
    assert f"Latest weekly audit: [[{reports[0].stem}]]" in (wiki / "index.md").read_text()
    saved = json.loads(state.read_text())
    assert saved["last_baseline_mtime"] > 0
    output = capsys.readouterr().out
    assert '"wakeAgent": false' in output
    declarations = [
        json.loads(line.removeprefix(m.ARTIFACT_PREFIX).strip())
        for line in output.splitlines() if line.startswith(m.ARTIFACT_PREFIX)
    ]
    assert {value["operation"] for value in declarations} == {
        "append", "create", "replace", "update",
    }
    assert {value["path"] for value in declarations} == {
        reports[0].relative_to(tmp_path).as_posix(),
        "wiki/log.md",
        "wiki/index.md",
        state.relative_to(tmp_path).as_posix(),
    }
    assert next(value for value in declarations if value["operation"] == "create")["count"] == 1
    assert next(value for value in declarations if value["operation"] == "append")["count"] == 1


def test_main_skips_when_content_has_not_changed(tmp_path, monkeypatch, capsys):
    wiki = _vault(tmp_path)
    state = tmp_path / "runtime" / "lint-state.json"
    m = _load("wiki_health_audit_skip_test")
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "WIKI", wiki)
    monkeypatch.setattr(m, "LOG", wiki / "log.md")
    monkeypatch.setattr(m, "STATE_PATH", state)
    monkeypatch.setattr(m.wiki_change_check, "STATE_PATH", state)

    assert m.main() == 0
    reports_before = list(wiki.glob("lint-*.md"))
    assert m.main() == 0
    assert list(wiki.glob("lint-*.md")) == reports_before
    declarations = [
        json.loads(line.removeprefix(m.ARTIFACT_PREFIX).strip())
        for line in capsys.readouterr().out.splitlines()
        if line.startswith(m.ARTIFACT_PREFIX)
    ]
    assert declarations[-1] == {
        "count": 0,
        "operation": "verify",
        "path": "wiki/index.md",
    }


def test_emit_artifact_preserves_count_and_normalizes_paths(tmp_path, monkeypatch, capsys):
    m = _load("wiki_health_audit_artifact_test")
    monkeypatch.setattr(m, "VAULT", tmp_path / "vault")
    inside = m.VAULT / "wiki" / "report.md"
    outside = tmp_path / "elsewhere.md"

    m.emit_artifact(inside, "create", 3)
    m.emit_artifact(outside, "verify")

    assert capsys.readouterr().out.splitlines() == [
        'OKENGINE_ARTIFACT: {"count": 3, "operation": "create", "path": "wiki/report.md"}',
        f'OKENGINE_ARTIFACT: {{"operation": "verify", "path": "{outside.resolve()}"}}',
    ]


def test_health_audit_empty_and_alternate_report_paths(tmp_path, monkeypatch, capsys):
    m = _load("wiki_health_audit_edges")
    missing = tmp_path / "missing"
    monkeypatch.setattr(m, "WIKI", missing)
    assert m._content_files() == []
    assert m.changed_pages(7.0) == ([], 7.0)
    assert m.main() == 2
    assert "wiki missing" in capsys.readouterr().err

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    monkeypatch.setattr(m, "WIKI", wiki)
    monkeypatch.setattr(m, "VAULT", tmp_path)
    # A same-prefix non-report filename is ignored by rotation parsing.
    (wiki / "lint-2026-07-31-not-a-version.md").write_text("x")
    assert m.next_report_path("2026-07-31").name == "lint-2026-07-31-v1.md"
    assert m.editorial_dashboards() == []

    report = wiki / "lint-2026-07-31-v1.md"
    monkeypatch.setattr(m, "editorial_dashboards", lambda: [])
    body = m.render_report(
        report, datetime(2026, 7, 31, tzinfo=timezone.utc), [], 0.0, 0.0,
        {}, {}, {
            "publisher-drift": {"candidates": [{"publisher": "Example", "sources": 2}]},
            "schema-drift": {"by_type": {"legacy": 1}},
        }, "## Schema audit\n",
    )
    assert "Top missing concepts\n\nNone." in body
    assert "Example" in body and "Review declared aliases" in body
    assert "No content pages changed" in body
    assert "No specialist editorial dashboard" in body


def test_append_log_is_idempotent_and_changed_pages_tracks_latest(tmp_path, monkeypatch):
    wiki = _vault(tmp_path)
    m = _load("wiki_health_audit_log_edges")
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "WIKI", wiki)
    monkeypatch.setattr(m, "LOG", wiki / "log.md")
    changed, latest = m.changed_pages(0.0)
    assert changed and latest > 0
    report = wiki / "lint-2026-07-31-v1.md"
    generated = datetime(2026, 7, 31, tzinfo=timezone.utc)
    m.append_log(generated, report, {})
    once = (wiki / "log.md").read_text()
    m.append_log(generated, report, {})
    assert (wiki / "log.md").read_text() == once
