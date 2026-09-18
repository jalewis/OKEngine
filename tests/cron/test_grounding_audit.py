"""grounding_audit (trust floor): grounded vs ungrounded vs dangling, reference imports excluded."""
import importlib.util, sys
from pathlib import Path
import pytest
yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent


def test_grounding(tmp_path, monkeypatch):
    w = tmp_path / "wiki"
    (w / "sources" / "2026" / "06").mkdir(parents=True)
    (w / "sources/2026/06/real-report.md").write_text("---\ntype: source\n---\n# r\n")
    e = w / "entities" / "a"; e.mkdir(parents=True)
    (e / "grounded.md").write_text("---\ntype: entity\nsources:\n- sources/2026/06/real-report\n---\n# g\n")
    (e / "dangling.md").write_text("---\ntype: entity\nsources:\n- sources/2026/06/missing\n---\n# d\n")
    (e / "ungrounded.md").write_text("---\ntype: entity\nsources:\n- Vendor advisory\n---\n# u\n")
    (e / "no-src.md").write_text("---\ntype: entity\n---\n# n\n")
    (e / "cve.md").write_text("---\ntype: vulnerability\nmitre_id: T1\n---\n# excluded\n")
    (tmp_path / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]}, "reference_fields": ["mitre_id"]}))   # cve.md is reference -> excluded
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("GROUNDING_NAMESPACES", "entities")
    spec = importlib.util.spec_from_file_location("grounding_audit", REPO / "scripts/cron/grounding_audit.py")
    m = importlib.util.module_from_spec(spec); sys.modules["grounding_audit"] = m; spec.loader.exec_module(m)
    assert m.main() == 0
    dash = (w / "dashboards" / "source-grounding.md").read_text()
    assert "in scope: **4**" in dash           # 4 entities (cve excluded as reference)
    assert "grounded: **1**" in dash           # only 'grounded'
    assert "ungrounded: **2**" in dash          # prose-only + no-src
    assert "dangling: **1**" in dash            # cites missing source
    assert "entities/a/cve" not in dash   # reference import excluded from worklists


def _load(tmp_path,monkeypatch):
    monkeypatch.setenv("WIKI_PATH",str(tmp_path));monkeypatch.setenv("GROUNDING_NAMESPACES","entities,missing")
    spec=importlib.util.spec_from_file_location("grounding_audit_edges",REPO/"scripts/cron/grounding_audit.py")
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m);return m


def test_grounding_empty_missing_and_filtered_pages(tmp_path,monkeypatch):
    m=_load(tmp_path,monkeypatch)
    assert m.main()==1
    entities=tmp_path/"wiki/entities";entities.mkdir(parents=True)
    for name,text in {
      "_skip.md":"plain","INDEX.md":"plain","INDEX-a.md":"plain",
      "plain.md":"plain","bad.md":"---\n[bad\n---\n",
    }.items():(entities/name).write_text(text)
    assert m._stem("[[sources/2026/X.md]]")=="x"
    assert m._fm(entities/"plain.md")=={}
    assert m.main()==0
    dash=(tmp_path/"wiki/dashboards/source-grounding.md").read_text()
    assert "100%" in dash and "in scope: **0**" in dash


def test_grounding_sample_cap_and_partial_band(tmp_path,monkeypatch):
    sources=tmp_path/"wiki/sources";sources.mkdir(parents=True)
    (sources/"_skip.md").write_text("x");(sources/"INDEX.md").write_text("x")
    (sources/"real.md").write_text("---\ntype: source\n---\n")
    entities=tmp_path/"wiki/entities";entities.mkdir()
    for i in range(4):
      src="sources/real" if i<2 else ("sources/missing" if i==2 else None)
      body=f"---\ntype: entity\n"+(f"sources: [{src}]\n" if src else "")+"---\n"
      (entities/f"{i}.md").write_text(body)
    m=_load(tmp_path,monkeypatch);m.SAMPLES=0
    assert m.main()==0
    dash=(tmp_path/"wiki/dashboards/source-grounding.md").read_text()
    assert "🟡 partial" in dash and "## Ungrounded" not in dash and "## Dangling" not in dash
