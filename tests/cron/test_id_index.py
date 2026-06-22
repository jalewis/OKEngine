"""P1 regression: the id->path index resolves sharded pages, aliases, tombstones,
and reports (never auto-merges) collisions.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "id_index.py"


def _load():
    spec = importlib.util.spec_from_file_location("id_index", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["id_index"] = m
    spec.loader.exec_module(m)
    return m


def _page(vault: Path, rel: str, fm: str) -> None:
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}\n---\nbody\n", encoding="utf-8")


def test_resolves_sharded_pages_aliases_tombstones(tmp_path):
    m = _load()
    # a deeply sharded page — a plain glob would miss this
    _page(tmp_path, "entities/vendor/a/acme.md", "type: vendor\nid: 'entities:acme'\naliases: ['entities:acme-corp']")
    _page(tmp_path, "attack-pattern/t/t1059.md", "type: attack-pattern\nid: 'mitre:t1059'")
    _page(tmp_path, "entities/x/old.md", "type: vendor\nid: 'entities:old'\nstatus: tombstoned")
    _page(tmp_path, "entities/n/noid.md", "type: vendor")             # no id -> skipped
    _page(tmp_path, "entities/_index.md", "type: dashboard\nid: 'entities:idx'")  # reserved -> skipped

    idx = m.build(tmp_path)
    assert idx.resolve("entities:acme") == "entities/vendor/a/acme.md"   # sharded
    assert idx.resolve("mitre:t1059") == "attack-pattern/t/t1059.md"
    assert idx.resolve("entities:acme-corp") == "entities/vendor/a/acme.md"  # via alias
    assert idx.is_tombstoned("entities:old")
    assert idx.resolve("entities:noid-anything") is None
    assert "entities:idx" not in idx.by_id                             # reserved skipped


def test_collisions_reported_not_merged(tmp_path):
    m = _load()
    _page(tmp_path, "entities/a/one.md", "type: vendor\nid: 'entities:acme'")
    _page(tmp_path, "entities/a/two.md", "type: product\nid: 'entities:acme'")  # same id, different page
    idx = m.build(tmp_path)
    cols = idx.collisions()
    assert "entities:acme" in cols
    assert set(cols["entities:acme"]) == {"entities/a/one.md", "entities/a/two.md"}


def test_write_index_persists(tmp_path):
    m = _load()
    _page(tmp_path, "entities/a/acme.md", "type: vendor\nid: 'entities:acme'")
    idx = m.build(tmp_path)
    out = tmp_path / "id-index.json"
    m.write_index(idx, out)
    import json
    data = json.loads(out.read_text())
    assert data["by_id"]["entities:acme"] == "entities/a/acme.md"
    assert data["norm_version"] == 1
