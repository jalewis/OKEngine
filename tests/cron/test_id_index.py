"""P1 regression: the id->path index resolves sharded pages, aliases, tombstones,
and reports (never auto-merges) collisions.
"""
import importlib.util
import builtins
import json
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
    _page(tmp_path, "entities/README.md", "type: entity\nid: 'entities:readme'")

    idx = m.build(tmp_path, force=True)
    assert idx.resolve("entities:acme") == "entities/vendor/a/acme.md"   # sharded
    assert idx.resolve("mitre:t1059") == "attack-pattern/t/t1059.md"
    assert idx.resolve("entities:acme-corp") == "entities/vendor/a/acme.md"  # via alias
    assert idx.is_tombstoned("entities:old")
    assert idx.resolve("entities:noid-anything") is None
    assert "entities:idx" not in idx.by_id                             # reserved skipped
    assert "entities:readme" not in idx.by_id


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
    assert data["norm_version"] == 3                              # v3 adds strict slug identity (#592)
    # name/alias identity maps are serialized + normalized (okengine#324)
    assert data["name_to_rels"]["acme"] == ["entities/a/acme.md"]
    assert data["alias_to_rels"]["acme-corp"] == ["entities/a/acme.md"]
    assert data["slug_identity_to_rels"]["entities:acme"] == ["entities/a/acme.md"]
    # and a v3 payload round-trips through from_dict
    idx2 = m.from_dict(data)
    assert idx2.name_to_rels == idx.name_to_rels and idx2.alias_to_rels == idx.alias_to_rels
    assert idx2.slug_identity_hits("entities", "A_C-ME") == ["entities/a/acme.md"]


def test_strict_slug_identity_is_namespace_and_subdomain_scoped(tmp_path):
    m = _load()
    (tmp_path / "wiki" / "acme").mkdir(parents=True)
    (tmp_path / "wiki" / "acme" / "schema.yaml").write_text("types: {entity: {}}\n")
    _page(tmp_path, "entities/a/agent-tesla.md", "type: entity\nid: entities:agent-tesla")
    _page(tmp_path, "concepts/a/agenttesla.md", "type: concept\nid: concepts:agenttesla")
    _page(tmp_path, "acme/entities/a/agent_tesla.md", "type: entity\nid: entities:agent-tesla")

    idx = m._scan(tmp_path)
    assert idx.slug_identity_hits("entities", "agenttesla") == [
        "entities/a/agent-tesla.md"
    ]
    assert idx.slug_identity_hits("concepts", "agent-tesla") == [
        "concepts/a/agenttesla.md"
    ]
    assert idx.slug_identity_hits("acme/entities", "AGENT TESLA") == [
        "acme/entities/a/agent_tesla.md"
    ]


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


def test_standalone_import_fallback_and_identity_empty_keys(monkeypatch):
    original_import = builtins.__import__
    attempts = {"id_lib": 0}

    def importing(name, *args, **kwargs):
        if name == "id_lib" and attempts["id_lib"] == 0:
            attempts["id_lib"] += 1
            raise ImportError("standalone")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    m = _load()
    assert attempts["id_lib"] == 1
    idx = m.IdIndex()
    idx._add_identity("", {"aliases": [""]})
    idx._add_identity("entities/a/no-extension", {"name": "No Extension"})
    assert "no-extension" in idx.name_to_rels
    assert idx.alias_to_rels


def test_scan_io_races_refresh_write_failure_and_main(tmp_path, monkeypatch, capsys):
    m = _load()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "schema.yaml").write_text("types: {}\n")
    schema = wiki / "domain/schema.yaml"
    schema.parent.mkdir()
    schema.write_text("types: {}\n")
    unreadable = wiki / "entities/a/unreadable.md"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("---\nid: unreadable\n---\n")
    outside = wiki / "entities/a/outside.md"
    outside.write_text("---\nid: outside\n---\n")
    original_read = Path.read_text
    original_resolve = Path.resolve
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == unreadable else original_read(self, *a, **k),
    )
    monkeypatch.setattr(
        Path, "resolve",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("outside"))
        if self in {schema.parent, outside} else original_resolve(self, *a, **k),
    )
    idx = m._scan(tmp_path)
    assert not idx.by_id

    target = m.IdIndex()
    target.by_id["racing"] = "entities/r/racing.md"
    target._add_slug_identity("entities", "entities/r/racing.md", "racing")
    target._add_slug_identity("entities", "entities/r/racing.md", "racing")
    monkeypatch.setattr(m, "_scan", lambda _vault: m.IdIndex())
    monkeypatch.setattr(
        m, "write_index", lambda *_a, **_k: (_ for _ in ()).throw(OSError("readonly")),
    )
    key = str(tmp_path)
    m._REFRESHING.add(key)
    m._refresh_into(target, tmp_path, key)
    assert target.by_id["racing"] == "entities/r/racing.md"
    assert target.slug_identity_hits("entities", "r-a-c-i-n-g") == [
        "entities/r/racing.md"
    ]
    assert target.has_slug_identity_index
    assert key not in m._REFRESHING

    collision = m.IdIndex()
    collision.by_id = {"same": "entities/a/a.md"}
    collision._collisions = {"same": ["entities/a/a.md", "entities/b/b.md"]}
    monkeypatch.setattr(m, "build", lambda **_kwargs: collision)
    monkeypatch.setattr(m, "write_index", lambda *_a, **_k: None)
    assert m.main([]) == 0
    assert "COLLISION same" in capsys.readouterr().out
    collision._collisions = {}
    assert m.main([]) == 0
