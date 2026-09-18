"""Regression: the concept-backfill wake-gate must recognize HIERARCHICAL
concept pages as existing.

After the OKF migration, concepts live at ``wiki/concepts/<letter>/<slug>.md``.
A flat ``glob('*.md')`` finds only ``INDEX.md`` and floods the agent with
already-existing concepts as "missing" — which the MCP write path then refuses
(and the old file_write path would have flat-duplicated). This locks in the
recursive, dual-form existence check.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "select_concept_candidates.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    sys.modules.pop("select_concept_candidates", None)
    spec = importlib.util.spec_from_file_location("select_concept_candidates", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_hierarchical_concepts_count_as_existing(tmp_path):
    wiki = tmp_path / "wiki"
    cdir = wiki / "concepts"
    (cdir / "r").mkdir(parents=True)
    (cdir / "p").mkdir(parents=True)
    (cdir / "r" / "ransomware.md").write_text("---\ntype: concept\nsources: [x]\n---\n# Ransomware\n")
    (cdir / "p" / "phishing.md").write_text("---\ntype: concept\nsources: [x]\n---\n# Phishing\n")
    (cdir / "INDEX.md").write_text("---\ntype: dashboard\ntitle: Index\n---\n")
    m = _load(tmp_path)

    existing = m.list_existing_concepts()
    # both forms present so either wikilink style matches
    assert "r/ransomware" in existing and "ransomware" in existing
    assert "p/phishing" in existing and "phishing" in existing
    assert "INDEX" not in existing


def test_existing_hierarchical_concept_not_reported_missing(tmp_path):
    wiki = tmp_path / "wiki"
    cdir = wiki / "concepts"
    (cdir / "r").mkdir(parents=True)
    (cdir / "r" / "ransomware.md").write_text("---\ntype: concept\nsources: [x]\n---\n# Ransomware\n")
    # a page that links to the concept BOTH ways + to a genuinely-missing one
    (wiki / "sources").mkdir()
    (wiki / "sources" / "s1.md").write_text(
        "---\ntype: source\n---\nSee [[concepts/r/ransomware]] and "
        "[[concepts/ransomware]] and [[concepts/q/quantum-risk]].\n")
    m = _load(tmp_path)

    existing = m.list_existing_concepts()
    refs = m.scan_wikilinks()
    missing = {slug for slug in refs if slug not in existing}
    # the existing concept must NOT be missing in either link form
    assert "r/ransomware" not in missing
    assert "ransomware" not in missing
    # the genuinely-absent one IS missing
    assert "q/quantum-risk" in missing


def test_missing_concept_instruction_uses_canonical_partition(tmp_path, capsys, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "schema.yaml").write_text(
        "types:\n  concept: {}\npartitioning:\n  namespaces:\n"
        "    concepts:\n      strategy: by-letter\n"
    )
    (wiki / "sources").mkdir()
    for i in range(3):
        (wiki / "sources" / f"s{i}.md").write_text("[[concepts/q/quantum-risk]]\n")
    m = _load(tmp_path)
    monkeypatch.setattr(m, "MIN_INBOUND_TO_FIRE", 3)

    assert m.main() == 0
    out = capsys.readouterr().out
    assert "canonical write key: `concepts/q/quantum-risk`" in out
    assert "wiki/concepts/<slug>.md" not in out
    assert "converge_concept" in out


def test_controlled_target_selects_exact_missing_concept(tmp_path, capsys, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "sources").mkdir()
    (wiki / "sources" / "a.md").write_text("[[concepts/alpha-gap]]\n")
    (wiki / "sources" / "z.md").write_text("[[concepts/qualification-gap]]\n")
    m = _load(tmp_path)
    monkeypatch.setattr(m, "MIN_INBOUND_TO_FIRE", 1)
    monkeypatch.setattr(m, "CONTROLLED_TARGET", "qualification-gap")

    assert m.main() == 0
    out = capsys.readouterr().out
    assert "controlled target: qualification-gap" in out
    assert "`concepts/qualification-gap`" in out
    assert "`concepts/alpha-gap`" not in out


def test_citing_evidence_is_embedded_and_bounded(tmp_path, capsys, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "sources").mkdir()
    body = "[[concepts/large-context-risk]]\n" + ("evidence " * 1000)
    for i in range(3):
        (wiki / "sources" / f"s{i}.md").write_text(body)
    m = _load(tmp_path)
    monkeypatch.setattr(m, "MIN_INBOUND_TO_FIRE", 3)
    monkeypatch.setattr(m, "MAX_CITING_BYTES", 80)

    assert m.main() == 0
    out = capsys.readouterr().out
    assert "ONLY from the bounded citing evidence embedded below" in out
    assert "Do not read the citing files separately" in out
    assert "Do not author computed reader panels" in out
    assert out.count("```citing-evidence") == 3
    assert out.count("[truncated at 80 bytes]") == 3
    assert "evidence " * 20 not in out


def test_bare_and_sharded_links_emit_one_manifest_key(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "sources").mkdir()
    (wiki / "schema.yaml").write_text(
        "types:\n  concept: {}\npartitioning:\n  namespaces:\n"
        "    concepts:\n      strategy: by-letter\n"
    )
    for i in range(3):
        (wiki / "sources" / f"bare-{i}.md").write_text(
            "[[concepts/prompt-injection]]\n"
        )
        (wiki / "sources" / f"shard-{i}.md").write_text(
            "[[concepts/p/r/prompt-injection]]\n"
        )
    manifest = tmp_path / "selection.json"
    m = _load(tmp_path)
    monkeypatch.setattr(m, "MIN_INBOUND_TO_FIRE", 1)
    monkeypatch.setattr(m, "SELECTION_MANIFEST", manifest)

    assert m.main() == 0
    selected = __import__("json").loads(manifest.read_text())["selected"]
    assert selected == ["concepts/p/prompt-injection"]


def test_historical_qwen_qualification_links_do_not_recreate_fixtures(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "briefings").mkdir()
    (wiki / "briefings" / "old.md").write_text(
        "[[concepts/q/w/qwen-final-local-backfill-control-g20260728j]]\n"
    )
    m = _load(tmp_path)
    assert m.scan_wikilinks() == {}


def test_explicit_target_selects_ephemeral_qwen_qualification_fixture(
        tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "sources").mkdir()
    page = wiki / "sources" / "control.md"
    page.write_text(
        "[[concepts/q/w/qwen-final-local-backfill-control-gtest]]\n"
    )
    m = _load(tmp_path)
    monkeypatch.setattr(
        m, "CONTROLLED_TARGET", "qwen-final-local-backfill-control-gtest")

    refs = m.scan_wikilinks()

    assert list(refs) == ["q/w/qwen-final-local-backfill-control-gtest"]
    assert refs["q/w/qwen-final-local-backfill-control-gtest"] == [page]


def test_default_batch_reserves_terminal_receipt_turn(tmp_path, monkeypatch):
    monkeypatch.delenv("CONCEPT_BACKFILL_BATCH_SIZE", raising=False)
    m = _load(tmp_path)
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    job = next(item for item in jobs if item["name"] == "concept-backfill")
    assert m.N <= job["max_iterations"] - 1


def test_missing_dirs_scan_skips_and_read_races(tmp_path, monkeypatch):
    m = _load(tmp_path)
    assert m.list_existing_concepts() == set()
    assert m.scan_wikilinks() == {}
    wiki = tmp_path / "wiki"
    (wiki / "dashboards").mkdir(parents=True)
    (wiki / "dashboards/x.md").write_text("[[concepts/ignored]]")
    lint = wiki / "lint-report.md"
    lint.write_text("[[concepts/ignored]]")
    unreadable = wiki / "page.md"
    unreadable.write_text("[[concepts/missing]]")
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == unreadable else original(self, *a, **k),
    )
    assert m.scan_wikilinks() == {}


def test_main_empty_threshold_batch_limit_and_unavailable_evidence(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    source_a = wiki / "a.md"
    source_b = wiki / "b.md"
    source_a.write_text("evidence")
    source_b.write_text("evidence")
    m = _load(tmp_path)
    manifest = tmp_path / "selection.json"
    monkeypatch.setattr(m, "SELECTION_MANIFEST", manifest)
    monkeypatch.setattr(m, "list_existing_concepts", lambda: set())

    monkeypatch.setattr(m, "scan_wikilinks", lambda: {})
    assert m.main() == 0
    assert '"wakeAgent": false' in capsys.readouterr().out

    monkeypatch.setattr(m, "scan_wikilinks", lambda: {"low": [source_a]})
    monkeypatch.setattr(m, "MIN_INBOUND_TO_FIRE", 2)
    assert m.main() == 0
    assert "only 1 inbound" in capsys.readouterr().out

    monkeypatch.setattr(m, "scan_wikilinks", lambda: {
        "alpha": [source_a, source_b], "beta": [source_a, source_b],
    })
    monkeypatch.setattr(m, "MIN_INBOUND_TO_FIRE", 1)
    monkeypatch.setattr(m, "N", 1)
    monkeypatch.setattr(m, "MAX_CITING_SOURCES", 1)
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("unavailable"))
        if self == source_a else original_read(self, *a, **k),
    )
    assert m.main() == 0
    output = capsys.readouterr().out
    assert "[unavailable: unavailable]" in output
    assert "and 1 more" in output
    assert "## 2." not in output
