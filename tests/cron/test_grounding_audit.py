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
