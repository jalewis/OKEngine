"""okengine#1014: a rename is judged like a create, and the deployment's own evidence that a name
is NOT an actor (a malware page, a pack's geopolitical titles) stops what the subject does from
grounding it.

QA of !1288 repro: `_update("entities/l/lockbit", {title: "ANY.RUN", name: "ANY.RUN", aliases:
[ANY.RUN]})` returned `updated`, with no review flag. A model cannot create an ANY.RUN actor, but it
could repurpose a real actor's page under that name.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = (
    "okf:\n  required: [type]\nstrict_types: false\ntypes:\n"
    "  actor: {required: [type]}\n  intrusion-set: {required: [type], role: actor}\n"
    "  malware: {required: [type]}\n  location: {required: [type]}\n"
    "  identity: {required: [type]}\n  source: {required: [type]}\n"
    "identity_admission:\n  actor:\n    excluded_exact_titles: [Atlantis]\n"
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", datetime.date(2026, 10, 9).isoformat())
    sys.modules.pop("write_server", None)
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def _write(root: Path, rel: str, fm: dict, body: str = "") -> Path:
    page = root / "wiki" / f"{rel}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n{body}\n", encoding="utf-8")
    return page


def _source(root: Path, ref: str, body: str) -> str:
    _write(root, ref, {"type": "source", "title": "Report", "source_kind": "report"}, body)
    return ref


@pytest.fixture
def lockbit(ws):
    m, root = ws
    ref = _source(root, "sources/2026/10/01/lockbit",
                  "LockBit claimed the attack on the bank. ANY.RUN identified the payload.")
    page = _write(root, "entities/l/lockbit",
                  {"id": "actor:lockbit", "type": "actor", "title": "LockBit", "name": "LockBit",
                   "aliases": ["LockBit 3.0"], "actor_type": "cybercriminal", "sources": [ref]})
    return m, root, page


# --- 1. a rename is judged -----------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"title": "ANY.RUN", "name": "ANY.RUN", "aliases": ["ANY.RUN"]},   # the QA repro
    {"name": "ANY.RUN"},
    {"aliases": ["LockBit 3.0", "ANY.RUN"]},
])
def test_renaming_a_stored_actor_is_grounded_like_a_create(lockbit, change):
    """Red before okengine#1014: every one of these returned `updated`."""
    m, _root, page = lockbit
    before = page.read_bytes()
    out = m._update("entities/l/lockbit", change)
    assert out.startswith("rejected: renaming this actor to"), out
    assert page.read_bytes() == before


def test_renaming_through_converge_is_grounded(lockbit):
    m, _root, page = lockbit
    before = page.read_bytes()
    out = m._converge("entities/l/lockbit", "type: actor\ntitle: ANY.RUN\nname: ANY.RUN\n", "")
    assert out.startswith("rejected: renaming this actor to"), out
    assert page.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"title": "Lockbit"},                       # a respelling is not a rename
    {"aliases": []},                            # dropping a name is not a rename
    {"tags": ["ransomware"]},                   # nor is enrichment
    {"aliases": ["LockBit 3.0", "LockBit Black"]},   # a name the sources ground
])
def test_what_is_not_a_rename_or_is_grounded_keeps_its_update_path(lockbit, change):
    m, root, _page = lockbit
    if "LockBit Black" in change.get("aliases", []):
        _source(root, "sources/2026/10/01/lockbit",
                "LockBit claimed the attack. LockBit, also known as LockBit Black, was deployed.")
    assert m._update("entities/l/lockbit", change).startswith("updated")


def test_a_declared_actor_type_is_judged_on_rename_too(ws):
    """okengine#1020 left stored declared-type pages alone; a rename is the one write that is not
    enrichment, so admission and grounding both run (red before okengine#1014)."""
    m, root = ws
    ref = _source(root, "sources/2026/10/01/crew", "Night Crew claimed the intrusion.")
    _write(root, "entities/n/night-crew",
           {"id": "x:night-crew", "type": "intrusion-set", "title": "Night Crew", "sources": [ref]})
    assert m._update("entities/n/night-crew", {"title": "Threat Actor"}).startswith(
        "rejected: generic class labels")
    assert m._update("entities/n/night-crew", {"title": "Day Crew"}).startswith(
        "rejected: renaming this actor to 'Day Crew'")
    assert m._update("entities/n/night-crew", {"tags": ["x"]}).startswith("updated")


# --- 3. the deployment's own evidence that a name is not an actor ------------------------------------

@pytest.mark.parametrize("held_at, kind", [
    ("entities/r/redline-malware", "malware"),     # sorts before the new page
    ("entities/z/redline-tool", "tool"),           # sorts after it, and its type after `source`
])
def test_a_name_a_malware_page_already_carries_is_not_grounded_by_verbs(ws, held_at, kind):
    """Red before okengine#1014: "RedLine steals ..." grounded a new RedLine actor although the
    corpus already holds RedLine as malware."""
    m, root = ws
    ref = _source(root, "sources/2026/10/02/redline", "RedLine steals browser passwords.")
    _write(root, held_at, {"id": f"{kind}:redline", "type": kind, "title": "RedLine",
                           "name": "RedLine"})
    out = m._create("entities/r/redline", {"type": "actor", "title": "RedLine", "sources": [ref]}, "")
    assert out.startswith("rejected: creating an actor"), out


def test_without_that_evidence_the_same_create_is_grounded(ws):
    m, root = ws
    ref = _source(root, "sources/2026/10/02/redline", "RedLine steals browser passwords.")
    _write(root, "entities/r/redline-campaign", {"id": "x:redline-source", "type": "source",
                                                 "title": "RedLine", "name": "RedLine"})
    out = m._create("entities/r/redline", {"type": "actor", "title": "RedLine", "sources": [ref]}, "")
    assert out.startswith("created"), out


def test_the_page_being_retyped_is_not_its_own_namesake(ws):
    m, root = ws
    ref = _source(root, "sources/2026/10/01/crew", "Night Crew claimed the intrusion.")
    _write(root, "entities/n/night-crew",
           {"id": "x:night-crew", "type": "identity", "title": "Night Crew", "sources": [ref]})
    assert m._update("entities/n/night-crew", {"type": "intrusion-set"}).startswith("updated")


def test_a_pack_geopolitical_title_is_not_grounded_by_verbs(ws):
    """A pack-supplied title (`identity_admission.actor.excluded_exact_titles`) is held even where
    admission is not the gate: here it is an alias on a renamed page."""
    m, root = ws
    m_root = root
    ref = _source(m_root, "sources/2026/10/01/a", "Atlantis targeted the ministry.")
    page = m_root / "wiki" / "entities" / "a" / "atlantis-team.md"
    fm = {"type": "actor", "aliases": ["Atlantis"], "sources": [ref]}
    assert m._actor_grounding_reject(page, fm) is not None
    assert m._actor_namesakes(page, ["Atlantis"], m._governing(page)) == ["Atlantis"]


def test_an_unreadable_namesake_page_is_skipped_and_the_next_one_still_read(ws, monkeypatch):
    m, root = ws
    gone = _write(root, "entities/a/redline-a", {"id": "malware:a", "type": "malware",
                                                 "title": "RedLine"})
    _write(root, "entities/b/redline-b", {"id": "malware:b", "type": "malware", "title": "RedLine"})
    page = root / "wiki" / "entities" / "r" / "redline.md"
    assert m._actor_namesakes(page, ["RedLine"], {}) == ["RedLine", "RedLine"]
    real = m._read_page

    def flaky(path):
        if Path(path) == gone:
            raise OSError("gone")
        return real(path)

    monkeypatch.setattr(m, "_read_page", flaky)
    assert m._actor_namesakes(page, ["RedLine"], {}) == ["RedLine"]
    monkeypatch.setattr(m, "_read_page", lambda _p: (_ for _ in ()).throw(ValueError("bad")))
    assert m._actor_namesakes(page, ["RedLine"], {}) == []


def test_naming_a_nameless_stored_actor_after_its_slug_is_not_a_rename(ws):
    m, root = ws
    _write(root, "entities/n/night-crew", {"id": "x:night-crew", "type": "intrusion-set",
                                           "sources": ["sources/missing"]})
    assert m._update("entities/n/night-crew", {"title": "Night Crew"}).startswith("updated")


def test_the_refusal_names_the_first_new_name(lockbit):
    m, _root, _page = lockbit
    out = m._update("entities/l/lockbit", {"title": "ANY.RUN", "aliases": ["LockBit 3.0", "Zeta"]})
    assert out.startswith("rejected: renaming this actor to 'ANY.RUN'"), out


def test_renaming_an_unverifiable_stored_actor_is_left_to_the_detector(ws):
    """A stored actor with no resolvable source was never grounded; a rename adds no claim its
    sources could test, so it is not refused (the corpus audit counts the page as unsourced)."""
    m, root = ws
    _write(root, "entities/u/unsourced-crew", {"id": "x:unsourced", "type": "actor",
                                               "title": "Unsourced Crew", "actor_type": "unknown"})
    assert m._update("entities/u/unsourced-crew", {"aliases": ["Quiet Crew"]}).startswith("updated")
