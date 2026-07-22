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

    idx = m.build(tmp_path, force=True)
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
    idx = m.build(tmp_path, force=True)
    cols = idx.collisions()
    assert "entities:acme" in cols
    assert set(cols["entities:acme"]) == {"entities/a/one.md", "entities/a/two.md"}


def test_non_string_scalar_alias_does_not_crash_build(tmp_path):
    """A page authored with a bare non-string scalar `aliases` (YAML int/bool)
    must not raise `TypeError: 'int' object is not iterable` and crash the whole
    build — the write path leaves such a scalar untouched (okengine#196)."""
    m = _load()
    _page(tmp_path, "entities/a/acme.md", "type: vendor\nid: 'entities:acme'\naliases: 3405")
    _page(tmp_path, "entities/b/beta.md", "type: vendor\nid: 'entities:beta'\naliases: yes")
    _page(tmp_path, "entities/g/good.md", "type: vendor\nid: 'entities:good'\naliases: ['entities:good-corp']")
    idx = m.build(tmp_path, force=True)  # must not raise
    # the bare scalar aliases are safely dropped (a non-list shape -> [], mirroring
    # normalize_bare_name_links); the pages themselves still index by id, and a
    # well-formed list alias on another page still resolves.
    assert idx.resolve("entities:acme") == "entities/a/acme.md"
    assert idx.resolve("entities:beta") == "entities/b/beta.md"
    assert idx.resolve("3405") is None
    assert idx.resolve("entities:good-corp") == "entities/g/good.md"


def test_build_fast_path_loads_artifact_not_scan(tmp_path, monkeypatch):
    """The write path (`build`, force=False) must LOAD the persisted artifact and never full-scan the
    64k-page vault inline — that scan is what blocked create_entity for 300s. `force=True` (the cron)
    still scans."""
    m = _load()
    # round-trip: to_dict -> from_dict -> resolve (incl. alias)
    src = m.IdIndex()
    src.by_id = {"entities:x": "entities/a/x.md"}
    src.aliases = {"entities:xa": "entities/a/x.md"}
    rt = m.from_dict(src.to_dict())
    assert rt.resolve("entities:x") == "entities/a/x.md" and rt.resolve("entities:xa") == "entities/a/x.md"

    scanned = {"n": 0}
    real_scan = m._scan
    monkeypatch.setattr(m, "_scan", lambda v: scanned.__setitem__("n", scanned["n"] + 1) or real_scan(v))
    monkeypatch.setattr(m, "load", lambda path=m.INDEX_PATH: src)          # artifact present
    monkeypatch.setattr(m.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())

    idx = m.build(tmp_path)                                                # force=False (write path)
    assert idx is src and scanned["n"] == 0                                # loaded, did NOT scan
    m.build(tmp_path, force=True)                                          # the cron path
    assert scanned["n"] == 1                                               # force still scans

    monkeypatch.setattr(m, "load", lambda path=m.INDEX_PATH: None)         # no artifact yet
    m.build(tmp_path)                                                      # first deploy -> one scan
    assert scanned["n"] == 2


def test_write_index_persists(tmp_path):
    m = _load()
    _page(tmp_path, "entities/a/acme.md", "type: vendor\nname: Acme\nid: 'entities:acme'\naliases: [ACME Corp]")
    idx = m.build(tmp_path, force=True)
    out = tmp_path / "id-index.json"
    m.write_index(idx, out)
    import json
    data = json.loads(out.read_text())
    assert data["by_id"]["entities:acme"] == "entities/a/acme.md"
    assert data["norm_version"] == 2                              # v2 adds identity maps (okengine#324)
    # name/alias identity maps are serialized + normalized (okengine#324)
    assert data["name_to_rels"]["acme"] == ["entities/a/acme.md"]
    assert data["alias_to_rels"]["acme-corp"] == ["entities/a/acme.md"]
    # and a v2 payload round-trips through from_dict
    idx2 = m.from_dict(data)
    assert idx2.name_to_rels == idx.name_to_rels and idx2.alias_to_rels == idx.alias_to_rels


def test_scan_indexes_subdomain_entities(tmp_path):  # invariant-audit #351 (A1)
    """A walk-up page <sub>/entities/x is namespace 'entities' and MUST be indexed in the identity
    maps (name/alias) so write_server's create-time dedup catches its duplicates. Before the fix the
    scan gated on rel.split('/')[0]=='entities' (root only), so every sub-domain entity was invisible
    to dedup and duplicate canonicals accreted in co-installed vaults."""
    m = _load()
    (tmp_path / "wiki" / "acme").mkdir(parents=True)
    (tmp_path / "wiki" / "acme" / "schema.yaml").write_text("types: {entity: {}}\n")   # sub-domain container
    _page(tmp_path, "acme/entities/s/shinyhunters.md", "type: entity\nname: ShinyHunters\naliases: [UNC6240]")
    _page(tmp_path, "entities/r/rootco.md", "type: entity\nname: RootCo")               # root, still indexed
    nk = m.id_lib.normalize_key
    idx = m._scan(tmp_path)
    assert "acme/entities/s/shinyhunters.md" in idx.name_to_rels.get(nk("ShinyHunters"), []), idx.name_to_rels
    assert "acme/entities/s/shinyhunters.md" in idx.alias_to_rels.get(nk("UNC6240"), []), idx.alias_to_rels
    assert "entities/r/rootco.md" in idx.name_to_rels.get(nk("RootCo"), [])             # root path preserved
