"""Behavioral contracts of the id -> path index that the original suite left unpinned.

Real temporary vaults throughout. The cron entry point runs as a real subprocess, and the only
monkeypatch is a delegating `Path.rglob` that fixes the filesystem's arbitrary directory order so a
container-discovery test is deterministic.
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
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


def _page(vault: Path, rel: str, fm: str) -> Path:
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}\n---\nbody\n", encoding="utf-8")
    return p


def _container(vault: Path, rel: str) -> None:
    d = vault / "wiki" / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "schema.yaml").write_text("types: {entity: {}}\n", encoding="utf-8")


def _identity_rels(idx) -> set[str]:
    return {rel for rels in idx.name_to_rels.values() for rel in rels}


# --- which pages are entity identities -----------------------------------------------------------

def test_only_entities_pages_enter_the_name_and_alias_identity_maps(tmp_path):
    m = _load()
    _page(tmp_path, "entities/a/acme.md", "type: entity\nname: Acme\naliases: [Acme Corp]")
    _page(tmp_path, "concepts/a/acme-theory.md", "type: concept\nname: Acme Theory\naliases: [AT]")
    _page(tmp_path, "sources/a/acme-report.md", "type: source\nname: Acme Report")
    idx = m._scan(tmp_path)
    assert _identity_rels(idx) == {"entities/a/acme.md"}
    assert {r for rels in idx.alias_to_rels.values() for r in rels} == {"entities/a/acme.md"}


def test_a_live_status_other_than_tombstoned_keeps_an_entity_live(tmp_path):
    m = _load()
    _page(tmp_path, "entities/v/verified-co.md",
          "type: entity\nname: Verified Co\nid: 'entities:verified-co'\nstatus: verified")
    _page(tmp_path, "entities/g/gone-co.md",
          "type: entity\nname: Gone Co\nid: 'entities:gone-co'\nstatus: Tombstoned")
    idx = m._scan(tmp_path)
    assert idx.name_to_rels[m.id_lib.normalize_key("Verified Co")] == ["entities/v/verified-co.md"]
    assert not idx.is_tombstoned("entities:verified-co")
    assert idx.resolve("entities:verified-co") == "entities/v/verified-co.md"
    assert idx.is_tombstoned("entities:gone-co")
    assert m.id_lib.normalize_key("Gone Co") not in idx.name_to_rels


@pytest.mark.parametrize("fm, expected", [
    ("type: entity", "acme-corp"),                                  # stem fallback, .md stripped
    ("type: entity\nname: Zeta Group", "zeta-group"),               # name wins over the stem
    ("type: entity\ntitle: Omega Labs", "omega-labs"),              # title when no name
    ("type: entity\nname: Zeta Group\ntitle: Omega Labs", "zeta-group"),
])
def test_primary_name_is_name_then_title_then_file_stem(tmp_path, fm, expected):
    m = _load()
    _page(tmp_path, "entities/a/acme-corp.md", fm)
    idx = m._scan(tmp_path)
    assert idx.name_to_rels == {expected: ["entities/a/acme-corp.md"]}


# --- sub-domain containers ------------------------------------------------------------------------

def test_nested_containers_resolve_to_the_innermost_namespace(tmp_path):
    m = _load()
    for rel in ("a", "a/b", "a/b/c"):
        _container(tmp_path, rel)
    _page(tmp_path, "a/b/c/entities/x/deep-co.md", "type: entity\nname: Deep Co")
    _page(tmp_path, "a/b/entities/x/mid-co.md", "type: entity\nname: Mid Co")
    _page(tmp_path, "a/b/c/entities/flat-co.md", "type: entity\nname: Flat Co")   # unsharded
    idx = m._scan(tmp_path)
    assert _identity_rels(idx) == {"a/b/c/entities/x/deep-co.md", "a/b/entities/x/mid-co.md",
                                   "a/b/c/entities/flat-co.md"}


def test_a_page_directly_inside_a_container_is_not_in_that_containers_namespace(tmp_path):
    """A container's own top-level page has no namespace below it, even when the container is
    named like one."""
    m = _load()
    _container(tmp_path, "entities")
    _page(tmp_path, "entities/loose.md", "type: entity\nname: Loose")
    _page(tmp_path, "entities/entities/x/inner.md", "type: entity\nname: Inner")
    idx = m._scan(tmp_path)
    assert _identity_rels(idx) == {"entities/entities/x/inner.md"}


def test_a_container_whose_name_sorts_before_a_dot_is_still_a_container(tmp_path):
    m = _load()
    _container(tmp_path, "-legacy")
    _page(tmp_path, "-legacy/entities/x/old-co.md", "type: entity\nname: Old Co")
    _page(tmp_path, "-legacy/topics/x/old-topic.md", "type: topic\nname: Old Topic")
    idx = m._scan(tmp_path)
    assert _identity_rels(idx) == {"-legacy/entities/x/old-co.md"}


def test_an_unresolvable_container_does_not_hide_the_ones_after_it(tmp_path, monkeypatch):
    m = _load()
    wiki = tmp_path / "wiki"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "schema.yaml").write_text("types: {}\n", encoding="utf-8")
    wiki.mkdir()
    (wiki / "aaa").symlink_to(outside, target_is_directory=True)    # resolves outside wiki/
    _container(tmp_path, "zzz")
    _page(tmp_path, "zzz/entities/x/zed.md", "type: entity\nname: Zed")

    real_rglob = Path.rglob
    linked = wiki / "aaa" / "schema.yaml"

    def ordered(self, pattern, *args, **kwargs):
        found = list(real_rglob(self, pattern, *args, **kwargs))
        if pattern == "schema.yaml":           # visit the unresolvable container FIRST
            found = [linked] + sorted(p for p in found if p != linked)
        return iter(found)

    monkeypatch.setattr(Path, "rglob", ordered)
    idx = m._scan(tmp_path)
    assert _identity_rels(idx) == {"zzz/entities/x/zed.md"}


# --- unreadable pages do not stop the scan -------------------------------------------------------

def test_an_unreadable_page_is_skipped_and_later_pages_still_index(tmp_path):
    m = _load()
    (tmp_path / "wiki" / "entities" / "a.md").mkdir(parents=True)      # a directory named *.md
    _page(tmp_path, "entities/b.md", "type: entity\nname: Bee\nid: 'entities:bee'")
    idx = m._scan(tmp_path)
    assert idx.resolve("entities:bee") == "entities/b.md"


def test_a_page_resolving_outside_the_wiki_is_skipped_and_later_pages_still_index(tmp_path):
    m = _load()
    target = tmp_path / "outside.md"
    target.write_text("---\nid: 'entities:outside'\nname: Outside\n---\n", encoding="utf-8")
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "wiki" / "entities" / "a.md").symlink_to(target)
    _page(tmp_path, "entities/b.md", "type: entity\nname: Bee\nid: 'entities:bee'")
    idx = m._scan(tmp_path)
    assert idx.resolve("entities:bee") == "entities/b.md"
    assert idx.resolve("entities:outside") is None


# --- first claimant and collisions ----------------------------------------------------------------

def test_the_first_live_claimant_keeps_the_id_whatever_its_path_order(tmp_path):
    m = _load()
    idx = m.IdIndex()
    idx._add("entities:x", "entities/z/zz.md", {})
    idx._add("entities:x", "entities/a/aa.md", {})
    assert idx.resolve("entities:x") == "entities/z/zz.md"
    assert idx.collisions() == {"entities:x": ["entities/z/zz.md", "entities/a/aa.md"]}


def test_re_adding_the_same_page_is_not_a_collision(tmp_path):
    m = _load()
    idx = m.IdIndex()
    rel = "entities/a/" + "".join(["ac", "me.md"])
    idx._add("entities:acme", rel, {})
    idx._add("entities:acme", "".join(["entities/a/", "acme.md"]), {})
    assert idx.collisions() == {}
    assert idx.resolve("entities:acme") == rel


def test_a_verified_page_is_live_not_tombstoned():
    m = _load()
    idx = m.IdIndex()
    idx._add("entities:v", "entities/v/v.md", {"status": "verified"})
    assert not idx.is_tombstoned("entities:v")
    assert idx.resolve("entities:v") == "entities/v/v.md"


# --- build / persistence --------------------------------------------------------------------------

def test_build_accepts_the_vault_by_keyword(tmp_path):
    m = _load()
    _page(tmp_path, "entities/a/acme.md", "type: entity\nid: 'entities:acme'")
    idx = m.build(vault=tmp_path, force=True)
    assert idx.resolve("entities:acme") == "entities/a/acme.md"


def test_write_index_creates_missing_parent_directories(tmp_path):
    m = _load()
    out = tmp_path / "data" / "state" / "deep" / "id-index.json"
    m.write_index(m.IdIndex(), out)
    assert json.loads(out.read_text())["norm_version"] == 3


def test_the_background_refresh_never_blocks_interpreter_exit(tmp_path, monkeypatch):
    """The write path's refresh thread is a daemon: a gateway shutting down must not wait on a
    64k-page scan."""
    monkeypatch.setenv("HERMES_DATA", str(tmp_path / "data"))
    m = _load()
    m.write_index(m.IdIndex())                      # an artifact exists -> the fast path
    seen = {}
    done = threading.Event()

    def scan(_vault):
        seen["daemon"] = threading.current_thread().daemon
        done.set()
        return m.IdIndex()

    monkeypatch.setattr(m, "_scan", scan)
    m.build(tmp_path)
    assert done.wait(10)
    assert seen["daemon"] is True


def test_standalone_import_puts_the_script_directory_first(monkeypatch):
    import builtins
    here = str(MOD.resolve().parent)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != here])   # earlier tests add it
    original_import = builtins.__import__
    attempts = {"n": 0}

    def importing(name, *args, **kwargs):
        if name == "id_lib" and attempts["n"] == 0:
            attempts["n"] += 1
            raise ImportError("standalone")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    _load()
    assert sys.path[0] == here


def test_the_cron_entry_point_full_scans_and_replaces_a_stale_artifact(tmp_path):
    vault = tmp_path / "vault"
    _page(vault, "entities/a/acme.md", "type: entity\nid: 'entities:acme'")
    data = tmp_path / "data"
    artifact = data / "state" / "id-index.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps({"by_id": {"entities:stale-1": "x.md",
                                              "entities:stale-2": "y.md"}}), encoding="utf-8")
    env = {**os.environ, "WIKI_PATH": str(vault), "HERMES_DATA": str(data)}
    proc = subprocess.run([sys.executable, str(MOD)], env=env, capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0].startswith("id-index: 1 ids, 0 aliases, 0 tombstoned, 0 collision(s)")
    assert json.loads(lines[-1]) == {"wakeAgent": False}
    assert json.loads(artifact.read_text())["by_id"] == {"entities:acme": "entities/a/acme.md"}
