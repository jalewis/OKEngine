"""page-quality-audit must survive a knowledge namespace beyond entities/concepts.

Regression for a fleet-wide daily crash: AUDITED_DIRS is derived from the schema's
`partitioning.namespaces`, so it grows as a pack adds namespaces (e.g. `briefings`). The
per-namespace tally `by_tier` was hardcoded to {entities, concepts}, so `by_tier[sub]` raised
`KeyError: 'briefings'` and the daily run crashed on okcti + cyber-market + vendor-risk.
"""
import importlib.util
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


def test_audit_survives_namespace_beyond_entities_concepts(tmp_path, monkeypatch, capsys):
    vault = tmp_path
    for sub, ptype in (("entities", "entity"), ("concepts", "concept"), ("briefings", "briefing")):
        d = vault / "wiki" / sub
        d.mkdir(parents=True)
        (d / f"{sub[:3]}-1.md").write_text(
            f"---\ntype: {ptype}\ntitle: {sub} one\n---\n# {sub} one\n\nSubstantial body prose here.\n",
            encoding="utf-8")
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
