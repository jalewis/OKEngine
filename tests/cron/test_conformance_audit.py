"""Conformance audit (okengine#158 P1): the ref_fields rule flags prose entries in ref list-fields
(sources written as 'Vendor advisory' instead of a source-page path), and writes a dashboard."""
import importlib.util
import os
import sys
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


SL = _load("schema_lib", "scripts/cron/schema_lib.py")


def test_is_page_ref():
    assert SL.is_page_ref("sources/2026/06/x")          # path
    assert SL.is_page_ref("[[entities/a/foo]]")          # wikilink path
    assert SL.is_page_ref("foo.md")                      # .md
    assert not SL.is_page_ref("Cisco Talos disclosure")  # prose
    assert not SL.is_page_ref("Vendor advisory")


def test_conformance_rules_reads_block():
    sch = {"conformance": {"rules": [
        {"id": "source-refs-are-pages", "kind": "ref_fields", "fields": ["sources"]},
        {"bad": "no id/kind"}]}}
    rules = SL.conformance_rules(sch)
    assert len(rules) == 1 and rules[0]["id"] == "source-refs-are-pages"
    assert SL.conformance_rules({}) == []                # absent -> none


def test_audit_flags_prose_sources(tmp_path, monkeypatch):
    vault = tmp_path
    w = vault / "wiki" / "entities" / "a"
    w.mkdir(parents=True)
    (w / "good.md").write_text("---\ntype: entity\nsources:\n- sources/2026/06/real-report\n---\n# good\n")
    (w / "bad.md").write_text("---\ntype: entity\nsources:\n- Cisco Talos disclosure\n- Vendor advisory\n---\n# bad\n")
    (vault / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]},
        "conformance": {"rules": [
            {"id": "source-refs-are-pages", "kind": "ref_fields", "fields": ["sources"],
             "severity": "fix", "remediation": "relink"}]}}))
    monkeypatch.setenv("WIKI_PATH", str(vault))
    audit = _load("conformance_audit", "scripts/cron/conformance_audit.py")
    assert audit.main() == 0
    dash = (vault / "wiki" / "dashboards" / "conformance.md").read_text()
    assert "source-refs-are-pages" in dash
    assert "1** page(s)" in dash or "**1**" in dash      # exactly 1 violating page (bad.md)
    assert "Cisco Talos disclosure" in dash               # the prose entry surfaced
    assert "good" not in dash.split("Non-conformant entries")[-1]  # good.md not in the sample table
