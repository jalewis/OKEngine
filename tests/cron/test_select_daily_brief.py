"""L1 regression: which prediction statuses count as OPEN is read from the schema's
tier.namespaces.predictions.open_values (the single config-driven contract tier_lib/build_hot_set
use), not a bare hardcoded literal in select_daily_brief that silently forks."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "select_daily_brief.py"


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["OKENGINE_BASE_SCHEMA"] = str(REPO / "config" / "base-schema.yaml")
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    spec = importlib.util.spec_from_file_location("select_daily_brief", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["select_daily_brief"] = m
    spec.loader.exec_module(m)
    return m


def test_open_values_read_from_schema(tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "wiki" / "schema.yaml").write_text(
        "tier:\n  namespaces:\n    predictions:\n      open_values: [open, active, proposed]\n")
    m = _load(tmp_path)
    assert m._open_prediction_values() == {"open", "active", "proposed"}   # schema-driven, not the literal


def test_open_values_defaults_when_no_schema(tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(tmp_path)
    assert m._open_prediction_values() == {"open", "active"}               # safe fallback


def test_movement_uses_composed_knowledge_namespaces(tmp_path):  # invariant-audit #27
    """The brief's movement section must iterate the vault's DECLARED knowledge namespaces (composed
    schema), not a hardcoded ('entities','concepts') that silently skipped every pack namespace on a
    composed vault (okcti: threat-actors, cves, detections, …)."""
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    actors: {strategy: by-letter}\n    cves: {strategy: by-date}\n")
    m = _load(tmp_path)
    nss = set(m._knowledge_namespaces())
    assert "actors" in nss and "cves" in nss, nss     # pack namespaces included, not the bare pair


def _page(path: Path, fm: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\nBody.\n", encoding="utf-8")


def test_empty_vault_does_not_wake(tmp_path, capsys):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "empty vault" in out
    assert '"wakeAgent": false' in out


def test_digest_selects_all_activity_classes(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    today = mdate = __import__("datetime").date.today().isoformat()
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    source = wiki / "sources" / f"{now.year:04d}" / f"{now.month:02d}" / "report.md"
    _page(source, f"type: source\ntitle: Fresh report\npublished: {today}\n")
    _page(wiki / "actors" / "a" / "acme.md",
          f"type: actor\ncreated: {today}\nlast_updated: {today}\n")
    _page(wiki / "predictions" / "due.md",
          f"type: prediction\nstatus: proposed\nresolves_by: {mdate}\nconfidence: high\n")
    _page(wiki / "gaps" / "new-gap.md",
          f"type: gap\nstatus: open\nfirst_seen: {today}\nseverity: high\n"
          "rule: missing-source\nsubject: actors/a/acme\n")
    m = _load(tmp_path)
    monkeypatch.setattr(m, "_knowledge_namespaces", lambda: ("actors",))
    monkeypatch.setattr(m, "_open_prediction_values", lambda: {"proposed"})

    assert m.main() == 0
    out = capsys.readouterr().out
    assert "Fresh sources (1" in out
    assert "[[sources/" in out
    assert "Entity/concept movement (1" in out
    assert "Predictions resolving" in out
    assert "New completeness gaps (1)" in out
    assert '"wakeAgent": true' in out


def test_digest_rejects_malformed_and_future_freshness_dates(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    dt = __import__("datetime")
    today = dt.date.today()
    month = wiki / "sources" / f"{today.year:04d}" / f"{today.month:02d}"
    _page(month / "valid.md", f"type: source\ntitle: Valid current\npublished: {today}\n")
    bad_values = {
        "queued.md": "queued for review at 2026-07-06+00:00",
        "placeholder.md": "'{{now}}'",
        "null.md": "'null'",
        "bad-offset.md": f"{today}T12:00+OO:OO",
        "future.md": (today + dt.timedelta(days=2)).isoformat(),
    }
    for name, value in bad_values.items():
        _page(month / name, f"type: source\ntitle: Must not appear {name}\npublished: {value}\n")
    m = _load(tmp_path)
    monkeypatch.setattr(m, "_knowledge_namespaces", lambda: ())

    assert m.main() == 0
    out = capsys.readouterr().out
    assert "Fresh sources (1" in out
    assert "Valid current" in out
    assert "Must not appear" not in out
    assert "freshness records skipped: invalid=4 · future=1" in out


def test_movement_excludes_products_sources_and_invalid_dates(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    today = __import__("datetime").date.today().isoformat()
    _page(wiki / "actors" / "valid.md",
          f"type: actor\ncreated: {today}\nlast_updated: {today}\n")
    _page(wiki / "actors" / "bad.md",
          "type: actor\ncreated: '{{now}}'\nlast_updated: '{{now}}'\n")
    _page(wiki / "actors" / "misfiled-source.md",
          f"type: source\ncreated: {today}\nlast_updated: {today}\n")
    _page(wiki / "briefings" / "daily.md",
          f"type: briefing\ncreated: {today}\nlast_updated: {today}\n")
    m = _load(tmp_path)
    monkeypatch.setattr(m, "_knowledge_namespaces", lambda: ("actors", "briefings"))

    assert m.main() == 0
    out = capsys.readouterr().out
    assert "Entity/concept movement (1" in out
    assert "[[actors/valid]]" in out
    assert "actors/bad" not in out
    assert "misfiled-source" not in out
    assert "briefings/daily" not in out
    assert "freshness records skipped: invalid=1" in out


def test_date_and_frontmatter_helper_edges(tmp_path, monkeypatch):
    dt = __import__("datetime")
    m = _load(tmp_path)
    assert m._date_value(dt.datetime(2026, 1, 2, 3, 4)) == dt.date(2026, 1, 2)
    assert m._date_value(dt.date(2026, 1, 2)) == dt.date(2026, 1, 2)
    assert m._date_value(3) is None
    assert m._date_value("   ") is None
    assert m._date_value("2026-01-02") == dt.date(2026, 1, 2)
    assert m._fresh_date(None, dt.date(2026, 1, 1), dt.date(2026, 1, 2)) == (None, None)
    assert m._fresh_date("2025-01-01", dt.date(2026, 1, 1), dt.date(2026, 1, 2)) == (None, None)

    page = tmp_path / "page.md"
    page.write_text("---\n- list\n---\n")
    assert m._fm(page) == {}
    original = m.Path.read_text
    monkeypatch.setattr(
        m.Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == page else original(self, *a, **k),
    )
    assert m._fm(page) == {}


def test_schema_helpers_fall_back_on_dependency_errors(tmp_path, monkeypatch):
    m = _load(tmp_path)

    class BrokenSchema:
        @staticmethod
        def merged_schema(*_args):
            raise RuntimeError("broken schema")

    monkeypatch.setitem(sys.modules, "schema_lib", BrokenSchema)
    assert m._open_prediction_values() == {"open", "active"}
    assert m._knowledge_namespaces() == ("entities", "concepts")

    class EmptySchema:
        merged_schema = staticmethod(lambda *_args: {})
        knowledge_namespaces = staticmethod(lambda _schema: set())
        excluded_dirs = staticmethod(lambda _schema: set())

    monkeypatch.setitem(sys.modules, "schema_lib", EmptySchema)
    assert m._open_prediction_values() == {"open", "active"}
    assert m._knowledge_namespaces() == ("entities", "concepts")


def test_quiet_digest_skips_indexes_closed_predictions_and_old_gaps(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    _page(wiki / "misc" / "only.md", "type: note\n")
    today = __import__("datetime").date.today()
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    source_month = wiki / "sources" / f"{now.year:04d}" / f"{now.month:02d}"
    _page(source_month / "INDEX.md", f"published: {today}\n")
    _page(wiki / "actors" / "_template.md", f"type: actor\ncreated: {today}\n")
    _page(wiki / "predictions" / "INDEX.md", f"status: open\nresolves_by: {today}\n")
    _page(wiki / "predictions" / "closed.md", f"status: closed\nresolves_by: {today}\n")
    _page(wiki / "predictions" / "later.md",
          f"status: open\nresolves_by: {today.replace(year=today.year + 1)}\n")
    _page(wiki / "gaps" / "INDEX.md", f"status: open\nfirst_seen: {today}\n")
    _page(wiki / "gaps" / "closed.md", f"status: closed\nfirst_seen: {today}\n")
    m = _load(tmp_path)
    monkeypatch.setattr(m, "_knowledge_namespaces", lambda: ("actors", "absent"))
    monkeypatch.setattr(m, "_open_prediction_values", lambda: {"open"})
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "Quiet window" in out
    assert '"wakeAgent": true' in out
