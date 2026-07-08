"""Regression: canonical_assemble must write the entity to its canonical by-letter shard.

write_canonical() hardcoded `entities/slug[0]/slug.md` — the RAW first char — so an uppercase,
digit, or symbol slug landed in a different shard than the schema's canonical key (which lowercases,
maps digits to `0-9` and symbols to `_`). reshelve then treated that as non-canonical and moved it,
churning/duplicating. This pins resolution through okf_migrate.find_page / canonical_key.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "canonical_assemble.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load():
    spec = importlib.util.spec_from_file_location("canonical_assemble", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["canonical_assemble"] = m
    spec.loader.exec_module(m)
    return m


def _vault(tmp_path):
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\ntypes:\n  entity: {}\n",
        encoding="utf-8")
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    return tmp_path


def test_uppercase_slug_lands_in_lowercase_shard(tmp_path):
    m = _load(); v = _vault(tmp_path)
    m.write_canonical(v, "AcmeCorp", "entity", {"name": "Acme"}, [], ["kev"], {}, "2026-07-07")
    assert (v / "wiki" / "entities" / "a" / "AcmeCorp.md").exists()   # canonical lowercase shard
    assert not (v / "wiki" / "entities" / "A").exists()               # not the raw first char


def test_digit_slug_lands_in_0_9_shard(tmp_path):
    m = _load(); v = _vault(tmp_path)
    m.write_canonical(v, "7zip-flaw", "entity", {"name": "7zip"}, [], ["kev"], {}, "2026-07-07")
    assert (v / "wiki" / "entities" / "0-9" / "7zip-flaw.md").exists()
    assert not (v / "wiki" / "entities" / "7").exists()


def test_existing_sharded_page_merged_not_duplicated(tmp_path):
    m = _load(); v = _vault(tmp_path)
    p = v / "wiki" / "entities" / "a" / "acme.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: entity\nname: Acme\ncurated_field: keepme\n---\nbody\n", encoding="utf-8")
    m.write_canonical(v, "acme", "entity", {"name": "Acme"}, [], ["kev"], {}, "2026-07-07")
    assert p.exists() and "keepme" in p.read_text(encoding="utf-8")   # merged in place, curated kept
    assert not (v / "wiki" / "entities" / "acme.md").exists()          # no flat duplicate
