"""The capture-lane conformance triangle (raw-feed metadata quality).

The raw landing lane (feed_fetch.write_item) is capture-first BY DESIGN: a
feed item with no parseable date still lands, with an explicit empty
`published:` key, and the write guard is not in that path. This file pins the
whole contract so no side of it silently regresses:

  1. capture ADMITS the degenerate item and marks the gap explicitly;
  2. the STRICT validator REJECTS the resulting page (source requires a
     non-empty `published` — so such a page can never pass a release gate);
  3. the conformance AUDIT (`nonempty_fields` rule, engine floor in
     base-schema.yaml) FLAGS it on the deployed vault's dashboard.

Trigger: a live deployment served a source card with a blank published date,
no source links, and an empty confidence field — admitted by capture, visible
to nothing but a human reading the card."""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parents[2]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


FF = _load("feed_fetch_ic", "scripts/cron/feed_fetch.py")


def _land_dateless_item(tmp_path):
    out = tmp_path / "wiki" / "sources" / "2026" / "07"
    dst = FF.write_item(out, "Example Feed", {
        "id": "item-1",
        "title": "Report with no date",
        "link": "https://feeds.example.test/report",
        "published": "",
        "summary": "A capture with no parseable publication date.",
    }, "")
    assert dst is not None
    return dst


def test_capture_admits_and_marks_missing_published(tmp_path):
    """Side 1: capture-first — the item lands, and the gap is explicit
    (an empty `published:` key, not an absent one)."""
    dst = _land_dateless_item(tmp_path)
    text = dst.read_text()
    assert "type: source" in text
    lines = [l for l in text.splitlines() if l.startswith("published:")]
    assert lines == ["published:"], "dateless capture must write an explicit empty published key"


def test_strict_validator_rejects_the_captured_page(tmp_path):
    """Side 2: strict conformance fails the page — blank `published` does not
    satisfy the source type's required floor, so it can't pass a release
    gate. (Runtime write-guard is fail-open and not in the capture path.)"""
    sv_spec = importlib.util.spec_from_file_location(
        "schema_validator_ic", REPO / "tools" / "schema_validator.py")
    sv = importlib.util.module_from_spec(sv_spec)
    sys.modules["schema_validator_ic"] = sv
    sv_spec.loader.exec_module(sv)

    dst = _land_dateless_item(tmp_path)
    (tmp_path / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]},
        "types": {"source": {"required": ["type", "published"], "extensible": True}},
    }))

    # As landed, the page fails the universal floor first: capture assigns no
    # page `id` (that's the id-backfill machinery's job). Pin that too.
    reason = sv.conformance_reject_reason(str(dst), dst.read_text())
    assert reason is not None and "id" in str(reason)

    # With an id supplied (post-backfill state), the blank `published:` alone
    # must still reject — an empty value does not satisfy the required floor.
    with_id = dst.read_text().replace("type: source", "type: source\nid: test-0001aa-aa")
    reason = sv.conformance_reject_reason(str(dst), with_id)
    assert reason is not None and "published" in str(reason)


def test_audit_flags_the_captured_page(tmp_path, monkeypatch):
    """Side 3: the deployed audit surfaces the capture-lane gap on the
    conformance dashboard via the engine-floor nonempty_fields rule."""
    _land_dateless_item(tmp_path)
    (tmp_path / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]},
        "conformance": {"rules": [
            {"id": "source-metadata-complete", "kind": "nonempty_fields",
             "type": "source", "fields": ["published"],
             "severity": "fix", "remediation": "recover dates"}]}}))
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    audit = _load("conformance_audit_ic", "scripts/cron/conformance_audit.py")
    assert audit.main() == 0
    dash = (tmp_path / "wiki" / "dashboards" / "conformance.md").read_text()
    assert "source-metadata-complete" in dash
    assert "**1** page(s)" in dash
    assert "(empty)" in dash


def test_engine_floor_declares_the_rule():
    """base-schema.yaml (the engine floor every pack composes on) must carry
    the source-metadata-complete rule — deleting it would silently disable
    the audit on every deployment."""
    base = yaml.safe_load((REPO / "config" / "base-schema.yaml").read_text())
    rules = {r["id"]: r for r in base["conformance"]["rules"]}
    rule = rules.get("source-metadata-complete")
    assert rule is not None
    assert rule["kind"] == "nonempty_fields"
    assert rule["type"] == "source"
    assert "published" in rule["fields"]
