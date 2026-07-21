"""Regression: okengine.reevaluation edge index (CHE core step 1, okengine#234).

Locks the artifact contract the dependency-aware selector (#235) will consume: open
propositions' citations — frontmatter ref fields (plain-path AND wikilink forms, since the
write path normalizes to plain paths but hand-authored pages carry wikilinks),
evidence[].source records, and body wikilinks — inverted into cited-page -> [propositions].
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
EXT = REPO / "extensions" / "okengine.reevaluation"
SCRIPT = EXT / "edge_index.py"


def _load():
    spec = importlib.util.spec_from_file_location("reeval_edge_index", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _page(path: Path, fm: str, body: str = "Body.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "wiki").mkdir(parents=True)
    return v


def test_manifest_and_cron_def():
    m = yaml.safe_load((EXT / "extension.yaml").read_text(encoding="utf-8"))
    assert m["id"] == "okengine.reevaluation"
    assert m["capabilities"]["write"] == [], "the edge lane must write no vault content"
    cron = json.loads((EXT / "crons" / "reevaluation-edges.cron.json").read_text(encoding="utf-8"))
    assert cron["entrypoint"] == "edge_index.py"
    assert cron["schedule"]["kind"] == "cron"


def test_edges_from_all_four_ref_surfaces(tmp_path):
    mod = _load()
    v = _vault(tmp_path)
    _page(
        v / "wiki" / "predictions" / "p1.md",
        "type: prediction\nstatus: open\nresolves_by: 2026-12-31\n"
        "subject: '[[entities/acme]]'\n"                       # wikilink-form ref field
        "sources:\n  - sources/2026/07/report-a\n"             # plain-path ref field
        "basis: sources/2026/06/report-b.md\n"                 # .md-suffixed ref
        "evidence:\n  - {direction: reinforces, source: sources/2026/07/report-c}\n",
        body="See [[concepts/loose-coupling#section|alias]] for context.",
    )
    art = mod.build(v)
    assert art["proposition_count"] == 1
    e = art["edges"]
    assert e["entities/acme"][0]["via"] == ["subject"]
    assert e["sources/2026/07/report-a"][0]["via"] == ["sources"]
    assert e["sources/2026/06/report-b"][0]["via"] == ["basis"]      # .md stripped
    assert e["sources/2026/07/report-c"][0]["via"] == ["evidence.source"]
    assert e["concepts/loose-coupling"][0]["via"] == ["body"]        # alias+anchor stripped
    row = e["sources/2026/07/report-a"][0]
    assert row["page"] == "predictions/p1" and row["status"] == "open"
    assert row["resolves_by"] == "2026-12-31"


def test_closed_and_foreign_types_excluded(tmp_path):
    mod = _load()
    v = _vault(tmp_path)
    _page(v / "wiki" / "predictions" / "done.md",
          "type: prediction\nstatus: confirmed\nsources: [sources/x]")
    _page(v / "wiki" / "entities" / "e.md",
          "type: entity\nstatus: open\nsources: [sources/x]")
    art = mod.build(v)
    assert art["proposition_count"] == 0 and art["edges"] == {}


def test_type_and_status_config_extensible(tmp_path, monkeypatch):
    """The #236 hook: a pack's diagnostic class registers via config, zero code change."""
    monkeypatch.setenv("OKENGINE_REEVAL_TYPES", "prediction,diagnostic")
    monkeypatch.setenv("OKENGINE_REEVAL_OPEN_STATUSES", "open,active,investigating")
    mod = _load()  # re-load so module-level config picks up the env
    v = _vault(tmp_path)
    _page(v / "wiki" / "assessments" / "d1.md",
          "type: diagnostic\nstatus: investigating\nsources: [sources/telemetry-a]")
    art = mod.build(v)
    assert art["proposition_count"] == 1
    assert art["edges"]["sources/telemetry-a"][0]["page"] == "assessments/d1"


def test_operational_namespaces_and_self_refs_skipped(tmp_path):
    mod = _load()
    v = _vault(tmp_path)
    _page(v / "wiki" / "dashboards" / "x.md",
          "type: prediction\nstatus: open\nsources: [sources/x]")
    _page(v / "wiki" / "predictions" / "selfy.md",
          "type: prediction\nstatus: open\nsources: [predictions/selfy]")
    art = mod.build(v)
    assert art["edges"] == {}, "dashboards pages and self-references must not create edges"


def test_main_writes_artifact_atomically(tmp_path, monkeypatch):
    v = _vault(tmp_path)
    _page(v / "wiki" / "predictions" / "p.md",
          "type: prediction\nstatus: open\nsources: [sources/a]")
    monkeypatch.setenv("WIKI_PATH", str(v))
    mod = _load()
    assert mod.main() == 0
    art = json.loads((v / "wiki" / ".reevaluation-edges.json").read_text(encoding="utf-8"))
    assert art["edge_count"] == 1 and "sources/a" in art["edges"]
    assert not (v / "wiki" / ".reevaluation-edges.json.tmp").exists()
