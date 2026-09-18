"""page-quality-audit must survive a knowledge namespace beyond entities/concepts.

Regression for a fleet-wide daily crash: AUDITED_DIRS is derived from the schema's
`partitioning.namespaces`, so it grows as a pack adds namespaces (e.g. `briefings`). The
per-namespace tally `by_tier` was hardcoded to {entities, concepts}, so `by_tier[sub]` raised
`KeyError: 'briefings'` and the daily run crashed on okcti + market-intel + vendor-risk.
"""
import importlib.util
import runpy
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def test_audit_survives_namespace_beyond_entities_concepts(tmp_path, monkeypatch, capsys):
    vault = tmp_path
    for sub, ptype in (("entities", "entity"), ("concepts", "concept"), ("briefings", "briefing")):
        d = vault / "wiki" / sub
        d.mkdir(parents=True)
        (d / f"{sub[:3]}-1.md").write_text(
            f"---\ntype: {ptype}\ntitle: {sub} one\n---\n# {sub} one\n\nSubstantial body prose here.\n",
            encoding="utf-8")
    (vault / "wiki" / "entities" / "complete.md").write_text(
        "---\ntype: entity\nsources: [a, b, c, d, e]\n---\n# Complete\n" +
        ("word " * 80) + "\n## One\n## Two\n## Three\n## Four\n",
        encoding="utf-8",
    )
    # partitioning.namespaces DRIVES AUDITED_DIRS — declare the extra namespace so the audit walks it
    (vault / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]},
        "partitioning": {"namespaces": {"entities": {}, "concepts": {}, "briefings": {}}}}), encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    audit = _load("page_quality_audit", "scripts/cron/page_quality_audit.py")
    assert "briefings" in audit.AUDITED_DIRS                 # the extra namespace is audited
    assert audit.main() == 0                                 # no KeyError on by_tier['briefings']
    out = capsys.readouterr().out
    assert "briefings:" in out                               # counted, not crashed
    assert (vault / "wiki" / "operational" / "page-quality-snapshots.md").is_file()


def test_read_parse_words_pages_and_inbound_edge_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    audit = _load("page_quality_helpers", "scripts/cron/page_quality_audit.py")
    page = tmp_path / "wiki" / "entities" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("plain body")
    assert audit.read_fm_body(page) == ({}, "plain body")
    page.write_text("---\n[\n---\nbody")
    assert audit.read_fm_body(page) == ({}, "body")
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("gone"))
            if path == page else original_read(path, *a, **k)
        ),
    )
    assert audit.read_fm_body(page) == ({}, "")
    monkeypatch.setattr(Path, "read_text", original_read)

    assert audit._parse_date(date(2026, 1, 1)) == date(2026, 1, 1)
    assert audit._parse_date(datetime(2026, 1, 2)) == date(2026, 1, 2)
    assert audit._parse_date("bad") is None
    assert audit._parse_date("2026-99-99") is None
    assert audit._parse_date(3) is None
    assert audit._body_words("# Title\nWords [[entities/x|X]] *here*") == 2

    audit.AUDITED_DIRS = ("entities", "missing")
    (page.parent / "_skip.md").write_text("x")
    (page.parent / ".hidden.md").write_text("x")
    directory = page.parent / "dir.md"
    directory.mkdir()
    assert [(sub, p.name) for sub, p in audit.all_pages()] == [("entities", "a.md")]

    ref = tmp_path / "wiki" / "ref.md"
    ref.write_text("[[entities/a]] [[entities/a|A]]")
    hidden = tmp_path / "wiki" / "_archive" / "old.md"
    hidden.parent.mkdir()
    hidden.write_text("[[entities/a]]")
    inbound = audit.build_inbound([])
    assert inbound["a"] == 2


def _body(words, sections=0):
    return ("word " * words) + "".join(f"\n## S{i}\n" for i in range(sections))


def test_classify_all_tiers_publishers_reference_redirect_and_depth(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    audit = _load("page_quality_tiers", "scripts/cron/page_quality_audit.py")
    today = date(2026, 1, 10)
    monkeypatch.setattr(audit.schema_lib, "is_reference_page",
                        lambda fm, _policy: bool(fm.get("reference")))
    audit.ENRICHABLE_TYPES = {"actor"}
    audit.DEPTH_CRITICAL = {"actor"}

    assert audit.classify("entities", {}, "", today)["tier"] == "empty"
    assert audit.classify("entities", {"link_stub": True}, "body", today)["tier"] == "redirect"
    assert audit.classify("entities", {"type": "media"}, "body", today)["tier"] == "publisher"
    assert audit.classify(
        "entities", {"type": "other", "tags": ["publisher"]}, "body", today
    )["tier"] == "publisher"
    assert audit.classify("concepts", {"tags": ["publisher"]}, "body", today)["tier"] == "stub"
    assert audit.classify("entities", {"reference": True}, "body", today)["tier"] == "reference"
    assert audit.classify("entities", {}, _body(20), today)["tier"] == "stub"
    assert audit.classify("entities", {}, _body(200), today)["tier"] == "thin"
    assert audit.classify(
        "entities", {"sources": list(range(8))}, _body(60, 6), today
    )["tier"] == "encyclopedic"
    assert audit.classify(
        "entities", {"sources": list(range(5))}, _body(60, 4), today
    )["tier"] == "strong"
    assert audit.classify(
        "entities", {"sources": ["a", "b"]}, _body(60, 2), today
    )["tier"] == "ok"
    assert audit.classify("entities", {}, _body(60, 1), today)["tier"] == "thin"
    demoted = audit.classify(
        "entities", {"type": "actor", "updated": "2026-01-01"},
        _body(300, 1), today,
    )
    assert demoted["tier"] == "thin" and demoted["age"] == 9


def test_main_replaces_same_day_snapshot_and_entrypoint(tmp_path, monkeypatch, capsys):
    for sub in ("entities", "concepts"):
        (tmp_path / "wiki" / sub).mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {}\n    concepts: {}\n"
    )
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    audit = _load("page_quality_main", "scripts/cron/page_quality_audit.py")
    assert audit.main() == 0
    first = audit.SNAP.read_text()
    assert audit.main() == 0
    assert audit.SNAP.read_text().count(f"| {datetime.now().date().isoformat()} |") == 1
    assert '"wakeAgent": false' in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", [str(REPO / "scripts/cron/page_quality_audit.py")])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts/cron/page_quality_audit.py"),
                       run_name="__main__")
    assert exc.value.code == 0
