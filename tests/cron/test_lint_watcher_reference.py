"""Regression: lint_watcher excludes pack-declared REFERENCE-CATALOG pages from the orphan
count (KB-health fix). CVE/ATT&CK/encyclopedia imports are link-target scaffolding — a catalog
entry with no inbound links yet is waiting to be cited, not content debt. Recognised by
`reference_types` (type) and `reference_fields` (field presence) in schema.yaml.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    sys.path.insert(0, str(CRON))
    for n in ("lint_watcher", "schema_lib"):
        sys.modules.pop(n, None)
    spec = importlib.util.spec_from_file_location("lint_watcher", CRON / "lint_watcher.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["lint_watcher"] = m
    spec.loader.exec_module(m)
    return m


def _page(vault: Path, rel: str, fm: dict, body: str = ""):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm) + "---\n" + body)


def test_orphan_count_excludes_reference_pages(tmp_path):
    (tmp_path / "schema.yaml").write_text(
        "reference_types: [vulnerability]\nreference_fields: [mitre_id]\n")
    _page(tmp_path, "entities/cve-2026-1.md", {"type": "vulnerability", "title": "CVE"})       # reference (type)
    _page(tmp_path, "entities/m/moonstone.md", {"type": "intrusion-set", "mitre_id": "G1036"})  # reference (field)
    _page(tmp_path, "entities/lonely-actor.md", {"type": "intrusion-set", "sources": ["sources/x"]})
    _page(tmp_path, "entities/referrer.md", {"type": "intrusion-set", "sources": ["sources/y"]},
          "See [[entities/lonely-actor]].\n")
    q = _load(tmp_path).scan_queues()
    assert q["reference-pages"] == 2
    # lonely-actor has 1 inbound (from referrer) -> not orphan; referrer 0 inbound -> orphan;
    # the two reference pages are excluded though they have 0 inbound.
    assert q["orphans"] == 1


def test_without_policy_reference_pages_count_as_orphans(tmp_path):
    """Control: with no reference_* declared, the same catalog pages ARE counted as orphans —
    i.e. the exclusion is what the policy buys (and it's opt-in / off by default)."""
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    _page(tmp_path, "entities/cve.md", {"type": "vulnerability"})
    _page(tmp_path, "entities/m/moon.md", {"type": "intrusion-set", "mitre_id": "G1"})
    _page(tmp_path, "entities/synth.md", {"type": "intrusion-set"})
    q = _load(tmp_path).scan_queues()
    assert q["reference-pages"] == 0
    assert q["orphans"] == 3


def test_broken_wikilinks_excludes_reference_only_targets(tmp_path):
    """A broken link FROM a reference-catalog page (e.g. an ATT&CK record cross-linking a
    technique that wasn't imported) is scaffolding noise — counted as reference-broken, not in
    the headline. A broken link from synthesized content is real debt."""
    (tmp_path / "schema.yaml").write_text(
        "reference_types: [vulnerability]\nreference_fields: [mitre_id]\n")
    _page(tmp_path, "entities/m/attck-group.md", {"type": "intrusion-set", "mitre_id": "G1"},
          "Uses [[entities/missing-technique]].\n")                       # reference origin
    _page(tmp_path, "entities/synth-actor.md", {"type": "intrusion-set", "sources": ["sources/x"]},
          "Linked to [[entities/missing-tool]].\n")                       # synthesized origin
    q = _load(tmp_path).scan_queues()
    assert q["broken-wikilinks"] == 1            # only missing-tool (from synthesized)
    assert q["reference-broken-wikilinks"] == 1  # missing-technique (reference-only)


def test_broken_target_linked_from_both_counts_as_real(tmp_path):
    """A broken target linked from BOTH a reference and a synthesized page is real debt —
    something real wants it to resolve."""
    (tmp_path / "schema.yaml").write_text("reference_fields: [mitre_id]\n")
    _page(tmp_path, "entities/m/grp.md", {"type": "intrusion-set", "mitre_id": "G1"},
          "See [[entities/ghost]].\n")
    _page(tmp_path, "entities/real.md", {"type": "intrusion-set", "sources": ["s/x"]},
          "Also [[entities/ghost]].\n")
    q = _load(tmp_path).scan_queues()
    assert q["broken-wikilinks"] == 1
    assert q["reference-broken-wikilinks"] == 0
