import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def _load(vault):
    spec = importlib.util.spec_from_file_location(
        "lint_watcher_runtime", REPO / "scripts" / "cron" / "lint_watcher.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    module.VAULT = vault
    module.OPS_DIR = vault / "wiki" / "operational"
    module.SNAPSHOTS = module.OPS_DIR / "queue-snapshots.md"
    return module


def test_parse_frontmatter_and_operational_path_rules(tmp_path):
    module = _load(tmp_path)
    assert module.parse_fm("body only") == (None, None)
    fm, match = module.parse_fm("---\ntype: report\n---\n")
    assert fm == {"type": "report"} and match
    fm, match = module.parse_fm("---\ntype: [\n---\n")
    assert fm is None and match
    top = tmp_path / "wiki" / "lint-today.md"
    nested = tmp_path / "wiki" / "x" / "lint-today.md"
    assert module.is_operational_path(top)
    assert not module.is_operational_path(nested)


def test_snapshot_roundtrip_ignores_malformed_values(tmp_path):
    module = _load(tmp_path)
    assert module.read_prior_snapshot() == {}
    module.append_snapshot("2026-07-24", {"orphans": 2, "broken": 1})
    with module.SNAPSHOTS.open("a") as handle:
        handle.write("| 2026-07-25 | orphans=3, bad=nope |\n")
    assert module.read_prior_snapshot() == {"orphans": 3}


def test_main_suppresses_first_run_regression_then_alerts_on_growth(tmp_path, monkeypatch):
    module = _load(tmp_path)
    queues = {"broken-wikilinks": 3, "fm-parse-errors": 2}
    monkeypatch.setattr(module, "scan_queues", lambda details=None: queues)
    monkeypatch.setattr(module, "read_prior_snapshot", lambda: {})
    with redirect_stdout(io.StringIO()) as output:
        assert module.main() == 0
    assert "alerts: 1" in output.getvalue()
    assert "UNHANDLED queue" in output.getvalue()

    monkeypatch.setattr(module, "read_prior_snapshot",
                        lambda: {"broken-wikilinks": 1, "fm-parse-errors": 2})
    with redirect_stdout(io.StringIO()) as output:
        assert module.main() == 0
    assert "alerts: 2" in output.getvalue()
    assert "despite an expected drain" in output.getvalue()


def test_page_enumeration_and_namespace_fallback_edges(tmp_path, monkeypatch):
    module = _load(tmp_path)
    wiki = tmp_path / "wiki"
    for rel in ("entities/live.md", ".hidden/x.md", "entities/_archive-old/x.md"):
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    (wiki / "fake.md").mkdir()
    assert module.all_wiki_pages() == [wiki / "entities/live.md"]

    monkeypatch.setattr(module.schema_lib, "governing_schema", lambda *_a: {})
    monkeypatch.setattr(module.schema_lib, "excluded_dirs", lambda *_a: set())
    monkeypatch.setattr(module.schema_lib, "knowledge_namespaces", lambda *_a: set())
    assert module.knowledge_namespaces() == {"entities", "fake.md"}
    module.VAULT = tmp_path / "absent"
    assert module.knowledge_namespaces() == set()


def test_snapshot_read_error_and_missing_rows(tmp_path, monkeypatch):
    module = _load(tmp_path)
    module.OPS_DIR.mkdir(parents=True)
    module.SNAPSHOTS.write_text("no table rows")
    assert module.read_prior_snapshot() == {}
    module.SNAPSHOTS.write_text("| 2026-08-01 | no-equals, good=2 |\n")
    assert module.read_prior_snapshot() == {"good": 2}
    monkeypatch.setattr(
        module.Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError("race")),
    )
    assert module.read_prior_snapshot() == {}


def test_report_covers_empty_manual_regressing_draining_and_stable_states(tmp_path):
    module = _load(tmp_path)
    queues = {
        "empty": 0,
        "fm-parse-errors": 2,
        "growing": 3,
        "draining": 1,
        "stable": 2,
    }
    prior = {"empty": 0, "fm-parse-errors": 1, "growing": 1, "draining": 3, "stable": 2}
    report = module.write_today_report(
        "2026-08-01", queues, prior, [],
        {"schema-drift": {
            "by_type": {"old": 1}, "examples": {"old": ["entities/a.md"]},
            "alias_targets": {"old": ["new"]},
        }},
    )
    text = report.read_text()
    for expected in ("✓ empty", "UNHANDLED", "REGRESSION", "draining", "stable", "declared alias"):
        assert expected in text
    assert "None — all queues healthy" in text


def test_main_skips_zero_depth_and_prints_no_alert_section(tmp_path, monkeypatch):
    module = _load(tmp_path)
    monkeypatch.setattr(module, "scan_queues", lambda details=None: {"broken-wikilinks": 0})
    monkeypatch.setattr(module, "read_prior_snapshot", lambda: {"broken-wikilinks": 0})
    with redirect_stdout(io.StringIO()) as output:
        assert module.main() == 0
    assert "alerts: 0" in output.getvalue()
    assert "ALERTS:" not in output.getvalue()


def test_scan_queue_malformed_source_drift_publisher_and_example_cap_edges(tmp_path, monkeypatch):
    module = _load(tmp_path)
    (tmp_path / "schema.yaml").write_text("""
types:
  entity: {}
  source: {}
partitioning:
  namespaces:
    entities: {strategy: flat}
    sources: {strategy: flat}
operational_types: [custom-op]
""")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "index.md").write_text("[[entities/indexed]]")
    (tmp_path / "CLAUDE.md").write_text("**Canonical names**\n\n`Canonical`\n")

    def page(rel, text):
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    page("lint-generated.md", "body only")
    page("entities/no-frontmatter.md", "body only")
    page("entities/yaml-invalid.md", "---\ntype: [\n---\n")
    page("entities/list.md", "---\n- item\n---\n")
    page("entities/indexed.md", "---\ntype: entity\n---\n[[missing/indexed-target]]")
    page("entities/custom.md", "---\ntype: custom-op\n---\n")
    for i in range(22):
        page(f"entities/orphan-{i}.md", f"---\ntype: invented\n---\n[[<placeholder-{i}>]]")
    page("sources/no-quality.md", "---\ntype: source\n---\n")
    for i in range(10):
        page(f"sources/publisher-{i}.md", "---\ntype: source\nreliability: a\ncredibility: b\npublisher: Drifted\n---\n")
    page("sources/canonical.md", "---\ntype: source\nreliability: a\ncredibility: b\npublisher: Canonical\n---\n")
    page("sources/no-publisher.md", "---\ntype: source\n---\n")
    raced = page("entities/raced.md", "---\ntype: entity\n---\n")
    original = module.Path.read_text
    monkeypatch.setattr(
        module.Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == raced else original(self, *a, **k),
    )

    details = {}
    queues = module.scan_queues(details)
    assert queues["fm-parse-errors"] == 1
    assert queues["yaml-invalid"] == 1
    assert queues["sources-missing-quality-scores"] >= 2
    assert queues["publisher-drift"] == 1
    assert queues["schema-drift"] == 22
    assert len(details["schema-drift"]["examples"]["invented"]) == 3
    assert len(details["orphans"]["examples"]) == 20
