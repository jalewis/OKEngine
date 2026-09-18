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


# ─── nonempty_fields (capture-lane metadata completeness) ────────────────

def test_nonempty_fields_flags_blank_and_missing(tmp_path, monkeypatch):
    """A `source` page with a blank `published:` (what feed_fetch writes for a
    dateless item) or with the key absent is flagged; complete pages and pages
    of other types are not."""
    vault = tmp_path
    w = vault / "wiki" / "sources" / "2026" / "07"
    w.mkdir(parents=True)
    (w / "complete.md").write_text(
        "---\ntype: source\npublished: 2026-07-13\n---\n# ok\n")
    (w / "blank.md").write_text(
        "---\ntype: source\npublished:\n---\n# blank published\n")
    (w / "absent.md").write_text(
        "---\ntype: source\n---\n# no published key\n")
    e = vault / "wiki" / "entities" / "a"
    e.mkdir(parents=True)
    (e / "ent.md").write_text("---\ntype: entity\n---\n# entity, out of rule scope\n")
    (vault / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]},
        "conformance": {"rules": [
            {"id": "source-metadata-complete", "kind": "nonempty_fields",
             "type": "source", "fields": ["published"],
             "severity": "fix", "remediation": "recover dates"}]}}))
    monkeypatch.setenv("WIKI_PATH", str(vault))
    audit = _load("conformance_audit_ne", "scripts/cron/conformance_audit.py")
    assert audit.main() == 0
    dash = (vault / "wiki" / "dashboards" / "conformance.md").read_text()
    assert "source-metadata-complete" in dash
    assert "**2** page(s)" in dash          # blank.md + absent.md
    assert "(empty)" in dash and "(missing)" in dash
    tail = dash.split("Non-conformant entries")[-1]
    assert "sources/2026/07/complete" not in tail   # complete page not sampled
    assert "entities/a/ent" not in tail             # type filter respected


def test_unknown_rule_kind_is_forward_compatible(tmp_path, monkeypatch):
    vault = tmp_path
    (vault / "wiki").mkdir(parents=True)
    (vault / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]},
        "conformance": {"rules": [
            {"id": "future-rule", "kind": "not-implemented-yet"}]}}))
    monkeypatch.setenv("WIKI_PATH", str(vault))
    audit = _load("conformance_audit_fc", "scripts/cron/conformance_audit.py")
    assert audit.main() == 0                 # no crash on unknown kinds


def test_audit_missing_wiki_frontmatter_edges_and_sample_cap(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "missing"))
    audit = _load("conformance_audit_edges", "scripts/cron/conformance_audit.py")
    assert audit.main() == 1
    assert "wiki not found" in capsys.readouterr().err

    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    wiki.mkdir(parents=True)
    plain = wiki / "plain.md"
    plain.write_text("no frontmatter")
    assert audit._fm(plain) == {}
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == plain else original(self, *a, **k),
    )
    assert audit._fm(plain) == {}
    monkeypatch.setattr(Path, "read_text", original)
    original_fast_load = audit.schema_lib.fast_load
    monkeypatch.setattr(audit.schema_lib, "fast_load", lambda _text: (_ for _ in ()).throw(ValueError()))
    plain.write_text("---\ntype: source\n---\n")
    assert audit._fm(plain) == {}

    # Reload to restore fast_load, then cover skipped pages, empty FM, unknown rules,
    # the no-rules path, and the bounded sample loop.
    (vault / "schema.yaml").write_text(yaml.safe_dump({"conformance": {"rules": []}}))
    monkeypatch.setenv("WIKI_PATH", str(vault))
    no_rules = _load("conformance_audit_no_rules", "scripts/cron/conformance_audit.py")
    monkeypatch.setattr(no_rules.schema_lib, "fast_load", original_fast_load)
    monkeypatch.setattr(no_rules.schema_lib, "conformance_rules", lambda _schema: [])
    assert no_rules.main() == 0
    assert "no rules" in (wiki / "dashboards/conformance.md").read_text()

    (wiki / "_skip.md").write_text("---\ntype: source\n---\n")
    plain.write_text("plain")
    for name in ("a", "b"):
        (wiki / f"{name}.md").write_text("---\ntype: source\nsources:\n- prose|value\n---\n")
    (vault / "schema.yaml").write_text(yaml.safe_dump({"conformance": {"rules": [
        {"id": "refs", "kind": "ref_fields", "fields": ["sources"]},
        {"id": "future", "kind": "future"},
    ]}}))
    monkeypatch.setenv("CONFORMANCE_SAMPLES", "1")
    capped = _load("conformance_audit_capped", "scripts/cron/conformance_audit.py")
    monkeypatch.setattr(capped.schema_lib, "conformance_rules", lambda _schema: [
        {"id": "refs", "kind": "ref_fields", "fields": ["sources"]},
        {"id": "future", "kind": "future"},
    ])
    assert capped.main() == 0
    dash = (wiki / "dashboards/conformance.md").read_text()
    assert "prose\\|value" in dash
