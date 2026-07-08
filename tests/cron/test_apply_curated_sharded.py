"""Regression: apply_curated_entity_fields must resolve pages in their by-letter shard.

resolve_page() probed only the flat `<namespace>/<slug>.md`, so on the entities/concepts
namespaces (which shard by leading letter) it returned None and curated fields were silently
never enforced. This pins resolution through the shared shard-aware okf_migrate.find_page.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "apply_curated_entity_fields.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    spec = importlib.util.spec_from_file_location("apply_curated_entity_fields", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["apply_curated_entity_fields"] = m
    spec.loader.exec_module(m)
    return m


def test_resolves_sharded_entity(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    page = wiki / "entities" / "a" / "acme.md"
    page.write_text("---\ntype: entity\n---\n# Acme\n", encoding="utf-8")
    m = _load(tmp_path)
    assert m.resolve_page("acme", ["entities"]) == page      # was None (flat-only probe)


def test_resolves_flat_entity_too(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    page = wiki / "entities" / "flatco.md"
    page.write_text("---\ntype: entity\n---\n# Flatco\n", encoding="utf-8")
    m = _load(tmp_path)
    assert m.resolve_page("flatco", ["entities"]) == page


def test_missing_slug_returns_none(tmp_path):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    m = _load(tmp_path)
    assert m.resolve_page("nope", ["entities"]) is None
